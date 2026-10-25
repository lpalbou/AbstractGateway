"""The gateway's half of a flow's `PUBLISH_EVENT`: an application-agnostic push to the person.

A flow (or an agent) publishes `{channel, type?, title, body?, subject?, data?, alert?}`; this host
writes it to the run's owner's event stream (`events_stream.record_published`): the event on its
channel, id `publish:<run_id>:<step_id>`, and with `alert: true` an attention item so the tray,
the Assistant and the watch announce it. Any application subscribes to its channel with
`GET /api/gateway/me/events/stream?channel=<name>`.

Runtime seam: `Runtime.set_publish_event_handler(handler)` with `handler(run, payload, step_id) ->
{event_id, sequence, alerted}`; a refused payload raises ValueError with the sentence the effect
reports. `wire_publish_event` registers it and fails loudly on a runtime without the seam.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional


class GatewayPublishEvent:
    """The handler for one plane: the stream of the plane's owner."""

    def __init__(self, *, data_dir: Path, tenant: str, user: str) -> None:
        self.data_dir = Path(data_dir)
        self.tenant = str(tenant)
        self.user = str(user)

    def __call__(self, run: Any, payload: Mapping[str, Any], step_id: Optional[str] = None) -> Dict[str, Any]:
        from .events_stream import record_published, stream_for

        run_id = str(getattr(run, "run_id", "") or "")
        if not run_id:
            raise ValueError("publish needs the calling run")
        step = str(step_id or "").strip() or f"{getattr(run, 'current_node', '') or 'step'}"
        return record_published(stream_for(self.data_dir, self.tenant, self.user), run_id=run_id, step_id=step, payload=payload)


def wire_publish_event(runtime: Any, *, data_dir: Path, tenant: str, user: str) -> GatewayPublishEvent:
    setter = getattr(runtime, "set_publish_event_handler", None)
    if not callable(setter):
        raise RuntimeError("This AbstractRuntime has no Runtime.set_publish_event_handler (PUBLISH_EVENT): upgrade abstractruntime.")
    handler = GatewayPublishEvent(data_dir=data_dir, tenant=tenant, user=user)
    setter(handler)
    return handler


__all__ = ["GatewayPublishEvent", "wire_publish_event"]
