"""Session tool policy through the real gateway app (contract v1).

A conversation approves a kind of tool once; the next call of that kind, made by the delegated
agent child run, runs with NO approval wait anywhere in the run tree (no waiting record, no
waiting snapshot) and an `abstract.tool_policy_decision` audit record names who set the policy.
A tool the gateway does not authorize for the account is never covered, even by "approve all".
"""
from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from email_fixtures import memory_keyring  # noqa: F401 - autouse: the OS keychain stays untouched

TOKEN = "t"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SID = "conv-stp-1"
# The tool call the stub model makes next (one per turn, then it answers).
NEXT_CALL: Dict[str, Any] = {}
URL = "http://127.0.0.1:9/never-listening"


def _wait_until(predicate, *, timeout_s: float = 20.0, poll_s: float = 0.05):
    end = time.time() + timeout_s
    while time.time() < end:
        out = predicate()
        if out:
            return out
        time.sleep(poll_s)
    raise AssertionError("timeout waiting for condition")


def _agent_flow() -> Dict[str, Any]:
    return {
        "id": "root", "name": "stp-agent", "description": "", "interfaces": [], "entryNode": "start",
        "nodes": [
            {"id": "start", "type": "on_flow_start", "position": {"x": 0, "y": 0},
             "data": {"nodeType": "on_flow_start", "label": "Start", "inputs": [],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"},
                                  {"id": "tools", "label": "tools", "type": "tools"}],
                      "pinDefaults": {"tools": ["fetch_url"]}}},
            {"id": "agent", "type": "agent", "position": {"x": 300, "y": 0},
             "data": {"nodeType": "agent", "label": "Agent",
                      "inputs": [{"id": "exec-in", "label": "", "type": "execution"},
                                 {"id": "task", "label": "task", "type": "string"},
                                 {"id": "tools", "label": "tools", "type": "tools"}],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"},
                                  {"id": "result", "label": "result", "type": "string"}],
                      "pinDefaults": {"task": "fetch it"},
                      "agentConfig": {"provider": "ollama", "model": "qwen3:1.7b", "tools": []}}},
            {"id": "end", "type": "on_flow_end", "position": {"x": 600, "y": 0},
             "data": {"nodeType": "on_flow_end", "label": "End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []}},
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "agent", "targetHandle": "exec-in"},
            {"id": "e2", "source": "start", "sourceHandle": "tools", "target": "agent", "targetHandle": "tools"},
            {"id": "e3", "source": "agent", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
        ],
    }


