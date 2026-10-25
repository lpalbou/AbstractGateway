"""The push dispatcher of one plane (framework backlog 1049).

Runs in the process that runs the plane's runner (started beside the email worker,
`service._start_push_dispatcher`). It reads the plane owner's event stream from its own cursor and
turns `attention.raised` events into requests to the push relay:

    approval              -> approval
    question              -> question
    notify, publish: key  -> notify     (a flow's published alert)
    notify (automation)   -> result
    failure               -> failure
    anything else         -> nothing

`attention.seen` drops the matching queued rows (an answered approval is never pushed). Each
(event, device) is one outbox row; the relay request is exactly
`{topic, env, token, kind, collapse, seq, reg}` (no text: the device fetches the event itself).
Collapse id: `sha256("attention:" + record_key)[:32]`, the same id the apps use for their own
catch-up notification, so nothing alerts twice.

Watch-first: a principal with a watch registration refreshed in the last 24 h gets alerts on the
watch only; otherwise on the phone (the two apps are separate families; both would ring the wrist).

Idle guarantee: with `--push-relay off` no dispatcher thread exists. With a relay but no
registration the thread is parked on an event with no timeout (in the serve process the device
route wakes it); a split runner process, which the API process cannot wake, stats `devices.json`
every 2 s and nothing else. No socket is opened until a row is due.

Retries: 2 s, 10 s, 60 s, 5 min, then every 15 min until the row is an hour old; then it is dropped
(`notification.failed` for an approval or a question). One log line per hour per plane at most.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from .store import WATCH_FRESH_S, DeviceStore, Outbox, read_cursor, write_cursor

logger = logging.getLogger("abstractgateway.push")

POLL_S = 2.0
BATCH = 200
RETRY_LADDER_S = (2.0, 10.0, 60.0, 300.0)
RETRY_EVERY_S = 900.0
DROP_AFTER_S = 3600.0
LOG_EVERY_S = 3600.0
PURGE_EVERY_S = 3600.0
RELAY_TIMEOUT_S = 10.0
PUSH_KINDS = ("approval", "question", "notify", "result", "failure")
RELAY_FIELDS = ("topic", "env", "token", "kind", "collapse", "seq", "reg")


def collapse_id(record_key: str) -> str:
    return hashlib.sha256(("attention:" + str(record_key)).encode("utf-8")).hexdigest()[:32]


def push_kind(event: Mapping[str, Any]) -> Optional[str]:
    """The push kind of a stream event, or None (not pushed)."""
    if event.get("type") != "attention.raised":
        return None
    data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
    kind = str(data.get("kind") or "")
    key = str(data.get("record_key") or "")
    if not key:
        return None
    if kind in ("approval", "question", "failure"):
        return kind
    if kind == "notify":
        if key.startswith("publish:"):
            return "notify"
        if data.get("automation_id"):
            return "result"
    return None


def seen_key_of_raised(data: Mapping[str, Any]) -> tuple:
    """(seen_key, seen_seq) that the item's `attention.seen` will carry."""
    if data.get("wait_key"):
        return f"wait:{data.get('run_id')}:{data.get('wait_key')}:{data.get('step_id')}", 0
    key = str(data.get("record_key") or "")
    if key.startswith("publish:"):
        return key, 0
    if data.get("automation_id"):
        return f"auto:{data.get('automation_id')}", int(data.get("seq") or 0)
    return key, 0


def seen_key_of_seen(data: Mapping[str, Any]) -> Optional[tuple]:
    """(seen_key, seen_seq) an `attention.seen` ends (rows with seen_seq <= it)."""
    if data.get("wait_key"):
        return f"wait:{data.get('run_id')}:{data.get('wait_key')}:{data.get('step_id')}", 0
    key = str(data.get("record_key") or "")
    if key:
        return key, 0
    if data.get("automation_id"):
        return f"auto:{data.get('automation_id')}", int(data.get("seq") or 0)
    return None


def alert_targets(devices: List[Mapping[str, Any]], now: float) -> List[Mapping[str, Any]]:
    """Watch-first: the fresh watches, else the phones, else whatever watches remain."""
    watches = [d for d in devices if d.get("app") == "watch"]
    fresh = [d for d in watches if float(d.get("refreshed_at") or 0.0) + WATCH_FRESH_S > float(now)]
    if fresh:
        return fresh
    phones = [d for d in devices if d.get("app") == "phone"]
    return phones or watches


