"""GET /api/gateway/runs/{run_id}/after (framework ADR-0041 "No hidden effects after a flow").

The builder over real event streams (hermetic dirs) and fake run/ledger stores: a triggered run
with one tray delivery and one push delivery answers both with their channel, route and device; a
run whose On Flow End notify title was empty answers `quiet_reason: "empty title"`; a Notify me
alert of a run is found by its id; email deliveries carry their alert's record key. Through the real
app: principal-scoped (another person's run is 404). And the enforcement half of the ADR: every
writer of a delivery names its channel."""

from __future__ import annotations

import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from abstractgateway.events_stream import CHANNELS, record_attention_raised, record_notification, record_published, stream_for
from abstractgateway.run_after import build_after

AID = "auto-1"
RID = "run-occ-15"
KEY = f"automation:completed:{AID}:15"


class _Runs:
    def __init__(self, *runs: Any) -> None:
        self.runs = {r.run_id: r for r in runs}

    def load(self, run_id: str) -> Any:
        return self.runs.get(run_id)


class _Tree(_Runs):
    def list_children(self, *, parent_run_id: str, status: Any = None) -> List[Any]:
        return [r for r in self.runs.values() if getattr(r, "parent_run_id", None) == parent_run_id]


class _Ledger:
    def __init__(self, records: Dict[str, List[Dict[str, Any]]]) -> None:
        self.records = records

    def list(self, run_id: str) -> List[Dict[str, Any]]:
        return list(self.records.get(run_id, []))


def _completed(index: int, attention: Any, notify: Any) -> Dict[str, Any]:
    return {
        "idempotency_key": f"automation:completed:{AID}:{index}",
        "status": "completed",
        "effect": {"type": "emit_event", "payload": {"name": "automation.completed", "payload": {
            "run_id": RID, "index": index, "status": "completed", "attempts": 1,
            "finished_at": "2026-10-26T00:16:09+00:00", "notify": notify, "attention": attention,
        }}},
    }


def _controller() -> Any:
    definition = {"title": "Gmail watch", "trigger": {"source_id": "email.received", "source_version": 1, "config": {"account": "self"}}}
    return SimpleNamespace(run_id=AID, status="running", vars={"_meta": {"automation": definition}}, output=None)


def _occurrence(output: Dict[str, Any], *, run_id: str = RID, index: int = 15) -> Any:
    occ = {
        "automation_id": AID,
        "occurrence_index": index,
        "fired_at": "2026-10-26T00:15:59+00:00",
        "trigger_envelope": {"source_id": "email.received", "fired_at": "2026-10-26T00:15:59+00:00", "payload": {"count": 4}},
    }
    messages = [{"subject": "O", "from": "codex@athena.ovh"}, {"subject": "A", "from": "codex@athena.ovh"}]
    return SimpleNamespace(
        run_id=run_id, status="completed", created_at="2026-10-26T00:16:00+00:00", updated_at="2026-10-26T00:16:09+00:00",
        vars={"_meta": {"occurrence": occ}, "messages": messages}, output=output,
    )


def _words(trigger: Dict[str, Any]) -> Any:
    return "When mail arrives in me@example.org", "me@example.org"


