"""The push dispatcher (framework backlog 1049), driven pass by pass with a clock and a fake relay
transport: the exact relay request, the kind mapping, the history baseline, a restart with queued
rows, `attention.seen` before the send, the `gone` timestamp rule, a 2-hour relay outage, the
outbox cap, phone-first, and the records it writes."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

import httpx
import pytest

from push_fakes import PHONE_TOPIC, REG, TOKEN_A, TOKEN_B, WATCH_TOPIC, raise_approval, resolve_wait

from abstractgateway.events_stream import record_attention_raised, record_attention_seen, record_published, stream_for
from abstractgateway.push.dispatcher import PushDispatcher, alert_targets, collapse_id, push_kind
from abstractgateway.push.store import DeviceStore, Outbox, read_cursor

T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Relay:
    """A fake relay behind httpx.MockTransport (no socket)."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.reply: Tuple[int, Dict[str, Any]] = (200, {"status": "sent"})
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        assert request.url.path == "/v1/push"
        self.calls.append(json.loads(request.content))
        code, payload = self.reply
        return httpx.Response(code, json=payload)


def _world(tmp_path: Path, clock: Clock, relay: Relay):
    plane = tmp_path / "plane"
    stream = stream_for(plane, "default", "alice")
    store = DeviceStore(plane, tmp_path)
    d = PushDispatcher(plane_dir=plane, root_dir=tmp_path, stream=stream, relay_url="http://relay.test",
                       clock=clock, transport=httpx.MockTransport(relay.handler))
    return plane, stream, store, d


def _register(store: DeviceStore, clock: Clock, device_id: str = "aw1", *, app: str = "watch", token: str = TOKEN_A) -> None:
    store.put(principal="default:alice", device_id=device_id, token=token, topic=WATCH_TOPIC if app == "watch" else PHONE_TOPIC,
              env="sandbox", app=app, reg=REG, name=None, now=clock())


def _types(stream) -> List[Tuple[str, Dict[str, Any]]]:
    return [(e["type"], e["data"]) for e in stream.read(after=0, limit=10_000)]


def test_one_approval_is_exactly_one_textless_relay_call(tmp_path: Path) -> None:
    clock, relay = Clock(), Relay()
    _plane, stream, store, d = _world(tmp_path, clock, relay)
    _register(store, clock)
    assert d.tick()["enqueued"] == 0  # first pass: baseline at the (empty) head
    item = raise_approval(stream, 1)
    out = d.tick()
    assert out["sent"] == 1 and len(relay.calls) == 1
    call = relay.calls[0]
    assert set(call) == {"topic", "env", "token", "kind", "collapse", "seq", "reg"}
    assert call == {"topic": WATCH_TOPIC, "env": "sandbox", "token": TOKEN_A, "kind": "approval",
                    "collapse": collapse_id(item["record_key"]), "seq": 1, "reg": REG}
    assert len(call["collapse"]) == 32
    # Nothing more on later passes; the delivery is recorded for the watch channel.
    d.tick()
    d.tick()
    assert len(relay.calls) == 1
    sent = [data for typ, data in _types(stream) if typ == "notification.sent"]
    assert sent == [{"channel": "watch", "notice": f"attention:{item['record_key']}", "record_key": item["record_key"],
                     "device": "aw1", "via": "push"}]
    assert store.get("aw1")["last_sent_at"] == clock()


def test_kind_mapping() -> None:
    def ev(**data):
        return {"type": "attention.raised", "data": data}

    assert push_kind(ev(kind="approval", record_key="k")) == "approval"
    assert push_kind(ev(kind="question", record_key="k")) == "question"
    assert push_kind(ev(kind="notify", record_key="publish:r:s")) == "notify"
    assert push_kind(ev(kind="notify", record_key="automation:completed:a:1", automation_id="a")) == "result"
    assert push_kind(ev(kind="failure", record_key="k", automation_id="a")) == "failure"
    assert push_kind(ev(kind="result", record_key="k")) is None
    assert push_kind(ev(kind="approval")) is None
    assert push_kind({"type": "run.finished", "data": {"kind": "approval", "record_key": "k"}}) is None
    assert push_kind({"type": "attention.seen", "data": {"kind": "approval", "record_key": "k"}}) is None


