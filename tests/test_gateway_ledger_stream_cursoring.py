"""Run-ledger SSE cursoring pins (H7 2026-07-12, re-based on 0075 2026-07-21).

History: the original body re-read the ENTIRE ledger every 0.25s per client
on the event loop; H7 gated news on the cheap count probe; 0075 replaced the
poll shape with per-client incremental tail readers (ledger_tail.py) + the
ObservableLedgerStore wakeup. These pins survive because the fallback tail
(ListSliceTail — plain stores like these test doubles) keeps the count-gate
discipline, and disconnect/terminal semantics are unchanged.
_ledger_news_count itself is legacy (stream no longer calls it) but stays
pinned for external callers.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

pytestmark = pytest.mark.basic


class _SpyLedger:
    """Counting proxy over an in-memory ledger list."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []
        self.list_calls = 0
        self.count_calls = 0

    def list(self, run_id: str) -> List[Dict[str, Any]]:
        self.list_calls += 1
        return list(self.records)

    def count(self, run_id: str) -> int:
        self.count_calls += 1
        return len(self.records)


class _NoCountLedger:
    def __init__(self, n: int) -> None:
        self.records = [{"i": i} for i in range(n)]
        self.list_calls = 0

    def list(self, run_id: str) -> List[Dict[str, Any]]:
        self.list_calls += 1
        return list(self.records)


def test_news_count_prefers_the_cheap_count_probe() -> None:
    from abstractgateway.routes.gateway import _ledger_news_count

    spy = _SpyLedger()
    spy.records = [{"a": 1}, {"b": 2}]
    assert _ledger_news_count(spy, "r") == 2
    assert spy.count_calls == 1
    assert spy.list_calls == 0, "count() present: the full list must not materialize"


def test_news_count_falls_back_to_list_len_without_count() -> None:
    from abstractgateway.routes.gateway import _ledger_news_count

    store = _NoCountLedger(3)
    assert _ledger_news_count(store, "r") == 3
    assert store.list_calls == 1


def test_idle_stream_never_materializes_the_list_and_stops_on_disconnect() -> None:
    """The load-bearing pin: an idle tail (cursor == count) polls the COUNT
    only — zero list() materializations — and a disconnected client ends
    the generator instead of polling forever."""
    import asyncio

    from abstractgateway.routes import gateway as gw

    class _FakeRequest:
        headers: Dict[str, str] = {}

        def __init__(self, disconnect_after: int) -> None:
            self.polls = 0
            self.disconnect_after = disconnect_after

        async def is_disconnected(self) -> bool:
            self.polls += 1
            return self.polls > self.disconnect_after

    class _RunStore:
        def load(self, run_id: str) -> Any:
            class _R:
                status = "running"
                vars: Dict[str, Any] = {}

            return _R()

    spy = _SpyLedger()
    spy.records = [{"only": "record"}]

    class _Host:
        run_store = _RunStore()
        ledger_store = spy
        data_dir = "/nonexistent/cursoring-test"  # the live-delta hub's scope (never written)

    class _Svc:
        host = _Host()

    async def _drive() -> int:
        # Call the route function directly with the service monkeypatched.
        original = gw.get_gateway_service
        gw.get_gateway_service = lambda: _Svc()  # type: ignore[assignment]
        try:
            # Consume the already-present record (cursor 0 -> 1), then idle
            # until the fake client disconnects.
            response = await gw.stream_ledger(_FakeRequest(disconnect_after=4), "r", after=0, heartbeat_s=5.0)
            events = 0
            async for part in response.body_iterator:
                # Catch-up sends are BATCHED (c2394 per-line tax fix): one
                # part may carry several events — count occurrences.
                events += part.count(b"event: step")
            return events
        finally:
            gw.get_gateway_service = original  # type: ignore[assignment]

    events = asyncio.run(asyncio.wait_for(_drive(), timeout=30))
    assert events == 1, "the single record streams once"
    # One list() materialization for the news; idle polls used count() only.
    assert spy.list_calls == 1, f"idle polls must not re-read the ledger (list_calls={spy.list_calls})"
    assert spy.count_calls >= 2, "idle polls probe the cheap count"


class _DivergentLedger:
    """count() persistently exceeds len(list()) — the real JSONL corrupt-line
    class (count() counts non-empty lines; list() drops unparseable ones)."""

    def __init__(self) -> None:
        self.records = [{"ok": 1}, {"ok": 2}]
        self.list_calls = 0
        self.count_calls = 0

    def list(self, run_id: str):
        self.list_calls += 1
        return list(self.records)

    def count(self, run_id: str) -> int:
        self.count_calls += 1
        return len(self.records) + 1  # the corrupt line counts but never lists


def test_divergent_count_never_busy_loops_and_terminal_done_still_sends() -> None:
    """Adversary F1 (P1 regression, live-repro'd at ~4.5k reads/sec): when the
    count probe permanently exceeds the parseable list length, the stream must
    fall through to the idle branch (sleep/heartbeat/terminal), emit `done` on
    a terminal run, and NEVER hot-loop full-ledger materializations."""
    import asyncio

    from abstractgateway.routes import gateway as gw

    class _NeverDisconnects:
        headers: Dict[str, str] = {}

        async def is_disconnected(self) -> bool:
            return False

    class _RunStore:
        def load(self, run_id: str):
            class _R:
                status = "completed"  # terminal from the start
                vars: dict = {}

            return _R()

    spy = _DivergentLedger()

    class _Host:
        run_store = _RunStore()
        ledger_store = spy
        data_dir = "/nonexistent/cursoring-test"  # the live-delta hub's scope (never written)

    class _Svc:
        host = _Host()

    async def _drive():
        original = gw.get_gateway_service
        gw.get_gateway_service = lambda: _Svc()  # type: ignore[assignment]
        try:
            response = await gw.stream_ledger(_NeverDisconnects(), "r", after=0, heartbeat_s=5.0)
            steps = 0
            done = False
            async for part in response.body_iterator:  # terminates on `done`
                # Batched catch-up: one part may carry several step events.
                steps += part.count(b"event: step")
                if b"event: done" in part:
                    done = True
            return steps, done
        finally:
            gw.get_gateway_service = original  # type: ignore[assignment]

    steps, done = asyncio.run(asyncio.wait_for(_drive(), timeout=30))
    assert steps == 2, "both parseable records stream"
    assert done is True, "terminal run must still close with done despite the phantom count"
    # The news signal fires every pass (count 3 > cursor 2) but each pass
    # sleeps in the idle branch — a 3s busy loop would show thousands of
    # list() calls (the repro measured 4.5k/sec); a healthy stream shows a
    # handful before `done` breaks the loop.
    assert spy.list_calls <= 5, f"divergent count must not busy-loop the ledger (list_calls={spy.list_calls})"
