"""The tray consumes the person's event stream (backlog 1046): an OS notification per live
`attention.raised`, a badge = raised minus seen, event-driven automation refresh instead of the
20 s `/automations` poll, and `notification.sent {channel: tray}` recorded back.

The follower runs against a REAL gateway (hermetic data dir, tmp HOME, scratch port)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from test_gateway_principal_isolation_matrix import _client, _create_user
from test_me_events_routes import _free_port, _plane, _wait

from abstractgateway.events_stream import stream_for
from abstractgateway.tray.events import AttentionLedger, EventFollower, SseParser, is_automation_event


def _raised(aid: str, seq: int) -> Dict[str, Any]:
    return {"type": "attention.raised", "sequence": seq, "data": {"automation_id": aid, "seq": seq, "kind": "notify", "record_key": f"automation:completed:{aid}:{seq}"}}


def _seen(aid: str, seq: int, sequence: int = 0) -> Dict[str, Any]:
    return {"type": "attention.seen", "sequence": sequence, "data": {"automation_id": aid, "seq": seq}}


def test_badge_is_raised_minus_seen() -> None:
    ledger = AttentionLedger()
    assert ledger.apply(_raised("a", 1)) is not None
    assert ledger.apply(_raised("a", 2)) is not None
    assert ledger.apply(_raised("b", 1)) is not None
    assert ledger.unseen == 3
    assert ledger.apply(_seen("a", 1)) is None
    assert ledger.unseen == 2
    assert ledger.apply(_raised("a", 1)) is None  # already seen: no notice
    ledger.apply(_seen("a", 5))
    ledger.apply(_seen("b", 1))
    assert ledger.unseen == 0


def test_sse_parser_frames_and_comments() -> None:
    p = SseParser()
    lines = [": stream user:default:a", "", "id: 3", "event: run.started", 'data: {"x":1}', "", ": keep-alive", ""]
    frames = [f for f in (p.feed(l + "\n") for l in lines) if f]
    assert frames == [("3", "run.started", '{"x":1}')]


def test_automation_events_are_recognised() -> None:
    assert is_automation_event(_raised("a", 1))
    assert is_automation_event({"type": "run.finished", "data": {"origin": {"kind": "trigger"}}})
    assert not is_automation_event({"type": "run.finished", "data": {"origin": {"kind": "person"}}})


def test_sampler_refetches_automations_on_events_not_on_a_timer() -> None:
    from abstractgateway.tray.sampler import AUTOMATIONS_RECONCILE_S, Sampler

    s = Sampler(client=None, runs_interval_s=20.0)  # type: ignore[arg-type]
    now = 1000.0
    assert s.automations_due(now)  # first pass fetches
    s._automations_dirty, s._last_automations = False, now
    assert s.automations_due(now + 21)  # stream not live: the 20 s poll, as before
    s.set_events_live(True)
    s._automations_dirty = False
    assert not s.automations_due(now + 21)  # live: no timer poll...
    assert not s.automations_due(now + AUTOMATIONS_RECONCILE_S - 1)
    s.mark_automations_dirty()
    assert s.automations_due(now + 21)  # ...an automation event refetches
    s._automations_dirty = False
    assert s.automations_due(now + AUTOMATIONS_RECONCILE_S + 1)  # slow reconciliation


class _FakeIcon:
    HAS_NOTIFICATION = True

    def __init__(self) -> None:
        self.shown: List[tuple] = []

    def notify(self, message: str, title: str) -> None:
        self.shown.append((title, message))


def test_tray_follows_the_stream_notifies_live_attention_and_records_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from abstractgateway.tray.app import TrayApp, tooltip_text

    client = _client(tmp_path, monkeypatch)
    with client:
        alice = _create_user(client, "alice")
        assert client.get("/api/gateway/me/events", headers=alice).status_code == 200
        token = alice["Authorization"].split(" ", 1)[1]
        stream = stream_for(_plane(tmp_path, "alice"), "default", "alice")
        # History: one item already seen, one not.
        stream.append("attention.raised", event_id="r1", data=_raised("auto1", 1)["data"])
        stream.append("attention.seen", event_id="s1", data={"automation_id": "auto1", "seq": 1})
        stream.append("attention.raised", event_id="r2", data=_raised("auto1", 2)["data"])

        from abstractgateway.app import app as asgi

        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(asgi, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        tray = None
        try:
            _wait(lambda: server.started, 10)
            tray = TrayApp({"base_url": f"http://127.0.0.1:{port}", "token": token, "parent_pid": 0})
            tray._icon = _FakeIcon()
            dirty: List[bool] = []
            tray.sampler.mark_automations_dirty = lambda: dirty.append(True)  # type: ignore[method-assign]
            tray._events.start()
            _wait(lambda: tray._events.state == "live" and tray._events.cursor == 3)
            assert tray._attention.unseen == 1  # badge from history
            assert tray._icon.shown == []  # history is never re-shown
            assert "1 thing waiting for you" in tooltip_text(_snap_stub(), attention_unseen=tray._attention.unseen)

            stream.append("attention.raised", event_id="r3", data=_raised("auto2", 1)["data"])  # live
            _wait(lambda: len(tray._icon.shown) == 1)
            assert tray._icon.shown[0] == ("An automation", "It has a result for you. Open the console to read it.")
            assert tray._attention.unseen == 2 and dirty

            def _reported() -> bool:
                return any(e["type"] == "notification.sent" and e["data"].get("channel") == "tray" for e in stream.read(after=0, limit=100))

            _wait(_reported)
            sent = [e for e in stream.read(after=0, limit=100) if e["type"] == "notification.sent"]
            assert sent[0]["data"]["record_key"] == "automation:completed:auto2:1"

            stream.append("attention.seen", event_id="s2", data={"automation_id": "auto2", "seq": 1})
            _wait(lambda: tray._attention.unseen == 1)
        finally:
            if tray is not None:
                tray._events.stop()
            server.should_exit = True
            thread.join(10)


def _snap_stub():
    from abstractgateway.tray.sampler import Sampler

    return Sampler(client=None).snapshot()  # type: ignore[arg-type]


def test_badge_counts_waits_on_a_person_until_each_is_resolved() -> None:
    ledger = AttentionLedger()
    w1 = {"kind": "question", "run_id": "r1", "wait_key": "user:r1:ask", "step_id": "s1", "record_key": "k1", "session_id": "S"}
    w2 = {"kind": "approval", "run_id": "r2", "wait_key": "approve:r2", "step_id": "s9", "record_key": "k2"}
    n1 = ledger.apply({"type": "attention.raised", "sequence": 1, "data": w1})
    assert n1 is not None and n1.kind == "question" and n1.run_id == "r1" and n1.record_key == "k1"
    ledger.apply({"type": "attention.raised", "sequence": 2, "data": w2})
    ledger.apply(_raised("auto", 1))
    assert ledger.unseen == 3
    ledger.apply({"type": "attention.seen", "sequence": 4, "data": {**w1, "outcome": "answered"}})
    assert ledger.unseen == 2
    assert ledger.apply({"type": "attention.raised", "sequence": 5, "data": w1}) is None  # a replay never re-raises
    ledger.apply({"type": "attention.seen", "sequence": 6, "data": {**w2, "outcome": "denied"}})
    ledger.apply(_seen("auto", 1))
    assert ledger.unseen == 0
