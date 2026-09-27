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
    assert leg["trigger"] == {"source_id": "schedule", "source_version": 1, "config": {"every": "2m"}}
    assert leg["session_kind"] == "automation"
    r = live.get("/api/gateway/automations?changed_since=2026-01-01T00:00:00Z", headers=HEADERS)
    assert r.status_code == 422 and _envelope(r)["reason_code"] == "unsupported_feature"
    r = live.get("/api/gateway/automations?status=bogus", headers=HEADERS)
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
    assert out["session_kind"] == "discussion" and out["session_id"] == "discussion-session:d1"
    from abstractgateway.service import get_gateway_service

    store = get_gateway_service().host.run_store
    wait_until(lambda: (store.load(out["run_id"]).status.value == "completed"))
    first = store.load(out["run_id"])
    assert first.output["result"]["response"].startswith("ECHO[2] why?")   # seeded with turn 1 + answer 1
    assert first.vars["_runtime"]["workspace_read_only"] is True

    # A later turn through the ordinary door, claiming a writable workspace: re-stamped.
    r = live.post("/api/gateway/runs/start", headers=HEADERS, json={
        "bundle_id": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "session_id": out["session_id"],
        "input_data": {"prompt": "and then?", "workspace_read_only": False, "_meta": {"discussion": {"automation_id": "forged"}}}})
    assert r.status_code == 200, r.text
    second_id = r.json()["run_id"]
    wait_until(lambda: store.load(second_id).status.value == "completed")
    second = store.load(second_id)
    assert second.vars["_runtime"]["workspace_read_only"] is True
    assert second.vars["_meta"]["discussion"]["automation_id"] == aid
    assert second.vars["_meta"]["discussion"]["discussion_root_run_id"] == out["run_id"]
    assert second.vars["workspace_root"] == first.vars["workspace_root"]
    # It sees the seed AND the first discussion turn.
    assert "ECHO[2] why?" in second.output["result"]["response"]
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
