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
