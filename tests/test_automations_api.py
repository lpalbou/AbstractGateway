"""Automations v1 gateway façade (contract F).

PART 1 (this file today): the error envelope on the automation paths, the
trigger-sources route, and the automation attribution of `GET /runs` rows.
PART 2 routes (create/list/get/patch/commands/occurrences/discuss/attention)
are added when AbstractRuntime's `abstractruntime.automations.service` lands.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import (
    HEADERS,
    chat_run,
    controller_run,
    discussion_run,
    gateway_env,
    legacy_schedule_run,
    occurrence_run,
    save_runs,
)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, origins="http://allowed.example")
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c


def _envelope(r) -> dict:
    body = r.json()
    assert set(body) == {"detail"}, body
    detail = body["detail"]
    assert isinstance(detail, dict), body
    assert isinstance(detail.get("reason_code"), str) and isinstance(detail.get("message"), str), body
    assert set(detail) <= {"reason_code", "message", "field", "command_id"}, body
    return detail


# ----------------------------------------------------------------- envelope


@pytest.mark.parametrize("path", ["/api/gateway/automations/abc/seen", "/api/gateway/trigger-sources"])
def test_401_without_token_carries_the_envelope(client: TestClient, path: str) -> None:
    r = client.post(path, json={"attention_cursor": "att1:0"}) if path.endswith("/seen") else client.get(path)
    assert r.status_code == 401, r.text
    assert _envelope(r)["reason_code"] == "unauthorized"


def test_403_from_the_auth_layer_carries_the_envelope(client: TestClient) -> None:
    r = client.get("/api/gateway/trigger-sources", headers={**HEADERS, "Origin": "http://evil.example"})
    assert r.status_code == 403, r.text
    assert _envelope(r)["reason_code"] == "forbidden"


def test_admin_required_rejection_is_enveloped_on_automation_paths() -> None:
    """The auth layer's admin-required 403 body (`_reject_forbidden_route`)
    is rewritten on the automation paths — no automation route requires admin
    today, so this drives the middleware with that exact payload."""
    from abstractgateway.automation_errors import AutomationErrorEnvelopeMiddleware

    payload = {"detail": "Admin principal required", "reason_code": "admin_required", "required_role": "admin"}

    async def inner(scope, receive, send):
        body = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    def run(path: str):
        sent: list = []

        async def send(message):
            sent.append(message)

        asyncio.run(AutomationErrorEnvelopeMiddleware(inner)({"type": "http", "path": path}, None, send))
        return sent[0]["status"], json.loads(sent[-1]["body"])

    assert run("/api/gateway/automations") == (403, {"detail": {"reason_code": "forbidden", "message": "Admin principal required"}})
    # Scoped: other paths keep their own error shape.
    assert run("/api/gateway/runs") == (403, payload)
    assert run("/api/gateway/automationsX") == (403, payload)


def test_malformed_json_and_validation_errors_are_invalid_request(client: TestClient) -> None:
    controller = controller_run()
    save_runs(controller)
    url = f"/api/gateway/automations/{controller.run_id}/seen"
    r = client.post(url, headers={**HEADERS, "content-type": "application/json"}, content=b"{not json")
    assert r.status_code == 422, r.text
    assert _envelope(r)["reason_code"] == "invalid_request"

    r = client.post(url, headers=HEADERS, json={})
    assert r.status_code == 422
    detail = _envelope(r)
    assert detail["reason_code"] == "invalid_request" and detail["field"] == "attention_cursor"

    r = client.post(url, headers=HEADERS, json={"attention_cursor": "att1:0", "extra": 1})
    assert r.status_code == 422 and _envelope(r)["field"] == "extra"


def test_unknown_automation_is_404_automation_not_found(client: TestClient) -> None:
    r = client.post("/api/gateway/automations/nope/seen", headers=HEADERS, json={"attention_cursor": "att1:0"})
    assert r.status_code == 404
    assert _envelope(r)["reason_code"] == "automation_not_found"


def test_other_routes_keep_their_error_shape(client: TestClient) -> None:
    r = client.get("/api/gateway/runs?bogus=1", headers=HEADERS)
    assert r.status_code == 400
    assert isinstance(r.json()["detail"], str)
    r = client.get("/api/gateway/runs")
    assert r.status_code == 401 and isinstance(r.json()["detail"], str)


# ----------------------------------------------------------- trigger sources


def test_trigger_sources_lists_the_registry(client: TestClient) -> None:
    r = client.get("/api/gateway/trigger-sources", headers=HEADERS)
    assert r.status_code == 200, r.text
    items = {(i["id"], i["version"]): i for i in r.json()["items"]}
    schedule = items[("schedule", 1)]
    assert schedule["available"] is True
    assert schedule["capabilities"] == {"kind": "time"}
    assert schedule["config_schema"]["properties"]["every"]["format"] == "duration"
    assert items[("manual", 1)]["capabilities"] == {"kind": "manual"}


def test_trigger_sources_reports_a_broken_third_party_source(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractruntime.triggers.registry as registry

    real = registry.trigger_sources()
    broken = {"descriptor": None, "name": "webhook", "available": False, "unavailable_reason": "ImportError: no module"}
    monkeypatch.setattr(registry, "trigger_sources", lambda: real + [broken])
    items = client.get("/api/gateway/trigger-sources", headers=HEADERS).json()["items"]
    assert {"id": "webhook", "available": False, "unavailable_reason": "ImportError: no module"} in items


def test_trigger_sources_fail_loudly_when_a_builtin_is_broken(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractruntime.triggers.registry as registry

    def boom():
        raise registry.TriggerRegistryError("built-in trigger source 'schedule' is missing")

    monkeypatch.setattr(registry, "trigger_sources", boom)
    r = client.get("/api/gateway/trigger-sources", headers=HEADERS)
    assert r.status_code == 500
    assert _envelope(r)["reason_code"] == "internal_error"


# ------------------------------------------------------------ /runs rows


def _seed_runs():
    ctrl = controller_run(session_id="auto-s", created_at="2026-09-27T09:00:00+00:00")
    occ1 = occurrence_run(ctrl.run_id, index=1, session_id="auto-s", session_kind="automation", created_at="2026-09-27T09:10:00+00:00")
    desc = occurrence_run(ctrl.run_id, index=1, session_id="auto-s", session_kind="automation", role="descendant",
                          parent_run_id=occ1.run_id, created_at="2026-09-27T09:11:00+00:00")
    chat = chat_run(session_id="chat-s", created_at="2026-09-27T08:00:00+00:00")
    chat_child = chat_run(session_id="chat-s", parent_run_id=chat.run_id, created_at="2026-09-27T08:01:00+00:00")
    disc = discussion_run(ctrl.run_id, index=1, created_at="2026-09-27T09:30:00+00:00")
    legacy = legacy_schedule_run(created_at="2026-09-27T07:00:00+00:00")
    save_runs(ctrl, occ1, desc, chat, chat_child, disc, legacy)
    return ctrl, occ1, desc, chat, chat_child, disc, legacy


def _rows(client: TestClient, query: str) -> dict:
    r = client.get(f"/api/gateway/runs?limit=50&include_ledger_len=false&{query}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return {row["run_id"]: row for row in r.json()["items"]}


def test_runs_rows_carry_automation_attribution(client: TestClient) -> None:
    ctrl, occ1, desc, chat, chat_child, disc, legacy = _seed_runs()
    rows = _rows(client, "")
    assert {k: rows[ctrl.run_id][k] for k in ("session_kind", "automation_id", "role", "occurrence_index")} == {
        "session_kind": "automation", "automation_id": ctrl.run_id, "role": "controller", "occurrence_index": None}
    assert {k: rows[occ1.run_id][k] for k in ("session_kind", "automation_id", "role", "occurrence_index")} == {
        "session_kind": "automation", "automation_id": ctrl.run_id, "role": "occurrence", "occurrence_index": 1}
    assert rows[desc.run_id]["role"] == "descendant"
    assert rows[chat.run_id]["session_kind"] == "chat" and rows[chat.run_id]["role"] is None
    assert rows[disc.run_id]["session_kind"] == "discussion" and rows[disc.run_id]["automation_id"] == ctrl.run_id
    # The index-row bug: a legacy scheduled wrapper listed through the index read as not scheduled.
    assert rows[legacy.run_id]["is_scheduled"] is True
    assert rows[legacy.run_id]["role"] == "legacy_schedule"
    assert rows[legacy.run_id]["session_kind"] == "automation" and rows[legacy.run_id]["legacy"] is True
    assert rows[chat.run_id]["legacy"] is False and rows[occ1.run_id]["legacy"] is False
    assert rows[chat.run_id]["is_scheduled"] is False


def test_root_only_returns_turn_roots(client: TestClient) -> None:
    ctrl, occ1, desc, chat, chat_child, disc, legacy = _seed_runs()
    rows = _rows(client, "root_only=true")
    # Occurrences ARE turns (children of the controller); controllers, descendants, sub-runs are not.
    assert occ1.run_id in rows and chat.run_id in rows and disc.run_id in rows and legacy.run_id in rows
    assert ctrl.run_id not in rows and desc.run_id not in rows and chat_child.run_id not in rows
    session = _rows(client, "root_only=true&session_id=auto-s")
    assert set(session) == {occ1.run_id}


def test_session_kind_filter(client: TestClient) -> None:
    ctrl, occ1, desc, chat, chat_child, disc, legacy = _seed_runs()
    rows = _rows(client, "root_only=true&session_kind=chat,discussion")
    assert set(rows) == {chat.run_id, disc.run_id}
    rows = _rows(client, "session_kind=automation")
    assert set(rows) == {ctrl.run_id, occ1.run_id, desc.run_id, legacy.run_id}
    r = client.get("/api/gateway/runs?session_kind=chat,bogus", headers=HEADERS)
    assert r.status_code == 400 and "bogus" in r.json()["detail"]


def test_children_listing_carries_attribution(client: TestClient) -> None:
    ctrl, occ1, *_ = _seed_runs()
    r = client.get(f"/api/gateway/runs?parent_run_id={ctrl.run_id}&include_ledger_len=false", headers=HEADERS)
    assert r.status_code == 200, r.text
    (row,) = [i for i in r.json()["items"] if i["run_id"] == occ1.run_id]
    assert row["role"] == "occurrence" and row["occurrence_index"] == 1


def test_history_bloc_turns_are_the_runtime_turn_roots(client: TestClient) -> None:
    """The session history bloc reads turns through AbstractRuntime's
    `select_session_turns`: an automation session's occurrences (child runs of
    the controller) are its turns; the controller and descendants are not."""
    from abstractgateway.service import get_gateway_service
    from abstractgateway.session_history_bloc import list_session_root_turns

    ctrl, occ1, desc, *_ = _seed_runs()
    occ2a = occurrence_run(ctrl.run_id, index=2, session_id="auto-s", session_kind="automation", created_at="2026-09-27T09:20:00+00:00")
    # A retried occurrence is ONE turn: its newest attempt.
    occ2 = occurrence_run(ctrl.run_id, index=2, session_id="auto-s", session_kind="automation", created_at="2026-09-27T09:25:00+00:00", attempt=2)
    save_runs(occ2a, occ2)
    turns = list_session_root_turns(get_gateway_service().runner.run_store, "auto-s")
    assert [t["run_id"] for t in turns] == [occ2.run_id, occ1.run_id]
    assert turns[0]["role"] == "occurrence" and turns[0]["occurrence_index"] == 2

    r = client.get("/api/gateway/sessions/auto-s/history/bloc?limit=10", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert [t["run_id"] for t in r.json()["turns"]] == [occ2.run_id, occ1.run_id]


# =====================================================================
# PART 2: the live façade (real runtime seams, real runner, echo target)
# =====================================================================

from automations_fixtures import ECHO_FLOW_ID, wait_until, write_echo_bundle  # noqa: E402


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, runner=True)
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c


def _create(c: TestClient, *, request_id: str = "r1", mode: str = "independent", notify: bool = False,
            trigger: dict | None = None, prompt: str = "hello") -> dict:
    body = {
        "request_id": request_id,
        "title": f"Echo {request_id}",
        "target": {"bundle_ref": c.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": prompt, "notify": notify}},
        "trigger": trigger or {"source_id": "manual", "source_version": 1, "config": {}},
        "context": {"mode": mode},
    }
    r = c.post("/api/gateway/automations", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _command(c: TestClient, aid: str, typ: str, cid: str, payload: dict | None = None) -> dict:
    r = c.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": cid, "type": typ, "payload": payload or {}})
    assert r.status_code == 200, r.text
    return r.json()


def _occurrences(c: TestClient, aid: str) -> list:
    r = c.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _run_now_and_wait(c: TestClient, aid: str, cid: str, *, index: int) -> dict:
    _command(c, aid, "automation.run_now", cid)

    def done():
        rows = [o for o in _occurrences(c, aid) if o["index"] == index]
        return rows[0] if rows and rows[0]["status"] in ("completed", "failed") else None

    return wait_until(done, timeout_s=20)


def test_create_is_idempotent_and_conflicts_on_a_different_request(live: TestClient) -> None:
    first = _create(live)
    again = _create(live)
    assert again["automation_id"] == first["automation_id"] and again["revision"] == 1
    s = first["summary"]
    assert s["legacy"] is False and s["session_kind"] == "automation" and s["status"] == "active"
    assert s["capabilities"] == ["revise", "pause", "resume", "run_now", "stop_current", "archive", "discuss"]
    assert s["attention"] == {"pending_waits": 0, "unread": False, "unseen_count": 0, "cursor": "att1:0", "items": [], "waits": []}
    r = live.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": "r1", "title": "Something else",
        "target": {"bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}}})
    assert r.status_code == 409 and _envelope(r)["reason_code"] == "identity_conflict"


@pytest.mark.parametrize("body, status, reason", [
    ({"trigger": {"source_id": "webhook", "source_version": 1, "config": {}}}, 422, "unknown_trigger_source"),
    ({"trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": "2x"}}}, 422, "invalid_definition"),
    ({"context": {"mode": "growing", "growing": {"summary": {"enabled": True, "every_n": 1, "max_tokens": 10}}}}, 422, "unsupported_feature"),
    ({"target": {"bundle_ref": "nope@1.0.0", "flow_id": "echo"}}, 422, "invalid_definition"),
    ({"target": {"flow_id": "@default"}}, 422, "invalid_definition"),
])
def test_create_refusals(live: TestClient, body: dict, status: int, reason: str) -> None:
    base = {"request_id": "bad", "title": "Bad", "target": {"bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID},
            "trigger": {"source_id": "manual", "source_version": 1, "config": {}}}
    r = live.post("/api/gateway/automations", headers=HEADERS, json={**base, **body})
    assert r.status_code == status, r.text
    assert _envelope(r)["reason_code"] == reason


def test_run_now_occurrence_rows_and_quiet_attention(live: TestClient) -> None:
    aid = _create(live)["automation_id"]
    row = _run_now_and_wait(live, aid, "c1", index=1)
    assert row["status"] == "completed" and row["attempts"] == 1
    assert row["trigger"] == {"source_id": "manual", "summary": "manual: run now (c1)"}
    assert row["user_turn"].startswith("[Trigger manual@1 · occurrence 1 · fired ") and row["user_turn"].endswith("\nhello")
    assert row["answer"].startswith("ECHO[0] [Trigger manual@1")
    assert row["notify"] is None and "failure" not in row
    assert row["ledger_url"] == f"/api/gateway/runs/{row['run_id']}/ledger"
    detail = live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()
    assert detail["definition"]["revision"] == 1 and detail["active_revision"] == 1
    last = detail["summary"]["last_occurrence"]
    assert last["index"] == 1 and last["excerpt"] == row["answer"][:280]
    # Quiet: no unread.
    assert detail["summary"]["attention"]["unread"] is False
    # The occurrence is a turn of /runs?root_only=true (it is the controller's child).
    rows = _rows(live, "root_only=true")
    assert row["run_id"] in rows and rows[row["run_id"]]["role"] == "occurrence"
    assert aid not in rows


def test_notify_marks_unread_and_seen_acknowledges(live: TestClient) -> None:
    aid = _create(live, notify=True)["automation_id"]
    _run_now_and_wait(live, aid, "c1", index=1)
    summary = live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]
    att = summary["attention"]
    assert att["unread"] is True and att["unseen_count"] == 1 and att["cursor"] == "att1:1"
    (item,) = att["items"]
    assert item["kind"] == "notify" and item["title"] == "Echo notify" and item["cursor"] == "att1:1"
    page = live.get(f"/api/gateway/automations/{aid}/attention", headers=HEADERS).json()
    assert page == {"items": [item], "next_cursor": None}
    assert live.post(f"/api/gateway/automations/{aid}/seen", headers=HEADERS, json={"attention_cursor": "att1:1"}).json() == {"attention_cursor": "att1:1"}
    att = live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]["attention"]
    assert att["unread"] is False and att["unseen_count"] == 0 and att["items"] == []
    assert live.get(f"/api/gateway/automations/{aid}/attention", headers=HEADERS).json()["items"] == []


def test_growing_vs_independent_history(live: TestClient) -> None:
    ind = _create(live, request_id="ind")["automation_id"]
    gro = _create(live, request_id="gro", mode="growing")["automation_id"]
    _run_now_and_wait(live, ind, "i1", index=1)
    second_ind = _run_now_and_wait(live, ind, "i2", index=2)
    _run_now_and_wait(live, gro, "g1", index=1)
    second_gro = _run_now_and_wait(live, gro, "g2", index=2)
    assert second_ind["answer"].startswith("ECHO[0]")          # fresh every time
    assert second_gro["answer"].startswith("ECHO[2]")          # sees turn 1 + answer 1
    assert "assistant:ECHO[0]" in second_gro["answer"]
    # The occurrence run records its history window (ADR-0026: explicit and
    # observable; operator ruling 2026-09-28: the most recent 50k tokens).
    from abstractgateway.service import get_gateway_service

    note = get_gateway_service().host.run_store.load(second_gro["run_id"]).vars["_runtime"]["session_history"]
    assert note["max_tokens"] == 50_000 and note["policy"] == "most_recent_whole_turns"
    assert note["replayed_messages"] == 2 and note["dropped_messages"] == 0 and note["session_kind"] == "automation"


def test_commands_door_revision_and_patch(live: TestClient) -> None:
    aid = _create(live)["automation_id"]
    r = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS,
                   json={"command_id": "p1", "expected_revision": 7, "changes": {"title": "New"}})
    assert r.status_code == 409 and _envelope(r) == {"reason_code": "revision_conflict", "message": "Automation is at revision 1, not 7.", "field": "expected_revision", "command_id": "p1"}
    r = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS,
                   json={"command_id": "p2", "expected_revision": 1, "changes": {"trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": "nope"}}}})
    assert r.status_code == 422 and _envelope(r)["reason_code"] == "invalid_definition"
    receipt = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS,
                         json={"command_id": "p3", "expected_revision": 1, "changes": {"title": "Renamed"}}).json()
    assert receipt["accepted"] is True and receipt["duplicate"] is False and receipt["command_id"] == "p3"
    dup = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS,
                     json={"command_id": "p3", "expected_revision": 1, "changes": {"title": "Renamed"}}).json()
    assert dup["duplicate"] is True and dup["seq"] == receipt["seq"]
    wait_until(lambda: live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]["revision"] == 2)
    detail = live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()
    assert detail["definition"]["title"] == "Renamed" and detail["summary"]["revision"] == 2
    # pause -> paused; resume -> active
    _command(live, aid, "automation.pause", "k1")
    wait_until(lambda: live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]["status"] == "paused")
    _command(live, aid, "automation.resume", "k2")
    wait_until(lambda: live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]["status"] == "active")
    r = live.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": "k3", "type": "pause"})
    assert r.status_code == 422 and _envelope(r)["field"] == "type"
    r = live.post("/api/gateway/automations/nope/commands", headers=HEADERS, json={"command_id": "k4", "type": "automation.pause"})
    assert r.status_code == 404 and _envelope(r)["reason_code"] == "automation_not_found"


def test_a_retried_revise_after_it_was_applied_is_a_duplicate_not_a_conflict(live: TestClient) -> None:
    """A client that lost the first receipt retries the same PATCH after the
    runner already applied it (revision 1 -> 2): the retry is the duplicate
    receipt of the first, never 409 revision_conflict for its own revise. (The
    commands-door test hit this as a timing flake whenever the runner won.)"""
    aid = _create(live)["automation_id"]
    body = {"command_id": "r1", "expected_revision": 1, "changes": {"title": "Renamed"}}
    receipt = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS, json=body).json()
    assert receipt["duplicate"] is False
    wait_until(lambda: live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]["revision"] == 2)
    retry = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS, json=body)
    assert retry.status_code == 200, retry.text
    assert retry.json()["duplicate"] is True and retry.json()["seq"] == receipt["seq"]
    # A NEW command with the stale revision is still a conflict.
    stale = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS, json={**body, "command_id": "r2"})
    assert stale.status_code == 409 and _envelope(stale)["reason_code"] == "revision_conflict"
    # The same id for a different change is still an identity conflict.
    other = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS, json={**body, "changes": {"title": "Other"}})
    assert other.status_code == 409 and _envelope(other)["reason_code"] == "identity_conflict"


def test_list_pages_status_filter_changed_since_and_legacy(live: TestClient) -> None:
    ids = [_create(live, request_id=f"L{i}")["automation_id"] for i in range(3)]
    legacy = legacy_schedule_run()
    save_runs(legacy)
    seen: list = []
    cursor = None
    while True:
        q = "limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = live.get(f"/api/gateway/automations?{q}", headers=HEADERS).json()
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert [s["automation_id"] for s in seen if not s["legacy"]] == list(reversed(ids))
    (leg,) = [s for s in seen if s["legacy"]]
    assert leg["automation_id"] == legacy.run_id and leg["capabilities"] == ["legacy"] and leg["revision"] is None
    assert leg["trigger"] == {"binding_id": legacy.run_id, "source_id": "schedule", "source_version": 1, "config": {"every": "2m"}}
    assert leg["session_kind"] == "automation"
    r = live.get("/api/gateway/automations?changed_since=2026-01-01T00:00:00Z", headers=HEADERS)
    assert r.status_code == 422 and _envelope(r)["reason_code"] == "unsupported_feature"
    r = live.get("/api/gateway/automations?status=bogus", headers=HEADERS)
    assert r.status_code == 422 and _envelope(r)["field"] == "status"
    r = live.get("/api/gateway/automations?status=cancelled", headers=HEADERS)   # not an automation status
    assert r.status_code == 422 and _envelope(r)["field"] == "status"
    r = live.get("/api/gateway/automations?cursor=garbage", headers=HEADERS)
    assert r.status_code == 422 and _envelope(r)["reason_code"] == "invalid_request"
    assert all(s["status"] == "paused" for s in live.get("/api/gateway/automations?status=paused", headers=HEADERS).json()["items"])
    r = live.get(f"/api/gateway/automations/{legacy.run_id}", headers=HEADERS)
    assert r.status_code == 404 and _envelope(r)["reason_code"] == "automation_not_found"


def test_trigger_defaults_from_the_published_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_env(monkeypatch, tmp_path, runner=True)
    defaults = {"schema_version": 1, "title": "From defaults", "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
                "context": {"mode": "growing"}, "input_data": {"prompt": "default prompt"}}
    ref = write_echo_bundle(tmp_path / "bundles", automation_defaults=defaults)
    from abstractgateway.app import app

    with TestClient(app) as c:
        r = c.post("/api/gateway/automations", headers=HEADERS, json={"request_id": "d1", "target": {"bundle_ref": ref, "flow_id": ECHO_FLOW_ID}})
        assert r.status_code == 200, r.text
        aid = r.json()["automation_id"]
        definition = c.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]
        assert definition["title"] == "From defaults" and definition["context"]["mode"] == "growing"
        assert definition["target"]["input_data"]["prompt"] == "default prompt"
        assert definition["trigger"]["source_id"] == "manual"


def test_discussion_is_a_read_only_fork_and_later_turns_are_restamped(live: TestClient) -> None:
    aid = _create(live, mode="growing")["automation_id"]
    _run_now_and_wait(live, aid, "c1", index=1)
    r = live.post(f"/api/gateway/automations/{aid}/discuss", headers=HEADERS,
                  json={"request_id": "d1", "occurrence_index": 1, "prompt": "why?"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["session_kind"] == "discussion" and out["session_id"].startswith("discussion-session:")
    again = live.post(f"/api/gateway/automations/{aid}/discuss", headers=HEADERS,
                      json={"request_id": "d1", "occurrence_index": 1, "prompt": "why?"})
    assert again.status_code == 200 and again.json() == out            # same request: same discussion
    clash = live.post(f"/api/gateway/automations/{aid}/discuss", headers=HEADERS,
                      json={"request_id": "d1", "occurrence_index": 1, "prompt": "something else"})
    assert clash.status_code == 409 and _envelope(clash)["reason_code"] == "identity_conflict"
    from abstractgateway.service import get_gateway_service

    store = get_gateway_service().host.run_store
    wait_until(lambda: (store.load(out["run_id"]).status.value == "completed"))
    first = store.load(out["run_id"])
    answer1 = first.output["result"]["response"]
    assert answer1.startswith("ECHO[") and not answer1.startswith("ECHO[0]") and " why? || " in answer1   # seeded
    automation_ws = live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]["workspace_root"]
    import os

    assert first.vars["workspace_root"] == out["workspace_root"] != automation_ws
    assert first.vars["_runtime"]["workspace_read_only_paths"] == [os.path.realpath(automation_ws)]

    # A later turn through the ordinary door, claiming a writable workspace: re-stamped.
    r = live.post("/api/gateway/runs/start", headers=HEADERS, json={
        "bundle_id": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "session_id": out["session_id"],
        "input_data": {"prompt": "and then?", "workspace_read_only": False, "_meta": {"discussion": {"automation_id": "forged"}}}})
    assert r.status_code == 200, r.text
    second_id = r.json()["run_id"]
    wait_until(lambda: store.load(second_id).status.value == "completed")
    second = store.load(second_id)
    assert second.vars["_runtime"]["workspace_read_only_paths"] == [os.path.realpath(automation_ws)]
    assert "workspace_read_only" not in second.vars
    assert second.vars["_meta"]["discussion"]["automation_id"] == aid
    assert second.vars["_meta"]["discussion"]["discussion_root_run_id"] == out["run_id"]
    assert second.vars["workspace_root"] == first.vars["workspace_root"]
    # It sees the seed AND the first discussion turn.
    assert "why?" in second.output["result"]["response"]
    # Strict seeding records its window receipt in the run (ADR-0026: stated,
    # never silent), with the session kind it seeded for.
    note = second.vars["_runtime"]["session_history"]
    assert note["strict"] is True and note["session_kind"] == "discussion"
    assert note["seeded"] == note["replayed_messages"] > 0 and note["max_tokens"] == 50_000
    # The automation's own session is untouched: still one occurrence turn.
    assert [t for t in _rows(live, f"root_only=true&session_kind=automation").values() if t["automation_id"] == aid and t["role"] == "occurrence"]
    rows = _rows(live, "root_only=true&session_kind=chat,discussion")
    assert out["run_id"] in rows and second_id in rows
    # Unknown occurrence.
    r = live.post(f"/api/gateway/automations/{aid}/discuss", headers=HEADERS, json={"request_id": "d2", "occurrence_index": 9, "prompt": "x"})
    assert r.status_code == 404 and _envelope(r)["reason_code"] == "occurrence_not_found"


def test_scheduled_ticks_are_driven_by_the_ordinary_runner(live: TestClient) -> None:
    """No gateway scheduler: the controller's WAIT_EVENT deadline is picked up
    by the runner's due-wait scan like any wait, and occurrences follow."""
    aid = _create(live, request_id="sched", trigger={"source_id": "schedule", "source_version": 1, "config": {"every": "1s"}})["automation_id"]
    rows = wait_until(lambda: (lambda r: r if len(r) >= 2 and all(o["status"] == "completed" for o in r[:2]) else None)(_occurrences(live, aid)), timeout_s=20)
    assert rows[0]["index"] > rows[1]["index"]                         # newest first
    assert rows[0]["trigger"]["summary"].startswith("schedule: every second (UTC), tick ")
    _command(live, aid, "automation.pause", "pause")
    wait_until(lambda: live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]["status"] == "paused")
    import time

    time.sleep(0.5)
    count = len(_occurrences(live, aid))
    time.sleep(2.5)
    assert len(_occurrences(live, aid)) == count                      # paused: no scheduled admission


