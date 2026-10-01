"""MCP tools in agent runs (round 3, lane mcp-runs): a registered server an admin enabled for agents is
offered to runs as `mcp::<server>::<tool>`, under the normal tool policy (Ask by default, allow-all runs),
never under untrusted input, with header secrets resolved only inside the gateway at call time."""
from __future__ import annotations

import json
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from mcp_run_fakes import FakeHttpEcho, stdio_calls, write_stdio_server
from email_fixtures import memory_keyring  # noqa: F401 - autouse: the OS keychain stays untouched

TOKEN = "t"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "sk-mcp-run-secret-7f3a"
MCP = "mcp::fake::echo"
LLM_PAYLOADS: List[Dict[str, Any]] = []


def _wait_until(predicate, *, timeout_s: float = 15.0, poll_s: float = 0.1):
    end = time.time() + timeout_s
    while time.time() < end:
        out = predicate()
        if out:
            return out
        time.sleep(poll_s)
    raise AssertionError("timeout waiting for condition")


def _agent_flow() -> Dict[str, Any]:
    """Start node with a `tools` pin (default: read_file) wired into an Agent node's tools input."""
    return {
        "id": "root", "name": "mcp-agent", "description": "", "interfaces": [], "entryNode": "start",
        "nodes": [
            {"id": "start", "type": "on_flow_start", "position": {"x": 0, "y": 0},
             "data": {"nodeType": "on_flow_start", "label": "Start", "inputs": [],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"},
                                  {"id": "tools", "label": "tools", "type": "tools"}],
                      "pinDefaults": {"tools": ["read_file"]}}},
            {"id": "agent", "type": "agent", "position": {"x": 300, "y": 0},
             "data": {"nodeType": "agent", "label": "Agent",
                      "inputs": [{"id": "exec-in", "label": "", "type": "execution"},
                                 {"id": "task", "label": "task", "type": "string"},
                                 {"id": "tools", "label": "tools", "type": "tools"}],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"},
                                  {"id": "result", "label": "result", "type": "string"}],
                      "pinDefaults": {"task": "echo hi"},
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
    manifest = {"bundle_format_version": "1", "bundle_id": "mcp-agent", "bundle_version": "0.0.0",
                "created_at": "2026-10-01T00:00:00+00:00",
                "entrypoints": [{"flow_id": "root", "name": "test", "description": "", "interfaces": []}],
                "flows": {"root": "flows/root.json"}, "artifacts": {}, "assets": {}, "metadata": {}}
    with zipfile.ZipFile(bundles_dir / "mcp-agent.flow", "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("flows/root.json", json.dumps(_agent_flow()))


def _stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline LLM: records every payload; calls the MCP tool once when offered, then answers."""
    from abstractruntime.core.models import EffectType
    from abstractruntime.core.runtime import EffectOutcome
    from abstractruntime.integrations.abstractcore import factory as ac_factory
    from abstractruntime.integrations.abstractcore.effect_handlers import make_tool_calls_handler

    def _llm(run, effect, default_next_node):
        payload = dict(effect.payload or {})
        LLM_PAYLOADS.append(json.loads(json.dumps(payload, default=str)))
        offered = [t.get("name") for t in payload.get("tools") or [] if isinstance(t, dict)]
        seen_result = "echo: hi" in json.dumps(payload.get("messages") or [], default=str)
        if MCP in offered and not seen_result:
            return EffectOutcome.completed({"content": "", "tool_calls": [{"name": MCP, "arguments": {"text": "hi"}, "call_id": "c1"}],
                                            "model": payload.get("model"), "provider": payload.get("provider")})
        return EffectOutcome.completed({"content": "done", "tool_calls": [], "model": payload.get("model"), "provider": payload.get("provider")})

    def _handlers(*, llm, tools, artifact_store=None, run_store=None):
        return {EffectType.LLM_CALL: _llm, EffectType.TOOL_CALLS: make_tool_calls_handler(tools=tools, artifact_store=artifact_store, run_store=run_store)}

    monkeypatch.setattr(ac_factory, "build_effect_handlers", _handlers)


@pytest.fixture()
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    LLM_PAYLOADS.clear()
    runtime_dir, bundles_dir = tmp_path / "runtime", tmp_path / "bundles"
    _write_bundle(bundles_dir)
    _stub_llm(monkeypatch)
    for k, v in {"ABSTRACTGATEWAY_DATA_DIR": str(runtime_dir), "ABSTRACTGATEWAY_FLOWS_DIR": str(bundles_dir),
                 "ABSTRACTGATEWAY_WORKFLOW_SOURCE": "bundle", "ABSTRACTGATEWAY_AUTH_TOKEN": TOKEN,
                 "ABSTRACTGATEWAY_ALLOWED_ORIGINS": "*", "ABSTRACTGATEWAY_POLL_S": "0.05", "ABSTRACTGATEWAY_TICK_WORKERS": "1"}.items():
        monkeypatch.setenv(k, v)
    from abstractgateway.app import app

    with TestClient(app) as client:
        client.runtime_dir = runtime_dir  # type: ignore[attr-defined]
        yield client


def _register(client: TestClient, transport: str, tmp_path: Path, fake: Any = None, *, enable: bool = True) -> Dict[str, Any]:
    if transport == "stdio":
        script, log = write_stdio_server(tmp_path / "stdio-fake")
        body = {"name": "fake", "transport": "stdio", "command": sys.executable, "args": [str(script), str(log)]}
    else:
        body = {"name": "fake", "transport": "http", "url": fake.url, "headers": {"Authorization": f"Bearer {SECRET}"}}
    r = client.post("/api/gateway/admin/mcp/servers", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    t = client.post("/api/gateway/admin/mcp/servers/fake/test", headers=HEADERS)
    assert t.status_code == 200 and t.json()["ok"], t.text
    if enable:
        e = client.post("/api/gateway/admin/mcp/servers/fake/agents", headers=HEADERS, json={"enabled": True})
        assert e.status_code == 200, e.text
        assert e.json()["agents_status"] == "Offered to agents · 1 tool"
    return body


def _start(client: TestClient, input_data: Dict[str, Any]) -> str:
    r = client.post("/api/gateway/runs/start", headers=HEADERS, json={"bundle_id": "mcp-agent", "flow_id": "root", "input_data": input_data})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def _run(client: TestClient, run_id: str) -> Dict[str, Any]:
    r = client.get(f"/api/gateway/runs/{run_id}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _vars(run_id: str) -> Dict[str, Any]:
    """The run's durable vars (the run store), where run-start notes live."""
    from abstractgateway.service import get_gateway_service

    run = get_gateway_service().host.run_store.load(run_id)
    return dict(run.vars or {})


def _ledger(client: TestClient, run_id: str) -> List[Dict[str, Any]]:
    r = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=1000", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json().get("items") or []


def _child_ids(client: TestClient, run_id: str) -> List[str]:
    """Agent child runs, from the start_subworkflow effects' outcomes in the ledger."""
    out: List[str] = []
    for item in _ledger(client, run_id):
        eff = item.get("effect") if isinstance(item.get("effect"), dict) else {}
        if eff.get("type") != "start_subworkflow":
            continue
        for src in (item.get("result"), (item.get("wait") or {}).get("details") if isinstance(item.get("wait"), dict) else None):
            sub = src.get("sub_run_id") if isinstance(src, dict) else None
            if isinstance(sub, str) and sub not in out:
                out.append(sub)
    w = _run(client, run_id).get("waiting")
    sub = ((w or {}).get("details") or {}).get("sub_run_id") if isinstance(w, dict) else None
    if isinstance(sub, str) and sub not in out:
        out.append(sub)
    return out


def _tool_wait(client: TestClient, run_id: str):
    """(run id, wait) of the approval wait in the run or one of its child runs, else None."""
    for rid in [run_id] + _child_ids(client, run_id):
        body = _run(client, rid)
        w = body.get("waiting") if body.get("status") == "waiting" else None
        if isinstance(w, dict) and (w.get("details") or {}).get("mode") == "approval_required":
            return rid, w
    return None


def _offered_names(payload: Dict[str, Any]) -> List[str]:
    return [t.get("name") for t in payload.get("tools") or [] if isinstance(t, dict)]


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_enabled_server_tool_asks_then_reaches_the_server_and_lands_in_the_ledger(gateway, transport, tmp_path):
    with FakeHttpEcho(require_bearer=SECRET) as fake:
        _register(gateway, transport, tmp_path, fake)
        run_id = _start(gateway, {})  # no `tools`: start-node defaults + the offered MCP tools
        rid, wait = _wait_until(lambda: _tool_wait(gateway, run_id))
        # The model was offered the tool, with its parameters, next to the start-node default.
        first = LLM_PAYLOADS[0]
        assert MCP in _offered_names(first) and "read_file" in _offered_names(first)
        spec = next(t for t in first["tools"] if t["name"] == MCP)
        assert "text" in spec["parameters"]
        # Ask: nothing reached the server before approval.
        assert (stdio_calls(tmp_path / "stdio-fake" / "stdio_requests.jsonl") if transport == "stdio" else fake.calls()) == []
        assert [c["name"] for c in wait["details"]["tool_calls"]] == [MCP]
        ok = gateway.post("/api/gateway/commands", headers=HEADERS, json={
            "command_id": f"approve-{transport}", "run_id": rid, "type": "resume",
            "payload": {"wait_key": wait["wait_key"], "payload": {"approved": True}}})
        assert ok.status_code == 200, ok.text
        _wait_until(lambda: _run(gateway, run_id).get("status") == "completed")
        calls = stdio_calls(tmp_path / "stdio-fake" / "stdio_requests.jsonl") if transport == "stdio" else fake.calls()
        assert len(calls) == 1 and calls[0]["params"] == {"name": "echo", "arguments": {"text": "hi"}}
        if transport == "http":
            assert fake.calls()[0]["authorization"] == f"Bearer {SECRET}"
        ledger_text = json.dumps([_ledger(gateway, r) for r in [run_id] + _child_ids(gateway, run_id)], default=str)
        assert '"echo: hi"' in ledger_text and MCP in ledger_text
        everything = ledger_text + json.dumps(LLM_PAYLOADS, default=str) + json.dumps(_run(gateway, rid), default=str)
        assert SECRET not in everything
        assert SECRET not in (gateway.runtime_dir / "config" / "mcp_servers.json").read_text()


def test_allow_all_runs_the_mcp_tool_without_asking(gateway, tmp_path):
    with FakeHttpEcho(require_bearer=SECRET) as fake:
        _register(gateway, "http", tmp_path, fake)
        run_id = _start(gateway, {"tools": ["read_file", MCP], "_runtime": {"tool_policy": {"auto_approve_max_risk_rank": 4}}})
        _wait_until(lambda: _run(gateway, run_id).get("status") == "completed")
        assert len(fake.calls()) == 1


@pytest.mark.parametrize("state", ["disabled", "archived"])
def test_a_disabled_or_archived_server_offers_nothing(gateway, tmp_path, state):
    with FakeHttpEcho(require_bearer=SECRET) as fake:
        _register(gateway, "http", tmp_path, fake, enable=state == "archived")
        if state == "archived":
            assert gateway.post("/api/gateway/admin/mcp/servers/fake/archive", headers=HEADERS).status_code == 200
        names = [i["name"] for i in gateway.get("/api/gateway/discovery/tools", headers=HEADERS).json()["items"]]
        assert MCP not in names and "read_file" in names
        inv = gateway.get("/api/gateway/mcp/servers", headers=HEADERS).json()
        assert inv["agents_can_call"] is False and inv["agents_note"].startswith("Agents can't call MCP tools yet")
        run_id = _start(gateway, {"tools": ["read_file", MCP]})
        _wait_until(lambda: _run(gateway, run_id).get("status") == "completed")
        assert all(MCP not in _offered_names(p) for p in LLM_PAYLOADS) and LLM_PAYLOADS
        assert fake.calls() == []
        assert _vars(run_id)["_runtime"]["mcp_notes"] == [
            "MCP server fake is not offered to agents: its tools were removed from this run."]


def test_discovery_and_inventory_show_an_enabled_server_tool_with_its_server(gateway, tmp_path):
    with FakeHttpEcho(require_bearer=SECRET) as fake:
        _register(gateway, "http", tmp_path, fake)
        items = gateway.get("/api/gateway/discovery/tools", headers=HEADERS).json()["items"]
        row = next(i for i in items if i["name"] == MCP)
        assert row["toolset"] == "mcp:fake" and row["mcp_server"] == "fake"
        assert row["enabled"] is True and row["approval_default"] == "ask"
        assert SECRET not in json.dumps(items) and fake.url not in json.dumps(row)
        inv = gateway.get("/api/gateway/mcp/servers", headers=HEADERS).json()
        assert inv["agents_can_call"] is True
        assert inv["agents_note"] == "Tools from enabled servers are offered to your agents. Each call asks for approval unless you allow all tools."
        assert inv["servers"][0]["enabled_for_agents"] is True and inv["servers"][0]["offered_to_agents"] is True


def test_enabling_needs_a_tested_live_server(gateway, tmp_path):
    with FakeHttpEcho(require_bearer=SECRET) as fake:
        _register(gateway, "http", tmp_path, fake, enable=False)
        assert gateway.post("/api/gateway/admin/mcp/servers/fake/archive", headers=HEADERS).status_code == 200
        r = gateway.post("/api/gateway/admin/mcp/servers/fake/agents", headers=HEADERS, json={"enabled": True})
        assert r.status_code == 409 and "archived" in r.json()["detail"]["message"]
        gateway.post("/api/gateway/admin/mcp/servers/fake/unarchive", headers=HEADERS)
        gateway.put("/api/gateway/admin/mcp/servers/fake", headers=HEADERS,
                    json={"transport": "http", "url": fake.url + "?v=2", "headers": {"Authorization": None}})
        r = gateway.post("/api/gateway/admin/mcp/servers/fake/agents", headers=HEADERS, json={"enabled": True})
        assert r.status_code == 409 and "Test the connection" in r.json()["detail"]["message"]


def test_untrusted_input_run_is_never_offered_an_mcp_tool(gateway, tmp_path):
    with FakeHttpEcho(require_bearer=SECRET) as fake:
        _register(gateway, "http", tmp_path, fake)
        run_id = _start(gateway, {"tools": ["read_file", MCP], "_runtime": {"untrusted_input": True}})
        _wait_until(lambda: _run(gateway, run_id).get("status") in ("completed", "waiting"))
        assert LLM_PAYLOADS and all(MCP not in _offered_names(p) for p in LLM_PAYLOADS)
        assert fake.calls() == []


def test_a_server_failing_initialize_at_run_start_is_skipped_with_a_note(gateway, tmp_path):
    with FakeHttpEcho(require_bearer=SECRET) as fake:
        _register(gateway, "http", tmp_path, fake)
        fake.fail_initialize = True
        run_id = _start(gateway, {"tools": ["read_file", MCP]})
        _wait_until(lambda: _run(gateway, run_id).get("status") == "completed")
        assert all(MCP not in _offered_names(p) for p in LLM_PAYLOADS) and LLM_PAYLOADS
        rt = _vars(run_id)["_runtime"]
        assert rt["mcp_notes"] == ["MCP server fake skipped: The server answered but refused initialize: maintenance"]


def test_a_server_disabled_after_the_run_started_refuses_the_approved_call(gateway, tmp_path):
    """The registry is read again at call time: an approved call to a server an admin disabled in
    the meantime never reaches it, and the run gets the reason as the tool's error."""
    with FakeHttpEcho(require_bearer=SECRET) as fake:
        _register(gateway, "http", tmp_path, fake)
        run_id = _start(gateway, {"tools": ["read_file", MCP]})
        rid, wait = _wait_until(lambda: _tool_wait(gateway, run_id))
        off = gateway.post("/api/gateway/admin/mcp/servers/fake/agents", headers=HEADERS, json={"enabled": False})
        assert off.status_code == 200 and off.json()["agents_status"] == "Not offered to agents"
        gateway.post("/api/gateway/commands", headers=HEADERS, json={
            "command_id": "approve-late", "run_id": rid, "type": "resume",
            "payload": {"wait_key": wait["wait_key"], "payload": {"approved": True}}})
        _wait_until(lambda: _run(gateway, run_id).get("status") == "completed")
        assert fake.calls() == []
        assert "The MCP server fake is not enabled for agents." in json.dumps(_ledger(gateway, rid), default=str)
