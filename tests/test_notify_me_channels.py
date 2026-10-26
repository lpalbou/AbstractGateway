"""Notify me's channel choice, end to end (framework ADR-0041 follow-up): a published alert carries
`recipient` ("me") and `channels` (tray, watch, phone, email, console; absent = every channel) on its
`attention.raised`, and every deliverer honours it: the tray consumer (no notification, no badge
without "tray"), the push dispatcher (only the listed apps, phone-first among them), the email outbox
(a Notify me email only with "email"). The console item is the record itself, always written; After
the run says what was asked beside what was delivered. Hermetic: scratch dirs, a MockTransport relay,
a fake usable mailbox (no network, no SMTP)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx
import pytest

from push_fakes import PHONE_TOPIC, REG, TOKEN_A, TOKEN_B, WATCH_TOPIC

from abstractgateway.events_stream import alert_asks, record_published, stream_for
from abstractgateway.publish_host import GatewayPublishEvent
from abstractgateway.push.dispatcher import PushDispatcher, alert_targets
from abstractgateway.push.store import DeviceStore
from abstractgateway.run_after import build_after
from abstractgateway.tray.events import AttentionLedger

T0 = 1_800_000_000.0


def _raised(stream) -> Dict[str, Any]:
    (event,) = [e for e in stream.read(after=0, limit=100) if e["type"] == "attention.raised"]
    return event["data"]


# --- the record --------------------------------------------------------------------------------


def test_the_alert_record_carries_recipient_and_the_asked_channels(tmp_path: Path) -> None:
    stream = stream_for(tmp_path, "default", "alice")
    record_published(stream, run_id="r1", step_id="s1", payload={"channel": "me", "title": "t", "alert": True, "channels": ["phone", "Watch"]})
    data = _raised(stream)
    assert data["recipient"] == "me" and data["channels"] == ["watch", "phone"]
    assert not alert_asks(data, "tray") and alert_asks(data, "watch") and not alert_asks(data, "email")
    # Absent = every channel (older flows, "All my channels"): no `channels` on the record.
    other = stream_for(tmp_path / "b", "default", "alice")
    record_published(other, run_id="r1", step_id="s1", payload={"channel": "me", "title": "t", "alert": True})
    assert "channels" not in _raised(other) and all(alert_asks(_raised(other), c) for c in ("tray", "watch", "phone", "email", "console"))
    # An automation item's `channels` is its own console/email setting, never a filter here.
    assert alert_asks({"record_key": "automation:completed:a:1", "channels": ["console"]}, "tray")
    for bad in ({"channels": []}, {"channels": ["sms"]}, {"channels": "tray"}, {"channels": [3]}, {"recipient": "bob"}):
        with pytest.raises(ValueError):
            record_published(stream, run_id="r1", step_id="bad", payload={"channel": "me", "title": "t", "alert": True, **bad})
    with pytest.raises(ValueError, match="alert"):
        record_published(stream, run_id="r1", step_id="bad2", payload={"channel": "me", "title": "t", "channels": ["tray"]})


# --- the tray ----------------------------------------------------------------------------------


def test_the_tray_skips_an_alert_that_did_not_ask_for_it(tmp_path: Path) -> None:
    stream = stream_for(tmp_path, "default", "alice")
    record_published(stream, run_id="r1", step_id="watch-only", payload={"channel": "me", "title": "w", "alert": True, "channels": ["watch"]})
    record_published(stream, run_id="r1", step_id="tray-only", payload={"channel": "me", "title": "t", "alert": True, "channels": ["tray"]})
    record_published(stream, run_id="r1", step_id="all", payload={"channel": "me", "title": "a", "alert": True})
    ledger = AttentionLedger()
    notices = [n for n in (ledger.apply(e) for e in stream.read(after=0, limit=100)) if n is not None]
    assert [n.record_key for n in notices] == ["publish:r1:tray-only", "publish:r1:all"]
    assert ledger.unseen == 2


# --- push --------------------------------------------------------------------------------------


class Relay:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "sent"})


def _push_world(tmp_path: Path, *, watch: bool = True, phone: bool = True):
    relay = Relay()
    plane = tmp_path / "plane"
    stream = stream_for(plane, "default", "alice")
    store = DeviceStore(plane, tmp_path)
    clock = lambda: T0  # noqa: E731
    if watch:
        store.put(principal="default:alice", device_id="aw1", token=TOKEN_A, topic=WATCH_TOPIC, env="sandbox", app="watch", reg=REG, name="Watch", now=T0)
    if phone:
        store.put(principal="default:alice", device_id="ap1", token=TOKEN_B, topic=PHONE_TOPIC, env="sandbox", app="phone", reg=REG, name="Phone", now=T0)
    d = PushDispatcher(plane_dir=plane, root_dir=tmp_path, stream=stream, relay_url="http://relay.test", clock=clock,
                       transport=httpx.MockTransport(relay.handler))
    d.tick()  # the baseline
    return stream, d, relay


@pytest.mark.parametrize("asked, tokens", [
    (["tray"], []),                      # asked tray only: push sends nothing
    (["email"], []),
    (["phone"], [TOKEN_B]),              # the phone even with a fresh watch
    (["watch"], [TOKEN_A]),
    (["watch", "phone"], [TOKEN_B]),     # phone-first among the listed (Apple mirrors to the watch)
    (None, [TOKEN_B]),                   # every channel: phone-first
])
def test_push_reaches_only_the_listed_apps(tmp_path: Path, asked: Optional[List[str]], tokens: List[str]) -> None:
    stream, d, relay = _push_world(tmp_path)
    payload: Dict[str, Any] = {"channel": "me", "title": "t", "alert": True, **({"channels": asked} if asked else {})}
    record_published(stream, run_id="r1", step_id="s1", payload=payload)
    d.tick()
    assert [c["token"] for c in relay.calls] == tokens
    sent = [e["data"]["channel"] for e in stream.read(after=0, limit=100) if e["type"] == "notification.sent"]
    assert sent == [{TOKEN_A: "watch", TOKEN_B: "phone"}[t] for t in tokens]


def test_watch_asked_without_a_watch_never_falls_back_to_the_phone() -> None:
    phone = {"device_id": "p", "app": "phone", "refreshed_at": T0}
    assert alert_targets([phone], T0, ["watch"]) == []
    assert [d["device_id"] for d in alert_targets([phone], T0, ["watch", "phone"])] == ["p"]


# --- email -------------------------------------------------------------------------------------


@pytest.fixture()
def mailbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A usable mailbox without an account: the outbox is real, nothing is sent."""
    from abstractgateway.mail import notifications
    from abstractgateway.mail.accounts import EmailPlane

    monkeypatch.setattr(notifications, "email_usable", lambda _plane: True)
    monkeypatch.setattr(notifications, "_console_link", lambda origin="": "")
    plane = EmailPlane("default", "alice", "default", tmp_path / "mailplane", True)
    return plane, notifications.NotificationOutbox(plane)