def test_runner_records_a_host_failure_before_advancing(live: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractruntime.automations.commands as commands

    aid = _create(live, request_id="boom")["automation_id"]

    def explode(*_a, **_k):
        raise RuntimeError("host exploded")

    monkeypatch.setattr(commands, "apply_automation_command", explode)
    _command(live, aid, "automation.pause", "explodes")
    from abstractgateway.service import get_gateway_service
    from abstractruntime.automations.ledger import find_by_idempotency_key, record_key, record_payload

    ledger = get_gateway_service().host.ledger_store

    def recorded():
        rec = find_by_idempotency_key(ledger, aid, record_key("automation.command_result", aid, "explodes"))
        return record_payload(rec) if rec is not None else None

    payload = wait_until(recorded, timeout_s=10)
    assert payload["status"] == "rejected"
    assert payload["error"]["reason_code"] == "internal_error" and "host exploded" in payload["error"]["message"]


def test_a_discussion_turn_without_readable_history_is_refused(live: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Strict seeding (contract C3): never the unseeded fallback for a discussion session."""
    aid = _create(live, mode="growing")["automation_id"]
    _run_now_and_wait(live, aid, "c1", index=1)
    out = live.post(f"/api/gateway/automations/{aid}/discuss", headers=HEADERS,
                    json={"request_id": "d1", "occurrence_index": 1, "prompt": "why?"}).json()
    import abstractruntime.session_history as sh

    real = sh.session_chat_messages

    def unreadable(*args, **kwargs):
        if kwargs.get("strict"):
            raise sh.SessionHistoryError("seed unresolvable")
        return real(*args, **kwargs)

    monkeypatch.setattr(sh, "session_chat_messages", unreadable)
    r = live.post("/api/gateway/runs/start", headers=HEADERS, json={
        "bundle_id": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "session_id": out["session_id"], "input_data": {"prompt": "again"}})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["reason_code"] == "history_unavailable"


# ------------------------------------------------ E2E defects D2 / D4 / D5 / D6


def _create_flow(c: TestClient, flow_id: str, request_id: str) -> str:
    r = c.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": request_id, "title": request_id, "target": {"bundle_ref": c.bundle_ref, "flow_id": flow_id},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}}})
    assert r.status_code == 200, r.text
    return r.json()["automation_id"]


def test_d2_waiting_only_on_a_person(live: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A root parked on its subworkflow (like an Agent node) is RUNNING; only a
    wait on a person (typed pending wait) reads `waiting`."""
    import threading

    slow = _create_flow(live, "slow", "slow")
    _command(live, slow, "automation.run_now", "s1")
    row = wait_until(lambda: (lambda r: r[0] if r and r[0]["status"] not in ("admitted",) else None)(_occurrences(live, slow)), timeout_s=10)
    from abstractgateway.service import get_gateway_service

    # The controller records `automation.dispatched` (the occurrence reads
    # "running" with its run id) BEFORE the effect creates that run, so the
    # run can be absent from the store for a moment: that window is the CI
    # flake (`load()` returned None -> AttributeError). It is forced here on
    # the test's own first look, then the wait reads "not created yet" as
    # "not waiting yet" instead of crashing.
    store = get_gateway_service().host.run_store
    real_load = store.load
    test_thread = threading.get_ident()
    hidden_once = {row["run_id"]}

    def _load(run_id, *a, **k):  # noqa: ANN001
        if threading.get_ident() == test_thread and run_id in hidden_once:
            hidden_once.discard(run_id)
            return None
        return real_load(run_id, *a, **k)

    monkeypatch.setattr(store, "load", _load)

    def _run_status(run_id: str):
        run = get_gateway_service().host.run_store.load(run_id)
        return run.status.value if run is not None else None

    wait_until(lambda: _run_status(row["run_id"]) == "waiting", timeout_s=10)
    assert not hidden_once  # the absent-run window was exercised
    row = _occurrences(live, slow)[0]
    assert row["status"] == "running" and row["waits"] == [], row
    assert live.get(f"/api/gateway/automations/{slow}", headers=HEADERS).json()["summary"]["last_occurrence"]["status"] == "running"

    ask = _create_flow(live, "ask", "ask")
    _command(live, ask, "automation.run_now", "a1")
    row = wait_until(lambda: (lambda r: r[0] if r and r[0]["waits"] else None)(_occurrences(live, ask)), timeout_s=10)
    assert row["status"] == "waiting" and row["waits"][0]["kind"] == "ask_user"
    assert live.get(f"/api/gateway/automations/{ask}", headers=HEADERS).json()["summary"]["last_occurrence"]["status"] == "waiting"


def test_d4_old_occurrences_keep_the_cadence_they_ran_under(live: TestClient) -> None:
    aid = _create(live, request_id="cad", trigger={"source_id": "schedule", "source_version": 1, "config": {"every": "1s"}})["automation_id"]
    wait_until(lambda: (lambda r: r if r and r[-1]["status"] == "completed" else None)(_occurrences(live, aid)), timeout_s=20)
    r = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS, json={
        "command_id": "rev", "changes": {"trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": "3m"}}}})
    assert r.status_code == 200, r.text
    wait_until(lambda: live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]["revision"] == 2)
    rows = _occurrences(live, aid)
    assert rows and all(o["trigger"]["summary"].startswith("schedule: every second (UTC)") for o in rows), [o["trigger"] for o in rows]


