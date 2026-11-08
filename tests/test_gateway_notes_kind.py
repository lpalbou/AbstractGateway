"""AbstractNote's internal work is `notes`, never a conversation or a user automation (0.20.3).

Operator report 2026-11-07: AbstractCode's Conversations listed "Conversation note-wat…" sessions,
`GET /runs?root_only=true` returned mostly `abstractnote@0.3.x:note-drain` occurrences, and every
Automations list showed "Process my notes", "Reflect on my notes", "Consolidate my notes".

- a session whose turn root runs an `abstractnote` workflow (the bundle id, exact) is `notes`:
  `POST /runs/start` marks it, a one-time migration (`abstractnote_bundle_v1`) marks the existing
  ones, and the occurrence sessions Note's automations open are `notes` by their workflow;
- `GET /runs?root_only=true` leaves them out by default; `kind=notes` lists them; `kind=all` both;
- `GET /automations` leaves Note's automations out by default (and out of its counts);
  `kind=notes` lists them; `kind=all` both; every row carries `kind`;
- AbstractNote's own Processing page (`GET /note/processing`) still reads its automations.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, _run, chat_run, controller_run, gateway_env, save_runs

NOTE_DRAIN = "abstractnote@0.3.1:note-drain"
NOTE_CAPTURE = "abstractnote@0.3.0:note-capture"


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


def _stored(c: TestClient) -> dict:
    from abstractgateway.session_kinds import FILENAME

    path = c.data_dir / FILENAME  # type: ignore[attr-defined]
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"sessions": {}, "migrations": {}}


def _note_root(session_id: str, workflow_id: str = NOTE_CAPTURE, created_at: str = "2026-11-06T08:00:00+00:00"):
    return _run(run_id=str(uuid.uuid4()), workflow_id=workflow_id, vars={}, session_id=session_id, parent_run_id=None, created_at=created_at)


def _live(ctl):
    """A controller as the runtime keeps it between occurrences (not ended: active, or paused)."""
    from abstractruntime.core.models import RunStatus

    ctl.status = RunStatus.RUNNING
    ctl.vars["_meta"]["automation"]["trigger"]["config"]["anchor"] = ctl.created_at  # the schedule's grid
    return ctl


def _note_controller(title: str = "Process my notes", workflow_id: str = NOTE_DRAIN, **kw):
    ctl = _live(controller_run(title=title, **kw))
    bundle_ref, flow_id = workflow_id.split(":", 1)
    ctl.vars["_meta"]["automation"]["target"] = {"workflow_id": workflow_id, "bundle_ref": bundle_ref, "flow_id": flow_id, "input_data": {}}
    return ctl


def _drain_occurrence(automation_id: str, index: int, created_at: str = "2026-11-06T09:00:00+00:00"):
    """A fresh-context occurrence: its session is its own run id (the runtime's choice), never
    started through POST /runs/start, so never marked."""
    rid = str(uuid.uuid4())
    return _run(
        run_id=rid, workflow_id=NOTE_DRAIN, session_id=rid, parent_run_id=automation_id, created_at=created_at,
        vars={"_meta": {"occurrence": {"automation_id": automation_id, "occurrence_index": index, "attempt": 1, "event_id": f"e{index}",
                                        "revision": 1, "role": "occurrence", "session_kind": "occurrence", "fired_at": created_at,
                                        "trigger_envelope": {}}}},
    )


# ----------------------------------------------------------------------------- sessions


def test_note_sessions_leave_the_conversation_list_and_are_listed_as_notes(client: TestClient) -> None:
    ctl = _note_controller()
    drain = _drain_occurrence(ctl.run_id, 1)
    save_runs(chat_run(session_id="s-conv", created_at="2026-11-06T07:00:00+00:00"), _note_root("note-watch-1"), ctl, drain)

    default = _roots(client)
    assert {r["session_id"] for r in default["items"]} == {"s-conv"}, default["items"]
    assert all(r["kind"] == "conversation" for r in default["items"])
    assert _sessions(client, "&kind=conversation") == {"s-conv"}
    notes = _roots(client, "&kind=notes")
    assert {r["session_id"] for r in notes["items"]} == {"note-watch-1", drain.session_id}
    assert all(r["kind"] == "notes" for r in notes["items"])
    assert _sessions(client, "&kind=all") == {"s-conv", "note-watch-1", drain.session_id}
    # Still the one pool: a note session is readable by its id, with its kind.
    direct = client.get("/api/gateway/runs?session_id=note-watch-1&include_ledger_len=false", headers=HEADERS).json()
    assert direct["items"] and all(r["kind"] == "notes" for r in direct["items"])
    # An occurrence session is notes by its workflow: never written into the file (one per drain).
    assert drain.session_id not in _stored(client)["sessions"]


def test_a_note_run_started_without_kind_marks_its_session_notes(client: TestClient) -> None:
    body = {"bundle_id": "abstractnote", "flow_id": "note-capture", "session_id": "note-web-1", "input_data": {"text": "milk"}}
    client.post("/api/gateway/runs/start", headers=HEADERS, json=body)  # the mark is written before the run starts
    # A namespaced flow id names the bundle too.
    client.post("/api/gateway/runs/start", headers=HEADERS,
                json={"flow_id": "abstractnote@0.3.1:note-capture", "session_id": "note-watch-2", "input_data": {"text": "eggs"}})
    # A bundle whose name merely contains it is not AbstractNote's.
    client.post("/api/gateway/runs/start", headers=HEADERS,
                json={"bundle_id": "my-abstractnote", "flow_id": "root", "session_id": "s-other", "input_data": {}})
    sessions = _stored(client)["sessions"]
    assert sessions["note-web-1"]["kind"] == "notes" and sessions["note-watch-2"]["kind"] == "notes"
    assert "s-other" not in sessions


def test_kind_notes_is_accepted_and_validated(client: TestClient) -> None:
    r = client.post("/api/gateway/runs/start", headers=HEADERS,
                    json={"bundle_id": "abstractnote", "flow_id": "note-capture", "kind": "notes", "input_data": {}})
    assert r.status_code == 400 and "needs a session_id" in r.text
    r = client.post("/api/gateway/runs/start", headers=HEADERS,
                    json={"bundle_id": "abstractnote", "flow_id": "note-capture", "session_id": "s", "kind": "bogus", "input_data": {}})
    assert r.status_code == 400 and "kind must be one of conversation, docs, notes" in r.text
    r = client.get("/api/gateway/runs?root_only=true&kind=bogus", headers=HEADERS)
    assert r.status_code == 400 and "conversation|docs|notes|all" in r.text
    r = client.get("/api/gateway/sessions/search?q=milk&kind=notes", headers=HEADERS)
    assert r.status_code == 200, r.text


def test_a_search_runs_the_migration_first(client: TestClient) -> None:
    """A search may come before any listing after an upgrade: it marks the old note sessions too."""
    from abstractgateway.session_kinds import NOTES_MIGRATION

    save_runs(_note_root("note-watch-old"))
    r = client.get("/api/gateway/sessions/search?q=milk", headers=HEADERS)
    assert r.status_code == 200, r.text
    stored = _stored(client)
    assert NOTES_MIGRATION in stored["migrations"] and stored["sessions"]["note-watch-old"]["kind"] == "notes"


def test_the_host_run_list_leaves_note_work_out(client: TestClient) -> None:
    """The tray's Conversations and Automations menus read `GET /host/runs` (every plane)."""
    ctl = _note_controller()
    drain = _drain_occurrence(ctl.run_id, 1)
    conv = chat_run(session_id="s-conv", created_at="2026-11-06T07:00:00+00:00")
    save_runs(conv, _note_root("note-watch-1"), ctl, drain)
    window = "&window_hours=100000"
    default = client.get(f"/api/gateway/host/runs?limit=50{window}", headers=HEADERS)
    assert default.status_code == 200, default.text
    assert {r["session_id"] for r in default.json()["items"]} == {"s-conv"}
    assert all(r["kind"] == "conversation" for r in default.json()["items"])
    notes = client.get(f"/api/gateway/host/runs?limit=50&kind=notes{window}", headers=HEADERS).json()
    assert {r["session_id"] for r in notes["items"]} == {"note-watch-1", drain.session_id}
    assert all(r["kind"] == "notes" for r in notes["items"])
    every = client.get(f"/api/gateway/host/runs?limit=50&kind=all{window}", headers=HEADERS).json()
    assert {r["session_id"] for r in every["items"]} == {"s-conv", "note-watch-1", drain.session_id}
    assert client.get("/api/gateway/host/runs?kind=bogus", headers=HEADERS).status_code == 400


def test_existing_note_sessions_are_migrated_once_by_bundle_id(client: TestClient) -> None:
    import base64

    from abstractgateway.session_kinds import DOCS_QA_MIGRATION, NOTES_MIGRATION, mark_session_kind

    b64 = lambda s: base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")  # noqa: E731
    catalog_wid = f"__catalog__v2__tenant_catalog__{b64('default')}__{b64('abstractnote')}@0.3.0:note-capture"
    mark_session_kind(client.data_dir, "s-docs", "docs")  # type: ignore[attr-defined]
    save_runs(
        chat_run(session_id="s-conv"),
        _note_root("note-watch-old"),                                     # a private-scope note capture
        _note_root("note-catalog-old", catalog_wid),                      # a catalog-scope one
        _note_root("s-named", "my-abstractnote@1.0.0:root"),              # another bundle whose name merely contains it
        _note_root("s-docs", "abstractnote@0.3.0:note-capture"),          # already marked: never overwritten
    )
    assert _sessions(client) == {"s-conv", "s-named"}
    assert _sessions(client, "&kind=notes") == {"note-watch-old", "note-catalog-old"}
    stored = _stored(client)
    assert stored["migrations"][NOTES_MIGRATION]["marked"] == 2
    assert DOCS_QA_MIGRATION in stored["migrations"]  # both pending migrations ran in the one scan
    assert stored["sessions"]["note-watch-old"] == {**stored["sessions"]["note-watch-old"], "kind": "notes", "by": NOTES_MIGRATION}
    assert stored["sessions"]["s-docs"]["kind"] == "docs"
    # Once: the file is not rewritten by later listings, and a later unmarked capture is not
    # marked by a re-scan (it is still listed as notes, by its workflow).
    path = client.data_dir / "session_kinds.json"  # type: ignore[attr-defined]
    before = path.read_bytes()
    save_runs(_note_root("note-late"))
    assert "note-late" in _sessions(client, "&kind=notes")
    assert path.read_bytes() == before


def test_the_migration_is_idempotent_at_the_module_level(tmp_path: Path) -> None:
    from abstractgateway.session_kinds import NOTES_MIGRATION, migrate_sessions, pending_migrations, session_kinds

    rows = [
        {"session_id": "note-a", "workflow_id": NOTE_CAPTURE, "parent_run_id": None},
        {"session_id": "occ-1", "workflow_id": NOTE_DRAIN, "parent_run_id": "ctl-1"},  # an occurrence: notes by workflow, no mark
        {"session_id": "s-conv", "workflow_id": "basic-agent@1.0.0:root", "parent_run_id": None},
    ]
    assert NOTES_MIGRATION in pending_migrations(tmp_path)
    assert migrate_sessions(tmp_path, rows)[NOTES_MIGRATION] == 1
    assert pending_migrations(tmp_path) == ()
    assert migrate_sessions(tmp_path, rows + [{"session_id": "note-b", "workflow_id": NOTE_CAPTURE}]) == {}
    assert session_kinds(tmp_path) == {"note-a": "notes"}


# ----------------------------------------------------------------------------- automations


def _automations(c: TestClient, query: str = "") -> dict:
    r = c.get(f"/api/gateway/automations?limit=200{query}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def test_note_automations_leave_the_default_automation_list(client: TestClient) -> None:
    mine = _live(controller_run(title="Memory monitor"))
    process = _note_controller("Process my notes")
    reflect = _note_controller("Reflect on my notes", "abstractnote@0.3.1:note-reflect")
    consolidate = _note_controller("Consolidate my notes", "abstractnote@0.3.1:note-consolidate")
    consolidate.vars["_runtime"]["automation"]["paused"] = True
    save_runs(mine, process, reflect, consolidate)

    default = _automations(client)
    assert [i["title"] for i in default["items"]] == ["Memory monitor"]
    assert default["items"][0]["kind"] == "user"
    assert default["paused_automations_count"] == 0  # Note's paused one is not counted in the user's list
    assert [i["automation_id"] for i in _automations(client, "&kind=user")["items"]] == [mine.run_id]
    notes = _automations(client, "&kind=notes")
    assert {i["title"] for i in notes["items"]} == {"Process my notes", "Reflect on my notes", "Consolidate my notes"}
    assert all(i["kind"] == "notes" for i in notes["items"]) and notes["paused_automations_count"] == 1
    assert len(_automations(client, "&kind=all")["items"]) == 4
    # Readable by id, as AbstractNote reads them.
    one = client.get(f"/api/gateway/automations/{process.run_id}", headers=HEADERS)
    assert one.status_code == 200 and one.json()["summary"]["kind"] == "notes"
    bad = client.get("/api/gateway/automations?kind=bogus", headers=HEADERS)
    assert bad.status_code == 422 and "user|notes|all" in bad.text


def test_the_note_processing_page_still_reads_its_automations(client: TestClient) -> None:
    pytest.importorskip("abstractnote.gateway.router", reason="abstractnote is not installed")
    from abstractnote.gateway.router import close_notebooks, notebook_for

    process = _note_controller("Process my notes")
    save_runs(process)
    try:
        notebook_for(client.data_dir).set_meta("processor_automation_id", process.run_id)  # type: ignore[attr-defined]
        r = client.get("/api/gateway/note/processing", headers=HEADERS)
        assert r.status_code == 200, r.text
        processor = r.json()["processor"]
        assert processor["id"] == process.run_id and processor["state"] == "active", processor
        assert processor["target"] == NOTE_DRAIN
    finally:
        close_notebooks()
