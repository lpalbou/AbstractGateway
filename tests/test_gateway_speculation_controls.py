"""MTP controls use the existing Core authority and preserve explicit Off."""
from __future__ import annotations

import asyncio
import pytest
from pydantic import ValidationError

from abstractgateway.routes import gateway as routes
from test_gateway_core_config_authority import scoped_store, split_core_server, _stored_routes, _served_row, _core_side_write, _split_row
from test_gateway_console_offline import _console_script, _slice_function, _node, _save_default_harness


@pytest.mark.parametrize("value", [False, True, {"mode": "native_mtp", "num_draft_tokens": 4}, {"num_draft_tokens": 3}])
@pytest.mark.parametrize("request_model", [routes.StartRunRequest, routes.ScheduleRunRequest])
def test_run_and_schedule_control_preserves_explicit_value(request_model, value):
    result = request_model(bundle_id="demo", flow_id="root", speculation=value)
    assert result.speculation == value


@pytest.mark.parametrize("value", [2, "typo", [], {"num_draft_tokens": 0}, {"num_draft_tokens": True}, {"typo": 2}])
@pytest.mark.parametrize("embedded", [False, True])
def test_invalid_speculation_is_rejected_before_durable_run_creation(value, embedded):
    payload = {"input_data": {"_runtime": {"speculation": value}}} if embedded else {"speculation": value}
    with pytest.raises(ValidationError):
        routes.StartRunRequest(bundle_id="demo", flow_id="root", **payload)


def test_gateway_default_uses_core_store_and_preserves_unrelated_options(scoped_store):
    from abstractgateway.core_config import save_gateway_capability_default
    options = {"temperature": 0.2, "speculation": {"mode": "native_mtp", "num_draft_tokens": 2, "require_acceleration": False}}
    save_gateway_capability_default("output", "text", provider="mlx", model="test-model", options=options)
    save_gateway_capability_default("input", "text", model="another-model")
    assert _stored_routes(scoped_store)["input.text"]["options"] == options
    assert _served_row(scoped_store, "output.text")["options"] == options
    save_gateway_capability_default("input", "text", options={"temperature": 0.2, "speculation": False})
    assert _stored_routes(scoped_store)["input.text"]["options"]["speculation"] is False
    save_gateway_capability_default("input", "text", options={"temperature": 0.2})
    assert "speculation" not in _stored_routes(scoped_store)["input.text"]["options"]


def test_execution_capability_discovery_uses_actual_runtime_facade(monkeypatch):
    class Discovery:
        def get_model_capabilities(self, model):
            return {"model": model, "capabilities": {"vision_support": True}}

        def get_execution_capabilities(self, model_name=None, *, provider=None):
            assert model_name == "candidate" and provider == "mlx"
            return {"speculation": {"supported": True, "ready": False, "reason": "head not loaded"}}

    monkeypatch.setattr(routes, "_gateway_abstractcore_discovery_facade", lambda: (Discovery(), None))
    result = asyncio.run(routes.discovery_model_capabilities(model_name="candidate", provider="mlx"))
    assert result["execution"]["speculation"]["ready"] is False
    assert result["capabilities"]["vision_support"] is True


def test_split_gateway_preserves_then_clears_core_speculation_default(split_core_server):
    core_config = split_core_server
    _core_side_write(provider="mlx", model="candidate", options={"temperature": 0.3, "speculation": {"num_draft_tokens": 2}})
    core_config.save_gateway_capability_default("output", "text", model="next-candidate")
    assert _split_row(core_config, "output.text")["options"]["speculation"] == {"num_draft_tokens": 2}
    core_config.save_gateway_capability_default("output", "text", options={"temperature": 0.3, "speculation": False})
    assert _split_row(core_config, "output.text")["options"]["speculation"] is False
    core_config.save_gateway_capability_default("output", "text", options={"temperature": 0.3})
    assert _split_row(core_config, "output.text")["options"] == {"temperature": 0.3}