def test_d5_door_refuses_what_the_state_rules_out(live: TestClient) -> None:
    slow = _create_flow(live, "slow", "busy")
    _command(live, slow, "automation.run_now", "b1")
    wait_until(lambda: _occurrences(live, slow))
    r = live.post(f"/api/gateway/automations/{slow}/commands", headers=HEADERS, json={"command_id": "b2", "type": "automation.run_now"})
    assert r.status_code == 409 and _envelope(r)["reason_code"] == "automation_busy" and _envelope(r)["command_id"] == "b2"
    # A retry of a command the runtime already decided is still a duplicate receipt, not a refusal.
    retry = live.post(f"/api/gateway/automations/{slow}/commands", headers=HEADERS, json={"command_id": "b1", "type": "automation.run_now"})
    assert retry.status_code == 200 and retry.json()["duplicate"] is True

    aid = _create(live, request_id="states")["automation_id"]
    r = live.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": "r0", "type": "automation.resume"})
    assert r.status_code == 409 and _envelope(r)["reason_code"] == "invalid_state"
    r = live.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": "s0", "type": "automation.stop_current"})
    assert r.status_code == 409 and _envelope(r)["reason_code"] == "invalid_state"
    _command(live, aid, "automation.pause", "p1")
    wait_until(lambda: live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]["status"] == "paused")
    r = live.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": "p2", "type": "automation.pause"})
    assert r.status_code == 409 and _envelope(r)["reason_code"] == "invalid_state"
    _command(live, aid, "automation.archive", "x1")
    wait_until(lambda: live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]["status"] == "archived")
    for typ in ("automation.run_now", "automation.resume", "automation.pause"):
        r = live.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": f"after-{typ}", "type": typ})
        assert r.status_code == 409 and _envelope(r)["reason_code"] == "invalid_state", (typ, r.text)
    r = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS, json={"command_id": "rv", "changes": {"title": "late"}})
    assert r.status_code == 409 and _envelope(r)["reason_code"] == "invalid_state"


