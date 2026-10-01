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
        # Set by nudge(): the next tick runs now instead of after tick_s.
        self._wake = threading.Event()
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

            runtime = self.runtime
            if runtime is not None:  # an entity's runtime opens with its first visit
                refresh_runtime_binding(runtime, self.plane)
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
        while True:
            # Clear the wake BEFORE checking stop: a stop() landing in between would otherwise
            # have its wake erased and the loop would sleep a full tick past it.
            self._wake.clear()
            if self._stop.is_set():
                break
            self.tick()
            self._wake.wait(self.tick_s)

    def nudge(self) -> None:
        """Run a tick now (a new email automation: the watcher takes its baseline right away,
        so mail sent to test it seconds later is new mail, not history)."""

        self._wake.set()

    def on_automation_command(self, command_type: str, automation_id: str) -> None:
        """Runner listener, called after an automation command is applied.

        A resumed email automation (or one revised onto `email.received`) is a consumer again:
        after a time with none the watcher's next read is a fresh baseline, so it must happen now,
        not at the next tick up to 15 s later (mail arriving in between was taken as history and
        never triggered the resumed automation). Mirrors the creation nudge (routes/automations.py).
        The command is already applied here, so the tick sees the automation active.
        """

        if command_type not in ("automation.resume", "automation.revise") or self._stop.is_set():
            return
        try:
            from abstractruntime.automations.ledger import definition_of

            run = self.svc.host.run_store.load(str(automation_id))
            trigger = definition_of(run)["trigger"] if run is not None else {}
        except Exception:  # noqa: BLE001 - not an automation this plane can read: nothing to wake
            return
        if str((trigger or {}).get("source_id") or "") == "email.received":
            self.nudge()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        runner = getattr(self.svc, "runner", None)
        if runner is not None:  # an entity's worker has no automation runner (EntityMailHost)
            try:
                runner.add_automation_command_listener(self.on_automation_command)
            except Exception:  # noqa: BLE001 - no runner hook: the 15 s tick still picks the resume up
                logger.warning("email worker %s: no automation command hook on the runner", self.name, exc_info=True)
        self._thread = threading.Thread(target=self._loop, name=f"gateway-{self.name}", daemon=True)
        self._thread.start()
        try:
            from ..worker_registry import register_worker

            register_worker(self.name, self._thread)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout=10.0)
        try:
            from ..worker_registry import unregister_worker

            unregister_worker(self.name)
        except Exception:  # noqa: BLE001
            pass


def build_email_worker(svc: Any) -> Optional[EmailWorker]:
    """The worker for the user plane `svc` serves, or None. Services are built per user
    runtime; an entity's mailbox has its own worker (`sync_entity_workers`), never a service's."""

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


# ---------------------------------------------------------------------------------------
# Entities: AI users with their own mailbox (round 3 §3.1)
# ---------------------------------------------------------------------------------------


class EntityMailHost:
    """The `svc` an entity plane's worker reads: the entity's own runtime and stores when its
    runtime is open (a visit opened it in some service's entity registry), else none (the
    watcher still reads the mailbox into the entity's event inbox; run notices wait for the
    runtime). No automation runner: entities run no automations."""

    runner = None

    def __init__(self, slug: str) -> None:
        self.slug = str(slug)

    @property
    def host(self) -> Any:
        from types import SimpleNamespace

        er = _open_entity_runtime(self.slug)
        if er is None:
            return SimpleNamespace(runtime=None, run_store=None, ledger_store=None)
        return SimpleNamespace(runtime=er.runtime, run_store=er.run_store, ledger_store=er.ledger_store)


def _open_entity_runtime(slug: str) -> Any:
    """The entity's runtime if a built service's entity registry has it open, else None (never
    opens one: opening loads the home and its embedder)."""

    from .. import service as service_mod

    with service_mod._service_lock:
        candidates = [service_mod._service] + list(service_mod._services_by_principal.values())
    for svc in candidates:
        registry = getattr(svc, "entity_registry", None) if svc is not None else None
        if registry is None:
            continue
        with registry._open_lock:
            er = registry._entity_runtimes.get(slug)
        if er is not None:
            return er
    return None


class EntityEmailWorker(EmailWorker):
    """An entity's mailbox worker: the same tick (watcher, notices, outbox) on the entity's
    plane. The watcher reads the mailbox whenever it is connected and in use (an entity's mail
    is its own; it does not wait for an email automation) into the entity's event inbox
    (`<home>/event_inbox`)."""

    def __init__(self, plane: EmailPlane, *, tick_s: float = TICK_S) -> None:
        super().__init__(EntityMailHost(plane.user_id), plane, tick_s=tick_s)

    @property
    def runtime(self) -> Any:
        return self.svc.host.runtime

    def _watcher(self) -> Any:
        from .watcher import MailWatcher

        return MailWatcher(self.plane, has_consumers=lambda: True)


_ENTITY_WORKERS: Dict[str, EntityEmailWorker] = {}
_ENTITY_WORKERS_LOCK = threading.Lock()


def sync_entity_workers() -> Dict[str, list]:
    """Start the worker of every entity whose mailbox may work (not archived, not suspended,
    home on this gateway) and stop the others. Idempotent; called when a user service starts
    its email worker (boot, first request) and after an entity is archived, unarchived,
    suspended or resumed, or its mailbox is configured."""

    from ..users import GatewayUserRegistry
    from .accounts import entity_mail_active, entity_plane

    wanted: Dict[str, EmailPlane] = {}
    for rec in GatewayUserRegistry().list_users():
        if rec.principal_kind != "entity" or not entity_mail_active(rec):
            continue
        plane = entity_plane(rec.user_id, tenant_id=rec.tenant_id)
        wanted[plane.key] = plane
    started: list = []
    stopped: list = []
    with _ENTITY_WORKERS_LOCK:
        for key in list(_ENTITY_WORKERS):
            if key not in wanted or _ENTITY_WORKERS[key].plane.root != wanted[key].root:
                _ENTITY_WORKERS.pop(key).stop()
                stopped.append(key)
        for key, plane in wanted.items():
            if key not in _ENTITY_WORKERS:
                worker = EntityEmailWorker(plane)
                _ENTITY_WORKERS[key] = worker
                worker.start()
                started.append(key)
    return {"started": started, "stopped": stopped}


def entity_worker(slug: str) -> Optional[EntityEmailWorker]:
    with _ENTITY_WORKERS_LOCK:
        return next((w for w in _ENTITY_WORKERS.values() if w.plane.user_id == str(slug)), None)


def stop_all_entity_workers() -> None:
    with _ENTITY_WORKERS_LOCK:
        workers = list(_ENTITY_WORKERS.values())
        _ENTITY_WORKERS.clear()
    for w in workers:
        w.stop()