def test_a_run_with_a_tray_and_a_push_delivery_answers_both(tmp_path: Path) -> None:
    stream = stream_for(tmp_path, "default", "alice")
    attention = {"kind": "notify", "seq": 5, "title": "Suspicious credential emails received", "body": "…", "channels": ["console"]}
    record_attention_raised(stream, {"record_key": KEY, "automation_id": AID, "run_id": RID, "index": 15, "kind": "notify", "seq": 5, "channels": ["console"]})
    record_notification(stream, "sent", channel="tray", notice=f"attention:{KEY}", data={"record_key": KEY, "device": "mac-1"})
    record_notification(stream, "sent", channel="watch", notice=f"attention:{KEY}", data={"record_key": KEY, "device": "watch-1", "via": "push"})
    # Another run's delivery never shows here.
    record_notification(stream, "sent", channel="phone", notice="attention:automation:completed:auto-1:14", data={"record_key": "automation:completed:auto-1:14"})

    run = _occurrence({"response": "Four emails look like keys.", "notify": {"title": attention["title"], "body": "…"}, "success": True, "note": "4 mails judged; 4 suspicious."})
    out = build_after(
        run, run_store=_Runs(_controller()), ledger_store=_Ledger({AID: [_completed(15, attention, {"title": attention["title"]})]}),
        stream=stream, trigger_words=_words, device_name=lambda d: "Laurent's Watch" if d == "watch-1" else None,
    )

    assert out["trigger"]["automation_title"] == "Gmail watch"
    assert out["trigger"]["text"] == "When mail arrives in me@example.org" and out["trigger"]["connection"] == "me@example.org"
    assert out["trigger"]["arrived"] == {"count": 4, "noun": "new emails", "line": "“O” from codex@athena.ovh and 3 more"}
    assert out["run"]["status"] == "completed" and out["run"]["ended"] == "2026-10-26T00:16:09+00:00"
    assert out["run"]["response_excerpt"] == "Four emails look like keys." and out["run"]["note"] == "4 mails judged; 4 suspicious."
    assert out["attention"]["title"] == attention["title"] and out["attention"]["kind"] == "notify" and out["attention"]["from"] == "flow_end"
    assert out["attention"]["raised_at"]  # the stream event's time
    rows = {(d["channel"], d["via"], d.get("device_name"), d["state"]) for d in out["deliveries"]}
    assert rows == {("console", "stream", None, "sent"), ("tray", "stream", None, "sent"), ("watch", "push", "Laurent's Watch", "sent")}
    assert all(d["record_key"] == KEY and d["time"] for d in out["deliveries"])
    assert "quiet_reason" not in out


def test_a_null_notify_is_no_notify_not_an_empty_title(tmp_path: Path) -> None:
    """An unwired On Flow End `notify` pin writes `notify: null`: Notify me never ran, so the
    reason is "no notify", never "empty title" (operator's email-triage flow, 2026-10-28)."""
    stream = stream_for(tmp_path, "default", "alice")
    done = _Ledger({AID: [_completed(15, None, None)]})
    for end in ({"response": "ok", "notify": None, "success": True}, {"response": "ok", "notify": "", "success": True}):
        assert build_after(_occurrence(end), run_store=_Runs(_controller()), ledger_store=done, stream=stream)["quiet_reason"] == "no notify"
    # A notify object whose title is blank (whitespace, or no title key) is the flow's quiet decision.
    for notify in ({"title": "   ", "body": "x"}, {"body": ""}):
        run = _occurrence({"response": "ok", "notify": notify, "success": True})
        assert build_after(run, run_store=_Runs(_controller()), ledger_store=done, stream=stream)["quiet_reason"] == "empty title"


def test_an_empty_title_answers_quiet_reason(tmp_path: Path) -> None:
    stream = stream_for(tmp_path, "default", "alice")
    run = _occurrence({"response": "Nothing worth your time.", "notify": {"title": "", "body": ""}, "success": True})
    out = build_after(run, run_store=_Runs(_controller()), ledger_store=_Ledger({AID: [_completed(15, None, None)]}), stream=stream, trigger_words=_words)
    assert out["attention"] is None and out["alerts"] == [] and out["deliveries"] == []
    assert out["quiet_reason"] == "empty title"
    # A flow without any notify (the new shape: On Flow End records, Notify me acts) says so.
    bare = _occurrence({"response": "ok", "success": True})
    assert build_after(bare, run_store=_Runs(_controller()), ledger_store=_Ledger({AID: [_completed(15, None, None)]}), stream=stream)["quiet_reason"] == "no notify"
    # The automation has not recorded its decision yet / the run is still going.
    assert build_after(bare, run_store=_Runs(_controller()), ledger_store=_Ledger({}), stream=stream)["quiet_reason"] == "not recorded yet"
    running = _occurrence({})
    running.status = "running"
    assert build_after(running, run_store=_Runs(_controller()), ledger_store=_Ledger({}), stream=stream)["quiet_reason"] == "still running"