def test_d6_legacy_rows_have_a_binding_and_a_last_occurrence(live: TestClient) -> None:
    legacy = legacy_schedule_run(created_at="2026-09-27T07:00:00+00:00")
    first = chat_run(session_id=legacy.run_id, parent_run_id=legacy.run_id, created_at="2026-09-27T07:01:00+00:00")
    second = chat_run(session_id=legacy.run_id, parent_run_id=legacy.run_id, created_at="2026-09-27T07:03:00+00:00")
    second.output = {"success": True, "result": {"response": "legacy answer"}}
    save_runs(legacy, first, second)
    (row,) = [s for s in live.get("/api/gateway/automations", headers=HEADERS).json()["items"] if s["legacy"]]
    assert row["trigger"]["binding_id"] == legacy.run_id
    last = row["last_occurrence"]
    assert last["run_id"] == second.run_id and last["index"] == 2 and last["status"] == "completed"
    assert last["excerpt"] == "legacy answer" and last["notify"] is None and last["fired_at"] == second.created_at
    assert row["occurrence_count"] == 2


def test_d3_a_retried_occurrence_is_one_turn(live: TestClient) -> None:
    r = live.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": "retry", "title": "retry", "target": {"bundle_ref": live.bundle_ref, "flow_id": "fail"},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
        "policy": {"retry": {"max_attempts": 2, "backoff": {"initial": "1s", "factor": 1, "max": "1s"}}}})
    assert r.status_code == 200, r.text
    aid = r.json()["automation_id"]
    _command(live, aid, "automation.run_now", "go")
    row = wait_until(lambda: (lambda o: o[0] if o and o[0]["status"] == "failed" else None)(_occurrences(live, aid)), timeout_s=30)
    assert row["attempts"] == 2 and row["failure"]["attempts"] == 2
    children = live.get(f"/api/gateway/runs?parent_run_id={aid}&include_ledger_len=false", headers=HEADERS).json()["items"]
    attempts = [c for c in children if c["role"] == "occurrence" and c["occurrence_index"] == 1]
    assert len(attempts) == 2 and len({c["session_id"] for c in attempts}) == 1
    session = attempts[0]["session_id"]
    turns = _rows(live, f"root_only=true&session_id={session}")
    assert list(turns) == [row["run_id"]]   # one turn: the last attempt
    bloc = live.get(f"/api/gateway/sessions/{session}/history/bloc?limit=10", headers=HEADERS).json()["turns"]
    assert [t["run_id"] for t in bloc] == [row["run_id"]]