def _publish(tmp_path: Path, plane: Any, payload: Dict[str, Any], step: str = "s1") -> Dict[str, Any]:
    handler = GatewayPublishEvent(data_dir=tmp_path / "data", tenant="default", user="alice", email_plane=plane)
    run = SimpleNamespace(run_id="run-1", current_node="n", workflow_id="Judge what arrived", vars={})
    return handler(run, payload, step_id=step)


def test_email_is_sent_only_when_asked(tmp_path: Path, mailbox) -> None:
    from abstractgateway.mail.notifications import notify_me_key

    plane, outbox = mailbox
    _publish(tmp_path, plane, {"channel": "me", "title": "Watch this", "alert": True, "channels": ["watch"]}, "watch-only")
    assert outbox.rows() == []  # asked watch: email sends nothing
    _publish(tmp_path, plane, {"channel": "me", "title": "Mail this", "body": "Two urgent emails", "alert": True, "channels": ["email"]}, "email")
    _publish(tmp_path, plane, {"channel": "me", "title": "Mail this", "alert": True, "channels": ["email"]}, "email")  # a replayed step
    (row,) = outbox.rows()
    assert row["idempotency_key"] == notify_me_key("publish:run-1:email") and row["kind"] == "notify_me" and row["state"] == "queued"
    assert row["subject"] == "[AbstractFramework] Judge what arrived: notification"
    # Every channel (absent) includes email; an event without an alert mails nothing.
    _publish(tmp_path, plane, {"channel": "me", "title": "All", "alert": True}, "all")
    _publish(tmp_path, plane, {"channel": "news", "title": "No alert"}, "quiet")
    assert sorted(r["idempotency_key"] for r in outbox.rows()) == sorted([notify_me_key("publish:run-1:email"), notify_me_key("publish:run-1:all")])


def test_the_notify_me_email_says_where_it_comes_from(tmp_path: Path, mailbox) -> None:
    from abstractgateway.mail.notifications import render_notice

    subject, text, _html = render_notice("notify_me", {"title": "Judge what arrived", "model_title": "2 urgent", "model_body": "From the bank", "ref": "run r"})
    assert subject == "[AbstractFramework] Judge what arrived: notification"
    assert "“Judge what arrived” notified you." in text and "Written by the flow:" in text and "From the bank" in text
    assert "Notify me step that reaches you by email" in text


