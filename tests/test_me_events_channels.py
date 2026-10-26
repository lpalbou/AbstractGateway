"""Publish to a channel: a flow's PUBLISH_EVENT lands on the person's stream under an
application-chosen channel; `?channel=` filters the catch-up and the SSE tail; `/me/events/channels`
lists them; `alert: true` raises an attention item the tray announces; `POST /me/events/seen` marks
it seen. Two channels, two principals through the real app."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

from test_gateway_principal_isolation_matrix import _client, _create_user
from test_me_events_routes import _plane

from abstractgateway.events_stream import record_published, stream_for
from abstractgateway.publish_host import GatewayPublishEvent, wire_publish_event
from abstractgateway.routes.me_events import sse_frames
from abstractgateway.tray.events import AttentionLedger


def test_published_event_shape_idempotency_and_refusals(tmp_path: Path) -> None:
    stream = stream_for(tmp_path, "default", "alice")
    out = record_published(stream, run_id="r1", step_id="s1", payload={
        "channel": "orders", "type": "orders.paid", "title": "Order 42 paid", "body": "Thank you", "subject": "42",
        "data": {"amount": 12}})
    assert out == {"event_id": "publish:r1:s1", "sequence": 1, "alerted": False}
    ev = stream.read(after=0, limit=10)[0]
    assert ev["type"] == "orders.paid" and ev["channel"] == "orders" and ev["subject"] == "42"
    assert ev["data"] == {"amount": 12, "title": "Order 42 paid", "body": "Thank you"} and ev["id"] == "publish:r1:s1"
    assert record_published(stream, run_id="r1", step_id="s1", payload={"channel": "orders", "title": "again"})["sequence"] == 1
    assert stream.head() == 1  # a replayed step appends nothing
    for bad in ({"channel": "Orders", "title": "x"}, {"channel": "attention", "title": "x"}, {"channel": "o", "title": ""},
                {"channel": "o", "title": "x", "type": "run.started"}, {"channel": "o", "title": "x", "alert": "yes"},
                {"channel": "o", "title": "x", "nope": 1}, {"channel": "o", "title": "x", "data": [1]}):
        with pytest.raises(ValueError):
            record_published(stream, run_id="r1", step_id="bad", payload=bad)
    # Default type: <channel>.message
    record_published(stream, run_id="r1", step_id="s2", payload={"channel": "news", "title": "Hello"})
    assert stream.read(after=1, limit=10)[0]["type"] == "news.message"


def test_an_alert_raises_attention_the_tray_counts_until_seen(tmp_path: Path) -> None:
    stream = stream_for(tmp_path, "default", "alice")
    out = record_published(stream, run_id="r9", step_id="s1", payload={"channel": "alerts", "title": "Disk full", "alert": True})
    assert out["alerted"] is True
    events = stream.read(after=0, limit=10)
    assert [e["type"] for e in events] == ["alerts.message", "attention.raised"]
    raised = events[1]["data"]
    assert raised == {"kind": "notify", "record_key": "publish:r9:s1", "channel": "alerts", "run_id": "r9", "title": "Disk full", "published_sequence": 1, "recipient": "me"}
    ledger = AttentionLedger()
    notice = None
    for e in events:
        notice = ledger.apply(e) or notice
    assert notice is not None and notice.channel == "alerts" and notice.title == "Disk full" and ledger.unseen == 1
    from abstractgateway.events_stream import record_publish_seen

    assert record_publish_seen(stream, record_key="publish:r9:s1") is True
    ledger.apply(stream.read(after=2, limit=10)[0])
    assert ledger.unseen == 0
    with pytest.raises(ValueError):
        record_publish_seen(stream, record_key="automation:completed:a:1")


def test_the_host_handler_writes_the_owner_stream(tmp_path: Path) -> None:
    handler = GatewayPublishEvent(data_dir=tmp_path, tenant="default", user="alice")
    out = handler(SimpleNamespace(run_id="run-1", current_node="n"), {"channel": "orders", "title": "x"}, step_id="st-1")
    assert out["event_id"] == "publish:run-1:st-1"
    assert stream_for(tmp_path, "default", "alice").read(after=0, limit=1)[0]["channel"] == "orders"

    class _Runtime:
        handler = None

        def set_publish_event_handler(self, h):
            self.handler = h

    rt = _Runtime()
    assert isinstance(wire_publish_event(rt, data_dir=tmp_path, tenant="default", user="alice"), GatewayPublishEvent) and rt.handler is not None
    with pytest.raises(RuntimeError, match="set_publish_event_handler"):
        wire_publish_event(object(), data_dir=tmp_path, tenant="default", user="alice")


def test_channel_filter_listing_and_isolation_through_the_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        alice, bob = _create_user(client, "alice"), _create_user(client, "bob")
        for h in (alice, bob):
            assert client.get("/api/gateway/me/events", headers=h).status_code == 200
        a = stream_for(_plane(tmp_path, "alice"), "default", "alice")
        b = stream_for(_plane(tmp_path, "bob"), "default", "bob")
        a.append("run.started", event_id="run.started:x", data={"run_id": "x"})
        for i in range(3):
            record_published(a, run_id="ra", step_id=f"o{i}", payload={"channel": "orders", "title": f"order {i}"})
        record_published(a, run_id="ra", step_id="n1", payload={"channel": "news", "title": "news", "alert": True})
        record_published(b, run_id="rb", step_id="o1", payload={"channel": "orders", "title": "bob's order"})

        def page(h, **params):
            r = client.get("/api/gateway/me/events", headers=h, params=params)
            assert r.status_code == 200, r.text
            return r.json()

        orders = page(alice, channel="orders")
        assert [e["data"]["title"] for e in orders["events"]] == ["order 0", "order 1", "order 2"] and orders["channels"] == ["orders"]
        assert "bob's order" not in json.dumps(orders)
        two = page(alice, channel="news,attention")
        assert [e["type"] for e in two["events"]] == ["news.message", "attention.raised"]
        first = page(alice, channel="orders", limit=1)
        assert len(first["events"]) == 1 and first["more"] is True
        rest = page(alice, channel="orders", after=first["next_after"])
        assert [e["data"]["title"] for e in rest["events"]] == ["order 1", "order 2"]
        assert len(page(alice)["events"]) == 6  # no filter: everything, as before
        assert client.get("/api/gateway/me/events", headers=alice, params={"channel": "Bad Name"}).status_code == 422

        listed = client.get("/api/gateway/me/events/channels", headers=alice).json()["channels"]
        assert {r["channel"]: r["count"] for r in listed} == {"orders": 3, "news": 1, "attention": 1}
        assert listed[0]["channel"] in ("attention", "news") and all(r["last_time"] for r in listed)
        assert {r["channel"]: r["count"] for r in client.get("/api/gateway/me/events/channels", headers=bob).json()["channels"]} == {"orders": 1}

        r = client.post("/api/gateway/me/events/seen", headers=alice, json={"record_key": "publish:ra:n1"})
        assert r.status_code == 200 and r.json()["created"] is True
        assert page(alice, channel="attention")["events"][-1]["type"] == "attention.seen"
        assert client.post("/api/gateway/me/events/seen", headers=alice, json={"record_key": "x"}).status_code == 422


def test_the_sse_tail_filters_by_channel(tmp_path: Path) -> None:
    stream = stream_for(tmp_path, "default", "alice")

    async def _run() -> List[bytes]:
        frames: List[bytes] = []
        stop = {"v": False}

        async def disconnected() -> bool:
            return stop["v"]

        gen = sse_frames(stream, after=0, heartbeat_s=5, disconnected=disconnected, poll_s=0.05, channels=frozenset({"orders"}))
        loop = asyncio.get_running_loop()
        loop.call_later(0.1, lambda: record_published(stream, run_id="r", step_id="n", payload={"channel": "news", "title": "N"}))
        loop.call_later(0.2, lambda: record_published(stream, run_id="r", step_id="o", payload={"channel": "orders", "title": "O"}))
        deadline = loop.time() + 5
        async for chunk in gen:
            frames.append(chunk)
            if b"orders.message" in chunk or loop.time() > deadline:
                stop["v"] = True
        return frames

    joined = b"".join(asyncio.run(_run()))
    assert b"event: orders.message" in joined and b"news.message" not in joined
    assert b"id: 2\n" in joined  # the id stays the stream sequence (Last-Event-ID resumes past the news event)


def test_a_flow_step_publishes_through_the_runtime_effect(tmp_path: Path) -> None:
    """The runtime's PUBLISH_EVENT effect reaches the gateway's handler (wired like the bundle host
    does) and lands on the owner's stream; a replayed step publishes once."""
    from abstractruntime import Effect, EffectType, Runtime, StepPlan, WorkflowSpec
    from abstractruntime.core.models import RunStatus
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    def go(run, ctx):
        return StepPlan(node_id="go", effect=Effect(type=EffectType.PUBLISH_EVENT, payload={
            "channel": "orders", "title": "Order 42 paid", "data": {"amount": 12}, "alert": True}, result_key="published"), next_node="done")

    def done(run, ctx):
        return StepPlan(node_id="done", complete_output={"published": run.vars.get("published")})

    wf = WorkflowSpec(workflow_id="pub", entry_node="go", nodes={"go": go, "done": done})
    rt = Runtime(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore())
    wire_publish_event(rt, data_dir=tmp_path, tenant="default", user="alice")
    rid = rt.start(workflow=wf)
    state = rt.tick(workflow=wf, run_id=rid)
    assert state.status == RunStatus.COMPLETED, state.error
    stream = stream_for(tmp_path, "default", "alice")
    events = stream.read(after=0, limit=10)
    assert [e["type"] for e in events] == ["orders.message", "attention.raised"]
    assert events[0]["id"].startswith(f"publish:{rid}:") and events[0]["data"]["amount"] == 12
    assert state.output["published"]["event_id"] == events[0]["id"]
