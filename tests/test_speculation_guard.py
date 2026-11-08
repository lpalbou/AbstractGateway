"""The MTP backstop (operator rule 2026-11-07): a saved MTP request never fails a run.

Incident: a saved "MTP depth 2, require acceleration" was sent with a model that has no MTP, and
AbstractCore refused every call (`SpeculationUnavailableError`), so every run failed. The gateway
now asks the execution host's capability card before each model call and drops a request the card
says the model cannot honour, recording that on the step.

These tests use the REAL AbstractRuntime LLM_CALL handler and the REAL AbstractCore client and
capability card (an `openai-compatible` provider has no MTP backend), against a local fake OpenAI
server: without the guard the strict request raises inside AbstractCore and the run fails.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Dict

import pytest

from model_host_fakes import FakeOpenAIServer

STRICT_MTP = {"mode": "native_mtp", "num_draft_tokens": 2, "require_acceleration": True}


@pytest.fixture
def fake_server():
    with FakeOpenAIServer() as server:
        yield server


def _client(server: FakeOpenAIServer) -> Any:
    from abstractruntime.integrations.abstractcore.llm_client import MultiLocalAbstractCoreLLMClient

    return MultiLocalAbstractCoreLLMClient(
        provider="openai-compatible", model="fake-model",
        llm_kwargs={"base_url": server.base_url, "api_key": "fake", "enable_tracing": False},
    )


def _bundle(bundles_dir: Path, speculation: Any) -> tuple[str, str]:
    flow_id = "root"
    flow = {
        "id": flow_id, "name": "saved-mtp", "description": "", "interfaces": [],
        "nodes": [
            {"id": "start", "type": "on_flow_start", "position": {"x": 0.0, "y": 0.0},
             "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]}},
            {"id": "llm", "type": "llm_call", "position": {"x": 200.0, "y": 0.0},
             "data": {"nodeType": "llm_call", "label": "LLM Call",
                      "inputs": [{"id": "exec-in", "label": "", "type": "execution"},
                                 {"id": "prompt", "label": "prompt", "type": "string"}],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"},
                                  {"id": "response", "label": "response", "type": "string"}],
                      "pinDefaults": {"prompt": "hello"},
                      # The saved node: a model without MTP, and an MTP depth chosen earlier.
                      "effectConfig": {"provider": "openai-compatible", "model": "fake-model",
                                       "speculation": speculation}}},
        ],
        "edges": [{"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "llm", "targetHandle": "exec-in"}],
        "entryNode": "start",
    }
    manifest = {
        "bundle_format_version": "1", "bundle_id": "saved-mtp", "bundle_version": "0.0.0",
        "created_at": "2026-11-07T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "saved-mtp", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"}, "artifacts": {}, "assets": {}, "metadata": {},
    }
    bundles_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundles_dir / "saved-mtp.flow", "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow))
    return "saved-mtp", flow_id


def _host_with_real_llm_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: FakeOpenAIServer) -> Any:
    """A bundle host whose runtime runs model calls with the real handler and client."""
    from abstractruntime.core.models import EffectType
    from abstractruntime.core.runtime import Runtime
    from abstractruntime.integrations.abstractcore import factory as ac_factory
    from abstractruntime.integrations.abstractcore.effect_handlers import make_llm_call_handler
    from abstractruntime.storage.artifacts import InMemoryArtifactStore
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    client = _client(server)

    def _create_local_runtime(**kwargs: Any) -> Runtime:
        runtime = Runtime(
            run_store=kwargs["run_store"], ledger_store=kwargs["ledger_store"],
            artifact_store=kwargs.get("artifact_store") or InMemoryArtifactStore(),
            effect_handlers={EffectType.LLM_CALL: make_llm_call_handler(llm=client)},
        )
        runtime._abstractcore_llm_client = client
        return runtime

    monkeypatch.setattr(ac_factory, "create_local_runtime", _create_local_runtime)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("ABSTRACTCORE_SERVER_BASE_URL", raising=False)

    import abstractgateway.hosts.bundle_host as bundle_host

    return bundle_host.WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=tmp_path / "bundles", data_dir=tmp_path / "runtime",
        run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), artifact_store=InMemoryArtifactStore(),
    )


def _run_to_end(host: Any, run_id: str) -> Any:
    from abstractruntime import RunStatus

    state = host.runtime.get_state(run_id)
    workflow = host.runtime.workflow_registry.get(state.workflow_id)
    for _ in range(20):
        state = host.runtime.tick(workflow=workflow, run_id=run_id)
        if state.status != RunStatus.RUNNING:
            return state
    raise AssertionError(f"run did not finish: {state.status}")


def _llm_step_results(host: Any, run_id: str) -> list:
    out = []
    for record in host.runtime.get_ledger(run_id):
        effect = record.get("effect") if isinstance(record, dict) else None
        if isinstance(effect, dict) and effect.get("type") == "llm_call" and record.get("status") == "completed":
            out.append(record.get("result") or {})
    return out


def test_a_saved_flow_with_mtp_on_a_model_without_mtp_runs_without_it_and_the_ledger_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: FakeOpenAIServer
) -> None:
    from abstractruntime import RunStatus

    bundle_id, flow_id = _bundle(tmp_path / "bundles", STRICT_MTP)
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "bundles"))
    host = _host_with_real_llm_handler(tmp_path, monkeypatch, fake_server)

    run_id = host.start_run(flow_id=flow_id, bundle_id=bundle_id, input_data={})
    state = _run_to_end(host, run_id)

    assert state.status == RunStatus.COMPLETED, state.error
    assert fake_server.requests, "the model was called"
    results = _llm_step_results(host, run_id)
    assert results, "the model step is in the ledger"
    note = (results[-1].get("metadata") or {}).get("speculation_dropped")
    assert note, results[-1].get("metadata")
    assert note["requested"] == STRICT_MTP
    assert note["used"] is False
    assert note["reason"] == "native_mtp_backend_unavailable"  # the card's own slug
    assert note["message"].startswith("MTP was not used: fake-model does not support MTP")


def test_a_run_level_mtp_request_on_a_model_without_mtp_runs_without_it_and_the_ledger_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: FakeOpenAIServer
) -> None:
    """Review NO-GO item 3: an automation or `/runs/start` puts MTP on the RUN
    (`_runtime.speculation`); AbstractRuntime applies it inside the LLM_CALL handler to a step that
    names none. The guard drops it for that call too (the handler sees no run-level request), and
    the step records why."""
    from abstractruntime import RunStatus

    bundle_id, flow_id = _bundle(tmp_path / "bundles", None)  # the node itself asks no MTP
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "bundles"))
    host = _host_with_real_llm_handler(tmp_path, monkeypatch, fake_server)

    run_id = host.start_run(flow_id=flow_id, bundle_id=bundle_id, input_data={"_runtime": {"speculation": STRICT_MTP}})
    assert host.runtime.get_state(run_id).vars["_runtime"]["speculation"] == STRICT_MTP
    state = _run_to_end(host, run_id)

    assert state.status == RunStatus.COMPLETED, state.error
    assert fake_server.requests, "the model was called"
    note = (_llm_step_results(host, run_id)[-1].get("metadata") or {}).get("speculation_dropped")
    assert note and note["requested"] == STRICT_MTP and note["used"] is False, note
    assert note["message"].startswith("MTP was not used: fake-model does not support MTP")
    # The run keeps what it was started with; only that call ran without it.
    assert host.runtime.get_state(run_id).vars["_runtime"]["speculation"] == STRICT_MTP


def test_without_the_guard_the_same_saved_flow_fails_inside_abstractcore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: FakeOpenAIServer
) -> None:
    """The incident, reproduced: proves the first test exercises the real refusal path."""
    from abstractruntime import RunStatus

    import abstractgateway.speculation_guard as guard

    monkeypatch.setattr(guard, "install_speculation_guard", lambda runtime: False)
    bundle_id, flow_id = _bundle(tmp_path / "bundles", STRICT_MTP)
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "bundles"))
    host = _host_with_real_llm_handler(tmp_path, monkeypatch, fake_server)

    run_id = host.start_run(flow_id=flow_id, bundle_id=bundle_id, input_data={})
    state = _run_to_end(host, run_id)
    assert state.status == RunStatus.FAILED
    assert "require_acceleration" in str(state.error)


def test_an_entity_mind_with_mtp_on_a_model_without_mtp_answers_without_it(
    monkeypatch: pytest.MonkeyPatch, fake_server: FakeOpenAIServer
) -> None:
    from abstractgateway import entity_chat, generation_pool

    client = _client(fake_server)
    monkeypatch.setattr(generation_pool, "principal_pool", lambda: client)
    adapter = entity_chat._RuntimeLLMAdapter("openai-compatible", model="fake-model", speculation=STRICT_MTP)

    reply = adapter.generate(messages=[{"role": "user", "content": "hi"}])

    assert reply.content.startswith("reply:") and reply.content.endswith("hi")
    note = reply.metadata["speculation_dropped"]
    assert note["requested"] == STRICT_MTP and note["used"] is False


class _CardClient:
    """A client whose capability card is given; records what generate received."""

    def __init__(self, card: Dict[str, Any]) -> None:
        self.card = card
        self.asked: list = []

    def get_execution_capabilities(self, model_name: Any = None, *, provider: Any = None) -> Dict[str, Any]:
        self.asked.append((provider, model_name))
        return self.card


@pytest.mark.parametrize("card", [
    {"model": "m", "provider": "mlx", "speculation": {"supported": True, "supported_depths": [2, 3, 4, 5]}},
    {"model": "m", "provider": "mlx", "speculation": {"supported": None}},
    {"model": "m", "provider": "mlx"},
])
def test_a_supported_or_unknown_card_keeps_the_request(card: Dict[str, Any]) -> None:
    from abstractgateway.speculation_guard import drop_unsupported_speculation

    params = {"speculation": dict(STRICT_MTP)}
    assert drop_unsupported_speculation(_CardClient(card), params, provider="mlx", model="m") is None
    assert params == {"speculation": STRICT_MTP}


@pytest.mark.parametrize("value", [None, False, {"mode": "off"}])
def test_off_or_no_request_is_never_touched_and_no_card_is_read(value: Any) -> None:
    from abstractgateway.speculation_guard import drop_unsupported_speculation

    client = _CardClient({"speculation": {"supported": False}})
    params = {"speculation": value}
    assert drop_unsupported_speculation(client, params, provider="openai", model="m") is None
    assert params == {"speculation": value}
    assert client.asked == []


def test_an_endpoint_connection_is_checked_by_its_provider_family() -> None:
    from abstractgateway.speculation_guard import drop_unsupported_speculation

    client = _CardClient({"model": "m", "provider": "mlx", "speculation": {"supported": True}})
    client.resolve_provider_endpoint_profile = lambda name: {"provider": "mlx"} if name == "endpoint:box" else None
    params = {"speculation": dict(STRICT_MTP)}
    assert drop_unsupported_speculation(client, params, provider="endpoint:box", model="m") is None
    assert client.asked == [("mlx", "m")]
