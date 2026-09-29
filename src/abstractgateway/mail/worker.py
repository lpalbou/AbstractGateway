"""The per-plane email worker: the mail watcher + the notification dispatcher, one thread.

Started with the principal's service wherever its runner starts (the single-user path, a
multi-user first request, the multi-user eager rehydration at boot), registered in
`worker_registry` as `email:<tenant>:<user>`, stopped with the service. Every tick:

1. the watcher polls when due (60 s; capped backoff on failures) and only while the plane has
   an active `email.received` automation;
2. the collector turns new automation / run facts into queued notices;
3. the outbox sends what is due.

A tick never raises; each step's failure is recorded in its own status (watcher state,
outbox rows, the account's last error) with the typed cause and fix.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from .accounts import EmailPlane, email_usable, plane_for_service_config

logger = logging.getLogger("abstractgateway.mail")

TICK_S = 15.0


class EmailWorker:
    def __init__(self, svc: Any, plane: EmailPlane, *, tick_s: float = TICK_S) -> None:
        self.svc = svc
        self.plane = plane
        self.tick_s = float(tick_s)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._recovered = False

    @property
    def name(self) -> str:
        return f"email:{self.plane.tenant_id}:{self.plane.user_id}"

    @property
    def runtime(self) -> Any:
        # Read per use: a host rebuild (publish, reload) swaps the runtime.
        return self.svc.host.runtime

    def _watcher(self) -> Any:
        from abstractruntime.email import wake_email_automations

        from .watcher import MailWatcher, plane_has_email_automations

        runtime = self.runtime
        return MailWatcher(
            self.plane,
            inbox=runtime.event_inbox,
            has_consumers=lambda: plane_has_email_automations(self.svc),
            on_appended=lambda _ids: wake_email_automations(runtime),
        )

    def tick(self) -> Dict[str, Any]:
        from .notifications import NotificationCollector, NotificationOutbox

        out: Dict[str, Any] = {}
        outbox = NotificationOutbox(self.plane)
        if not self._recovered:
            try:
                out["recovered_unknown"] = outbox.recover_interrupted()
            except Exception:  # noqa: BLE001
                logger.warning("email outbox recovery failed", exc_info=True)
            self._recovered = True
        try:
            from .runtime_wiring import refresh_runtime_binding

            refresh_runtime_binding(self.runtime, self.plane)
        except Exception:  # noqa: BLE001
            logger.warning("email binding refresh failed for %s", self.name, exc_info=True)
        try:
            watcher = self._watcher()
            if watcher.due():
                out["watcher"] = watcher.poll_once()
        except Exception:  # noqa: BLE001
            logger.warning("email watcher tick failed for %s", self.name, exc_info=True)
        try:
            usable = email_usable(self.plane)
        except Exception:  # noqa: BLE001
            usable = False
        if not usable:
            # No account / turned off: nothing to collect or send (D5). The collector
            # baselines on its first usable tick, so history is never mailed later. Notices
            # already queued are closed as failed with the typed cause (never silent).
            try:
                out["delivered"] = outbox.deliver()
            except Exception:  # noqa: BLE001
                logger.warning("notification delivery failed for %s", self.name, exc_info=True)
            return out
        try:
            out["collected"] = NotificationCollector(self.plane, self.svc).collect()
        except Exception:  # noqa: BLE001
            logger.warning("notification collector failed for %s", self.name, exc_info=True)
        try:
            out["delivered"] = outbox.deliver()
        except Exception:  # noqa: BLE001
            logger.warning("notification delivery failed for %s", self.name, exc_info=True)
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.tick_s)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"gateway-{self.name}", daemon=True)
        self._thread.start()
        try:
            from ..worker_registry import register_worker

            register_worker(self.name, self._thread)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=10.0)
        try:
            from ..worker_registry import unregister_worker

            unregister_worker(self.name)
        except Exception:  # noqa: BLE001
            pass


def build_email_worker(svc: Any) -> Optional[EmailWorker]:
    """The worker for the plane `svc` serves, or None (an entity's plane has no mailbox)."""

    try:
        plane = plane_for_service_config(svc.config)
    except Exception:  # noqa: BLE001
        return None
    if not plane.is_default:
        try:
            from ..users import GatewayUserRegistry

            rec = GatewayUserRegistry().get_user(plane.user_id, tenant_id=plane.tenant_id)
            if rec is not None and rec.principal_kind == "entity":
                return None
        except Exception:  # noqa: BLE001
            pass
    return EmailWorker(svc, plane)