# --- After the run -----------------------------------------------------------------------------


def test_after_the_run_shows_what_was_asked_beside_the_deliveries(tmp_path: Path, mailbox) -> None:
    from abstractgateway.events_stream import record_notification
    from abstractgateway.mail.notifications import notify_me_key

    stream = stream_for(tmp_path, "default", "alice")
    record_published(stream, run_id="run-p", step_id="n7", payload={"channel": "me", "title": "Judged", "alert": True, "channels": ["watch", "phone", "email"]})
    record_notification(stream, "sent", channel="watch", notice="attention:publish:run-p:n7", data={"record_key": "publish:run-p:n7", "device": "aw1", "via": "push"})
    record_notification(stream, "sent", channel="email", notice=notify_me_key("publish:run-p:n7"), data={"kind": "notify_me"})
    run = SimpleNamespace(run_id="run-p", status="completed", created_at="a", updated_at="b", vars={}, output={})
    out = build_after(run, run_store=None, ledger_store=None, stream=stream)
    assert out["attention"]["asked"] == ["watch", "phone", "email"] and out["attention"]["recipient"] == "me"
    assert [(d["channel"], d["via"], d["record_key"]) for d in out["deliveries"]] == [
        ("watch", "push", "publish:run-p:n7"), ("email", "outbox", "publish:run-p:n7")]
    # No choice recorded: "all".
    record_published(stream, run_id="run-q", step_id="n1", payload={"channel": "me", "title": "All", "alert": True})
    run.run_id = "run-q"
    assert build_after(run, run_store=None, ledger_store=None, stream=stream)["attention"]["asked"] == "all"


# --- end to end: a compiled Notify me, the runtime, the gateway's deliverers, a fake relay -------


def test_end_to_end_a_compiled_notify_me_reaches_only_the_phone(tmp_path: Path, mailbox) -> None:
    """Flow saves `effectConfig.channels`; the runtime compiles and runs the node; the gateway's
    handler records the alert; the tray skips it, push rings the phone only (a fresh watch is
    registered), no email is queued; After the run says "asked: phone" with the phone delivery."""
    from abstractruntime import Runtime
    from abstractruntime.core.models import RunStatus
    from abstractruntime.scheduler.registry import WorkflowRegistry
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore
    from abstractruntime.visualflow_compiler.compiler import compile_visualflow

    plane, outbox = mailbox
    stream, dispatcher, relay = _push_world(tmp_path)
    flow = {
        "id": "judge", "name": "Judge what arrived", "entryNode": "n1", "edges": [],
        "nodes": [{"id": "n1", "type": "publish_event", "data": {
            "nodeType": "publish_event", "label": "Notify me",
            "inputs": [{"id": "exec-in", "type": "execution"}, {"id": "title", "type": "string"}, {"id": "body", "type": "string"}],
            "outputs": [{"id": "exec-out", "type": "execution"}, {"id": "result", "type": "object"}, {"id": "success", "type": "boolean"}],
            "pinDefaults": {"channel": "me", "alert": True, "title": "2 urgent emails", "body": "From the bank"},
            "effectConfig": {"channels": ["phone"]},
        }}],
    }
    spec = compile_visualflow(flow)
    registry = WorkflowRegistry()
    registry.register(spec)
    rt = Runtime(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), workflow_registry=registry)
    # The plane's handler on the dispatcher's stream (the same owner's stream).
    rt.set_publish_event_handler(GatewayPublishEvent(data_dir=tmp_path / "plane", tenant="default", user="alice", email_plane=plane))
    run_id = rt.start(workflow=spec, vars={})
    state = rt.tick(workflow=spec, run_id=run_id)
    assert state.status == RunStatus.COMPLETED, state.error

    raised = _raised(stream)
    assert raised["channels"] == ["phone"] and raised["title"] == "2 urgent emails"
    assert AttentionLedger().apply({"type": "attention.raised", "data": raised}) is None  # tray: not asked
    dispatcher.tick()
    assert [c["token"] for c in relay.calls] == [TOKEN_B] and all("urgent" not in json.dumps(c) for c in relay.calls)
    assert outbox.rows() == []  # email: not asked

    out = build_after(rt.run_store.load(run_id), run_store=None, ledger_store=None, stream=stream)
    assert out["attention"]["from"] == "notify_me" and out["attention"]["asked"] == ["phone"]
    assert [(d["channel"], d["via"], d["device"]) for d in out["deliveries"]] == [("phone", "push", "ap1")]