def test_a_notify_me_alert_and_an_email_delivery(tmp_path: Path) -> None:
    from abstractgateway.automation_attention import email_notice_keys

    stream = stream_for(tmp_path, "default", "alice")
    # Notify me on a person's run (no automation): found by its id publish:<run>:<step>.
    person = SimpleNamespace(run_id="run-p", status="completed", created_at="a", updated_at="b", vars={}, output={"response": "done"})
    record_published(stream, run_id="run-p", step_id="node-7", payload={"channel": "me", "title": "Judged: 2 urgent", "alert": True})
    record_notification(stream, "sent", channel="phone", notice="attention:publish:run-p:node-7", data={"record_key": "publish:run-p:node-7", "device": "ph", "via": "push"})
    out = build_after(person, run_store=_Runs(), ledger_store=None, stream=stream)
    assert out["trigger"] is None
    assert out["attention"]["from"] == "notify_me" and out["attention"]["title"] == "Judged: 2 urgent" and out["attention"]["step_id"] == "node-7"
    assert [(d["channel"], d["via"]) for d in out["deliveries"]] == [("phone", "push")]

    # Email (the outbox key, not the record key): given its alert's record key; a failure keeps its reason.
    attention = {"kind": "notify", "seq": 9, "title": "Digest", "channels": ["console", "email"]}
    (mail_key,) = email_notice_keys(AID, attention)
    record_notification(stream, "failed", channel="email", notice=mail_key, data={"kind": "automation_result", "code": "smtp_auth", "cause": "The mail server refused the password."})
    run = _occurrence({"response": "x", "notify": {"title": "Digest"}}, index=15)
    out = build_after(run, run_store=_Runs(_controller()), ledger_store=_Ledger({AID: [_completed(15, attention, {"title": "Digest"})]}), stream=stream)
    (mail,) = [d for d in out["deliveries"] if d["channel"] == "email"]
    assert mail == {"record_key": KEY, "channel": "email", "via": "outbox", "time": mail["time"], "state": "failed", "reason": "The mail server refused the password."}


def test_every_delivery_writer_names_its_channel(tmp_path: Path) -> None:
    """ADR-0041 enforcement: a delivery without its channel cannot be recorded, and every writer
    in the gateway passes `channel=` (the After the run section shows each channel)."""
    with pytest.raises(ValueError):
        record_notification(stream_for(tmp_path, "default", "x"), "sent", channel="pager", notice="n")
    src = Path(__file__).resolve().parents[1] / "src" / "abstractgateway"
    calls = 0
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"(?<!def )record_notification\(|off_loop\(\"light\",\s*record_notification,", text):
            calls += 1
            assert "channel=" in text[m.end(): m.end() + 300], f"{path.name}: record_notification without channel="
    assert calls >= 4  # events_stream (console), push dispatcher, email outbox, client reports
    assert {"console", "email", "tray", "watch", "phone"} <= CHANNELS


# --- through the real app ---------------------------------------------------------------------

from test_gateway_principal_isolation_matrix import _client, _create_user, _write_min_bundle  # noqa: E402