def test_fresh_install_gateway_reads_core_depth_two_policy(tmp_path, monkeypatch):
    from abstractgateway.core_config import gateway_capability_defaults_payload
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(tmp_path / "fresh-core.json"))
    payload = gateway_capability_defaults_payload()
    text = next(row for row in payload["routes"] if row["key"] == "input.text")
    assert text["options"]["speculation"] == {"mode": "native_mtp", "num_draft_tokens": 2, "require_acceleration": False}


def test_unavailable_host_discovery_is_not_replaced_with_registry_claims(monkeypatch):
    class Discovery:
        def get_model_capabilities(self, model):
            return {"capabilities": {"speculation": {"native_mtp": True}}}

        def get_execution_capabilities(self, *args, **kwargs):
            raise RuntimeError("remote Core unreachable")

    monkeypatch.setattr(routes, "_gateway_abstractcore_discovery_facade", lambda: (Discovery(), None))
    result = asyncio.run(routes.discovery_model_capabilities(model_name="candidate", provider="mlx"))
    assert result["execution"]["available"] is False
    assert "unreachable" in result["execution"]["error"]


def test_web_mtp_selector_edits_only_its_owned_option():
    source = _console_script()
    script = _slice_function(source, "speculationFromChoice") + _save_default_harness("""
state.activeDefaultRow = {key:"input.text", kind:"input", modality:"text", provider:"mlx", model:"model", options:{temperature:0.3, speculation:{mode:"native_mtp",num_draft_tokens:2,drafter:"matching/head"}}};
$("modal-default-provider").value="mlx";
$("modal-default-model").value="model";
$("modal-default-provider-custom").className="hidden";
$("modal-default-model-custom").className="hidden";
$("modal-default-options").value='{"temperature":0.3}';
prefillFrom(state.activeDefaultRow);
state.defaultModalPrefill.speculation="2";
$("modal-default-speculation").value="2";
await saveDefault(); results.push(puts.at(-1).body);
$("modal-default-speculation").value="4";
await saveDefault(); results.push(puts.at(-1).body);
$("modal-default-speculation").value="off";
await saveDefault(); results.push(puts.at(-1).body);
$("modal-default-speculation").value="";
await saveDefault(); results.push(puts.at(-1).body);
""")
    unchanged, depth, off, inherit = _node(script)
    assert "options" not in unchanged, "An untouched picker must not roll back newer Core settings"
    assert depth["options"]["temperature"] == 0.3
    assert depth["options"]["speculation"]["num_draft_tokens"] == 4
    assert depth["options"]["speculation"]["drafter"] == "matching/head"
    assert depth["options"]["speculation"]["require_acceleration"] is False
    assert off["options"]["speculation"] is False
    assert inherit["options"] == {"temperature": 0.3}


@pytest.mark.parametrize("endpoint", ["start", "schedule"])
@pytest.mark.parametrize("value", [False, {"mode": "native_mtp", "num_draft_tokens": 4}])
def test_run_and_schedule_fold_and_wrapper_lift(tmp_path, monkeypatch, endpoint, value):
    from test_gateway_discovery_endpoints import _make_client
    from test_gateway_schedule_thinking import _load_run_json
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    payload = {"bundle_id": "bundle-discovery", "flow_id": "root", "input_data": {"_runtime": {"speculation": {"num_draft_tokens": 2}}}, "speculation": value}
    if endpoint == "schedule":
        payload["start_at"] = "2099-01-01T00:00:00Z"
    with client:
        response = client.post(f"/api/gateway/runs/{endpoint}", headers=headers, json=payload)
        assert response.status_code == 200, response.text
        run = _load_run_json(tmp_path / "runtime", response.json()["run_id"])
        assert run["vars"]["_runtime"]["speculation"] == value
        if endpoint == "schedule":
            assert run["vars"]["vars"]["_runtime"]["speculation"] == value
