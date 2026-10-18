"""Phone inputs reach actual LLM schemas through shipped workflows, offline.

Only the LLM transport is replaced: HTTP start, routing, compilation, registry
schema materialization, and approval handling are real.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def assistant_gateway(tmp_path, monkeypatch):
    from abstractruntime.core.models import EffectType
    from abstractruntime.core.runtime import EffectOutcome
    from abstractruntime.integrations.abstractcore import factory
    from abstractruntime.integrations.abstractcore.effect_handlers import make_tool_calls_handler

    calls = []
    behavior = {"request_read": False}

    def llm_stub(run, effect, default_next_node):
        payload = copy.deepcopy(effect.payload)
        # Exercise the orchestrator's real structured-output edge into chat.
        if payload.get("response_schema"):
            data = {"mode": "chat", "media_prompt": "", "assistant_message": ""}
            return EffectOutcome.completed({"content": json.dumps(data), "data": data, "tool_calls": []})
        calls.append({"run_id": run.run_id, "payload": payload,
                      "runtime": copy.deepcopy(run.vars.get("_runtime", {}))})
        tool_calls = ([{"name": "read_file", "arguments": {"file_path": "example.txt"}, "call_id": "read-1"}]
                      if behavior["request_read"] else [])
        return EffectOutcome.completed({"content": "" if tool_calls else "FINAL: done", "tool_calls": tool_calls})

    def handlers(*, llm, tools, artifact_store=None, run_store=None):
        return {EffectType.LLM_CALL: llm_stub,
                EffectType.TOOL_CALLS: make_tool_calls_handler(tools=tools, artifact_store=artifact_store, run_store=run_store)}

    monkeypatch.setattr(factory, "build_effect_handlers", handlers)
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    shipped = Path(__file__).resolve().parents[1] / "flows" / "bundles"
    for name in ("abstractassistant-orchestrator@0.0.0.flow", "basic-agent.flow"):
        shutil.copyfile(shipped / name, bundles / name)
    for key, value in {
        "ABSTRACTGATEWAY_DATA_DIR": str(tmp_path / "runtime"),
        "ABSTRACTGATEWAY_FLOWS_DIR": str(bundles),
        "ABSTRACTGATEWAY_WORKFLOW_SOURCE": "bundle",
        "ABSTRACTGATEWAY_AUTH_TOKEN": "test-phone",
        "ABSTRACTGATEWAY_ALLOWED_ORIGINS": "*",
        "ABSTRACTGATEWAY_POLL_S": "0.02",
        "ABSTRACTGATEWAY_TICK_WORKERS": "1",
    }.items():
        monkeypatch.setenv(key, value)
    from abstractgateway.app import app
    with TestClient(app, headers={"Authorization": "Bearer test-phone"}) as client:
        yield client, calls, behavior


def start(client, input_data, *, basic=False):
    result = client.post("/api/gateway/runs/start", json={
        "bundle_id": "basic-agent" if basic else "abstractassistant-orchestrator",
        "flow_id": "81795ea9" if basic else "d5d4e5a1",
        "input_data": {"prompt": "Inspect the environment", **input_data},
    })
    assert result.status_code == 200, result.text
    return result.json()["run_id"]


def wait_for(predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for real gateway execution")


@pytest.mark.parametrize("selection", [None, [], ["read_file", "list_files"]],
                         ids=["omitted-regression", "deny-all", "resolved-inventory"])
def test_assistant_phone_tools_reach_llm_schemas(assistant_gateway, selection):
    client, calls, _ = assistant_gateway
    inputs = {} if selection is None else {"tools": selection, "_runtime": {"allowed_tools": selection}}
    root = start(client, inputs)
    wait_for(lambda: client.get(f"/api/gateway/runs/{root}").json().get("status") == "completed")
    assert calls, "The chat Agent must actually call its LLM"
    schemas = {tool["name"]: tool for tool in calls[0]["payload"].get("tools", [])}
    assert set(schemas) == set(selection or [])
    assert set(calls[0]["runtime"]["allowed_tools"]) == set(selection or [])
    if selection:
        assert schemas["read_file"]["parameters"]["file_path"]["type"] == "string"
        assert "default" not in schemas["read_file"]["parameters"]["file_path"]
        assert schemas["list_files"]["parameters"]["directory_path"]["type"] == "string"
        assert not calls[0]["runtime"].get("tool_policy", {}).get("auto_approve_tools")


def test_basic_agent_has_its_own_defaults(assistant_gateway):
    client, calls, _ = assistant_gateway
    root = start(client, {}, basic=True)
    wait_for(lambda: client.get(f"/api/gateway/runs/{root}").json().get("status") == "completed")
    schemas = {tool["name"]: tool for tool in calls[0]["payload"]["tools"]}
    assert {"read_file", "list_files", "web_search"} <= schemas.keys()
    assert schemas["read_file"]["parameters"]["file_path"]["type"] == "string"


def test_explicit_phone_approval_reaches_child_and_waits(assistant_gateway):
    client, calls, behavior = assistant_gateway
    behavior["request_read"] = True
    policy = {"auto_approve_tools": [], "require_approval_tools": ["read_file"]}
    start(client, {"tools": ["read_file"], "_runtime": {"allowed_tools": ["read_file"], "tool_policy": policy}})
    wait_for(lambda: calls)
    assert calls[0]["runtime"]["tool_policy"] == policy
    child_id = calls[0]["run_id"]
    child = wait_for(lambda: (value if (value := client.get(f"/api/gateway/runs/{child_id}").json()).get("status") == "waiting" else None))
    assert "tool_approval" in json.dumps(child["waiting"])
    assert "read_file" in json.dumps(child["waiting"])