def test_route_is_principal_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        alice, bob = _create_user(client, "alice"), _create_user(client, "bob")
        _write_min_bundle(bundles_dir=tmp_path / "runtime" / "users" / "default" / "alice" / "flows")
        r = client.post("/api/gateway/runs/start", headers=alice, json={"bundle_id": "iso-bundle", "flow_id": "root", "input_data": {"prompt": "hi"}})
        assert r.status_code == 200, r.text
        rid = r.json()["run_id"]
        end = time.time() + 15
        body: Dict[str, Any] = {}
        while time.time() < end:
            body = client.get(f"/api/gateway/runs/{rid}/after", headers=alice).json()
            if body.get("run", {}).get("status") == "completed":
                break
            time.sleep(0.05)
        assert body["run"]["status"] == "completed" and body["trigger"] is None
        assert body["deliveries"] == [] and body["quiet_reason"] == "no notify"
        assert client.get(f"/api/gateway/runs/{rid}/after", headers=bob).status_code == 404
        assert client.get(f"/api/gateway/runs/{rid}/after").status_code == 401
        # The /me/events routes kept their paths.
        assert client.get("/api/gateway/me/events", headers=alice).status_code == 200


def test_an_approval_on_a_sub_run_is_the_parent_runs_attention(tmp_path: Path) -> None:
    """The live shape of 2026-10-26 (run 7abde419…): a person's basic-agent run waits on its agent
    sub-run; the sub-run asks for a tool approval; the tray reports it, the phone and the watch get
    a push, the email outbox mails it. The PARENT's after-the-run answers that open approval and
    its four deliveries, never "still running"."""
    from abstractgateway.events_stream import record_wait_raised, record_wait_seen
    from abstractgateway.mail.notifications import idempotency_key

    stream = stream_for(tmp_path, "default", "admin")
    parent = SimpleNamespace(run_id="7abde419", status="waiting", parent_run_id=None, created_at="2026-10-26T01:22:26+00:00",
                             updated_at="2026-10-26T01:22:26+00:00", vars={"_meta": {"origin": {"kind": "person"}}}, output=None)
    child = SimpleNamespace(run_id="4a02d8d6", status="waiting", parent_run_id="7abde419", created_at="x", updated_at="y", vars={}, output=None)
    wait_key = "tool_approval:4a02d8d6:act:f046d418"
    item = {"run_id": "4a02d8d6", "wait_key": wait_key, "step_id": "3ce288ff", "kind": "approval", "wait_kind": "tool_approval",
            "record_key": "f046d418dda6cdaa8a9fe0d06cf1684c", "result_key": None}
    record_wait_raised(stream, item, session_id="7abde419")
    rk = item["record_key"]
    record_notification(stream, "sent", channel="tray", notice=f"attention:{rk}", data={"record_key": rk, "device": "mac"})
    record_notification(stream, "sent", channel="phone", notice=f"attention:{rk}", data={"record_key": rk, "device": "ph"})
    record_notification(stream, "sent", channel="watch", notice=f"attention:{rk}", data={"record_key": rk, "device": "wa", "via": "push"})
    mail = idempotency_key("approval_needed", "4a02d8d6", wait_key)
    record_notification(stream, "sent", channel="email", notice=mail, data={"kind": "approval_needed", "subject_ref": None})

    out = build_after(parent, run_store=_Tree(parent, child), ledger_store=None, stream=stream)
    assert out["tree"] == ["7abde419", "4a02d8d6"]
    att = out["attention"]
    assert att["kind"] == "approval" and att["open"] is True and att["from"] == "wait"
    assert att["run_id"] == "4a02d8d6" and att["prompt_ref"] == "/api/gateway/runs/4a02d8d6" and att["raised_at"]
    assert "quiet_reason" not in out
    assert {(d["channel"], d["via"], d["record_key"]) for d in out["deliveries"]} == {
        ("tray", "stream", rk), ("phone", "stream", rk), ("watch", "push", rk), ("email", "outbox", rk),
    }
    # Answered: the approval is no longer open, still listed with its deliveries.
    record_wait_seen(stream, item, outcome="approved")
    again = build_after(parent, run_store=_Tree(parent, child), ledger_store=None, stream=stream)
    assert again["attention"]["open"] is False and len(again["deliveries"]) == 4
    # The run alone (no children index): the approval is not seen, the run is "still running".
    alone = build_after(parent, run_store=_Runs(parent), ledger_store=None, stream=stream)
    assert alone["attention"] is None and alone["quiet_reason"] == "still running"
