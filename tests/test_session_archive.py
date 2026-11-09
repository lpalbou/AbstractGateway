"""GATEWAY ARCHIVE CONTRACT (round 5): archive / unarchive a session.

- `POST /sessions/{id}/archive|unarchive` marks the session in the caller's plane;
  runs, ledgers and the session's own listing are untouched (history kept);
- `GET /runs?root_only=true` leaves archived sessions out, `archived_only=true`
  lists only them (`archived: true`), a `session_id` filter always reads its
  session, and every listing carries `archived_sessions`;
- another account's session answers 404 (planes are per principal);
- every call is audited; an unreadable archive file is a 500, never "nothing archived".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, chat_run, gateway_env, save_runs


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


def test_archive_hides_a_session_from_the_default_list_and_keeps_everything(client: TestClient) -> None:
    a1 = chat_run(session_id="s-arch", created_at="2026-10-01T08:00:00+00:00")
    a2 = chat_run(session_id="s-arch", created_at="2026-10-01T09:00:00+00:00")
    b1 = chat_run(session_id="s-keep", created_at="2026-10-01T10:00:00+00:00")
    save_runs(a1, a2, b1)
    before = _roots(client)
    assert {r["session_id"] for r in before["items"]} == {"s-arch", "s-keep"} and before["archived_sessions"] == 0

    r = client.post("/api/gateway/sessions/s-arch/archive", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == "s-arch" and body["archived"] is True and body["changed"] is True and body["archived_at"]

    page = _roots(client)
    assert [r["session_id"] for r in page["items"]] == ["s-keep"] and page["archived_sessions"] == 1
    arch = _roots(client, "&archived_only=true")
    assert sorted(r["run_id"] for r in arch["items"]) == sorted([a1.run_id, a2.run_id])
    assert all(r["archived"] is True and r["archived_at"] == body["archived_at"] for r in arch["items"])
    assert arch["archived_sessions"] == 1
    # An archived session is still readable by its id, with every run.
    direct = client.get("/api/gateway/runs?session_id=s-arch&include_ledger_len=false", headers=HEADERS).json()
    assert sorted(r["run_id"] for r in direct["items"]) == sorted([a1.run_id, a2.run_id])
    assert all(r["archived"] is True for r in direct["items"])
    # Runs untouched.
    from abstractgateway.service import get_gateway_service

    assert get_gateway_service().host.run_store.load(a1.run_id) is not None

    again = client.post("/api/gateway/sessions/s-arch/archive", headers=HEADERS).json()
    assert again["changed"] is False and again["archived_at"] == body["archived_at"]

    r = client.post("/api/gateway/sessions/s-arch/unarchive", headers=HEADERS)
    assert r.status_code == 200 and r.json()["archived"] is False and r.json()["changed"] is True
    page = _roots(client)
    assert {r["session_id"] for r in page["items"]} == {"s-arch", "s-keep"} and page["archived_sessions"] == 0
    assert all("archived" not in r for r in page["items"])
    assert client.post("/api/gateway/sessions/s-arch/unarchive", headers=HEADERS).json()["changed"] is False

    # History kept: both acts recorded in the plane's archive file, and audited.
    from abstractgateway.session_archive import FILENAME

    stored = json.loads((client.data_dir / FILENAME).read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert [h["event"] for h in stored["history"]] == ["archived", "unarchived"] and stored["sessions"] == {}
    audit = [json.loads(line) for line in (client.data_dir / "audit_log.jsonl").read_text(encoding="utf-8").splitlines()]  # type: ignore[attr-defined]
    events = [(e["event"], e.get("changed")) for e in audit if str(e.get("event", "")).startswith("session.")]
    assert events == [("session.archived", True), ("session.archived", False), ("session.unarchived", True), ("session.unarchived", False)]


def test_an_unknown_session_is_404_and_lists_refuse_nothing_new(client: TestClient) -> None:
    for verb in ("archive", "unarchive"):
        r = client.post(f"/api/gateway/sessions/no-such/{verb}", headers=HEADERS)
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["reason_code"] == "session_not_found"
    assert not (client.data_dir / "session_archive.json").exists()  # type: ignore[attr-defined]


def test_an_unreadable_archive_is_an_error_not_an_empty_archive(client: TestClient) -> None:
    save_runs(chat_run(session_id="s1"))
    (client.data_dir / "session_archive.json").write_text("{not json", encoding="utf-8")  # type: ignore[attr-defined]
    r = client.get("/api/gateway/runs?root_only=true&include_ledger_len=false", headers=HEADERS)
    assert r.status_code == 500 and r.json()["detail"]["reason_code"] == "session_archive_unreadable"
    r = client.post("/api/gateway/sessions/s1/archive", headers=HEADERS)
    assert r.status_code == 500 and r.json()["detail"]["reason_code"] == "session_archive_unreadable"


@pytest.fixture()
def two_users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from automations_fixtures import write_echo_bundle

    bundles = tmp_path / "bundles"
    ref = write_echo_bundle(bundles)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles))
    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    from abstractgateway.service import reset_gateway_boot_state
    from abstractgateway.users import GatewayUserRegistry

    reset_gateway_boot_state()
    reg = GatewayUserRegistry()
    _a, alice = reg.create_user(user_id="alice", roles=["user"])
    _b, bob = reg.create_user(user_id="bob", roles=["user"])
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c, ref, {"Authorization": f"Bearer {alice}"}, {"Authorization": f"Bearer {bob}"}


def test_only_the_owner_plane_can_archive_a_session(two_users) -> None:
    from automations_fixtures import ECHO_FLOW_ID, wait_until

    c, ref, alice, bob = two_users
    body = {"request_id": "r", "title": "Mine", "target": {"bundle_ref": ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "p"}},
            "trigger": {"source_id": "manual", "source_version": 1, "config": {}}}
    aid = c.post("/api/gateway/automations", headers=alice, json=body).json()["automation_id"]
    assert c.post(f"/api/gateway/automations/{aid}/commands", headers=alice, json={"command_id": "go", "type": "automation.run_now"}).status_code == 200

    # The occurrence is an automation session (backlog 1166): the plain default lists
    # conversations only, so the turn listing asks for both purposes.
    listing = "/api/gateway/runs?root_only=true&include_ledger_len=false&kind=conversation,automation"

    def a_turn():
        items = c.get(listing, headers=alice).json()["items"]
        return items[0] if items else None

    sid = wait_until(a_turn, timeout_s=20)["session_id"]
    r = c.post(f"/api/gateway/sessions/{sid}/archive", headers=bob)
    assert r.status_code == 404 and r.json()["detail"]["reason_code"] == "session_not_found"
    assert c.get(listing, headers=bob).json()["archived_sessions"] == 0
    assert c.post(f"/api/gateway/sessions/{sid}/archive", headers=alice).status_code == 200
    mine = c.get(listing, headers=alice).json()
    assert mine["items"] == [] and mine["archived_sessions"] == 1


def _helper_in_session(session_id: str, tmp_path: Path):
    """A dev server an agent started in this conversation: registered where AbstractRuntime's local_helper_start
    puts it (the session scope of the calling run, `helper_scope`; the start itself is the runtime's own test)."""
    import os
    import subprocess
    import sys
    import tempfile
    import time

    from abstractruntime.integrations.abstractcore.local_helper_tools import (
        LocalHelperProcess,
        get_local_helper_registry,
        helper_scope,
        namespaced_helper_id,
    )

    from abstractruntime.integrations.abstractcore.local_helper_tools import register_session_scope

    from abstractgateway.service import get_gateway_service

    # This hermetic host serves no tool-running workflow, so its runtime was built without the factory's
    # helper wiring (a tools/LLM host registers it in register_shell_session_teardown): register it here.
    register_session_scope(get_gateway_service().host.runtime)
    turn = chat_run(session_id=session_id, created_at="2026-11-09T08:00:00+00:00")
    save_runs(turn)
    scope = helper_scope(turn.run_id)
    assert scope.startswith("session:") and scope.endswith(f":{session_id}"), scope
    log = tempfile.NamedTemporaryFile(mode="w+", suffix=".log", delete=False, dir=tmp_path)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=(os.name == "posix"))
    get_local_helper_registry().put(LocalHelperProcess(
        key=namespaced_helper_id(scope, "main"), helper_id="main", process=proc, log_path=Path(log.name), log_handle=log,
        working_directory=None, ready_port=None, ready_url=None, ready_text=None, local_url=None, created_at=time.time()))
    return proc.pid, None


def _gone(pid: int) -> bool:
    import os
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        time.sleep(0.05)
    return False


def test_archiving_a_conversation_stops_the_helpers_its_agent_started(client: TestClient, tmp_path: Path) -> None:
    """Helpers outlive each turn (operator decision 2026-11-09), never the conversation: archiving it stops them."""
    import sys

    if sys.platform.startswith("win"):
        pytest.skip("POSIX process groups")
    pid, _port = _helper_in_session("s-dev", tmp_path)
    r = client.post("/api/gateway/sessions/s-dev/archive", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["helpers_stopped"] == 1
    assert _gone(pid), "the conversation's dev server outlived its archive"


def test_the_gateway_stopping_stops_every_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    if sys.platform.startswith("win"):
        pytest.skip("POSIX process groups")
    gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    with TestClient(app):
        pid, _port = _helper_in_session("s-left-open", tmp_path)
    assert _gone(pid), "a dev server outlived the gateway"
