"""Per-user event stream (framework backlog 1046; design round-1 F2, round-2 findings 6 and 7).

One ordered, durable, cursor-read stream per person: what happened FOR them that is not a step of
a run. The ledger stays the run truth; this stream holds only pointers and facts outside runs:

    trigger.event        an automation admitted an occurrence (its controller ledger has the rest)
    wake.scheduled       a one-shot wake was set (SCHEDULE_RUN, backlog 1045)
    wake.cancelled       that wake was cancelled before it fired
    attention.raised     an attention item exists (`record_key` = the controller ledger record)
    attention.seen       the person's seen cursor of an automation moved
    notification.sent    a notice reached a channel (console, email, tray)
    notification.failed  a notice could not reach a channel
    run.started          pointer {run_id, session_id, origin} (written AFTER the run store save)
    run.finished         pointer {run_id, session_id, origin, status}

Never a step, a model call, a tool call or a copy of an attention body: a consumer that needs the
content follows the pointer (`run_id`, `record_key`) to the ledger.

Storage: the runtime's `JsonFileEventInbox`, reused as is, one inbox directory per principal in the
plane's data folder (`<data dir>/events/users/<sha256([tenant, user])>/`), stream name
`user:<tenant>:<user>`. One inbox per person makes the inbox `seq` that person's own gapless
sequence (the SSE `id`, the catch-up `after`), and keeps one person's reads off another's files.
Event ids are idempotent (`run.started:<run_id>`, `attention.raised:<record_key>`...): a writer
that repeats itself after a crash appends nothing.

Wire shape: CloudEvents 1.0 attribute names,
`{specversion, id, type, source, subject?, time, data, sequence}` with `sequence` the integer
`seq` (the CloudEvents sequence extension is a string; an integer is what every cursor here is).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

SPEC_VERSION = "1.0"
EVENT_TYPES = frozenset(
    {
        "trigger.event",
        "wake.scheduled",
        "wake.cancelled",
        "attention.raised",
        "attention.seen",
        "notification.sent",
        "notification.failed",
        "run.started",
        "run.finished",
    }
)
CHANNELS = frozenset({"console", "email", "tray"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
# Boot repair window: runs updated this long before the newest stream event are re-checked.
REBUILD_MARGIN_S = 600.0


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def stream_principal(tenant: Any, user: Any) -> Tuple[str, str]:
    """The canonical (tenant, user) of a stream: the same safe components the plane uses
    (`service._config_for_principal`, `mail.accounts.plane_for_service_config`), so a request
    principal and the plane owner name the same stream."""
    from .security.principal import safe_principal_component

    t = safe_principal_component(tenant or "default", default="default")
    u = safe_principal_component(user or "", default="")
    if not u:
        raise ValueError("a user event stream needs an identified user")
    return t, u


def stream_name(tenant: str, user: str) -> str:
    t, u = stream_principal(tenant, user)
    return f"user:{t}:{u}"


def _principal_dir_key(tenant: str, user: str) -> str:
    canonical = json.dumps([str(tenant), str(user)], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def to_cloudevent(record: Mapping[str, Any]) -> Dict[str, Any]:
    """An inbox record -> the wire event."""
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    out: Dict[str, Any] = {
        "specversion": SPEC_VERSION,
        "id": str(record.get("event_id") or ""),
        "type": str(payload.get("type") or ""),
        "source": str(payload.get("source") or "/abstractgateway"),
        "time": str(payload.get("time") or record.get("appended_at") or ""),
        "data": payload.get("data") if isinstance(payload.get("data"), dict) else {},
        "sequence": int(record.get("seq") or 0),
    }
    subject = payload.get("subject")
    if isinstance(subject, str) and subject:
        out["subject"] = subject
    return out


class UserEventStream:
    """The stream of ONE principal in ONE plane."""

    def __init__(self, data_dir: Path, tenant: str, user: str) -> None:
        from abstractruntime.email.inbox import JsonFileEventInbox

        self.tenant, self.user = stream_principal(tenant, user)
        self.name = f"user:{self.tenant}:{self.user}"
        self.dir = Path(data_dir) / "events" / "users" / _principal_dir_key(self.tenant, self.user)
        self.inbox = JsonFileEventInbox(self.dir)
        self._listeners: List[Any] = []
        self._listeners_lock = threading.Lock()

    # -- writes ------------------------------------------------------------------------------

    def append(
        self,
        type: str,
        *,
        event_id: str,
        data: Mapping[str, Any],
        subject: Optional[str] = None,
        source: str = "/abstractgateway",
        time: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], bool]:
        """Append one event; returns (event, created). An id already present appends nothing."""
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown user event type {type!r}")
        payload: Dict[str, Any] = {"type": type, "source": source, "time": time or _now_iso(), "data": dict(data)}
        if subject:
            payload["subject"] = str(subject)
        result = self.inbox.append(stream=self.name, event_id=str(event_id), payload=payload)
        event = to_cloudevent(result.record)
        if not result.duplicate:
            self._notify()
        return event, not result.duplicate

    def add_listener(self, fn: Any) -> Any:
        """In-process wake (an SSE handler): `fn()` after each new append; returns unsubscribe."""
        with self._listeners_lock:
            self._listeners.append(fn)

        def _off() -> None:
            with self._listeners_lock:
                try:
                    self._listeners.remove(fn)
                except ValueError:
                    pass

        return _off

    def _notify(self) -> None:
        with self._listeners_lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn()
            except Exception:  # noqa: BLE001 - a listener never breaks a writer
                logger.debug("user event listener failed", exc_info=True)

    # -- reads -------------------------------------------------------------------------------

    def read(self, *, after: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
        return [to_cloudevent(r) for r in self.inbox.read(after_seq=max(0, int(after)), stream=self.name, limit=max(1, int(limit)))]

    def head(self) -> int:
        return int(self.inbox.head_seq())

    def change_token(self) -> Tuple[int, int]:
        """(mtime_ns, size) of the inbox index: one stat; moves on every append (any process)."""
        try:
            st = (self.dir / "index.json").stat()
            return int(st.st_mtime_ns), int(st.st_size)
        except OSError:
            return (0, 0)

    def state(self) -> Dict[str, Any]:
        return self.inbox.stream_state(self.name)

    def set_state(self, state: Mapping[str, Any]) -> None:
        self.inbox.set_stream_state(self.name, dict(state))

    def prune(self) -> Dict[str, Any]:
        """Default retention (90 days AND 10,000 events; receipts kept, ids never reused)."""
        return self.inbox.prune()


_STREAMS: Dict[Tuple[str, str, str], UserEventStream] = {}
_STREAMS_LOCK = threading.Lock()


def stream_for(data_dir: Path, tenant: Any, user: Any) -> UserEventStream:
    """The process-wide object of a stream (shared by writers and SSE readers, so an append wakes
    the readers in this process at once; other processes are seen through `change_token`)."""
    t, u = stream_principal(tenant, user)
    key = (str(Path(data_dir).expanduser().resolve()), t, u)
    with _STREAMS_LOCK:
        stream = _STREAMS.get(key)
        if stream is None:
            stream = _STREAMS[key] = UserEventStream(Path(key[0]), t, u)
        return stream


def plane_owner(cfg: Any) -> Tuple[str, str]:
    """The (tenant, user) whose plane a service built for `cfg` (GatewayHostConfig) serves: the
    rule of the mail planes (`mail.accounts.plane_for_service_config`)."""
    from .mail.accounts import plane_for_service_config

    plane = plane_for_service_config(cfg)
    return stream_principal(plane.tenant_id, plane.user_id)


# ---------------------------------------------------------------------------------------------
# Run pointers
# ---------------------------------------------------------------------------------------------


def _vars(run: Any) -> Dict[str, Any]:
    v = getattr(run, "vars", None)
    return v if isinstance(v, dict) else {}


def _meta(run: Any) -> Dict[str, Any]:
    m = _vars(run).get("_meta")
    return m if isinstance(m, dict) else {}


def _status(run: Any) -> str:
    s = getattr(run, "status", None)
    return str(getattr(s, "value", s) or "").strip().lower()


def run_role(run: Any) -> Optional[str]:
    from abstractruntime.core.run_attribution import automation_index_fields

    return automation_index_fields(_vars(run), run_id=str(getattr(run, "run_id", "") or "")).get("role")


def is_pointer_run(run: Any) -> bool:
    """Runs that get pointers: session turn roots (a person's run, an occurrence), never a
    controller (the sleeper) nor a child run (its parent's step)."""
    from abstractruntime.core.run_attribution import is_turn_root

    return bool(is_turn_root(parent_run_id=getattr(run, "parent_run_id", None), role=run_role(run)))


def run_origin(run: Any) -> Optional[Dict[str, Any]]:
    """The run's recorded origin (backlog 1038), else the trigger origin of an occurrence."""
    meta = _meta(run)
    origin = meta.get("origin")
    if isinstance(origin, dict):
        return dict(origin)
    occ = meta.get("occurrence")
    if isinstance(occ, dict):
        return {
            "kind": "trigger",
            "actor": None,
            "client": None,
            "ref": {"automation_id": occ.get("automation_id"), "occurrence_index": occ.get("occurrence_index")},
        }
    return None


def _pointer_data(run: Any) -> Dict[str, Any]:
    return {
        "run_id": str(getattr(run, "run_id", "") or ""),
        "session_id": getattr(run, "session_id", None),
        "origin": run_origin(run),
    }


def record_run_started(stream: UserEventStream, run: Any) -> bool:
    """`run.started` (+ `trigger.event` first for an occurrence). Call AFTER the run store save.
    Returns True when something new was appended."""
    if not is_pointer_run(run):
        return False
    created = False
    occ = _meta(run).get("occurrence")
    rid = str(getattr(run, "run_id", "") or "")
    if isinstance(occ, dict) and occ.get("automation_id"):
        aid = str(occ.get("automation_id"))
        env = occ.get("trigger_envelope") if isinstance(occ.get("trigger_envelope"), dict) else {}
        _e, c = stream.append(
            "trigger.event",
            event_id=f"trigger.event:{aid}:{occ.get('occurrence_index')}",
            subject=aid,
            source=f"/abstractgateway/automations/{aid}",
            data={
                "automation_id": aid,
                "occurrence_index": occ.get("occurrence_index"),
                "event_id": occ.get("event_id"),
                "source_id": env.get("source_id"),
                "source_version": env.get("source_version"),
                "fired_at": occ.get("fired_at") or env.get("fired_at"),
                "run_id": rid,
                "session_id": getattr(run, "session_id", None),
            },
        )
        created = created or c
    _e, c = stream.append(
        "run.started",
        event_id=f"run.started:{rid}",
        subject=rid,
        source="/abstractgateway/runs",
        time=str(getattr(run, "created_at", "") or "") or None,
        data=_pointer_data(run),
    )
    return created or c


def record_run_finished(stream: UserEventStream, run: Any) -> bool:
    """`run.finished` for a terminal pointer run (its `run.started` first, idempotently)."""
    if not is_pointer_run(run) or _status(run) not in TERMINAL_STATUSES:
        return False
    started = record_run_started(stream, run)
    rid = str(getattr(run, "run_id", "") or "")
    data = _pointer_data(run)
    data["status"] = _status(run)
    _e, c = stream.append(
        "run.finished",
        event_id=f"run.finished:{rid}",
        subject=rid,
        source="/abstractgateway/runs",
        time=str(getattr(run, "updated_at", "") or "") or None,
        data=data,
    )
    return started or c


def record_run(stream: UserEventStream, run: Any) -> bool:
    """started, and finished when terminal (both idempotent)."""
    if _status(run) in TERMINAL_STATUSES:
        return record_run_finished(stream, run)
    return record_run_started(stream, run)


def _epoch(value: Any) -> Optional[float]:
    try:
        dt = datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def rebuild_run_pointers(stream: UserEventStream, run_store: Any, *, margin_s: float = REBUILD_MARGIN_S) -> Dict[str, int]:
    """Repair a crash between a run save and its pointer append: every turn root updated since
    the newest stream event (minus `margin_s`) gets its pointers, idempotently.

    A stream with no event yet takes no history (its first boot is a baseline, never a flood of
    old runs)."""
    out = {"checked": 0, "appended": 0}
    head = stream.head()
    if head <= 0:
        return out
    last = stream.read(after=head - 1, limit=1)
    newest = _epoch(last[0]["time"]) if last else None
    if newest is None:
        return out
    since = newest - float(margin_s)
    rows = run_store.list_run_index(root_only=True, limit=1_000_000)
    for row in rows:
        updated = _epoch(row.get("updated_at"))
        if updated is None or updated < since:
            continue
        run = run_store.load(str(row.get("run_id")))
        if run is None:
            continue
        out["checked"] += 1
        if record_run(stream, run):
            out["appended"] += 1
    return out


# ---------------------------------------------------------------------------------------------
# Attention and notifications
# ---------------------------------------------------------------------------------------------


def attention_from_record(record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """`{record_key, automation_id, run_id, index, kind, seq, channels}` from an
    `automation.completed` ledger record carrying an attention item, else None. No title, no body:
    the ledger record is the content (`record_key`)."""
    from abstractruntime.automations.ledger import record_name, record_payload

    try:
        if record_name(record) != "automation.completed":
            return None
        payload = record_payload(record)
    except Exception:
        return None
    att = payload.get("attention")
    if not isinstance(att, dict) or not att.get("seq"):
        return None
    key = str(record.get("idempotency_key") or "")
    if not key:
        return None
    return {
        "record_key": key,
        "automation_id": str(record.get("run_id") or ""),
        "run_id": payload.get("run_id"),
        "index": payload.get("index"),
        "kind": att.get("kind"),
        "seq": int(att.get("seq")),
        "channels": [str(c) for c in (att.get("channels") or ["console"])],
    }


def record_attention_raised(stream: UserEventStream, item: Mapping[str, Any]) -> bool:
    """`attention.raised` (+ `notification.sent {channel: console}` when the item goes to the
    console: the console delivery IS the item being listed there). Idempotent per record key."""
    key = str(item["record_key"])
    aid = str(item.get("automation_id") or "")
    _e, created = stream.append(
        "attention.raised",
        event_id=f"attention.raised:{key}",
        subject=aid,
        source=f"/abstractgateway/automations/{aid}",
        data={k: item.get(k) for k in ("record_key", "automation_id", "run_id", "index", "kind", "seq", "channels")},
    )
    if "console" in (item.get("channels") or []):
        record_notification(stream, "sent", channel="console", notice=f"attention:{key}", data={"record_key": key, "automation_id": aid})
    if created:
        state = stream.state()
        floors = dict(state.get("attention_floor") or {})
        floors[aid] = max(int(floors.get(aid) or 0), int(item.get("seq") or 0))
        state["attention_floor"] = floors
        stream.set_state(state)
    return created


def record_attention_seen(stream: UserEventStream, *, automation_id: str, seq: int) -> bool:
    """`attention.seen` when the principal's seen cursor of `automation_id` moved to `seq`."""
    aid = str(automation_id)
    _e, created = stream.append(
        "attention.seen",
        event_id=f"attention.seen:{aid}:{int(seq)}",
        subject=aid,
        source=f"/abstractgateway/automations/{aid}",
        data={"automation_id": aid, "seq": int(seq)},
    )
    return created


def record_notification(
    stream: UserEventStream,
    outcome: str,
    *,
    channel: str,
    notice: str,
    data: Optional[Mapping[str, Any]] = None,
) -> bool:
    """`notification.sent|failed` for one notice on one channel (idempotent per (channel, notice))."""
    if outcome not in ("sent", "failed"):
        raise ValueError("outcome must be 'sent' or 'failed'")
    if channel not in CHANNELS:
        raise ValueError(f"unknown notification channel {channel!r}")
    body = {"channel": channel, "notice": str(notice)}
    body.update(dict(data or {}))
    _e, created = stream.append(
        f"notification.{outcome}",
        event_id=f"notification.{outcome}:{channel}:{notice}",
        subject=str(notice),
        source=f"/abstractgateway/notifications/{channel}",
        data=body,
    )
    return created


def rebuild_attention(stream: UserEventStream, run_store: Any, ledger_store: Any) -> Dict[str, int]:
    """Repair a crash between an attention record and its stream event: items above the stream's
    per-automation floor are (re)appended. A stream that never recorded a floor takes the current
    items as its baseline (no history flood)."""
    from abstractruntime.automation_queries import list_automations
    from abstractruntime.automations.ledger import automation_records

    out = {"automations": 0, "appended": 0}
    state = stream.state()
    baseline = "attention_floor" not in state
    floors: Dict[str, int] = {str(k): int(v) for k, v in dict(state.get("attention_floor") or {}).items()}
    cursor = None
    while True:
        page = list_automations(run_store, cursor=cursor, limit=200)
        for item in page.items:
            aid = str(item.get("automation_id") or "")
            if not aid:
                continue
            out["automations"] += 1
            floor = floors.get(aid, 0)
            for rec in automation_records(ledger_store, aid, "automation.completed"):
                att = (rec.get("payload") or {}).get("attention")
                if not isinstance(att, dict) or int(att.get("seq") or 0) <= floor:
                    continue
                if baseline:
                    floors[aid] = max(floors.get(aid, 0), int(att["seq"]))
                    continue
                parsed = attention_from_record(
                    {"idempotency_key": rec.get("key"), "run_id": aid, "effect": {"type": "emit_event", "payload": {"name": "automation.completed", "payload": rec.get("payload")}}}
                )
                if parsed is not None and record_attention_raised(stream, parsed):
                    out["appended"] += 1
                    floors[aid] = max(floors.get(aid, 0), int(att["seq"]))
        if not page.next_cursor:
            break
        cursor = page.next_cursor
    if baseline:
        state = stream.state()
        state["attention_floor"] = floors
        stream.set_state(state)
    return out


__all__ = [
    "CHANNELS",
    "EVENT_TYPES",
    "SPEC_VERSION",
    "UserEventStream",
    "attention_from_record",
    "is_pointer_run",
    "plane_owner",
    "rebuild_attention",
    "rebuild_run_pointers",
    "record_attention_raised",
    "record_attention_seen",
    "record_notification",
    "record_run",
    "record_run_finished",
    "record_run_started",
    "run_origin",
    "stream_for",
    "stream_name",
    "stream_principal",
    "to_cloudevent",
]