def test_published_alert_and_automation_items_map_through(tmp_path: Path) -> None:
    clock, relay = Clock(), Relay()
    _plane, stream, store, d = _world(tmp_path, clock, relay)
    _register(store, clock)
    d.tick()
    record_published(stream, run_id="r9", step_id="s1", payload={"channel": "news", "title": "Hi", "alert": True})
    record_attention_raised(stream, {"record_key": "automation:completed:a1:3", "automation_id": "a1", "kind": "notify", "seq": 3})
    record_attention_raised(stream, {"record_key": "automation:completed:a1:4", "automation_id": "a1", "kind": "failure", "seq": 4})
    d.tick()
    assert [c["kind"] for c in relay.calls] == ["notify", "result", "failure"]
    assert all("title" not in c and "Hi" not in json.dumps(c) for c in relay.calls)


def test_first_start_over_500_old_events_sends_nothing(tmp_path: Path) -> None:
    clock, relay = Clock(), Relay()
    plane, stream, store, d = _world(tmp_path, clock, relay)
    for i in range(500):
        raise_approval(stream, i)
    assert stream.head() == 500
    _register(store, clock)
    out = d.tick()
    assert out["enqueued"] == 0 and relay.calls == [] and read_cursor(plane) == 500
    raise_approval(stream, 1000)
    d.tick()
    assert [c["seq"] for c in relay.calls] == [501]


def test_restart_with_queued_rows_sends_each_once(tmp_path: Path) -> None:
    clock, relay = Clock(), Relay()
    plane, stream, store, d = _world(tmp_path, clock, relay)
    _register(store, clock, "aw1")
    _register(store, clock, "aw2", token=TOKEN_B)
    d.tick()
    for i in range(3):
        raise_approval(stream, i)
    relay.reply = (503, {"status": "retry", "after_s": 1})
    d.tick()
    assert len(relay.calls) == 1  # the relay is down for this pass: one call, the rest wait
    d.stop()
    relay.calls.clear()
    relay.reply = (200, {"status": "sent"})
    # A new process: the same files, the queued rows go out when due, once each.
    d2 = PushDispatcher(plane_dir=plane, root_dir=tmp_path, stream=stream, relay_url="http://relay.test", clock=clock,
                        transport=httpx.MockTransport(relay.handler))
    clock.t += 3
    d2.tick()
    clock.t += 3600
    d2.tick()
    d2.tick()
    sent = sorted((c["seq"], c["token"]) for c in relay.calls)
    assert sent == sorted((s, t) for s in (1, 2, 3) for t in (TOKEN_A, TOKEN_B))
    assert {r["state"] for r in Outbox(plane).rows()} == {"sent"}


def test_seen_before_the_send_means_zero_calls(tmp_path: Path) -> None:
    clock, relay = Clock(), Relay()
    _plane, stream, store, d = _world(tmp_path, clock, relay)
    _register(store, clock)
    d.tick()
    item = raise_approval(stream, 1)
    resolve_wait(stream, item)
    # An automation item seen before the pass, too.
    record_attention_raised(stream, {"record_key": "automation:completed:a1:2", "automation_id": "a1", "kind": "notify", "seq": 2})
    record_attention_seen(stream, automation_id="a1", seq=2)
    d.tick()
    assert relay.calls == []
    # A queued (retrying) row dropped by a later seen is never sent either.
    item2 = raise_approval(stream, 2)
    relay.down = True
    d.tick()
    relay.down = False
    resolve_wait(stream, item2)
    clock.t += 30
    d.tick()
    assert relay.calls == []
    assert {r["error"] for r in Outbox(_plane).rows()} == {"seen"}


def test_gone_deletes_the_device_only_when_apple_is_newer(tmp_path: Path) -> None:
    clock, relay = Clock(), Relay()
    _plane, stream, store, d = _world(tmp_path, clock, relay)
    _register(store, clock)
    d.tick()
    relay.reply = (200, {"status": "gone", "since_ms": int((clock() - 60) * 1000)})  # before the last refresh
    raise_approval(stream, 1)
    d.tick()
    assert store.get("aw1") is not None
    relay.reply = (200, {"status": "gone", "since_ms": int((clock() + 60) * 1000)})
    raise_approval(stream, 2)
    d.tick()
    assert store.get("aw1") is None
    relay.reply = (200, {"status": "rejected", "reason": "BadTopic"})
    _register(store, clock)
    raise_approval(stream, 3)
    d.tick()
    assert store.get("aw1")["last_error"] == "The push relay refused an alert: BadTopic"
    failed = [data for typ, data in _types(stream) if typ == "notification.failed"]
    assert failed and failed[-1]["channel"] == "watch"


