"""The tray's Sessions submenu (operator-approved redesign, 2026-10-15).

What a person at the menu gets, pinned without a display:

- ONE header line; a second only when it carries news. No "Ready · N models
  loaded", no grey URL line. **Addresses ▸** rows say where each URL reaches.
- **Sessions ▸**: Running (with Pause · Stop…; a paused run with Resume),
  Need attention (an approval: what is asked, **Show and approve…**, Deny; a
  question: the question, free-text answers stay in the clients), Scheduled
  (the automations with the gateway's own schedule words, Run now, Pause /
  Resume), Recent (last 24 h, ≤5 rows then "…and N more").
- Rows carry the SESSION's title (the served `title`), never the workflow id.
- Every action goes through the gateway's run command / wait-resolution /
  automation command routes, and an approval is NEVER sent from a bare menu
  click: the dialog shows the tool and its full arguments first.
- Another account's session is shown, never commanded (greyed, with the reason).
- The menu signature carries what the new sections read; a ticking duration
  still never rebuilds the menu.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from abstractgateway.tray import menu_model as mm
from abstractgateway.tray.client import Result
from abstractgateway.tray.sampler import AutomationRow, RunRow, automation_rows_from_listing, run_rows_from_listing

pytestmark = pytest.mark.basic

NOW = time.time()


def _snap(**over: Any):
    from test_gateway_tray_helper import _snap as base

    return base(**over)


def _row(run_id: str, title: str, status: str = "running", **kw: Any) -> RunRow:
    base: Dict[str, Any] = dict(workflow_id="basic-agent:x", steps=4, duration_s=130.0, started_at=NOW - 130, observer_path=f"/apps/observer/#run/{run_id}", title=title, title_source="prompt")
    base.update(kw)
    return RunRow(run_id, base.pop("workflow_id"), status, base.pop("steps"), base.pop("duration_s"), base.pop("started_at"), **base)


APPROVAL = _row("ap", "Clean the build folder", "waiting", wait_kind="tool_approval", wait_summary='execute_command {"command": "rm -rf build"}', wait_tool="execute_command", wait_key="tool_approval:k1", wait_run_id="ap-sub")
QUESTION = _row("q", "Plan the trip", "waiting", wait_kind="ask_user", wait_summary="Which dates?", wait_key="ask:1", wait_run_id="q")
TIMER = _row("t", "Daily digest", "waiting", wait_kind="other", wait_summary="waiting for a timer")
EVENT = _row("e", "Inbox triage", "waiting", wait_kind="event", wait_summary="waiting for email.received")
PAUSED = _row("p", "Market study", "waiting", wait_kind="paused", wait_summary="paused", duration_s=302.0)
RUNNING = _row("r", "Refactor the sampler into two lanes")
FOREIGN = _row("f", "Someone else's run", commandable=False, plane="default/bob")
DONE = _row("d", "Write the changelog", "completed", steps=7, duration_s=102.0)
DAILY = AutomationRow("a1", "Daily digest", "active", "Every day at 09:00 · next Fri 17 Oct 09:00", "2026-10-17T09:00:00+02:00", ("revise", "pause", "run_now", "stop_current", "archive", "discuss"))
TRIAGE = AutomationRow("a2", "Inbox triage", "paused", "When an email arrives", None, ("revise", "resume", "run_now", "archive", "discuss"))


# ----------------------------------------------------------- the payload


def test_rows_read_the_served_title_wait_and_plane() -> None:
    """`GET /host/runs` items → rows: the session's title and source, what a
    waiting row waits for (and which run HOLDS the wait), and whether this
    tray's commands reach it (`plane` == the payload's `caller_plane`)."""
    payload = {
        "caller_plane": "default",
        "items": [
            {"run_id": "a", "label": "basic-agent:x", "status": "waiting", "activity": "waiting", "plane": "default", "title": "Clean the build folder", "title_source": "prompt",
             "wait": {"kind": "tool_approval", "summary": 'execute_command {"command": "rm -rf build"}', "tool": "execute_command", "wait_key": "tool_approval:k1", "run_id": "a-sub"}},
            {"run_id": "b", "label": "daily-digest:main", "status": "running", "activity": "running", "plane": "default/bob", "title": "Daily digest", "title_source": "automation"},
            {"run_id": "c", "label": "coding-agent:coder", "status": "completed", "activity": "done", "created_at": NOW - 60, "updated_at": NOW - 10},
        ],
    }
    rows = {r.run_id: r for r in run_rows_from_listing(payload, now=NOW)}
    a = rows["a"]
    assert (a.title, a.title_source) == ("Clean the build folder", "prompt")
    assert (a.wait_kind, a.wait_tool, a.wait_key, a.wait_run_id) == ("tool_approval", "execute_command", "tool_approval:k1", "a-sub")
    assert a.wait_summary == 'execute_command {"command": "rm -rf build"}'
    assert a.commandable is True and a.plane == "default"
    # Another plane's row is read, and marked as one this tray cannot command.
    assert rows["b"].commandable is False and rows["b"].plane == "default/bob"
    # An older gateway (no title, no caller_plane): the row still renders, under the workflow name.
    c = rows["c"]
    assert c.title == "" and c.wait_kind is None and c.commandable is True
    assert mm.run_title(c) == "coding-agent:coder" and mm.run_title(a) == "Clean the build folder"
    # The caller's plane decides, not the word "default".
    other = run_rows_from_listing({"caller_plane": "default/bob", "items": payload["items"]}, now=NOW)
    assert {r.run_id: r.commandable for r in other} == {"a": False, "b": True, "c": False}


def test_automation_rows_take_the_served_schedule_words_verbatim() -> None:
    payload = {
        "items": [
            {"automation_id": "a1", "title": "Daily digest", "status": "active", "schedule_text": "Every day at 09:00 · next Fri 17 Oct 09:00", "next_run_local": "2026-10-17T09:00:00+02:00", "capabilities": ["revise", "pause", "run_now"], "next_fire_at": "2026-10-17T07:00:00Z"},
            {"automation_id": "a2", "title": "Old wrapper", "status": "active", "legacy": True, "capabilities": ["legacy"]},
            {"title": "no id"},
        ]
    }
    rows = automation_rows_from_listing(payload)
    assert [r.automation_id for r in rows] == ["a1", "a2"]
    assert rows[0].schedule_text == "Every day at 09:00 · next Fri 17 Oct 09:00" and rows[0].next_run_local == "2026-10-17T09:00:00+02:00"
    assert rows[0].capabilities == ("revise", "pause", "run_now") and rows[1].legacy is True and rows[1].schedule_text == ""
    assert automation_rows_from_listing({"items": "nope"}) == [] and automation_rows_from_listing(None) == []


def test_the_tray_never_computes_a_next_run() -> None:
    """Grep-proof like the Observer: the next-run words come from the gateway
    (`schedule_text`); the tray does no date arithmetic for them."""
    src = Path(mm.__file__).read_text(encoding="utf-8") + Path(mm.__file__).with_name("sampler.py").read_text(encoding="utf-8")
    for needle in ("timedelta", "next_fire_at", "croniter", "strptime"):
        assert needle not in src, needle
    assert mm.automation_row_label(DAILY) == "⏱ Daily digest · Every day at 09:00 · next Fri 17 Oct 09:00"
    assert mm.automation_row_label(TRIAGE) == "⏸ Inbox triage · paused · When an email arrives"


# ------------------------------------------------------------- the labels


def test_rows_are_grouped_and_labelled_by_what_they_wait_for() -> None:
    assert mm.run_group(RUNNING) == "running" and mm.run_group(TIMER) == "running" and mm.run_group(EVENT) == "running"
    assert mm.run_group(APPROVAL) == "attention" and mm.run_group(QUESTION) == "attention"
    assert mm.run_group(PAUSED) == "paused" and mm.run_group(DONE) == "done"

    assert mm.run_row_label(RUNNING) == "🟢 Refactor the sampler into two lanes · step 4 · 2m 10s so far"
    assert mm.run_row_label(TIMER) == "🟢 Daily digest · step 4 · waiting for a timer · 2m 10s so far"
    assert mm.run_row_label(EVENT) == "🟢 Inbox triage · step 4 · waiting for email.received · 2m 10s so far"
    assert mm.run_row_label(APPROVAL) == "🟡 Clean the build folder · approve `execute_command`"
    assert mm.run_row_label(QUESTION) == "🟡 Plan the trip · a question"
    assert mm.run_row_label(PAUSED) == "⏸ Market study · paused · 5m 02s so far"
    assert mm.run_row_label(DONE) == "✅ Write the changelog · 7 steps · 1m 42s"
    # An approval whose tool name is unknown still reads as an approval.
    bare = dataclasses.replace(APPROVAL, wait_tool=None)
    assert mm.run_row_label(bare) == "🟡 Clean the build folder · approve a tool call"


def test_the_header_is_one_line_unless_there_is_news() -> None:
    labels = lambda snap: [n.label for n in mm.build_menu(mm.MenuInputs(snap=snap)) if not n.separator][:2]  # noqa: E731
    assert labels(_snap()) == ["AbstractGateway — Running", "Open Console"]
    assert labels(_snap(inflight_ticks=3)) == ["AbstractGateway — Running", "Working on 3 steps"]
    assert labels(_snap(gateway_state="paused", paused=True)) == ["AbstractGateway — Paused", "Paused — conversations wait until you resume"]
    assert labels(_snap(gateway_state="pausing", paused=True, inflight_ticks=1)) == ["AbstractGateway — Pausing…", "Finishing 1 run at the next step, then pausing"]
    assert labels(_snap(gateway_state="unreachable", reachable=False)) == ["AbstractGateway — Not responding", "Can't reach it on this computer · retrying"]
    # The meters are ONE line -- machine-wide, then what this gateway process occupies (2026-10-16).
    meters = [n.label for n in mm.build_menu(mm.MenuInputs(snap=_snap())) if n.label.startswith("Memory")]
    assert meters == ["Memory 41.2 GB of 64.0 GB (64%) · this gateway 3.1 GB · GPU 34% busy"]


# ----------------------------------------------------------- the sections


def _sessions(snap, *, observer: bool = True):
    return mm.sessions_items(snap, observer=observer)


def _labels(nodes) -> List[str]:
    return [n.label for n in nodes if not n.separator]


def _row_node(nodes, prefix: str) -> mm.Node:
    return next(n for n in nodes if n.label.startswith(prefix))


def test_sections_in_order_with_headers_and_the_empty_cases() -> None:
    full = _snap(runs=(RUNNING, TIMER, PAUSED, APPROVAL, QUESTION, DONE), automations=(DAILY, TRIAGE), observer_automations_path="/apps/observer/#automations")
    labels = _labels(_sessions(full))
    assert labels[0] == "Running (3)"
    assert labels[1:4] == [mm.run_row_label(RUNNING), mm.run_row_label(TIMER), mm.run_row_label(PAUSED)]
    assert labels[4] == "Need attention (2)" and labels[5:7] == [mm.run_row_label(APPROVAL), mm.run_row_label(QUESTION)]
    assert labels[7] == "Scheduled (2)" and labels[8:10] == [mm.automation_row_label(DAILY), mm.automation_row_label(TRIAGE)]
    assert labels[10] == "Recent — 1 done" and labels[11] == mm.run_row_label(DONE)
    assert labels[-1] == "Open Conversations in Console"
    # Every header is an information line, every row a submenu.
    nodes = _sessions(full)
    assert all(n.enabled is False for n in nodes if n.label in {"Running (3)", "Need attention (2)", "Scheduled (2)", "Recent — 1 done"})
    assert all(n.children for n in nodes if n.label[:1] in {"🟢", "🟡", "⏸", "⏱", "✅"})

    # Empty: Running says none; Need attention and Scheduled are absent, not "(0)".
    empty = _labels(_sessions(_snap()))
    assert empty == ["Running — none", "Recent — none in the last 24 hours", "Open Conversations in Console"]

    # Recent is a glance: five rows then ONE line naming the rest.
    many = tuple(dataclasses.replace(DONE, run_id=f"d{i}", title=f"Job {i}") for i in range(8))
    recent = _labels(_sessions(_snap(runs=many)))
    assert recent[1] == "Recent — 8 done" and recent[2:7] == [mm.run_row_label(r) for r in many[:5]] and recent[7] == "…and 3 more"

    # A failed run list says so and keeps the console door; a failed automations read says so under Scheduled.
    down = _labels(_sessions(_snap(runs=(), runs_error="HTTP 500", automations_error="HTTP 503")))
    assert down[:2] == ["Conversation list unavailable", "HTTP 500"] and "Scheduled unavailable: HTTP 503" in down and down[-1] == "Open Conversations in Console"
    # Completed / archived automations are not "scheduled".
    gone = _labels(_sessions(_snap(automations=(dataclasses.replace(DAILY, status="completed"),))))
    assert not any(label.startswith("Scheduled") for label in gone)


def test_each_row_kind_opens_the_right_submenu() -> None:
    snap = _snap(runs=(RUNNING, PAUSED, APPROVAL, QUESTION, DONE, FOREIGN), automations=(DAILY, TRIAGE), observer_automations_path="/apps/observer/#automations")
    nodes = _sessions(snap)

    def sub(prefix: str) -> List[tuple]:
        return [(c.label, c.action, c.enabled) for c in _row_node(nodes, prefix).children]

    running = sub("🟢 Refactor")
    assert running[0][0].startswith("Running · started ") and running[0][2] is False
    assert running[1:] == [("Open in Observer", ("open_run_observer", "r"), True), ("Pause", ("run_pause", "r"), True), ("Stop…", ("run_stop", "r"), True)]

    paused = sub("⏸ Market study")
    assert paused[0][0].startswith("Paused · started ")
    assert paused[1:] == [("Open in Observer", ("open_run_observer", "p"), True), ("Resume", ("run_resume", "p"), True), ("Stop…", ("run_stop", "p"), True)]

    approval = sub("🟡 Clean the build folder")
    assert approval == [
        ('execute_command {"command": "rm -rf build"}', None, False),
        ("Show and approve…", ("run_approve_show", "ap"), True),
        ("Deny", ("run_deny", "ap"), True),
        ("Open in Observer", ("open_run_observer", "ap"), True),
    ]
    # No bare "Approve" row anywhere: an approval needs the dialog.
    assert not any(c.label == "Approve" for c in mm.iter_nodes(nodes))

    question = sub("🟡 Plan the trip")
    assert question == [("Which dates?", None, False), ("Open in Observer", ("open_run_observer", "q"), True)]

    done = sub("✅ Write the changelog")
    assert done[0][0].startswith("Started ") and done[1:] == [("Open in Observer", ("open_run_observer", "d"), True)]

    # Another account's session: the commands are greyed and the row says why.
    foreign = sub("🟢 Someone else's run")
    assert [(label, enabled) for label, _a, enabled in foreign[1:]] == [("Open in Observer", True), ("Pause", False), ("Stop…", False), (mm.ANOTHER_ACCOUNT_LINE, False)]

    daily = sub("⏱ Daily digest")
    assert daily == [
        ("Active · Every day at 09:00 · next Fri 17 Oct 09:00", None, False),
        ("Open in Observer", ("open_automations_observer",), True),
        ("Run now", ("auto_run_now", "a1"), True),
        ("Pause", ("auto_pause", "a1"), True),
    ]
    triage = sub("⏸ Inbox triage")
    assert triage[2:] == [("Run now", ("auto_run_now", "a2"), True), ("Resume", ("auto_resume", "a2"), True)]

    # The gateway's capabilities gate the automation actions; Observer's page link gates "Open in Observer".
    legacy = AutomationRow("a3", "Old wrapper", "active", "", None, ("legacy",), legacy=True)
    nodes2 = _sessions(_snap(automations=(legacy,)), observer=False)
    old = [(c.label, c.enabled) for c in _row_node(nodes2, "⏱ Old wrapper").children]
    assert old == [("Active · legacy schedule", False), ("Open in Observer", False), ("Run now", False), ("Pause", False)]

    # Unreachable gateway: nothing is clickable but the rows still read.
    down = _sessions(_snap(gateway_state="unreachable", reachable=False, runs=(RUNNING, APPROVAL)))
    assert all(c.enabled is False for n in down if n.children for c in n.children)


def test_the_approval_dialog_shows_the_arguments_in_full_then_says_where_the_rest_is() -> None:
    wait = {"kind": "tool_approval", "tool": "execute_command", "tool_calls": [{"name": "execute_command", "arguments": {"command": "rm -rf build", "cwd": "/tmp/x"}}]}
    title, body = mm.approval_dialog(wait, title="Clean the build folder")
    assert title == "Approve execute_command?"
    assert body.startswith("Clean the build folder asks to run execute_command.\n\nexecute_command {\n")
    assert '"command": "rm -rf build"' in body and '"cwd": "/tmp/x"' in body and "…" not in body

    two = dict(wait, tool_calls=wait["tool_calls"] + [{"name": "write_file", "arguments": {"path": "a", "content": "b"}}])
    assert mm.approval_dialog(two, title="T")[0] == "Approve execute_command and 1 more?"

    big = {"tool_calls": [{"name": "write_file", "arguments": {"content": "x" * 5000}}]}
    _, body = mm.approval_dialog(big, title="T")
    assert body.endswith("…\n(open in Observer for the rest)") and len(body) < 1700
    assert mm.approval_dialog({}, title="T") == ("Approve this tool call?", "T asks to run a tool.")


# -------------------------------------------------------------- the actions


class FakeClient:
    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.wait: Result = Result(True, 200, {"run_id": "ap-sub", "waiting": True, "kind": "tool_approval", "tool": "execute_command", "wait_key": "tool_approval:k1", "tool_calls": [{"name": "execute_command", "arguments": {"command": "rm -rf build"}}]})
        self.command: Result = Result(True, 200, {"accepted": True, "duplicate": False, "seq": 1})

    def run_command(self, run_id, type_, payload=None):
        self.calls.append(("command", run_id, type_, dict(payload or {})))
        return self.command

    def answer_wait(self, run_id, wait_key, answer):
        self.calls.append(("answer", run_id, wait_key, dict(answer)))
        return self.command

    def run_wait(self, run_id):
        self.calls.append(("wait", run_id))
        return self.wait

    def automation_command(self, automation_id, type_):
        self.calls.append(("automation", automation_id, type_))
        return self.command

    def app_open(self, app_id, path=None):
        self.calls.append(("open", app_id, path))
        return Result(True, 200, {"ok": True, "open_url": "/apps/handover/obs"})


@pytest.fixture()
def tray(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from abstractgateway.tray import app as tray_app

    app = tray_app.TrayApp({"base_url": "http://127.0.0.1:18840", "token": "t", "data_dir": str(tmp_path)})
    app.client = FakeClient()  # type: ignore[assignment]
    snap = _snap(runs=(RUNNING, PAUSED, APPROVAL, QUESTION, DONE), automations=(DAILY, TRIAGE), observer_automations_path="/apps/observer/#automations")
    app._snap = snap
    app.sampler.snapshot = lambda: snap  # type: ignore[method-assign]
    events: List[tuple] = []
    app._notify = lambda title, msg: events.append(("notify", title, msg))  # type: ignore[assignment]
    app._info = lambda title, body, style="informational": events.append(("info", title, body))  # type: ignore[assignment]
    app._bg = lambda fn, name="": fn()  # type: ignore[assignment]
    app._force_menu_rebuild = lambda: None  # type: ignore[assignment]
    app.sampler.poke = lambda: None  # type: ignore[assignment]
    opened: List[str] = []
    app._open_signed_in = lambda url: opened.append(url)  # type: ignore[method-assign]
    confirms: List[tuple] = []
    monkeypatch.setattr(tray_app.dialogs, "confirm", lambda title, body, **kw: confirms.append((title, body, kw)) or True)
    app.events, app.opened, app.confirms = events, opened, confirms  # type: ignore[attr-defined]
    return app


def test_every_new_action_has_a_handler(tray) -> None:
    table = tray.dispatch_table()
    wanted = {"run_pause", "run_resume", "run_stop", "run_approve_show", "run_deny", "auto_run_now", "auto_pause", "auto_resume", "open_automations_observer"}
    assert wanted <= set(table)
    nodes = mm.build_menu(mm.MenuInputs(snap=tray._snap, apps=(), apps_fetched=True))
    assert {n.action[0] for n in mm.iter_nodes(nodes) if n.action} >= wanted | {"open_runs", "open_run_observer", "pause", "copy_address"}


def test_pause_resume_and_stop_go_through_the_run_command_route(tray, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.tray import app as tray_app

    tray.dispatch(("run_pause", "r"))
    assert tray.client.calls[-1] == ("command", "r", "pause", {})
    assert tray.events[-1][:2] == ("notify", "Pausing Refactor the sampler into two lanes")

    tray.dispatch(("run_resume", "p"))
    assert tray.client.calls[-1] == ("command", "p", "resume", {})

    # Stop asks first, naming the session; the cancel carries a reason.
    tray.dispatch(("run_stop", "r"))
    title, body, kw = tray.confirms[-1]
    assert title == "Stop Refactor the sampler into two lanes?" and kw["ok_label"] == "Stop" and kw["danger"] is True and "history stays" in body
    assert tray.client.calls[-1] == ("command", "r", "cancel", {"reason": "Stopped from the AbstractGateway menu"})
    assert tray.events[-1][1] == "Stopped Refactor the sampler into two lanes"
    # Cancelling the confirmation sends NOTHING.
    monkeypatch.setattr(tray_app.dialogs, "confirm", lambda title, body, **kw: False)
    tray.client.calls.clear()
    tray.dispatch(("run_stop", "r"))
    assert tray.client.calls == []

    # A refusal is shown with the gateway's words, never swallowed.
    tray.client.command = Result(False, 409, {"detail": {"reason_code": "invalid_state", "message": "'pause' does not apply to an automation"}}, error="invalid_state")
    tray.dispatch(("run_pause", "r"))
    assert tray.events[-1][0] == "info" and tray.events[-1][1] == "Couldn't pause Refactor the sampler into two lanes" and "does not apply" in tray.events[-1][2]


def test_show_and_approve_reads_the_wait_shows_the_dialog_and_answers_only_from_it(tray, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.tray import app as tray_app

    shown: List[tuple] = []
    answer = {"choice": 0}
    monkeypatch.setattr(tray_app.dialogs, "choose", lambda title, body, buttons, **kw: shown.append((title, body, tuple(buttons), kw)) or answer["choice"])

    # Approve: the wait is read in full, the dialog shows the arguments, the
    # resolution goes to the run that HOLDS the wait with its key.
    tray.dispatch(("run_approve_show", "ap"))
    assert tray.client.calls[0] == ("wait", "ap-sub")
    title, body, buttons, kw = shown[-1]
    assert title == "Approve execute_command?" and '"command": "rm -rf build"' in body and buttons == ("Approve", "Deny", "Cancel")
    assert kw == {"default_index": 2, "cancel_index": 2}, "Cancel is the default and the dismiss path"
    assert tray.client.calls[-1] == ("answer", "ap-sub", "tool_approval:k1", {"approved": True})
    assert tray.events[-1] == ("notify", "Approved execute_command", "Clean the build folder continues.")

    # Deny from the dialog.
    answer["choice"] = 1
    tray.client.calls.clear()
    tray.dispatch(("run_approve_show", "ap"))
    assert tray.client.calls[-1] == ("answer", "ap-sub", "tool_approval:k1", {"approved": False})

    # Cancel (or Esc): nothing is sent.
    answer["choice"] = 2
    tray.client.calls.clear()
    tray.dispatch(("run_approve_show", "ap"))
    assert tray.client.calls == [("wait", "ap-sub")]

    # The wait is gone by the time the dialog would open: say so, send nothing.
    tray.client.wait = Result(False, 409, {"detail": {"reason_code": "not_waiting", "message": "run 'ap-sub' is completed, not waiting"}}, error="not_waiting")
    tray.client.calls.clear()
    tray.dispatch(("run_approve_show", "ap"))
    assert tray.client.calls == [("wait", "ap-sub")] and tray.events[-1][0] == "info" and tray.events[-1][1] == "Couldn't read the approval"

    # No dialog facility: the two-step menu confirmation approves; nothing is sent until it is chosen.
    tray.client.wait = FakeClient().wait
    monkeypatch.setattr(tray_app.dialogs, "choose", lambda *a, **k: None)
    tray.client.calls.clear()
    tray.dispatch(("run_approve_show", "ap"))
    assert tray.client.calls == [("wait", "ap-sub")] and tray._pending and tray._pending["label"] == "Approve execute_command"
    tray._run_pending()
    assert tray.client.calls[-1] == ("answer", "ap-sub", "tool_approval:k1", {"approved": True})


def test_deny_from_the_row_and_the_question_row_never_answers(tray) -> None:
    tray.dispatch(("run_deny", "ap"))
    assert tray.client.calls == [("answer", "ap-sub", "tool_approval:k1", {"approved": False})]
    assert tray.events[-1] == ("notify", "Denied execute_command", "Clean the build folder continues.")
    # A question is answered in a client, not here; a stale deny is refused.
    tray.client.calls.clear()
    tray.dispatch(("run_deny", "q"))
    assert tray.client.calls == [] and tray.events[-1][:2] == ("info", "Nothing to deny")


def test_automation_actions_go_through_the_automation_commands_route(tray) -> None:
    tray.dispatch(("auto_run_now", "a1"))
    assert tray.client.calls[-1] == ("automation", "a1", "automation.run_now") and tray.events[-1][1] == "Running Daily digest now"
    tray.dispatch(("auto_pause", "a1"))
    assert tray.client.calls[-1] == ("automation", "a1", "automation.pause") and tray.events[-1][1] == "Paused Daily digest"
    tray.dispatch(("auto_resume", "a2"))
    assert tray.client.calls[-1] == ("automation", "a2", "automation.resume") and tray.events[-1][1] == "Resumed Inbox triage"
    tray.client.command = Result(False, 409, {"error": {"reason_code": "automation_busy", "message": "An occurrence is already running or waiting to run."}}, error="automation_busy")
    tray.dispatch(("auto_run_now", "a1"))
    assert tray.events[-1][0] == "info" and "already running" in tray.events[-1][2]

    # Open in Observer on a Scheduled row: the Automations page the gateway served.
    tray.dispatch(("open_automations_observer",))
    assert tray.client.calls[-1] == ("open", "observer", "/#automations") and tray.opened == ["/apps/handover/obs"]


def test_the_client_sends_the_bodies_the_routes_expect() -> None:
    """The HTTP bodies, pinned against the gateway's models (`SubmitCommandRequest`,
    `AutomationCommandBody` forbids extra fields)."""
    from abstractgateway.tray.client import GatewayClient

    sent: List[tuple] = []

    class _Client(GatewayClient):
        def _request(self, method, path, *, body=None, timeout=4.0, api=True):  # type: ignore[override]
            sent.append((method, path, body))
            return Result(True, 200, {"ok": True})

    c = _Client("http://127.0.0.1:1", "t")
    c.run_command("r1", "pause")
    c.answer_wait("r1-sub", "tool_approval:k1", {"approved": True})
    c.automation_command("a 1", "automation.run_now")
    c.run_wait("r1/sub")
    c.automations()
    assert sent[0][:2] == ("POST", "/commands") and sent[0][2]["run_id"] == "r1" and sent[0][2]["type"] == "pause" and sent[0][2]["payload"] == {} and sent[0][2]["command_id"].startswith("tray-pause-")
    assert sent[1][2]["type"] == "resume" and sent[1][2]["payload"] == {"wait_key": "tool_approval:k1", "payload": {"approved": True}}
    assert sent[2][:2] == ("POST", "/automations/a%201/commands") and set(sent[2][2]) == {"command_id", "type", "payload"} and sent[2][2]["type"] == "automation.run_now"
    assert sent[3][:2] == ("GET", "/host/runs/r1%2Fsub/wait")
    assert sent[4][:2] == ("GET", "/automations?limit=50")


def test_the_slow_lane_reads_the_automations_next_to_the_runs() -> None:
    from abstractgateway.tray.sampler import Sampler

    class _Client:
        def __init__(self) -> None:
            self.calls: List[str] = []

        def recent_runs(self, limit: int = 25) -> Result:
            self.calls.append("runs")
            return Result(True, 200, {"items": [{"run_id": "r", "status": "running", "activity": "running", "title": "T", "title_source": "prompt"}], "caller_plane": "default", "observer_automations_path": "/apps/observer/#automations"})

        def automations(self) -> Result:
            self.calls.append("automations")
            return Result(True, 200, {"items": [{"automation_id": "a1", "title": "Daily digest", "status": "active", "schedule_text": "Every day at 09:00", "capabilities": ["pause"]}]})

    client = _Client()
    s = Sampler(client)  # type: ignore[arg-type]
    s._sample_runs()
    snap = s.snapshot()
    assert client.calls == ["runs", "automations"]
    assert [r.title for r in snap.runs] == ["T"] and snap.observer_automations_path == "/apps/observer/#automations"
    assert [(a.automation_id, a.schedule_text) for a in snap.automations] == [("a1", "Every day at 09:00")] and snap.automations_error is None


def test_the_menu_signature_follows_the_automations_but_not_a_ticking_clock() -> None:
    from abstractgateway.tray.app import menu_signature

    def sig(**over: Any):
        return menu_signature(_snap(**over), update_phase="idle", pending=None, tk_available=False)

    assert sig(automations=(DAILY,)) != sig(automations=())
    assert sig(automations=(DAILY,)) != sig(automations=(dataclasses.replace(DAILY, status="paused"),))
    assert sig(automations=(DAILY,)) != sig(automations=(dataclasses.replace(DAILY, schedule_text="Every day at 09:00 · next Sat 18 Oct 09:00"),))
    assert sig(automations=(DAILY,)) == sig(automations=(dataclasses.replace(DAILY, next_run_local="whatever"),)), "the row shows schedule_text; next_run_local alone changes nothing a person sees"
    assert sig(runs=(RUNNING,)) == sig(runs=(dataclasses.replace(RUNNING, duration_s=131.0),))
    assert sig(automations_error=None) != sig(automations_error="HTTP 503")