class RelayClient:
    """POST <relay>/v1/push over httpx. The client (and its sockets) exist only once a row is sent."""

    def __init__(self, base_url: str, *, transport: Any = None, timeout_s: float = RELAY_TIMEOUT_S) -> None:
        self.base_url = str(base_url).rstrip("/")
        self._transport = transport
        self._timeout = float(timeout_s)
        self._client: Any = None
        self._lock = threading.Lock()

    def _http(self) -> Any:
        with self._lock:
            if self._client is None:
                import httpx

                kwargs: Dict[str, Any] = {"timeout": self._timeout}
                if self._transport is not None:
                    kwargs["transport"] = self._transport
                self._client = httpx.Client(**kwargs)
            return self._client

    def send(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        """`{status: sent|gone|rejected|retry, ...}`; a network failure is `retry`."""
        try:
            resp = self._http().post(f"{self.base_url}/v1/push", json=dict(body))
        except Exception as exc:  # noqa: BLE001 - every transport failure is a retry
            return {"status": "retry", "after_s": 0, "reason": f"{type(exc).__name__}: {exc}"}
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if resp.status_code in (429, 503) or resp.status_code >= 500:
            return {"status": "retry", "after_s": float(payload.get("after_s") or 0), "reason": f"relay answered {resp.status_code}"}
        if resp.status_code == 200 and payload.get("status") in ("sent", "gone", "rejected"):
            return payload
        reason = payload.get("reason") or payload.get("detail") or f"relay answered {resp.status_code}"
        return {"status": "rejected", "reason": str(reason)[:200]}

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


class PushDispatcher:
    """One plane's dispatcher. `tick()` is one synchronous pass (tests drive it with a clock)."""

    def __init__(
        self,
        *,
        plane_dir: Path,
        root_dir: Path,
        stream: Any,
        relay_url: Optional[str],
        poll_when_idle: bool = False,
        clock: Callable[[], float] = time.time,
        transport: Any = None,
        name: str = "push",
    ) -> None:
        from .store import principal_key

        self.plane_dir = Path(plane_dir)
        self.stream = stream
        self.owner = principal_key(stream.tenant, stream.user)
        self.store = DeviceStore(self.plane_dir, Path(root_dir))
        self.outbox = Outbox(self.plane_dir)
        self.relay_url = relay_url
        self.relay = RelayClient(relay_url, transport=transport) if relay_url else None
        self.poll_when_idle = bool(poll_when_idle)
        self.clock = clock
        self.name = name
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._listener_off: Optional[Callable[[], None]] = None
        self._last_log = float("-inf")
        self._last_purge = float("-inf")
        self.ticks = 0  # passes that did work (the idle tests read it)

    # -- lifecycle ---------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """No relay (`off`): nothing at all. Else the dispatcher thread (idempotent)."""
        if self.relay is None or self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"gateway-{self.name}", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout_s)
        self._thread = None
        self._unsubscribe()
        if self.relay is not None:
            self.relay.close()

    def kick(self) -> None:
        """A registration changed (the device route, same process)."""
        self._wake.set()

    def _subscribe(self) -> None:
        if self._listener_off is None:
            self._listener_off = self.stream.add_listener(self._wake.set)

    def _unsubscribe(self) -> None:
        off, self._listener_off = self._listener_off, None
        if off is not None:
            off()

    def _devices(self, now: float) -> List[Dict[str, Any]]:
        return self.store.active(principal=self.owner, now=now)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._devices(self.clock()):
                    self._park()
                    continue
                self._subscribe()
                result = self.tick()
                self._wait_active(result.get("next_due"))
            except Exception:  # noqa: BLE001 - the dispatcher never dies on one bad pass
                logger.warning("push dispatcher pass failed (%s)", self.plane_dir, exc_info=True)
                self._stop.wait(POLL_S)

    def _park(self) -> None:
        """Idle: no listener, no reads; the device route (or, split, one stat per 2 s) ends it."""
        self._unsubscribe()
        if not self.poll_when_idle:
            self._wake.wait()
            self._wake.clear()
            return
        token = self.store.change_token()
        while not self._stop.is_set():
            if self._wake.wait(POLL_S) or self.store.change_token() != token:
                self._wake.clear()
                return

    def _wait_active(self, next_due: Optional[float]) -> None:
        stream_token = self.stream.change_token()
        devices_token = self.store.change_token()
        while not self._stop.is_set():
            now = self.clock()
            timeout = POLL_S if next_due is None else max(0.05, min(POLL_S, next_due - now))
            if self._wake.wait(timeout):
                self._wake.clear()
                return
            if next_due is not None and self.clock() >= next_due:
                return
            # Another process may have appended (split mode) or changed a registration.
            if self.stream.change_token() != stream_token or self.store.change_token() != devices_token:
                return

    # -- one pass ----------------------------------------------------------------------------

    def tick(self, now: Optional[float] = None) -> Dict[str, Any]:
        clock = float(now if now is not None else self.clock())
        devices = self._devices(clock)
        if not devices or self.relay is None:
            return {"idle": True, "next_due": None}
        self.ticks += 1
        cursor = read_cursor(self.plane_dir)
        if cursor is None:
            # First start: the stream head is the baseline (old events are never pushed).
            cursor = int(self.stream.head())
            write_cursor(self.plane_dir, cursor)
        enqueued = dropped = 0
        start = cursor
        while True:
            events = self.stream.read(after=cursor, limit=BATCH)
            for event in events:
                cursor = int(event["sequence"])
                kind = push_kind(event)
                data = event.get("data") if isinstance(event.get("data"), Mapping) else {}
                if kind is not None:
                    seen_key, seen_seq = seen_key_of_raised(data)
                    record_key = str(data.get("record_key"))
                    rows = [
                        {
                            "event_id": str(event.get("id") or f"seq:{cursor}"),
                            "device_id": d["device_id"],
                            "seq": cursor,
                            "kind": kind,
                            "collapse": collapse_id(record_key),
                            "record_key": record_key,
                            "created_ts": clock,
                            "next_ts": clock,
                            "seen_key": seen_key,
                            "seen_seq": seen_seq,
                        }
                        for d in alert_targets(devices, clock)
                    ]
                    enqueued += self.outbox.enqueue(rows)
                elif event.get("type") == "attention.seen":
                    match = seen_key_of_seen(data)
                    if match is not None:
                        dropped += self.outbox.drop_seen(*match)
            if len(events) < BATCH:
                break
        if cursor != start:
            write_cursor(self.plane_dir, cursor)
        sent = self._send_due(clock, {d["device_id"]: d for d in devices})
        if clock - self._last_purge >= PURGE_EVERY_S or enqueued:
            self.outbox.purge(clock)
            self._last_purge = clock
        return {"idle": False, "enqueued": enqueued, "dropped_seen": dropped, "sent": sent, "cursor": cursor,
                "next_due": self.outbox.next_due()}

    def _log(self, now: float, msg: str, *args: Any) -> None:
        if now - self._last_log >= LOG_EVERY_S:
            self._last_log = now
            logger.warning(msg, *args)

    def _record(self, outcome: str, row: Mapping[str, Any], device: Mapping[str, Any], error: Optional[str] = None) -> None:
        from ..events_stream import record_notification

        data: Dict[str, Any] = {"record_key": row["record_key"], "device": row["device_id"], "via": "push"}
        if error:
            data["error"] = error
        try:
            record_notification(self.stream, outcome, channel=str(device.get("app")), notice=f"attention:{row['record_key']}", data=data)
        except Exception:  # noqa: BLE001 - a record never stops delivery
            logger.debug("push notification record failed", exc_info=True)

    def _drop(self, row: Mapping[str, Any], device: Optional[Mapping[str, Any]], error: str) -> None:
        self.outbox.mark(row["event_id"], row["device_id"], state="dropped", error=error)
        if device is not None and row["kind"] in ("approval", "question"):
            self._record("failed", row, device, error)

    def _send_due(self, now: float, devices: Mapping[str, Mapping[str, Any]]) -> int:
        sent = 0
        relay_down: Optional[str] = None
        for row in self.outbox.due(now):
            device = devices.get(row["device_id"])
            if device is None:
                self.outbox.mark(row["event_id"], row["device_id"], state="dropped", error="device removed")
                continue
            if relay_down is None:
                body = {
                    "topic": device["topic"],
                    "env": device["env"],
                    "token": device["token"],
                    "kind": row["kind"],
                    "collapse": row["collapse"],
                    "seq": int(row["seq"]),
                    "reg": device["reg"],
                }
                answer = self.relay.send(body)
            else:
                # The relay is down for this pass: the remaining rows wait without a call.
                answer = {"status": "retry", "after_s": 0, "reason": relay_down}
            status = answer.get("status")
            if status == "sent":
                sent += 1
                self.outbox.mark(row["event_id"], row["device_id"], state="sent", attempts=int(row["attempts"]) + 1)
                self.store.update(row["device_id"], last_sent_at=now, last_error=None, relay_unreachable_since=None)
                self._record("sent", row, device)
            elif status == "gone":
                self._drop(row, device, "device token no longer valid")
                self.store.remove_if_gone(row["device_id"], since_ms=answer.get("since_ms"))
            elif status == "rejected":
                reason = str(answer.get("reason") or "refused")[:200]
                self._drop(row, device, f"rejected: {reason}")
                self.store.update(row["device_id"], last_error=f"The push relay refused an alert: {reason}")
            else:
                relay_down = relay_down or str(answer.get("reason") or "relay unavailable")
                attempts = int(row["attempts"]) + 1
                if now - float(row["created_ts"]) >= DROP_AFTER_S:
                    self._drop(row, device, f"relay unreachable for an hour ({relay_down})")
                    self._log(now, "push: alerts dropped after an hour without the relay %s (%s)", self.relay_url, relay_down)
                    continue
                step = RETRY_LADDER_S[attempts - 1] if attempts - 1 < len(RETRY_LADDER_S) else RETRY_EVERY_S
                after = float(answer.get("after_s") or 0.0)
                next_ts = min(now + max(step, min(after, RETRY_EVERY_S)), float(row["created_ts"]) + DROP_AFTER_S)
                self.outbox.mark(row["event_id"], row["device_id"], state="queued", attempts=attempts, next_ts=next_ts, error=relay_down)
                if not device.get("relay_unreachable_since"):
                    self.store.update(row["device_id"], relay_unreachable_since=now)
                self._log(now, "push: relay %s unavailable (%s); alerts retry for an hour", self.relay_url, relay_down)
        return sent


__all__ = [
    "PushDispatcher",
    "RelayClient",
    "RELAY_FIELDS",
    "alert_targets",
    "collapse_id",
    "push_kind",
]