# ------------------------------------------------------ reviews 46 / 47 / 49


def test_p2_1_client_server_keys_never_reach_the_definition(live: TestClient) -> None:
    body = {
        "request_id": "crafted", "title": "crafted",
        "target": {"bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {
            "prompt": "p",
            "workspace_read_only": False,
            "_meta": {"automation": {"title": "FAKE"}, "discussion": {"automation_id": "x"}, "creation_digest": "sha256:0"},
            "_runtime": {"workspace_read_only": False, "tool_policy": {"auto_approve_tools": ["execute_command"]}, "allowed_tools": ["read_file"]},
        }},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
    }
    r = live.post("/api/gateway/automations", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    aid = r.json()["automation_id"]
    data = live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]["target"]["input_data"]
    assert "_meta" not in data and "workspace_read_only" not in data
    assert data["_runtime"] == {"allowed_tools": ["read_file"]}
    row = _run_now_and_wait(live, aid, "c1", index=1)
    assert _rows(live, "")[row["run_id"]]["role"] == "occurrence"
    assert live.get("/api/gateway/automations", headers=HEADERS).status_code == 200


def test_p2_1_a_malformed_row_is_skipped_not_a_500(live: TestClient) -> None:
    good = _create(live, request_id="good")["automation_id"]
    broken = controller_run()
    broken.vars.pop("_runtime")          # a controller row without its state
    save_runs(broken)
    r = live.get("/api/gateway/automations", headers=HEADERS)
    assert r.status_code == 200, r.text
    ids = [s["automation_id"] for s in r.json()["items"]]
    assert good in ids and broken.run_id not in ids