def test_relay_down_two_hours(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    clock, relay = Clock(), Relay()
    plane, stream, store, d = _world(tmp_path, clock, relay)
    _register(store, clock)
    d.tick()
    relay.down = True
    for i in range(20):
        raise_approval(stream, i)
    with caplog.at_level(logging.WARNING, logger="abstractgateway.push"):
        dropped_at = None
        for _ in range(2 * 3600 // 20):
            d.tick()
            rows = Outbox(plane).rows()
            if dropped_at is None and rows and all(r["state"] == "dropped" for r in rows):
                dropped_at = clock() - T0
            clock.t += 20
    assert dropped_at is not None and 3600 <= dropped_at <= 3600 + 40
    lines = [r for r in caplog.records if r.name == "abstractgateway.push"]
    assert 1 <= len(lines) <= 2, [r.getMessage() for r in lines]
    assert relay.calls == []
    # Approvals that never reached the wrist are said in the stream; the outbox stays bounded.
    failed = [data for typ, data in _types(stream) if typ == "notification.failed"]
    assert len(failed) == 20
    assert Outbox(plane).count() == 20
    assert store.get("aw1")["relay_unreachable_since"] is not None


def test_outbox_is_capped_and_purged(tmp_path: Path) -> None:
    ob = Outbox(tmp_path)
    rows = [{"event_id": f"e{i}", "device_id": "d", "seq": i, "kind": "approval", "collapse": "c", "record_key": "k",
             "created_ts": float(i), "next_ts": 0.0, "seen_key": "", "seen_seq": 0} for i in range(60)]
    ob.enqueue(rows)
    for i in range(10):
        ob.mark(f"e{i}", "d", state="sent")
    assert ob.purge(100.0, cap=50, keep_s=1e9) == 10
    assert ob.count() == 50 and all(r["state"] == "queued" for r in ob.rows())
    for r in ob.rows():
        ob.mark(r["event_id"], "d", state="dropped")
    assert ob.purge(7 * 86400 + 100.0) == 50 and ob.count() == 0
    assert oct((tmp_path / "push" / "outbox.sqlite3").stat().st_mode & 0o777) == "0o600"


def test_phone_first() -> None:
    """One app family: the phone gets the alert and Apple mirrors it to the watch; a watch signed in
    on its own (no fresh phone) gets it directly; a stale device is the last resort."""
    now = T0
    watch = {"device_id": "w", "app": "watch", "refreshed_at": now - 3600}
    stale_watch = {"device_id": "sw", "app": "watch", "refreshed_at": now - 2 * 86400}
    phone = {"device_id": "p", "app": "phone", "refreshed_at": now}
    stale_phone = {"device_id": "sp", "app": "phone", "refreshed_at": now - 2 * 86400}
    assert [d["device_id"] for d in alert_targets([watch, phone], now)] == ["p"]
    assert [d["device_id"] for d in alert_targets([watch, stale_phone], now)] == ["w"]
    assert [d["device_id"] for d in alert_targets([stale_watch, stale_phone], now)] == ["sp"]
    assert [d["device_id"] for d in alert_targets([stale_watch], now)] == ["sw"]
    assert [d["device_id"] for d in alert_targets([watch], now)] == ["w"]


def test_watch_and_phone_registered_send_one_alert(tmp_path: Path) -> None:
    clock, relay = Clock(), Relay()
    _plane, stream, store, d = _world(tmp_path, clock, relay)
    _register(store, clock, "aw1")
    _register(store, clock, "ap1", app="phone", token=TOKEN_B)
    d.tick()
    raise_approval(stream, 1)
    d.tick()
    assert [c["token"] for c in relay.calls] == [TOKEN_B]  # phone-first: Apple mirrors it to the watch
