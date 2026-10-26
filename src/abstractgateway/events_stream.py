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
import re
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
CHANNELS = frozenset({"console", "email", "tray", "assistant", "code", "watch", "phone"})
# The client channels that report what they showed (POST /me/events/notifications).
CLIENT_CHANNELS = ("tray", "assistant", "code", "watch", "phone")
# Dedupe window of the client notification rule (`already_notified`).
NOTIFY_DEDUPE_S = 60.0
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
# Published events (a flow's PUBLISH_EVENT, `record_published`): an application-chosen channel and
# type. `attention` is the implicit channel of attention.* events and cannot be published to.
ATTENTION_CHANNEL = "attention"
CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PUBLISHED_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
PUBLISH_TITLE_MAX = 200
# Where a published alert can reach its recipient (Notify me's "Where"; AbstractRuntime
# `core/publish.DELIVERY_CHANNELS`): absent on the record = every channel.
ALERT_CHANNELS = ("tray", "watch", "phone", "email", "console")
# Whom a published alert reaches: the run's owner. Another account is a later feature.
ALERT_RECIPIENT = "me"
PUBLISH_BODY_MAX = 4000
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
    channel = payload.get("channel")
    if isinstance(channel, str) and channel:
        out["channel"] = channel
    return out


def event_channel(event: Mapping[str, Any]) -> Optional[str]:
    """The channel of a wire event: a published event's own, `attention` for attention.*, else None."""
    channel = event.get("channel")
    if isinstance(channel, str) and channel:
        return channel
    if str(event.get("type") or "").startswith("attention."):
        return ATTENTION_CHANNEL
    return None