@pytest.mark.parametrize("typ", ["pause", "resume", "cancel", "conclude", "update_schedule", "inject_guidance", "compact_memory"])
def test_g1_run_commands_are_refused_on_an_automation_root(live: TestClient, typ: str) -> None:
    aid = _create(live, request_id=f"g1-{typ}")["automation_id"]
    r = live.post("/api/gateway/commands", headers=HEADERS, json={"command_id": f"g1-{typ}", "run_id": aid, "type": typ, "payload": {}})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["reason_code"] == "invalid_state"


def test_g2_a_failed_host_lookup_is_recorded(live: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.service import get_gateway_service
    from abstractruntime.automations.ledger import find_by_idempotency_key, record_key, record_payload

    aid = _create(live, request_id="g2")["automation_id"]
    svc = get_gateway_service()

    def lookup_fails(run_id):
        raise KeyError(f"Workflow for {run_id} not registered")

    monkeypatch.setattr(svc.host, "runtime_and_workflow_for_run", lookup_fails)
    _command(live, aid, "automation.pause", "g2-pause")
    rec = wait_until(lambda: find_by_idempotency_key(svc.host.ledger_store, aid, record_key("automation.command_result", aid, "g2-pause")), timeout_s=10)
    payload = record_payload(rec)
    assert payload["status"] == "rejected" and payload["error"]["reason_code"] == "internal_error"


def test_g2_an_unrecordable_failure_keeps_the_cursor(live: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    import abstractruntime.automations.commands as commands
    from abstractgateway.service import get_gateway_service
    from abstractruntime.automations.ledger import find_by_idempotency_key, record_key

    aid = _create(live, request_id="g2b")["automation_id"]
    svc = get_gateway_service()

    def boom(*_a, **_k):
        raise RuntimeError("host down")

    monkeypatch.setattr(svc.host, "runtime_and_workflow_for_run", boom)
    monkeypatch.setattr(commands, "record_automation_command_result", boom)
    receipt = _command(live, aid, "automation.pause", "g2b-pause")
    time.sleep(1.0)
    assert svc.runner._cursor_store.load() < receipt["seq"]          # not passed
    monkeypatch.undo()
    wait_until(lambda: find_by_idempotency_key(svc.host.ledger_store, aid, record_key("automation.command_result", aid, "g2b-pause")), timeout_s=10)
    wait_until(lambda: svc.runner._cursor_store.load() >= receipt["seq"], timeout_s=10)


def test_a49_2_event_answers_are_wrapped_in_payload() -> None:
    from types import SimpleNamespace

    from abstractruntime.core.models import RunState, RunStatus, WaitReason, WaitState
    from fastapi import HTTPException

    from abstractgateway.routes.gateway import _check_automation_wait_answer

    run = RunState(run_id="r1", workflow_id="w", status=RunStatus.WAITING, current_node="n",
                   vars={"_meta": {"occurrence": {"automation_id": "a", "occurrence_index": 1, "role": "occurrence"}}},
                   waiting=WaitState(reason=WaitReason.EVENT, wait_key="evt:1", prompt="Pick one"))
    svc = SimpleNamespace(runner=SimpleNamespace(run_store=SimpleNamespace(load=lambda _rid: run)))
    _check_automation_wait_answer(svc, run_id="r1", command_payload={"wait_key": "evt:1", "payload": {"payload": {"choice": "a"}}})
    for bad in ({"choice": "a"}, {"response": "a"}, {"payload": "a"}):
        with pytest.raises(HTTPException) as err:
            _check_automation_wait_answer(svc, run_id="r1", command_payload={"wait_key": "evt:1", "payload": bad})
        assert err.value.status_code == 422 and err.value.detail["field"] == "payload"


def test_a_reused_command_id_for_another_command_is_an_identity_conflict(live: TestClient) -> None:
    from abstractgateway.service import get_gateway_service

    aid = _create(live, request_id="ids")["automation_id"]
    store = get_gateway_service().host.run_store
    assert store.load(aid).actor_id == "gateway"                      # owned from creation
    _command(live, aid, "automation.pause", "same-id")
    wait_until(lambda: live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]["status"] == "paused")
    r = live.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": "same-id", "type": "automation.archive"})
    assert r.status_code == 409 and _envelope(r) == {
        "reason_code": "identity_conflict", "message": _envelope(r)["message"], "field": "command_id", "command_id": "same-id"}
    dup = live.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": "same-id", "type": "automation.pause"})
    assert dup.status_code == 200 and dup.json()["duplicate"] is True


def test_summary_rows_carry_the_definitions_workspace_root(live: TestClient) -> None:
    aid = _create(live, request_id="ws-row")["automation_id"]
    definition = live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]
    rows = {s["automation_id"]: s for s in live.get("/api/gateway/automations", headers=HEADERS).json()["items"]}
    assert rows[aid]["workspace_root"] == definition["workspace_root"]
    assert live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]["workspace_root"] == definition["workspace_root"]


