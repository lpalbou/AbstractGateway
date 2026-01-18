from __future__ import annotations

import json
import time
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient


def _wait_until(predicate, *, timeout_s: float = 5.0, poll_s: float = 0.05):
    end = time.time() + timeout_s
    while time.time() < end:
        if predicate():
            return
        time.sleep(poll_s)
    raise AssertionError("timeout waiting for condition")


def _write_bundle(*, bundles_dir: Path, bundle_id: str, flows: dict[str, dict], entrypoint: str) -> tuple[str, str]:
    bundles_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-08T00:00:00+00:00",
        "entrypoints": [{"flow_id": entrypoint, "name": "test", "description": "", "interfaces": []}],
        "flows": {fid: f"flows/{fid}.json" for fid in flows.keys()},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }

    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for fid, flow in flows.items():
            zf.writestr(f"flows/{fid}.json", json.dumps(flow, indent=2))

    return bundle_id, entrypoint


def _stub_runtime_effect_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch AbstractCore integration so LLM calls are deterministic and offline."""
    from abstractruntime.core.runtime import EffectOutcome
    from abstractruntime.core.models import EffectType
    from abstractruntime.integrations.abstractcore import factory as ac_factory
    from abstractruntime.integrations.abstractcore.effect_handlers import make_tool_calls_handler

    def _llm_stub(run, effect, default_next_node):
        del run, default_next_node
        payload = dict(effect.payload or {})
        prompt = payload.get("prompt")
        # Deterministic response for tests. ReAct logic treats this as a final answer.
        return EffectOutcome.completed(
            {
                "content": "FINAL: ok",
                "tool_calls": [],
                "model": payload.get("model"),
                "provider": payload.get("provider"),
                "prompt": str(prompt or ""),
            }
        )

    def _build_effect_handlers(*, llm, tools, artifact_store=None, run_store=None):
        del llm
        del artifact_store
        del run_store
        return {
            EffectType.LLM_CALL: _llm_stub,
            EffectType.TOOL_CALLS: make_tool_calls_handler(tools=tools),
        }

    monkeypatch.setattr(ac_factory, "build_effect_handlers", _build_effect_handlers)


def test_gateway_bundle_llm_call_completes_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"

    flow_id = "root"
    flow = {
        "id": flow_id,
        "name": "llm-test",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 224.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "node-2",
                "type": "llm_call",
                "position": {"x": 288.0, "y": 224.0},
                "data": {
                    "nodeType": "llm_call",
                    "label": "LLM Call",
                    "inputs": [
                        {"id": "exec-in", "label": "", "type": "execution"},
                        {"id": "prompt", "label": "prompt", "type": "string"},
                    ],
                    "outputs": [
                        {"id": "exec-out", "label": "", "type": "execution"},
                        {"id": "response", "label": "response", "type": "string"},
                    ],
                    "pinDefaults": {"prompt": "hello"},
                    "effectConfig": {"provider": "ollama", "model": "qwen3:1.7b"},
                },
            },
            {
                "id": "node-3",
                "type": "on_flow_end",
                "position": {"x": 544.0, "y": 224.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [
            {"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"},
            {"id": "e2", "source": "node-2", "sourceHandle": "exec-out", "target": "node-3", "targetHandle": "exec-in"},
        ],
        "entryNode": "node-1",
    }

    bundle_id, entry = _write_bundle(bundles_dir=bundles_dir, bundle_id="bundle-llm", flows={flow_id: flow}, entrypoint=flow_id)
    assert entry == flow_id

    _stub_runtime_effect_handlers(monkeypatch)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    # Contract: bundle mode must not require importing AbstractFlow at runtime.
    import sys

    assert "abstractflow" not in sys.modules

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        def _is_completed():
            rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            return rr.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=10.0, poll_s=0.1)

        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=500", headers=headers)
        assert ledger.status_code == 200, ledger.text
        items = ledger.json().get("items") or []
        assert any(isinstance(i, dict) and isinstance(i.get("effect"), dict) and i["effect"].get("type") == "llm_call" for i in items)


def test_gateway_bundle_agent_node_starts_react_subworkflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"

    root_id = "root"
    agent_node_id = "agent-1"
    flow = {
        "id": root_id,
        "name": "agent-test",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 224.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": agent_node_id,
                "type": "agent",
                "position": {"x": 288.0, "y": 224.0},
                "data": {
                    "nodeType": "agent",
                    "label": "Agent",
                    "inputs": [
                        {"id": "exec-in", "label": "", "type": "execution"},
                        {"id": "task", "label": "task", "type": "string"},
                    ],
                    "outputs": [
                        {"id": "exec-out", "label": "", "type": "execution"},
                        {"id": "result", "label": "result", "type": "string"},
                    ],
                    "pinDefaults": {"task": "say hello"},
                    "agentConfig": {"provider": "ollama", "model": "qwen3:1.7b", "tools": []},
                },
            },
            {
                "id": "node-3",
                "type": "on_flow_end",
                "position": {"x": 544.0, "y": 224.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [
            {"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": agent_node_id, "targetHandle": "exec-in"},
            {"id": "e2", "source": agent_node_id, "sourceHandle": "exec-out", "target": "node-3", "targetHandle": "exec-in"},
        ],
        "entryNode": "node-1",
    }

    bundle_id, _entry = _write_bundle(bundles_dir=bundles_dir, bundle_id="bundle-agent", flows={root_id: flow}, entrypoint=root_id)

    _stub_runtime_effect_handlers(monkeypatch)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": root_id, "input_data": {}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        def _is_completed():
            rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            return rr.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=10.0, poll_s=0.1)

        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=500", headers=headers)
        assert ledger.status_code == 200, ledger.text
        items = ledger.json().get("items") or []

        # Ensure the START_SUBWORKFLOW references the derived agent workflow id (namespaced).
        from abstractruntime.visualflow_compiler.visual.agent_ids import visual_react_workflow_id

        expected_child = visual_react_workflow_id(flow_id=f"{bundle_id}@0.0.0:{root_id}", node_id=agent_node_id)
        started = [
            i
            for i in items
            if isinstance(i, dict)
            and isinstance(i.get("effect"), dict)
            and i["effect"].get("type") == "start_subworkflow"
        ]
        assert started, "Expected a start_subworkflow effect in the ledger"
        payload = started[0]["effect"].get("payload") if isinstance(started[0].get("effect"), dict) else {}
        assert isinstance(payload, dict)
        assert payload.get("workflow_id") == expected_child


def test_gateway_bundle_tool_calls_passthrough_waits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"

    flow_id = "root"
    flow = {
        "id": flow_id,
        "name": "tool-test",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 224.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "node-2",
                "type": "tool_calls",
                "position": {"x": 288.0, "y": 224.0},
                "data": {
                    "nodeType": "tool_calls",
                    "label": "Tool Calls",
                    "inputs": [
                        {"id": "exec-in", "label": "", "type": "execution"},
                        {"id": "tool_calls", "label": "tool_calls", "type": "array"},
                    ],
                    "outputs": [
                        {"id": "exec-out", "label": "", "type": "execution"},
                        {"id": "results", "label": "results", "type": "array"},
                    ],
                    "pinDefaults": {"tool_calls": [{"name": "list_files", "arguments": {}}]},
                },
            },
            {
                "id": "node-3",
                "type": "on_flow_end",
                "position": {"x": 544.0, "y": 224.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [
            {"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"},
            {"id": "e2", "source": "node-2", "sourceHandle": "exec-out", "target": "node-3", "targetHandle": "exec-in"},
        ],
        "entryNode": "node-1",
    }

    bundle_id, _entry = _write_bundle(bundles_dir=bundles_dir, bundle_id="bundle-tools", flows={flow_id: flow}, entrypoint=flow_id)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_TOOL_MODE", "passthrough")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        def _is_waiting_tool():
            rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            body = rr.json()
            if body.get("status") != "waiting":
                return False
            w = body.get("waiting")
            if not isinstance(w, dict):
                return False
            details = w.get("details")
            if not isinstance(details, dict):
                return False
            return details.get("mode") == "approval_required"

        _wait_until(_is_waiting_tool, timeout_s=10.0, poll_s=0.1)


def test_gateway_bundle_tool_calls_local_executes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    (sandbox_dir / "a.txt").write_text("x", encoding="utf-8")

    flow_id = "root"
    flow = {
        "id": flow_id,
        "name": "tool-test-local",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 224.0},
                "data": {
                    "nodeType": "on_flow_start",
                    "label": "On Flow Start",
                    "inputs": [],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}],
                },
            },
            {
                "id": "node-2",
                "type": "tool_calls",
                "position": {"x": 288.0, "y": 224.0},
                "data": {
                    "nodeType": "tool_calls",
                    "label": "Tool Calls",
                    "inputs": [
                        {"id": "exec-in", "label": "", "type": "execution"},
                        {"id": "tool_calls", "label": "tool_calls", "type": "array"},
                    ],
                    "outputs": [
                        {"id": "exec-out", "label": "", "type": "execution"},
                        {"id": "results", "label": "results", "type": "array"},
                    ],
                    "pinDefaults": {"tool_calls": [{"name": "list_files", "arguments": {"directory_path": str(sandbox_dir)}}]},
                },
            },
            {
                "id": "node-3",
                "type": "on_flow_end",
                "position": {"x": 544.0, "y": 224.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [
            {"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"},
            {"id": "e2", "source": "node-2", "sourceHandle": "exec-out", "target": "node-3", "targetHandle": "exec-in"},
        ],
        "entryNode": "node-1",
    }

    bundle_id, _entry = _write_bundle(bundles_dir=bundles_dir, bundle_id="bundle-tools-local", flows={flow_id: flow}, entrypoint=flow_id)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_TOOL_MODE", "local")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {"workspace_root": str(sandbox_dir)}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        def _is_completed():
            rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            body = rr.json()
            return body.get("status") == "completed" and not body.get("error")

        _wait_until(_is_completed, timeout_s=10.0, poll_s=0.1)

        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=500", headers=headers)
        assert ledger.status_code == 200, ledger.text
        items = ledger.json().get("items") or []

        tool_steps = [
            i
            for i in items
            if isinstance(i, dict)
            and isinstance(i.get("effect"), dict)
            and i["effect"].get("type") == "tool_calls"
            and i.get("status") == "completed"
        ]
        assert tool_steps, "Expected a tool_calls effect in the ledger"
        result = tool_steps[0].get("result")
        assert isinstance(result, dict)
        assert result.get("mode") == "executed"
        results = result.get("results")
        assert isinstance(results, list) and results, "Expected executed tool results"
        assert results[0].get("name") == "list_files"
        assert results[0].get("success") is True
        assert "a.txt" in str(results[0].get("output") or "")


def test_gateway_bundle_start_defaults_to_entrypoint_when_flow_id_omitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"

    flow_id = "root"
    flow = {
        "id": flow_id,
        "name": "entrypoint-test",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 224.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "node-2",
                "type": "on_flow_end",
                "position": {"x": 288.0, "y": 224.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}],
        "entryNode": "node-1",
    }

    bundle_id, _entry = _write_bundle(bundles_dir=bundles_dir, bundle_id="bundle-entrypoint", flows={flow_id: flow}, entrypoint=flow_id)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "input_data": {}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        def _is_completed():
            rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            body = rr.json()
            return body.get("status") == "completed" and body.get("workflow_id") == f"{bundle_id}@0.0.0:{flow_id}"

        _wait_until(_is_completed, timeout_s=10.0, poll_s=0.1)


def test_gateway_bundle_start_requires_flow_id_when_multiple_entrypoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"

    flows = {}
    for fid in ("root1", "root2"):
        flows[fid] = {
            "id": fid,
            "name": fid,
            "description": "",
            "interfaces": [],
            "nodes": [
                {
                    "id": "node-1",
                    "type": "on_flow_start",
                    "position": {"x": 32.0, "y": 224.0},
                    "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
                },
                {
                    "id": "node-2",
                    "type": "on_flow_end",
                    "position": {"x": 288.0, "y": 224.0},
                    "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
                },
            ],
            "edges": [{"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}],
            "entryNode": "node-1",
        }

    # Write a bundle with multiple entrypoints.
    bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = "bundle-multi-entry"
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-09T00:00:00+00:00",
        "entrypoints": [
            {"flow_id": "root1", "name": "root1", "description": "", "interfaces": []},
            {"flow_id": "root2", "name": "root2", "description": "", "interfaces": []},
        ],
        "flows": {fid: f"flows/{fid}.json" for fid in flows.keys()},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for fid, flow in flows.items():
            zf.writestr(f"flows/{fid}.json", json.dumps(flow, indent=2))

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "input_data": {}},
            headers=headers,
        )
        assert r.status_code == 400, r.text
        assert "flow_id" in r.text
        assert "entrypoint" in r.text or "entrypoints" in r.text


def test_gateway_bundle_start_uses_default_entrypoint_when_multiple_entrypoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"

    flows = {}
    for fid in ("root1", "root2"):
        flows[fid] = {
            "id": fid,
            "name": fid,
            "description": "",
            "interfaces": [],
            "nodes": [
                {
                    "id": "node-1",
                    "type": "on_flow_start",
                    "position": {"x": 32.0, "y": 224.0},
                    "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
                },
                {
                    "id": "node-2",
                    "type": "on_flow_end",
                    "position": {"x": 288.0, "y": 224.0},
                    "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
                },
            ],
            "edges": [{"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}],
            "entryNode": "node-1",
        }

    bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = "bundle-multi-default"
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-09T00:00:00+00:00",
        "entrypoints": [
            {"flow_id": "root1", "name": "root1", "description": "", "interfaces": []},
            {"flow_id": "root2", "name": "root2", "description": "", "interfaces": []},
        ],
        "default_entrypoint": "root2",
        "flows": {fid: f"flows/{fid}.json" for fid in flows.keys()},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for fid, flow in flows.items():
            zf.writestr(f"flows/{fid}.json", json.dumps(flow, indent=2))

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "input_data": {}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
        assert rr.status_code == 200, rr.text
        run = rr.json()
        assert run.get("workflow_id") == f"{bundle_id}@0.0.0:root2"

        rb = client.get(f"/api/gateway/bundles/{bundle_id}", headers=headers)
        assert rb.status_code == 200, rb.text
        b = rb.json()
        assert b.get("default_entrypoint") == "root2"


def test_gateway_bundle_metadata_endpoints_expose_entrypoint_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"

    flow_id = "root"
    flow = {
        "id": flow_id,
        "name": "meta-test",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 224.0},
                "data": {
                    "nodeType": "on_flow_start",
                    "label": "On Flow Start",
                    "headerColor": "#C0392B",
                    "inputs": [],
                    "outputs": [
                        {"id": "exec-out", "label": "", "type": "execution"},
                        {"id": "prompt", "label": "prompt", "type": "string"},
                        {"id": "max_iterations", "label": "max_iterations", "type": "number"},
                    ],
                    "pinDefaults": {"max_iterations": 5},
                },
            },
            {
                "id": "node-2",
                "type": "on_flow_end",
                "position": {"x": 288.0, "y": 224.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}],
        "entryNode": "node-1",
    }

    bundle_id, _entry = _write_bundle(bundles_dir=bundles_dir, bundle_id="bundle-meta", flows={flow_id: flow}, entrypoint=flow_id)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.get("/api/gateway/bundles", headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("default_bundle_id") == bundle_id
        items = data.get("items")
        assert isinstance(items, list)
        assert any(isinstance(i, dict) and i.get("bundle_id") == bundle_id for i in items)

        r2 = client.get(f"/api/gateway/bundles/{bundle_id}", headers=headers)
        assert r2.status_code == 200, r2.text
        b = r2.json()
        assert b.get("bundle_id") == bundle_id
        eps = b.get("entrypoints")
        assert isinstance(eps, list) and eps
        ep0 = eps[0]
        assert ep0.get("flow_id") == flow_id
        assert ep0.get("workflow_id") == f"{bundle_id}@0.0.0:{flow_id}"
        inputs = ep0.get("inputs")
        assert isinstance(inputs, list)
        # Ensure exec pin is omitted and defaults are surfaced.
        assert any(isinstance(x, dict) and x.get("id") == "prompt" for x in inputs)
        max_it = next((x for x in inputs if isinstance(x, dict) and x.get("id") == "max_iterations"), None)
        assert isinstance(max_it, dict)
        assert max_it.get("default") == 5
        node_index = ep0.get("node_index")
        assert isinstance(node_index, dict)
        assert isinstance(node_index.get("node-1"), dict)

        rf = client.get(f"/api/gateway/bundles/{bundle_id}/flows/{flow_id}", headers=headers)
        assert rf.status_code == 200, rf.text
        flow_payload = rf.json()
        assert flow_payload.get("bundle_id") == bundle_id
        assert flow_payload.get("flow_id") == flow_id
        assert flow_payload.get("workflow_id") == f"{bundle_id}@0.0.0:{flow_id}"
        assert isinstance(flow_payload.get("flow"), dict)

        # Attach UX helper: input_data endpoint returns only entrypoint pin ids (bundle mode).
        rstart = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {"prompt": "hi", "max_iterations": 7, "extra": "ignored"}},
            headers=headers,
        )
        assert rstart.status_code == 200, rstart.text
        run_id = rstart.json()["run_id"]

        rin = client.get(f"/api/gateway/runs/{run_id}/input_data", headers=headers)
        assert rin.status_code == 200, rin.text
        payload = rin.json()
        assert payload.get("bundle_id") == bundle_id
        assert payload.get("flow_id") == flow_id
        assert payload.get("workflow_id") == f"{bundle_id}@0.0.0:{flow_id}"
        assert payload.get("input_data") == {"prompt": "hi", "max_iterations": 7}