def parse_channels(value: Optional[str]) -> Optional[frozenset]:
    """`a,b` -> {a, b}; None/empty -> None (no filter). Raises ValueError on a malformed name."""
    if value is None or not str(value).strip():
        return None
    names = [c.strip() for c in str(value).split(",") if c.strip()]
    for name in names:
        if not CHANNEL_RE.match(name):
            raise ValueError(f"invalid channel name {name!r} (lowercase letters, digits, '.', '_' or '-', at most 64)")
    return frozenset(names)


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

    def append_published(self, *, event_id: str, type: str, channel: str, data: Mapping[str, Any],
                         subject: Optional[str] = None, source: str = "/abstractgateway") -> Tuple[Dict[str, Any], bool]:
        """Append an application event on `channel` (validated by `record_published`)."""
        payload: Dict[str, Any] = {"type": type, "channel": channel, "source": source, "time": _now_iso(), "data": dict(data)}
        if subject:
            payload["subject"] = str(subject)
        result = self.inbox.append(stream=self.name, event_id=str(event_id), payload=payload)
        event = to_cloudevent(result.record)
        if not result.duplicate:
            self._notify()
        return event, not result.duplicate

    def read_filtered(self, *, after: int = 0, limit: int = 100, channels: Optional[frozenset] = None) -> Tuple[List[Dict[str, Any]], int]:
        """Events with `sequence > after` on `channels` (None = all), and the sequence scanned up to
        (the cursor to send next, past non-matching events)."""
        if not channels:
            events = self.read(after=after, limit=limit)
            return events, (int(events[-1]["sequence"]) if events else int(after))
        out: List[Dict[str, Any]] = []
        cursor = int(after)
        batch = max(200, int(limit) * 4)
        while len(out) < int(limit):
            chunk = self.read(after=cursor, limit=batch)
            if not chunk:
                break
            for ev in chunk:
                cursor = int(ev["sequence"])
                if event_channel(ev) in channels:
                    out.append(ev)
                    if len(out) >= int(limit):
                        break
            if len(chunk) < batch:
                break
        return out, cursor

    def channels(self) -> List[Dict[str, Any]]:
        """`[{channel, count, last_time, last_sequence}]` over the retained stream, most recent first."""
        seen: Dict[str, Dict[str, Any]] = {}
        cursor = 0
        while True:
            chunk = self.read(after=cursor, limit=1000)
            if not chunk:
                break
            for ev in chunk:
                cursor = int(ev["sequence"])
                ch = event_channel(ev)
                if ch is None:
                    continue
                row = seen.setdefault(ch, {"channel": ch, "count": 0, "last_time": None, "last_sequence": 0})
                row["count"] += 1
                row["last_time"] = ev.get("time")
                row["last_sequence"] = cursor
            if len(chunk) < 1000:
                break
        return sorted(seen.values(), key=lambda r: -int(r["last_sequence"]))

    def read(self, *, after: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
        return [to_cloudevent(r) for r in self.inbox.read(after_seq=max(0, int(after)), stream=self.name, limit=max(1, int(limit)))]

    def head(self) -> int:
        return int(self.inbox.head_seq())

    def index_event_ids(self) -> Dict[str, int]:
        """`{event id: sequence}` from the inbox index: one file read, no event file opened.
        Event ids are deterministic (`notification.sent:<channel>:attention:<record_key>`, ...),
        so a reader that knows what it looks for finds it here without scanning the stream."""
        try:
            idx = json.loads((self.dir / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        ids = idx.get("event_ids") if isinstance(idx, dict) else None
        if not isinstance(ids, dict):
            return {}
        out: Dict[str, int] = {}
        for key, seq in ids.items():
            try:
                out[str(key)] = int(seq)
            except (TypeError, ValueError):
                continue
        return out

    def events_by_ids(self, event_ids: Iterable[str], *, index: Optional[Mapping[str, int]] = None) -> List[Dict[str, Any]]:
        """The wire events with these ids (absent or pruned ones skipped), oldest first: one read of
        the index, then one event file per hit."""
        ids = index if index is not None else self.index_event_ids()
        out: List[Dict[str, Any]] = []
        for seq in sorted({int(ids[e]) for e in event_ids if e in ids}):
            rows = self.read(after=seq - 1, limit=1)
            if rows and int(rows[0]["sequence"]) == seq:
                out.append(rows[0])
        return out

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


def notice_event_ids(record_key: str, *, email_keys: Iterable[str] = ()) -> List[str]:
    """Every id a delivery of the attention item `record_key` can carry in the stream:
    `notification.<sent|failed>:<channel>:attention:<record_key>` for the console and the client
    and push channels, `notification.<sent|failed>:email:<outbox key>` for each email notice key."""
    ids = [
        f"notification.{outcome}:{channel}:attention:{record_key}"
        for channel in sorted(CHANNELS - {"email"})
        for outcome in ("sent", "failed")
    ]
    ids += [f"notification.{outcome}:email:{key}" for key in email_keys for outcome in ("sent", "failed")]
    return ids


def published_alert_ids(index: Mapping[str, int], run_id: str) -> List[str]:
    """The `attention.raised` ids of the alerts a run published (Notify me, Publish to channel with
    Alert on): `attention.raised:publish:<run_id>:<step_id>`, from the index keys alone."""
    prefix = f"attention.raised:publish:{run_id}:"
    return sorted((k for k in index if k.startswith(prefix)), key=lambda k: int(index[k]))


def delivery_route(event: Mapping[str, Any]) -> str:
    """How a recorded delivery travelled: `push` (the gateway's push dispatcher), `outbox` (email),
    else `stream` (an app read the person's stream and showed it; the console lists the item)."""
    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    if data.get("via") == "push":
        return "push"
    if data.get("channel") == "email":
        return "outbox"
    return "stream"


def already_notified(
    events: Iterable[Mapping[str, Any]],
    *,
    record_key: str,
    channel: str,
    device: Optional[str],
    now: Optional[float] = None,
    window_s: float = NOTIFY_DEDUPE_S,
) -> bool:
    """The client dedupe rule: before showing an OS notification for `record_key`, an app stays
    silent when the stream already holds a `notification.sent` for that record from ANOTHER client
    channel on the SAME device within the last `window_s` seconds (the tray is the default notifier;
    an app notifies only when no tray, or other app, reported). Pure: give it recent events."""
    import time as _time

    clock = float(now if now is not None else _time.time())
    for ev in events:
        if ev.get("type") != "notification.sent":
            continue
        data = ev.get("data") if isinstance(ev.get("data"), Mapping) else {}
        if data.get("record_key") != record_key or data.get("channel") == channel:
            continue
        if data.get("channel") not in CLIENT_CHANNELS or not device or data.get("device") != device:
            continue
        at = _epoch(ev.get("time"))
        if at is not None and 0 <= clock - at <= float(window_s):
            return True
    return False


def alert_channels(data: Mapping[str, Any]) -> Optional[List[str]]:
    """The channels a published alert asked for (`channels` on its `attention.raised`), or None:
    every channel (an older record, a flow that chose "All my channels", an automation item, whose
    `channels` is the automation's own console/email setting)."""
    if not str(data.get("record_key") or "").startswith("publish:"):
        return None
    asked = data.get("channels")
    if not isinstance(asked, list):
        return None
    return [str(c) for c in asked]


def alert_asks(data: Mapping[str, Any], channel: str) -> bool:
    """Does this `attention.raised` reach `channel` (tray, watch, phone, email, console)? Every
    deliverer asks before delivering: absent `channels` = yes."""
    asked = alert_channels(data)
    return asked is None or channel in asked


def _alert_channels_of(payload: Mapping[str, Any]) -> Optional[List[str]]:
    value = payload.get("channels")
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"channels must be a non-empty list of {', '.join(ALERT_CHANNELS)} (or left out for all)")
    if not all(isinstance(c, str) for c in value):
        raise ValueError(f"channels must be names among {', '.join(ALERT_CHANNELS)}")
    asked = {c.strip().lower() for c in value}
    unknown = sorted(asked - set(ALERT_CHANNELS))
    if unknown or not asked:
        raise ValueError(f"unknown channel(s) {unknown}: choose from {', '.join(ALERT_CHANNELS)}")
    return [c for c in ALERT_CHANNELS if c in asked]


def record_published(stream: UserEventStream, *, run_id: str, step_id: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    """A flow's `PUBLISH_EVENT` on the caller's stream: `{type, channel, subject?, data: {title, body?, ...data}}`
    with id `publish:<run_id>:<step_id>` (a replayed step appends nothing); with `alert: true` also an
    `attention.raised {kind: notify, record_key: publish:<run_id>:<step_id>, channel, run_id, title, recipient,
    channels?}` so the tray, the Assistant and the watch announce it: `recipient` is "me" (the only one
    today), `channels` the deliverers it asked for (absent = every channel; each deliverer reads it with
    `alert_asks`). Returns `{event_id, sequence, alerted}`. Raises ValueError with a sentence on a
    refused payload."""
    if not isinstance(payload, Mapping):
        raise ValueError("publish needs an object")
    unknown = sorted(k for k in payload if k not in ("channel", "type", "title", "body", "subject", "data", "alert", "recipient", "channels"))
    if unknown:
        raise ValueError(f"unknown publish field(s) {unknown}")
    channel = str(payload.get("channel") or "").strip()
    if not CHANNEL_RE.match(channel):
        raise ValueError("channel must be a name: lowercase letters, digits, '.', '_' or '-', at most 64, starting with a letter or digit")
    if channel == ATTENTION_CHANNEL:
        raise ValueError("the 'attention' channel is the gateway's own; use alert: true to reach the person")
    typ = str(payload.get("type") or f"{channel}.message").strip()
    if typ in EVENT_TYPES or not PUBLISHED_TYPE_RE.match(typ):
        raise ValueError("type must be your own name (lowercase letters, digits, '.', '_' or '-'), not one of the gateway's event types")
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > PUBLISH_TITLE_MAX:
        raise ValueError(f"title must be a non-empty text of at most {PUBLISH_TITLE_MAX} characters")
    body = payload.get("body")
    if body is not None and (not isinstance(body, str) or len(body) > PUBLISH_BODY_MAX):
        raise ValueError(f"body must be a text of at most {PUBLISH_BODY_MAX} characters")
    extra = payload.get("data") or {}
    if not isinstance(extra, Mapping):
        raise ValueError("data must be an object")
    subject = payload.get("subject")
    if subject is not None and (not isinstance(subject, str) or len(subject) > 200):
        raise ValueError("subject must be a text of at most 200 characters")
    alert = payload.get("alert", False)
    if not isinstance(alert, bool):
        raise ValueError("alert must be true or false")
    recipient = str(payload.get("recipient") or ALERT_RECIPIENT).strip()
    if recipient != ALERT_RECIPIENT:
        raise ValueError(f"notifying another account is not available yet: the recipient is \"{ALERT_RECIPIENT}\" (you)")
    channels = _alert_channels_of(payload)
    if channels is not None and not alert:
        raise ValueError("channels choose where the alert reaches you: set alert on, or leave channels out")
    data: Dict[str, Any] = {**dict(extra), "title": title.strip()}
    if body:
        data["body"] = body
    json.dumps(data)  # JSON-safe or raise
    key = f"publish:{run_id}:{step_id}"
    event, _created = stream.append_published(
        event_id=key, type=typ, channel=channel, data=data, subject=subject, source=f"/abstractgateway/runs/{run_id}"
    )
    if alert:
        stream.append(
            "attention.raised",
            event_id=f"attention.raised:{key}",
            subject=str(run_id),
            source=f"/abstractgateway/runs/{run_id}",
            data={"kind": "notify", "record_key": key, "channel": channel, "run_id": str(run_id), "title": data["title"],
                  "published_sequence": event["sequence"], "recipient": recipient,
                  **({"channels": channels} if channels is not None else {})},
        )
    return {"event_id": key, "sequence": event["sequence"], "alerted": bool(alert)}


def record_publish_seen(stream: UserEventStream, *, record_key: str) -> bool:
    """`attention.seen {record_key}` for a published alert the person has seen."""
    key = str(record_key)
    if not key.startswith("publish:"):
        raise ValueError("only a published alert's record_key (publish:...) is marked seen here")
    _e, created = stream.append(
        "attention.seen", event_id=f"attention.seen:{key}", subject=key, source="/abstractgateway/notifications",
        data={"record_key": key},
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


# ---------------------------------------------------------------------------------------------
# Interactive waits: a run parked on a person (an approval, a question)
# ---------------------------------------------------------------------------------------------

WAIT_KIND_WORDS = {"tool_approval": "approval", "ask_user": "question", "event": "question"}


def wait_attention_from_record(record: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """`{run_id, wait_key, step_id, kind, wait_kind, record_key, result_key}` from the ledger record
    of a step that parked its run on a PERSON (the structure of `automations.attention.wait_kind`:
    a USER wait that is not a pause, or an EVENT wait with a prompt or choices; never a controller's
    wake). No prompt, no tool arguments: the client reads the wait from the run."""
    if not isinstance(record, Mapping):
        return None
    result = record.get("result")
    wait = result.get("wait") if isinstance(result, Mapping) else None
    if not isinstance(wait, Mapping):
        return None
    run_id = str(record.get("run_id") or "")
    step_id = str(record.get("step_id") or "")
    key = str(wait.get("wait_key") or "")
    if not run_id or not step_id or not key:
        return None
    if key.startswith("automation:") and key.endswith(":wake"):
        return None
    reason = str(wait.get("reason") or "").lower()
    details = wait.get("details") if isinstance(wait.get("details"), Mapping) else {}
    if reason == "user":
        if key == f"pause:{run_id}" or details.get("kind") == "pause":
            return None
        wait_kind = "tool_approval" if details.get("mode") == "approval_required" else "ask_user"
    elif reason == "event":
        if not (wait.get("prompt") or wait.get("choices")):
            return None
        wait_kind = "event"
    else:
        return None
    return {
        "run_id": run_id,
        "wait_key": key,
        "step_id": step_id,
        "kind": WAIT_KIND_WORDS[wait_kind],
        "wait_kind": wait_kind,
        "record_key": str(record.get("idempotency_key") or "") or f"step:{run_id}:{step_id}",
        "result_key": wait.get("result_key"),
    }


def _wait_event_id(prefix: str, item: Mapping[str, Any]) -> str:
    return f"{prefix}:{item['run_id']}:{item['wait_key']}:{item['step_id']}"


def record_wait_raised(stream: UserEventStream, item: Mapping[str, Any], *, session_id: Optional[str]) -> bool:
    """`attention.raised {kind: approval|question, run_id, session_id, wait_key, step_id,
    record_key, prompt_ref}`: the client fetches the prompt (and the calls awaiting approval) from
    `prompt_ref`, the run."""
    rid = str(item["run_id"])
    _e, created = stream.append(
        "attention.raised",
        event_id=_wait_event_id("attention.raised", item),
        subject=rid,
        source="/abstractgateway/runs",
        data={
            "kind": item["kind"],
            "wait_kind": item.get("wait_kind"),
            "run_id": rid,
            "session_id": session_id,
            "wait_key": item["wait_key"],
            "step_id": item["step_id"],
            "record_key": item.get("record_key"),
            "prompt_ref": f"/api/gateway/runs/{rid}",
        },
    )
    return created


def _get_path(obj: Any, path: Any) -> Any:
    cur = obj
    for part in str(path or "").split("."):
        if not part:
            continue
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def wait_outcome(run: Any, item: Mapping[str, Any]) -> str:
    """How an open wait ended: cancelled | failed | timed_out | approved | denied | answered."""
    st = _status(run) if run is not None else "cancelled"
    if run is None or st == "cancelled":
        return "cancelled"
    payload = _get_path(_vars(run), item.get("result_key")) if item.get("result_key") else None
    if isinstance(payload, dict):
        if payload.get("timed_out") is True:
            return "timed_out"
        if item.get("wait_kind") == "tool_approval" and isinstance(payload.get("approved"), bool):
            return "approved" if payload["approved"] else "denied"
    if st == "failed":
        return "failed"
    return "answered"


def record_wait_seen(stream: UserEventStream, item: Mapping[str, Any], *, outcome: str) -> bool:
    """`attention.seen {kind, run_id, wait_key, step_id, outcome}` once the wait is resolved."""
    rid = str(item["run_id"])
    _e, created = stream.append(
        "attention.seen",
        event_id=_wait_event_id("attention.seen", item),
        subject=rid,
        source="/abstractgateway/runs",
        data={"kind": item["kind"], "run_id": rid, "wait_key": item["wait_key"], "step_id": item["step_id"], "outcome": str(outcome)},
    )
    return created


def still_waiting_on(run: Any, item: Mapping[str, Any]) -> bool:
    """The run is still parked on this wait (same key, still on a person)."""
    if run is None or _status(run) != "waiting":
        return False
    waiting = getattr(run, "waiting", None)
    if waiting is None or str(getattr(waiting, "wait_key", "") or "") != str(item.get("wait_key")):
        return False
    try:
        from abstractruntime.automations.attention import wait_kind

        return wait_kind(run) is not None
    except Exception:
        return False


def latest_wait_record(ledger_store: Any, run: Any) -> Optional[Dict[str, Any]]:
    """The wait item of the run's CURRENT wait, from its newest ledger record naming it (the boot
    repair's step_id source)."""
    waiting = getattr(run, "waiting", None)
    key = str(getattr(waiting, "wait_key", "") or "")
    if not key:
        return None
    try:
        records = list(ledger_store.list(str(run.run_id)) or [])
    except Exception:
        return None
    for rec in reversed(records):
        item = wait_attention_from_record(rec) if isinstance(rec, Mapping) else None
        if item is not None and item["wait_key"] == key:
            return item
    return None


__all__ = [
    "CHANNELS",
    "EVENT_TYPES",
    "CLIENT_CHANNELS",
    "NOTIFY_DEDUPE_S",
    "SPEC_VERSION",
    "UserEventStream",
    "already_notified",
    "attention_from_record",
    "delivery_route",
    "notice_event_ids",
    "published_alert_ids",
    "is_pointer_run",
    "plane_owner",
    "rebuild_attention",
    "rebuild_run_pointers",
    "record_attention_raised",
    "record_attention_seen",
    "record_notification",
    "record_run",
    "record_run_finished",
    "record_published",
    "alert_asks",
    "alert_channels",
    "ALERT_CHANNELS",
    "record_publish_seen",
    "event_channel",
    "parse_channels",
    "record_run_started",
    "record_wait_raised",
    "record_wait_seen",
    "run_origin",
    "stream_for",
    "stream_name",
    "stream_principal",
    "to_cloudevent",
]