def test_runs_rows_carry_the_folder_turns_execute_in(live: TestClient, tmp_path: Path) -> None:
    import os

    # A chat turn started with a launch-folder override reports THAT folder.
    project = tmp_path / "project"
    project.mkdir()
    r = live.post("/api/gateway/runs/start", headers=HEADERS, json={
        "bundle_id": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "session_id": "ws-chat", "input_data": {"prompt": "p", "workspace_root": str(project)}})
    assert r.status_code == 200, r.text
    chat_id = r.json()["run_id"]
    rows = _rows(live, "root_only=true&session_id=ws-chat")
    assert os.path.realpath(rows[chat_id]["workspace_root"]) == os.path.realpath(str(project))

    # A discussion row reports its OWN folder, not the automation's mount.
    aid = _create(live, request_id="ws-disc", mode="growing")["automation_id"]
    occ = _run_now_and_wait(live, aid, "c1", index=1)
    out = live.post(f"/api/gateway/automations/{aid}/discuss", headers=HEADERS,
                    json={"request_id": "d", "occurrence_index": 1, "prompt": "why?"}).json()
    rows = _rows(live, f"root_only=true&session_id={out['session_id']}")
    assert rows[out["run_id"]]["workspace_root"] == out["workspace_root"] != out["mounted_workspace"]
    # The occurrence (a turn, though a child of the controller) carries its folder, on both paths.
    automation_ws = live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]["workspace_root"]
    assert _rows(live, "root_only=true")[occ["run_id"]]["workspace_root"] == automation_ws
    children = live.get(f"/api/gateway/runs?parent_run_id={aid}&include_ledger_len=false", headers=HEADERS).json()["items"]
    assert [c["workspace_root"] for c in children if c["run_id"] == occ["run_id"]] == [automation_ws]

    # A sub-run (child of a turn) does not carry it; neither does the controller.
    sub = chat_run(session_id="ws-chat", parent_run_id=chat_id, created_at="2026-09-27T12:00:00+00:00")
    sub.vars["workspace_root"] = str(project)
    save_runs(sub)
    everything = _rows(live, "")
    assert "workspace_root" not in everything[sub.run_id]
    assert "workspace_root" not in everything[aid]
    kids = live.get(f"/api/gateway/runs?parent_run_id={chat_id}&include_ledger_len=false", headers=HEADERS).json()["items"]
    assert kids and all("workspace_root" not in k for k in kids)


