"""The tray's consumer of the person's event stream (framework backlog 1046).

One daemon thread follows `GET /api/gateway/me/events/stream` (SSE) after a catch-up through
`GET /me/events`; it reconnects with `Last-Event-ID` after any break, so nothing is missed and
nothing is shown twice. What the tray does with the events:

- `attention.raised` arriving LIVE (after the tray started) -> one OS notification, then
  `POST /me/events/notifications {channel: "tray"}` records that it was shown (or why not);
- the badge = attention items raised and not yet seen (`attention.raised` seq above the latest
  `attention.seen` seq of the same automation), over the whole retained history;
- automation-shaped events (`trigger.event`, `attention.*`, `wake.*`, an occurrence's
  `run.started|finished`) refresh the Scheduled rows: the sampler's `/automations` fetch is
  event-driven instead of a 20 s poll.

Pure parts (`AttentionLedger`, `SseParser`) are separated from the thread for tests.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

AUTOMATION_EVENT_TYPES = frozenset({"trigger.event", "attention.raised", "attention.seen", "wake.scheduled", "wake.cancelled"})
PAGE = 1000


@dataclass(frozen=True)
class AttentionNotice:
    """One attention item the tray should show (a live `attention.raised` not yet seen)."""

    record_key: str
    automation_id: str
    kind: str  # notify | failure (an automation item) | approval | question (a run waiting on you)
    seq: int
    run_id: str = ""
    session_id: str = ""


WAIT_KINDS = frozenset({"approval", "question"})


class AttentionLedger:
    """Raised minus seen, from the stream (pure): automation items per automation (a seen cursor
    covers every seq below it) and waits on a person per (run, wait_key, step) (each one is seen
    when it is resolved)."""

    def __init__(self) -> None:
        self._raised: Dict[str, Set[int]] = {}
        self._seen: Dict[str, int] = {}
        self._waits: Set[Tuple[str, str, str]] = set()
        self._waits_seen: Set[Tuple[str, str, str]] = set()

    def _apply_wait(self, typ: Any, data: Dict[str, Any]) -> Optional[AttentionNotice]:
        key = (str(data.get("run_id") or ""), str(data.get("wait_key") or ""), str(data.get("step_id") or ""))
        if not key[0]:
            return None
        if typ == "attention.raised":
            if key in self._waits_seen:
                return None
            self._waits.add(key)
            return AttentionNotice(
                str(data.get("record_key") or ""), "", str(data.get("kind")), 0,
                run_id=key[0], session_id=str(data.get("session_id") or ""),
            )
        if typ == "attention.seen":
            self._waits.discard(key)
            self._waits_seen.add(key)
        return None

    def apply(self, event: Dict[str, Any]) -> Optional[AttentionNotice]:
        """Fold one event; returns the notice when it raised an unseen item."""
        typ = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if data.get("kind") in WAIT_KINDS:
            return self._apply_wait(typ, data)
        aid = str(data.get("automation_id") or "")
        if not aid:
            return None
        try:
            seq = int(data.get("seq") or 0)
        except (TypeError, ValueError):
            return None
        if typ == "attention.raised" and seq > 0:
            self._raised.setdefault(aid, set()).add(seq)
            if seq > self._seen.get(aid, 0):
                return AttentionNotice(str(data.get("record_key") or ""), aid, str(data.get("kind") or ""), seq)
        elif typ == "attention.seen":
            self._seen[aid] = max(self._seen.get(aid, 0), seq)
            self._raised[aid] = {s for s in self._raised.get(aid, set()) if s > self._seen[aid]}
        return None

    @property
    def unseen(self) -> int:
        items = sum(len([s for s in seqs if s > self._seen.get(aid, 0)]) for aid, seqs in self._raised.items())
        return items + len(self._waits)


class SseParser:
    """Incremental SSE parser: feed lines, get `(id, event_type, data)` per complete frame."""

    def __init__(self) -> None:
        self._id: Optional[str] = None
        self._event: Optional[str] = None
        self._data: List[str] = []

    def feed(self, line: str) -> Optional[Tuple[Optional[str], Optional[str], str]]:
        line = line.rstrip("\r\n")
        if line == "":
            if not self._data:
                self._id, self._event = None, None
                return None
            frame = (self._id, self._event, "\n".join(self._data))
            self._id, self._event, self._data = None, None, []
            return frame
        if line.startswith(":"):
            return None  # comment / keep-alive
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            self._id = value
        elif field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        return None


def is_automation_event(event: Dict[str, Any]) -> bool:
    typ = event.get("type")
    if typ in AUTOMATION_EVENT_TYPES:
        return True
    if typ in ("run.started", "run.finished"):
        origin = (event.get("data") or {}).get("origin") if isinstance(event.get("data"), dict) else None
        return isinstance(origin, dict) and origin.get("kind") == "trigger"
    return False


class EventFollower:
    """Catch-up, then the SSE tail, reconnecting with `Last-Event-ID` (a daemon thread).

    `on_event(event, live)` is called on this thread for every event in order; `live` is False
    for the history read at start. `on_state(state)` reports `connecting|live|unsupported|
    unauthorized`. Never raises to the caller."""

    def __init__(
        self,
        client: Any,
        *,
        on_event: Callable[[Dict[str, Any], bool], None],
        on_state: Optional[Callable[[str], None]] = None,
        max_backoff_s: float = 30.0,
    ) -> None:
        self._client = client
        self._on_event = on_event
        self._on_state = on_state or (lambda _s: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._max_backoff = float(max_backoff_s)
        self.cursor = 0
        self.state = "connecting"
        self._caught_up = False
        self._auth_failures = 0
        self._response: Any = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tray-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        resp = self._response
        if resp is not None:
            # A read blocked in another thread is only released by shutting the socket down
            # (closing the file object does not interrupt recv).
            try:
                import socket

                sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001
                pass
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            try:
                self._on_state(state)
            except Exception:  # noqa: BLE001
                logger.debug("tray events: state listener failed", exc_info=True)

    def _deliver(self, event: Dict[str, Any], live: bool) -> None:
        try:
            seq = int(event.get("sequence") or 0)
        except (TypeError, ValueError):
            return
        if seq <= self.cursor:
            return  # already delivered (a replayed frame never shows twice)
        self.cursor = seq
        try:
            self._on_event(event, live)
        except Exception:  # noqa: BLE001 - a UI bug must not stop the stream
            logger.debug("tray events: listener failed", exc_info=True)

    def _refused(self, status: int) -> bool:
        """401/403 twice: the token is dead; 404: a gateway without the stream. Stop either way."""
        if status in (401, 403):
            self._auth_failures += 1
            if self._auth_failures >= 2:
                self._set_state("unauthorized")
                return True
        elif status == 404:
            self._set_state("unsupported")
            return True
        return False

    def catch_up(self) -> bool:
        """History through the page route (not live). False on a failure."""
        while not self._stop.is_set():
            r = self._client.my_events(after=self.cursor, limit=PAGE)
            if not r.ok or not isinstance(r.data, dict):
                if self._refused(int(r.status or 0)):
                    self._stop.set()
                return False
            self._auth_failures = 0
            for ev in r.data.get("events") or []:
                if isinstance(ev, dict):
                    self._deliver(ev, False)
            if not r.data.get("more"):
                return True
        return False

    def follow_once(self) -> None:
        """One SSE connection until it breaks (or stop)."""
        resp, status = self._client.open_event_stream(after=self.cursor)
        if resp is None:
            if self._refused(int(status or 0)):
                self._stop.set()
            return
        self._response = resp
        self._auth_failures = 0
        self._set_state("live")
        parser = SseParser()
        try:
            while not self._stop.is_set():
                raw = resp.readline()
                if not raw:
                    return  # the server closed the stream
                frame = parser.feed(raw.decode("utf-8", "replace"))
                if frame is None:
                    continue
                _id, _typ, data = frame
                try:
                    ev = json.loads(data)
                except ValueError:
                    continue
                if isinstance(ev, dict):
                    self._deliver(ev, True)
        finally:
            self._response = None
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass

    def _loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            self._set_state("connecting")
            try:
                if not self._caught_up:
                    self._caught_up = self.catch_up()
                if self._caught_up and not self._stop.is_set():
                    self.follow_once()
                    backoff = 1.0
            except Exception:  # noqa: BLE001 - network resets, timeouts
                logger.debug("tray events: stream broke", exc_info=True)
            if self._stop.is_set():
                break
            self._stop.wait(backoff)
            backoff = min(self._max_backoff, backoff * 2)


__all__ = ["AttentionLedger", "AttentionNotice", "EventFollower", "SseParser", "is_automation_event"]
