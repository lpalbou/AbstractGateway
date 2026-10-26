"""The gateway's half of a flow's `PUBLISH_EVENT`: an application-agnostic push to the person.

A flow (or an agent) publishes `{channel, type?, title, body?, subject?, data?, alert?}`; this host
writes it to the run's owner's event stream (`events_stream.record_published`): the event on its
channel, id `publish:<run_id>:<step_id>`, and with `alert: true` an attention item so the tray,
the Assistant and the watch announce it. Any application subscribes to its channel with
`GET /api/gateway/me/events/stream?channel=<name>`.

The alert reaches `recipient` ("me", the plane's owner; another account is refused for now) on
`channels` (Notify me's "Where": tray, watch, phone, email, console; absent = every channel), written
on the `attention.raised` record. Each deliverer reads it (`events_stream.alert_asks`): the tray, the
push dispatcher (watch, phone) and, here, email: a Notify me whose channels include email queues one
notice in the person's email outbox (`mail.notifications.queue_notify_me`) when the mailbox is usable.
The console item is the record itself, always written.

Runtime seam: `Runtime.set_publish_event_handler(handler)` with `handler(run, payload, step_id) ->
{event_id, sequence, alerted}`; a refused payload raises ValueError with the sentence the effect
reports. `wire_publish_event` registers it and fails loudly on a runtime without the seam.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional


class GatewayPublishEvent:
    """The handler for one plane: the stream of the plane's owner."""

    def __init__(self, *, data_dir: Path, tenant: str, user: str, email_plane: Any = None) -> None:
        self.data_dir = Path(data_dir)
        self.tenant = str(tenant)
        self.user = str(user)
        # The owner's email plane (None: a host without email; email is then never sent).
        self.email_plane = email_plane

    def __call__(self, run: Any, payload: Mapping[str, Any], step_id: Optional[str] = None) -> Dict[str, Any]:
        from .events_stream import record_published, stream_for

        run_id = str(getattr(run, "run_id", "") or "")
        if not run_id:
            raise ValueError("publish needs the calling run")
        step = str(step_id or "").strip() or f"{getattr(run, 'current_node', '') or 'step'}"
        out = record_published(stream_for(self.data_dir, self.tenant, self.user), run_id=run_id, step_id=step, payload=payload)
        if out.get("alerted") and self.email_plane is not None:
            self._email(run, payload, f"publish:{run_id}:{step}")
        return out

    def _email(self, run: Any, payload: Mapping[str, Any], record_key: str) -> None:
        from .events_stream import alert_asks

        if not alert_asks({"record_key": record_key, "channels": payload.get("channels")}, "email"):
            return
        try:
            from .mail.notifications import queue_notify_me

            body = payload.get("body")
            queue_notify_me(self.email_plane, record_key=record_key, run=run, title=str(payload.get("title") or "").strip(),
                            body=body if isinstance(body, str) else None)
        except Exception:  # noqa: BLE001 - the alert is recorded; a mail problem never fails the step
            import logging

            logging.getLogger(__name__).warning("Notify me email could not be queued (%s)", record_key, exc_info=True)


def wire_publish_event(runtime: Any, *, data_dir: Path, tenant: str, user: str, email_plane: Any = None) -> GatewayPublishEvent:
    setter = getattr(runtime, "set_publish_event_handler", None)
    if not callable(setter):
        raise RuntimeError("This AbstractRuntime has no Runtime.set_publish_event_handler (PUBLISH_EVENT): upgrade abstractruntime.")
    handler = GatewayPublishEvent(data_dir=data_dir, tenant=tenant, user=user, email_plane=email_plane)
    setter(handler)
    return handler


__all__ = ["GatewayPublishEvent", "wire_publish_event"]