def _write_bundle(bundles_dir: Path) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"bundle_format_version": "1", "bundle_id": "stp-agent", "bundle_version": "0.0.0",
                "created_at": "2026-10-27T00:00:00+00:00",
                "entrypoints": [{"flow_id": "root", "name": "test", "description": "", "interfaces": []}],
                "flows": {"root": "flows/root.json"}, "artifacts": {}, "assets": {}, "metadata": {}}
    with zipfile.ZipFile(bundles_dir / "stp-agent.flow", "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("flows/root.json", json.dumps(_agent_flow()))


def _stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline model: makes NEXT_CALL once per turn (until a tool result is in its messages), then answers."""
    from abstractruntime.core.models import EffectType
    from abstractruntime.core.runtime import EffectOutcome
    from abstractruntime.integrations.abstractcore import factory as ac_factory
    from abstractruntime.integrations.abstractcore.effect_handlers import make_tool_calls_handler

    def _llm(run, effect, default_next_node):
        payload = dict(effect.payload or {})
        messages = json.dumps(payload.get("messages") or [], default=str)
        if NEXT_CALL and '"role": "tool"' not in messages and "Tool Results" not in messages and "tool_result" not in messages:
            return EffectOutcome.completed({"content": "", "tool_calls": [dict(NEXT_CALL)],
                                            "model": payload.get("model"), "provider": payload.get("provider")})
        return EffectOutcome.completed({"content": "done", "tool_calls": [], "model": payload.get("model"), "provider": payload.get("provider")})

    def _handlers(*, llm, tools, artifact_store=None, run_store=None):
        return {EffectType.LLM_CALL: _llm, EffectType.TOOL_CALLS: make_tool_calls_handler(tools=tools, artifact_store=artifact_store, run_store=run_store)}

    monkeypatch.setattr(ac_factory, "build_effect_handlers", _handlers)


@pytest.fixture()
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    NEXT_CALL.clear()
    runtime_dir, bundles_dir = tmp_path / "runtime", tmp_path / "bundles"
    _write_bundle(bundles_dir)
    _stub_llm(monkeypatch)
    for k, v in {"ABSTRACTGATEWAY_DATA_DIR": str(runtime_dir), "ABSTRACTGATEWAY_FLOWS_DIR": str(bundles_dir),
                 "ABSTRACTGATEWAY_WORKFLOW_SOURCE": "bundle", "ABSTRACTGATEWAY_AUTH_TOKEN": TOKEN,
                 "ABSTRACTGATEWAY_ALLOWED_ORIGINS": "*", "ABSTRACTGATEWAY_POLL_S": "0.05", "ABSTRACTGATEWAY_TICK_WORKERS": "1"}.items():
        monkeypatch.setenv(k, v)
    # Hermetic: hosted provider keys in the developer's shell would make discovery probe them.
    for k in ("ACEMUSIC_API_KEY", "ACEMUSIC_BASE_URL", "ELEVENLABS_API_KEY", "ELEVENLABS_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    from abstractgateway.app import app

    with TestClient(app) as client:
        yield client


def _start(client: TestClient, tools: List[str], session_id: str = SID) -> str:
    r = client.post("/api/gateway/runs/start", headers=HEADERS, json={
        "bundle_id": "stp-agent", "flow_id": "root", "session_id": session_id, "input_data": {"tools": tools}})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def _run(client: TestClient, run_id: str) -> Dict[str, Any]:
    r = client.get(f"/api/gateway/runs/{run_id}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _ledger(client: TestClient, run_id: str) -> List[Dict[str, Any]]:
    r = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=1000", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json().get("items") or []


def _tree(client: TestClient, run_id: str) -> List[str]:
    """The root and every descendant, from the run store."""
    from abstractgateway.service import get_gateway_service

    store = get_gateway_service().host.run_store
    out, todo = [], [run_id]
    while todo:
        rid = todo.pop()
        out.append(rid)
        todo.extend(r.run_id for r in store.list_children(parent_run_id=rid))
    return out


def _decisions(client: TestClient, run_ids: List[str]) -> List[Dict[str, Any]]:
    out = []
    for rid in run_ids:
        for rec in _ledger(client, rid):
            eff = rec.get("effect") or {}
            p = eff.get("payload") or {}
            if eff.get("type") == "emit_event" and p.get("name") == "abstract.tool_policy_decision":
                out.append({"run_id": rid, **(p.get("payload") or {})})
    return out


def _approval_wait(client: TestClient, run_id: str):
    for rid in _tree(client, run_id):
        body = _run(client, rid)
        w = body.get("waiting") if body.get("status") == "waiting" else None
        if isinstance(w, dict) and (w.get("details") or {}).get("mode") == "approval_required":
            return rid, w
    return None


def _waiting_records(client: TestClient, run_ids: List[str]) -> List[Dict[str, Any]]:
    return [rec for rid in run_ids for rec in _ledger(client, rid)
            if rec.get("status") == "waiting" and "approval" in json.dumps(rec.get("waiting") or rec.get("wait") or {}, default=str)]


def _policy(client: TestClient, method: str, body: Any = None, session_id: str = SID):
    r = client.request(method, f"/api/gateway/sessions/{session_id}/tool-policy", headers=HEADERS,
                       **({"json": body} if body is not None else {}))
    return r


def _executed_fetches(client: TestClient, run_ids: List[str]) -> int:
    n = 0
    for rid in run_ids:
        for rec in _ledger(client, rid):
            eff = rec.get("effect") or {}
            if eff.get("type") == "tool_calls" and rec.get("status") == "completed":
                for res in ((rec.get("result") or {}).get("results") or []):
                    if isinstance(res, dict) and res.get("name") == "fetch_url":
                        n += 1
    return n


def test_capability_is_advertised(gateway):
    caps = gateway.get("/api/gateway/discovery/capabilities", headers=HEADERS).json()["capabilities"]
    cap = caps["session_tool_policy"]
    assert cap["supported"] is True and cap["version"] == 1
    assert cap["endpoint"] == "/api/gateway/sessions/{session_id}/tool-policy"


def test_approving_a_kind_runs_the_next_call_with_no_interaction_and_an_audit_record(gateway):
    NEXT_CALL.update({"name": "fetch_url", "arguments": {"url": URL}, "call_id": "c1"})
    # Control: without a policy the delegated child parks on an approval wait.
    first = _start(gateway, ["fetch_url"])
    rid, wait = _wait_until(lambda: _approval_wait(gateway, first))
    assert [c["name"] for c in wait["details"]["tool_calls"]] == ["fetch_url"]

    # "Approve all web tools" for this conversation: policy first, then the shown wait, once.
    r = _policy(gateway, "POST", {"approve": {"kinds": ["web"]}, "client": "test-suite"})
    assert r.status_code == 200, r.text
    policy = r.json()["policy"]
    assert policy["approve"]["fetch_url"] == "kind:web" and policy["revision"] == 1
    assert policy["set_by"]["client"] == "test-suite" and policy["clamped"] == []
    ok = gateway.post("/api/gateway/commands", headers=HEADERS, json={
        "command_id": "approve-once", "run_id": rid, "type": "resume",
        "payload": {"wait_key": wait["wait_key"], "payload": {"approved": True}}})
    assert ok.status_code == 200, ok.text
    _wait_until(lambda: _run(gateway, first).get("status") == "completed")

    # The next turn of the conversation: the same kind of call, no interaction anywhere.
    second = _start(gateway, ["fetch_url"])
    _wait_until(lambda: _run(gateway, second).get("status") == "completed")
    tree = _tree(gateway, second)
    assert len(tree) >= 2, "the agent runs its tools in a delegated child run"
    assert _waiting_records(gateway, tree) == []
    assert all(_run(gateway, r).get("waiting") in (None, {}) or (_run(gateway, r).get("waiting") or {}).get("details", {}).get("mode") != "approval_required" for r in tree)
    assert _executed_fetches(gateway, tree) == 1
    [decision] = _decisions(gateway, tree)
    assert decision["run_id"] != second  # recorded on the delegated child that made the call
    assert decision["decision"] == "auto_approved" and decision["session_id"] == SID
    assert decision["tool_calls"] == [{"call_id": "c1", "name": "fetch_url", "matched": "kind:web"}]
    assert decision["policy"]["revision"] == 1 and decision["policy"]["set_by"]["user_id"]

    # Reset: the same call asks again.
    r = _policy(gateway, "DELETE")
    assert r.status_code == 200 and r.json()["policy"] is None
    third = _start(gateway, ["fetch_url"])
    _wait_until(lambda: _approval_wait(gateway, third))


def test_a_tool_the_gateway_does_not_authorize_is_never_covered_even_by_approve_all(gateway):
    # Email tools are off for this account (no connected account): not authorized.
    r = _policy(gateway, "PUT", {"approve": {"all": True, "tools": ["send_email"]}})
    assert r.status_code == 200, r.text
    policy = r.json()["policy"]
    assert "send_email" not in policy["approve"]
    assert {"selector": "tools", "name": "send_email", "reason": "not_authorized"} in policy["clamped"]
    assert policy["approve"]["fetch_url"] == "all"
    # The model calls it anyway: it is not auto-approved (it parks on approval or is refused by
    # the run's tool ceiling), never executed, never recorded as approved by the policy.
    NEXT_CALL.update({"name": "send_email", "arguments": {"to": "someone@example.invalid", "subject": "x", "body_text": "y"}, "call_id": "e1"})
    run_id = _start(gateway, ["fetch_url", "send_email"])

    def _settled():
        if _approval_wait(gateway, run_id):
            return "asked"
        return "done" if _run(gateway, run_id).get("status") in {"completed", "failed"} else None

    assert _wait_until(_settled) == "done"
    tree = _tree(gateway, run_id)
    assert not [d for d in _decisions(gateway, tree) if d["decision"] == "auto_approved"]
    results = [res for rid in tree for rec in _ledger(gateway, rid)
               if (rec.get("effect") or {}).get("type") == "tool_calls" and rec.get("status") == "completed"
               for res in ((rec.get("result") or {}).get("results") or [])]
    [sent] = [res for res in results if res.get("name") == "send_email"]
    assert sent["success"] is False and "not allowed" in sent["error"]


def test_approve_all_expands_only_to_enabled_rows(gateway):
    rows = gateway.get("/api/gateway/discovery/tools", headers=HEADERS).json()["items"]
    enabled = {r["name"] for r in rows if r.get("enabled") is True}
    disabled = {r["name"] for r in rows if r.get("enabled") is not True} - enabled
    assert disabled, "the catalog serves disabled toolsets as visible rows"
    policy = _policy(gateway, "PUT", {"approve": {"all": True}}).json()["policy"]
    assert set(policy["approve"]) == enabled and not set(policy["approve"]) & disabled


def test_deny_refuses_with_no_wait(gateway):
    NEXT_CALL.update({"name": "fetch_url", "arguments": {"url": URL}, "call_id": "c9"})
    assert _policy(gateway, "PUT", {"deny": {"tools": ["fetch_url"]}}).status_code == 200
    run_id = _start(gateway, ["fetch_url"])
    _wait_until(lambda: _run(gateway, run_id).get("status") == "completed")
    tree = _tree(gateway, run_id)
    assert _waiting_records(gateway, tree) == []
    [decision] = _decisions(gateway, tree)
    assert decision["decision"] == "denied" and decision["tool_calls"][0]["name"] == "fetch_url"


def test_routes_validate_merge_and_guard_revisions(gateway):
    got = _policy(gateway, "GET").json()
    assert (got["session_id"], got["policy"], got["revision"]) == (SID, None, 0) and "deny" in got["scope"]
    assert _policy(gateway, "PUT", {"approve": {"nope": True}}).status_code == 422
    assert _policy(gateway, "PUT", {"approve": {"max_risk_rank": 9}}).status_code == 422
    assert _policy(gateway, "PUT", {"approve": {"all": True}}, session_id="bad id!").status_code in (404, 422)
    one = _policy(gateway, "POST", {"approve": {"tools": ["fetch_url"]}, "ask": ["write_file"]}).json()["policy"]
    two = _policy(gateway, "POST", {"approve": {"kinds": ["files"], "max_risk_rank": 1}}).json()["policy"]
    assert two["revision"] == 2 and two["approve"]["fetch_url"] == "tool"  # merged
    assert two["approve"].get("read_file") == "kind:files" and "write_file" not in two["approve"]  # rank 2 and ask
    assert _policy(gateway, "PUT", {"approve": {"kinds": ["no-such-kind"]}, "if_revision": 1}).status_code == 409
    three = _policy(gateway, "PUT", {"approve": {"kinds": ["no-such-kind"]}, "if_revision": 2}).json()["policy"]
    assert three["approve"] == {} and three["clamped"] == [{"selector": "kinds", "name": "no-such-kind", "reason": "unknown_kind"}]
    assert one["revision"] == 1


def _serve_inventory(monkeypatch, mutate):
    """Serve GET /discovery/tools through `mutate(rows)`: the account's authorization changes."""
    import abstractgateway.routes.gateway as gw

    real = gw.discovery_tools

    async def _patched():
        out = await real()
        out["items"] = mutate([dict(r) for r in out.get("items") or []])
        return out

    monkeypatch.setattr(gw, "discovery_tools", _patched)


def test_an_executable_allowed_tool_the_account_is_not_authorized_for_still_asks(gateway, monkeypatch):
    # fetch_url is in the run's tools and in the host's executor, but this account's inventory
    # serves it disabled: approve-all must not cover it.
    _serve_inventory(monkeypatch, lambda rows: [dict(r, enabled=False) if r.get("name") == "fetch_url" else r for r in rows])
    policy = _policy(gateway, "PUT", {"approve": {"all": True, "tools": ["fetch_url"]}}).json()["policy"]
    assert "fetch_url" not in policy["approve"] and "write_file" in policy["approve"]
    assert {"selector": "tools", "name": "fetch_url", "reason": "not_authorized"} in policy["clamped"]
    NEXT_CALL.update({"name": "fetch_url", "arguments": {"url": URL}, "call_id": "u1"})
    run_id = _start(gateway, ["fetch_url"])
    _rid, wait = _wait_until(lambda: _approval_wait(gateway, run_id))
    assert [c["name"] for c in wait["details"]["tool_calls"]] == ["fetch_url"]
    assert _decisions(gateway, _tree(gateway, run_id)) == []


def test_a_merge_never_narrows_and_never_re_expands_an_earlier_all(gateway, monkeypatch):
    first = _policy(gateway, "PUT", {"approve": {"all": True, "max_risk_rank": 4}}).json()["policy"]
    assert "execute_command" in first["approve"]
    # A later grant made on a quieter batch keeps what was granted before.
    second = _policy(gateway, "POST", {"approve": {"all": True, "max_risk_rank": 1}}).json()["policy"]
    assert "execute_command" in second["approve"] and second["request"]["approve"]["max_risk_rank"] == 4
    # A tool enabled after the `all` write is not covered by it, merge or not.
    _serve_inventory(monkeypatch, lambda rows: rows + [{"name": "new_tool", "toolset": "web", "enabled": True, "risk_rank": 1}])
    third = _policy(gateway, "POST", {"approve": {"tools": ["read_file"]}}).json()["policy"]
    assert "new_tool" not in third["approve"]


def test_a_reset_racing_a_merge_is_not_undone(gateway, monkeypatch):
    import abstractgateway.routes.session_tool_policy as routes

    assert _policy(gateway, "PUT", {"approve": {"tools": ["fetch_url"]}}).status_code == 200
    real = routes._store()

    class _ResetLandsFirst:
        """The other device's DELETE lands between this merge's read and its write, once."""

        def __init__(self):
            self.raced = False

        def __getattr__(self, name):
            return getattr(real, name)

        def put(self, sid, policy, *, if_revision=None):
            if not self.raced:
                self.raced = True
                real.delete(sid)
            return real.put(sid, policy, if_revision=if_revision)

    racer = _ResetLandsFirst()
    monkeypatch.setattr(routes, "_store", lambda: racer)
    merged = _policy(gateway, "POST", {"approve": {"tools": ["write_file"]}}).json()["policy"]
    assert racer.raced
    assert merged["approve"] == {"write_file": "tool"}  # the reset stood: fetch_url is not back


def test_the_documented_envelopes(gateway, monkeypatch):
    """docs/api.md clauses: GET revision with a null policy, DELETE ?if_revision, the 409/503 bodies, scope."""
    put = _policy(gateway, "PUT", {"approve": {"tools": ["fetch_url"]}}).json()
    assert set(put) == {"session_id", "policy", "scope"} and set(put["scope"]) == {"approve", "deny"}
    stale = gateway.delete(f"/api/gateway/sessions/{SID}/tool-policy?if_revision=0", headers=HEADERS)
    assert stale.status_code == 409
    assert stale.json()["detail"]["reason_code"] == "revision_conflict" and stale.json()["detail"]["revision"] == 1
    gone = gateway.delete(f"/api/gateway/sessions/{SID}/tool-policy?if_revision=1", headers=HEADERS).json()
    assert (gone["policy"], gone["revision"]) == (None, 2) and "scope" in gone
    got = _policy(gateway, "GET").json()
    assert (got["policy"], got["revision"]) == (None, 2)

    import abstractgateway.routes.gateway as gw

    async def _broken():
        return {"items": [], "error": "registry down"}

    monkeypatch.setattr(gw, "discovery_tools", _broken)
    down = _policy(gateway, "POST", {"approve": {"all": True}})
    assert down.status_code == 503 and down.json()["detail"]["reason_code"] == "tool_inventory_unavailable"
    assert _policy(gateway, "GET").json()["revision"] == 2  # nothing written