def test_summary_passes_next_fire_at_and_current_occurrence_through(live: TestClient) -> None:
    from abstractruntime.automation_queries import automation_summary

    from abstractgateway.service import get_gateway_service

    r = live.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": "nfa", "title": "nfa", "target": {"bundle_ref": live.bundle_ref, "flow_id": "slow"},
        "trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": "1h"}}})
    aid = r.json()["automation_id"]

    def running():
        s = live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]
        return s if (s.get("current_occurrence") or {}).get("status") == "running" else None

    summary = wait_until(running, timeout_s=15)             # the first tick (start_at = now) is running
    base = automation_summary(get_gateway_service().host.run_store.load(aid))
    assert summary["current_occurrence"] == base["current_occurrence"] and summary["current_occurrence"]["index"] == 1
    assert summary["next_fire_at"] and summary["next_fire_at"] == base["next_fire_at"]   # present while running
    listed = {s["automation_id"]: s for s in live.get("/api/gateway/automations", headers=HEADERS).json()["items"]}[aid]
    assert listed["current_occurrence"] == summary["current_occurrence"] and listed["next_fire_at"] == summary["next_fire_at"]

    _command(live, aid, "automation.pause", "p")
    paused = wait_until(lambda: (lambda s: s if s["status"] == "paused" else None)(
        live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]), timeout_s=10)
    assert "next_fire_at" not in paused

    idle = _create(live, request_id="idle")["summary"]        # manual trigger, nothing running
    assert idle["current_occurrence"] is None and "next_fire_at" not in idle


def test_a_discussion_turn_with_a_client_transcript_is_still_refused(live: TestClient) -> None:
    """The /runs/start client-context window (ADR-0026) never turns the strict
    refusal into acceptance: a discussion session is seeded by the gateway."""
    aid = _create(live, mode="growing")["automation_id"]
    _run_now_and_wait(live, aid, "c1", index=1)
    out = live.post(f"/api/gateway/automations/{aid}/discuss", headers=HEADERS,
                    json={"request_id": "dx", "occurrence_index": 1, "prompt": "why?"}).json()
    long_transcript = []
    for i in range(40):
        long_transcript += [{"role": "user", "content": f"q{i}"}, {"role": "assistant", "content": "x" * 8_000}]
    r = live.post("/api/gateway/runs/start", headers=HEADERS, json={
        "bundle_id": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "session_id": out["session_id"],
        "input_data": {"prompt": "and then?", "context": {"messages": long_transcript}}})
    assert r.status_code == 400, r.text
    assert "do not send context.messages" in r.text


def test_create_accepts_email_notify_policy_and_the_email_trigger(live: TestClient) -> None:
    """Apps create "Email me the result" automations and "When an email arrives" ones in one
    request (framework backlog 0992): `notify`, `policy.email_allowed_recipients`,
    `policy.untrusted_input_tools` and the email.received@1 trigger fields reach the runtime,
    which validates them; the definition reads them back."""
    body = {
        "request_id": "email-1",
        "title": "Invoices",
        "target": {"bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "hello"}},
        "trigger": {
            "source_id": "email.received",
            "source_version": 1,
            "config": {
                "folder": "INBOX",
                "uses_model": False,
                "every": "60s",
                "max_batch": 20,
                "filter": {
                    "from_in": ["billing@example.test"],
                    "from_domain_in": ["example.org"],
                    "to_in": ["me@example.test"],
                    "subject_contains": "invoice",
                    "has_attachment": True,
                },
            },
        },
        "policy": {"email_allowed_recipients": ["self", "boss@example.test"], "untrusted_input_tools": []},
        "notify": {"channels": ["console", "email"]},
    }
    r = live.post("/api/gateway/automations", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    aid = r.json()["automation_id"]
    d = live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]
    assert d["notify"] == {"channels": ["console", "email"]}
    assert d["policy"]["email_allowed_recipients"] == ["self", "boss@example.test"]
    assert d["trigger"]["source_id"] == "email.received"
    assert d["trigger"]["config"]["filter"]["subject_contains"] == "invoice"
    assert d["trigger"]["config"]["every"] == "60s"

    bad = live.post("/api/gateway/automations", headers=HEADERS, json={**body, "request_id": "email-2", "notify": {"channels": ["pager"]}})
    assert bad.status_code == 422, bad.text


def test_growing_budget_round_trip_in_create_list_detail_and_revise(live: TestClient):
    body = {
        "request_id": "custom-growing-budget", "title": "Budget test",
        "target": {"bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "hello"}},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
        "context": {"mode": "growing", "growing": {"max_tokens": 30000}},
    }
    created = live.post("/api/gateway/automations", headers=HEADERS, json=body)
    assert created.status_code == 200, created.text
    assert created.json()["summary"]["growing_max_tokens"] == 30000
    aid = created.json()["automation_id"]
    path = f"/api/gateway/automations/{aid}"
    detail = live.get(path, headers=HEADERS).json()
    assert detail["definition"]["context"]["growing"]["max_tokens"] == 30000
    assert detail["summary"]["growing_max_tokens"] == 30000
    rows = live.get("/api/gateway/automations", headers=HEADERS).json()["items"]
    assert next(row for row in rows if row["automation_id"] == aid)["growing_max_tokens"] == 30000
    patched = live.patch(path, headers=HEADERS, json={"command_id": "budget-edit", "expected_revision": 1,
        "changes": {"context": {"mode": "growing", "growing": {"max_tokens": 20000}}}})
    assert patched.status_code == 200, patched.text
    wait_until(lambda: live.get(path, headers=HEADERS).json()["summary"]["growing_max_tokens"] == 20000)
    assert live.get(path, headers=HEADERS).json()["definition"]["context"]["growing"]["max_tokens"] == 20000
    for bad in (0, -1, True, "30000", 1.5):
        rejected = live.patch(path, headers=HEADERS, json={"command_id": f"bad-budget-{bad}", "expected_revision": 2,
            "changes": {"context": {"mode": "growing", "growing": {"max_tokens": bad}}}})
        assert rejected.status_code == 422, rejected.text
        assert _envelope(rejected)["field"] == "context.growing.max_tokens"
