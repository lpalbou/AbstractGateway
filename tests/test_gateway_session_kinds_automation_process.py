"""Session kinds `automation` and `process` (backlog 1166, operator 2026-11-08 11:45).

- an automation's occurrence sessions are `automation`: out of the default conversation list,
  listed by `kind=automation`, by `kind=all`, by a comma list, AND by the attribution ask the
  Observer and Code already send (`session_kind=automation,occurrence`), whatever `kind` says;
- AbstractNote's processor/reflect/consolidate occurrences are `process` (exact flow ids); its
  captures and drains stay `notes`;
- `POST /runs/start` accepts `kind: "process"` / `"automation"` with a session (the phone and
  watch probes send `process`) and marks the session before the run starts;
- a one-time migration (`process_sessions_v1`, `automation_sessions_v1`) marks existing sessions
  once, recorded in the marks file like `docs_qa_bundle_v1`;
- every row carries `kind` and `session_kind`; `/host/runs` keeps automation sessions by default
  and leaves processes out, like notes.
Removing the mapping (`workflow_kind`) turns these red: the rows fall back to `conversation`.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, _run, chat_run, controller_run, gateway_env, occurrence_run, save_runs

pytestmark = pytest.mark.basic

NOTE_PROCESS = "abstractnote@0.2.0:note-process"
NOTE_DRAIN = "abstractnote@0.2.0:note-drain"
NOTE_CAPTURE = "abstractnote@0.2.0:note-capture"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.data_dir = data_dir  # type: ignore[attr-defined]
        yield c


def _roots(c: TestClient, query: str = "") -> dict:
    r = c.get(f"/api/gateway/runs?root_only=true&include_ledger_len=false{query}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _sessions(c: TestClient, query: str = "") -> set:
    return {r["session_id"] for r in _roots(c, query)["items"]}


def _note_occurrence(controller_id: str, index: int, workflow_id: str):
    occ = occurrence_run(controller_id, index=index, session_id=f"note-occ:{index}:{workflow_id.rsplit(':', 1)[1]}")
    occ.workflow_id = workflow_id
    return occ


def _seed() -> dict:
    """One conversation, one user automation with two occurrences, Note's automation with a
    process occurrence and a drain occurrence, and a Note capture started by a person."""
    ctl = controller_run(title="Monitor")
    occ1 = occurrence_run(ctl.run_id, index=1, session_id=f"occ-session:{ctl.run_id}:1")
    occ2 = occurrence_run(ctl.run_id, index=2, session_id=f"occ-session:{ctl.run_id}:2")
    note_ctl = controller_run(title="Note processing")
    note_proc = _note_occurrence(note_ctl.run_id, 1, NOTE_PROCESS)
    note_drain = _note_occurrence(note_ctl.run_id, 2, NOTE_DRAIN)
    capture = _run(run_id=str(uuid.uuid4()), workflow_id=NOTE_CAPTURE, vars={}, session_id="note-watch-1", parent_run_id=None)
    conv = chat_run(session_id="s-conv")
    save_runs(conv, ctl, occ1, occ2, note_ctl, note_proc, note_drain, capture)
    return {"occ": {occ1.session_id, occ2.session_id}, "process": {note_proc.session_id}, "drain": note_drain.session_id}


def test_automation_occurrences_are_automation_sessions_not_conversations(client: TestClient) -> None:
    seeded = _seed()
    # The default list is conversations: no occurrence, no note, no process.
    assert _sessions(client) == {"s-conv"}
    default_rows = _roots(client)["items"]
    assert all(r["kind"] == "conversation" for r in default_rows)
    # By purpose.
    assert _sessions(client, "&kind=automation") == seeded["occ"]
    rows = _roots(client, "&kind=automation")["items"]
    assert all(r["kind"] == "automation" and r["session_kind"] == "occurrence" for r in rows), rows
    assert _sessions(client, "&kind=process") == seeded["process"]
    assert _sessions(client, "&kind=notes") == {"note-watch-1", seeded["drain"]}, "captures and drains stay notes"
    assert _sessions(client, "&kind=conversation,automation") == {"s-conv", *seeded["occ"]}
    assert _sessions(client, "&kind=all") == {"s-conv", *seeded["occ"], *seeded["process"], "note-watch-1", seeded["drain"]}
    # The attribution ask the Observer and Code send today lists the occurrences whatever `kind` says.
    # (Note's own occurrences are notes/processes, not automation sessions: they stay out.)
    assert _sessions(client, "&session_kind=automation,occurrence") == seeded["occ"]
    assert _sessions(client, "&kind=conversation&session_kind=automation,occurrence") == seeded["occ"]
    # Unknown or empty kinds refuse in words.
    r = client.get("/api/gateway/runs?root_only=true&kind=bogus", headers=HEADERS)
    assert r.status_code == 400 and "automation or process" in r.text
    assert client.get("/api/gateway/runs?root_only=true&kind=automation,bogus", headers=HEADERS).status_code == 400


def test_note_process_occurrences_are_processes_by_exact_flow_id(client: TestClient) -> None:
    from abstractgateway.session_kinds import kind_of, start_kind, workflow_kind

    assert workflow_kind(NOTE_PROCESS, "occurrence") == "process"
    assert workflow_kind("abstractnote@0.2.0:note-reflect") == "process"
    assert workflow_kind("abstractnote@0.2.0:note-consolidate", "occurrence") == "process"
    assert workflow_kind(NOTE_DRAIN, "occurrence") == "notes"
    assert workflow_kind(NOTE_CAPTURE) == "notes"
    assert workflow_kind("my-abstractnote@1.0.0:note-process") is None, "exact bundle id, never text"
    assert workflow_kind("bundle-target@0.0.0:root", "occurrence") == "automation"
    assert workflow_kind("bundle-target@0.0.0:root", "automation") == "automation"
    assert workflow_kind("bundle-target@0.0.0:root", "discussion") is None, "a discussion is a conversation"
    assert workflow_kind("docs-qa@0.1.1:docsqa001") == "docs"
    assert kind_of({}, "s", "bundle-target@0.0.0:root", "occurrence") == "automation"
    assert kind_of({"s": "docs"}, "s", "bundle-target@0.0.0:root", "occurrence") == "docs", "a mark wins"
    assert start_kind("abstractnote", "note-process") == "process"
    assert start_kind("abstractnote", "note-capture") == "notes"
    assert start_kind(None, "abstractnote@0.2.0:note-reflect") == "process"
    assert start_kind("docs-qa", "docsqa001") == "docs"


def test_runs_start_accepts_process_and_automation_and_marks_the_session(client: TestClient) -> None:
    from abstractgateway.session_kinds import FILENAME

    base = {"registry_scope": "tenant_catalog", "bundle_id": "docs-qa", "flow_id": "docsqa001", "input_data": {"prompt": "x", "docs": "# D", "app": "AbstractAssistant"}}
    # A watch voice probe: a process, marked before the run starts.
    client.post("/api/gateway/runs/start", headers=HEADERS, json={**base, "session_id": "session_memory_watch-voice-probe-1", "kind": "process"})
    client.post("/api/gateway/runs/start", headers=HEADERS, json={**base, "session_id": "auto-made:1", "kind": "automation"})
    stored = json.loads((client.data_dir / FILENAME).read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert stored["sessions"]["session_memory_watch-voice-probe-1"]["kind"] == "process"
    assert stored["sessions"]["auto-made:1"]["kind"] == "automation"
    # The validation names the whole set; a non-conversation kind still needs a session.
    r = client.post("/api/gateway/runs/start", headers=HEADERS, json={**base, "session_id": "s", "kind": "bogus"})
    assert r.status_code == 400 and "kind must be one of conversation, docs, notes, automation, process" in r.text
    r = client.post("/api/gateway/runs/start", headers=HEADERS, json={**base, "kind": "process"})
    assert r.status_code == 400 and "needs a session_id" in r.text
    # The mark classifies the session's turns: listed under its purpose, never as a conversation
    # (a turn row of that session, saved here because the scratch gateway has no model to run one).
    save_runs(chat_run(session_id="session_memory_watch-voice-probe-1"), chat_run(session_id="auto-made:1"))
    assert _sessions(client).isdisjoint({"session_memory_watch-voice-probe-1", "auto-made:1"})
    assert _sessions(client, "&kind=process") == {"session_memory_watch-voice-probe-1"}
    assert _sessions(client, "&kind=automation") == {"auto-made:1"}


def test_existing_automation_and_process_sessions_are_migrated_once(client: TestClient) -> None:
    from abstractgateway.session_kinds import AUTOMATION_MIGRATION, FILENAME, PROCESS_MIGRATION

    seeded = _seed()
    _roots(client)  # the first listing runs the pending migrations
    stored = json.loads((client.data_dir / FILENAME).read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert stored["migrations"][AUTOMATION_MIGRATION]["marked"] == 2
    assert stored["migrations"][PROCESS_MIGRATION]["marked"] == 1
    for sid in seeded["occ"]:
        assert stored["sessions"][sid] == {**stored["sessions"][sid], "kind": "automation", "by": AUTOMATION_MIGRATION}
    (proc_sid,) = seeded["process"]
    assert stored["sessions"][proc_sid]["kind"] == "process" and stored["sessions"][proc_sid]["by"] == PROCESS_MIGRATION
    # A Note drain occurrence is notes by its workflow at read time (it has a parent: the notes
    # migration marks only door-started roots), so the file never grows by one entry per drain.
    assert seeded["drain"] not in stored["sessions"]
    assert seeded["drain"] in _sessions(client, "&kind=notes")
    # Idempotent: a later occurrence is derived at read time, never re-scanned into the file.
    ctl = controller_run(title="Later")
    late = occurrence_run(ctl.run_id, index=1, session_id=f"occ-session:{ctl.run_id}:1")
    save_runs(ctl, late)
    assert late.session_id in _sessions(client, "&kind=automation")
    again = json.loads((client.data_dir / FILENAME).read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert again["migrations"] == stored["migrations"] and late.session_id not in again["sessions"]


def test_host_runs_keep_automations_and_leave_processes_out(client: TestClient) -> None:
    seeded = _seed()
    window = "&window_hours=100000"
    default = client.get(f"/api/gateway/host/runs?limit=50{window}", headers=HEADERS).json()
    assert {r["session_id"] for r in default["items"]} == {"s-conv", *seeded["occ"]}
    assert {r["kind"] for r in default["items"]} == {"conversation", "automation"}
    procs = client.get(f"/api/gateway/host/runs?limit=50&kind=process{window}", headers=HEADERS).json()
    assert {r["session_id"] for r in procs["items"]} == seeded["process"]
    autos = client.get(f"/api/gateway/host/runs?limit=50&kind=automation{window}", headers=HEADERS).json()
    assert {r["session_id"] for r in autos["items"]} == seeded["occ"]
    every = client.get(f"/api/gateway/host/runs?limit=50&kind=all{window}", headers=HEADERS).json()
    assert {r["session_id"] for r in every["items"]} == {"s-conv", *seeded["occ"], *seeded["process"], "note-watch-1", seeded["drain"]}
