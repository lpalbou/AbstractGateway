"""Start pins are optional unless an author explicitly requires them.

No Gateway service or provider is started here. These are contract tests for
the descriptor consumed by every schema-driven workflow UI.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _schema(flow: dict) -> dict:
    from abstractgateway.routes.gateway import _entrypoint_input_schema_from_visualflow

    return _entrypoint_input_schema_from_visualflow(flow)


def _flow(outputs: list[dict], defaults: dict | None = None) -> dict:
    return {
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "data": {
                    "nodeType": "on_flow_start",
                    "outputs": [{"id": "exec-out", "type": "execution"}, *outputs],
                    "pinDefaults": defaults or {},
                },
            }
        ]
    }


def test_unset_visualflow_pins_do_not_invent_required_inputs_or_defaults() -> None:
    contract = _schema(
        _flow(
            [
                {"id": "provider", "type": "provider"},
                {"id": "model", "type": "model"},
                {"id": "max_in_tokens", "type": "number"},
                {"id": "primary_image_artifact", "type": "artifact_image"},
                {"id": "resp_schema", "type": "object"},
                {"id": "image_provider", "type": "string"},
            ]
        )
    )

    assert contract["input_data_schema"].get("required", []) == []
    assert contract["defaults"] == {}
    assert all(pin["required"] is False for pin in contract["inputs"])
    assert all("default" not in field for field in contract["input_data_schema"]["properties"].values())
    image = contract["input_data_schema"]["properties"]["primary_image_artifact"]
    # The optional artifact's own required key is not a top-level requirement.
    assert image["required"] == ["$artifact"]
    assert image["x-abstract-artifact-modality"] == "image"


def test_explicit_required_is_preserved_with_or_without_a_default() -> None:
    contract = _schema(
        _flow(
            [
                {"id": "ticket", "type": "string", "required": True, "schema": {"minLength": 1}},
                {"id": "label", "type": "string", "required": True},
                {"id": "optional", "type": "string", "required": False},
                {"id": "not_a_boolean", "type": "string", "required": "false"},
            ],
            {"label": ""},
        )
    )

    assert contract["input_data_schema"]["required"] == ["ticket", "label"]
    assert contract["input_data_schema"]["properties"]["ticket"]["minLength"] == 1
    assert contract["defaults"] == {"label": ""}


def test_falsy_and_nullable_defaults_and_typed_provider_pins_survive() -> None:
    defaults = {"enabled": False, "limit": 0, "label": "", "schema": None, "provider": "endpoint:flow", "model": "flow-model"}
    contract = _schema(
        _flow(
            [
                {"id": "enabled", "type": "boolean"},
                {"id": "limit", "type": "integer"},
                {"id": "label", "type": "string"},
                {"id": "schema", "type": "object", "schema": {"type": ["object", "null"]}},
                {"id": "provider", "type": "provider"},
                {"id": "model", "type": "model"},
            ],
            defaults,
        )
    )

    assert contract["defaults"] == defaults
    props = contract["input_data_schema"]["properties"]
    assert {key: field["default"] for key, field in props.items()} == defaults
    assert props["schema"]["type"] == ["object", "null"]
    assert props["provider"]["x-abstract-type"] == "provider"
    assert props["model"]["x-abstract-type"] == "model"


def test_actual_assistant_orchestrator_does_not_require_optional_media_inputs() -> None:
    # This cross-repository regression additionally checks the source that
    # produced the user's failing screen. The synthetic contract above always
    # runs when AbstractGateway is checked out on its own.
    source = Path(__file__).resolve().parents[2] / "abstractassistant" / "abstractassistant" / "assistant_workflow.py"
    if not source.is_file():
        pytest.skip("AbstractAssistant sibling checkout is unavailable")
    spec = importlib.util.spec_from_file_location("_assistant_input_contract_fixture", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    flow = module.managed_assistant_visualflow()
    contract = _schema(flow)
    props = contract["input_data_schema"]["properties"]

    optional = {
        "max_in_tokens", "resp_schema", "primary_image_artifact", "provider", "model",
        "image_provider", "image_model", "image_edit_provider", "image_edit_model",
        "image_upscale_provider", "image_upscale_model", "video_provider", "video_model",
        "image_to_video_provider", "image_to_video_model", "music_provider", "music_model",
    }
    assert optional <= props.keys()
    assert not optional.intersection(contract["input_data_schema"].get("required", []))
    assert not optional.intersection(contract["defaults"])
    assert all("default" not in props[name] for name in optional)
    assert contract["defaults"]["has_primary_image_context"] is False
    assert contract["defaults"]["max_iterations"] == 24
    assert contract["defaults"]["system"].startswith("You are AbstractAssistant")
