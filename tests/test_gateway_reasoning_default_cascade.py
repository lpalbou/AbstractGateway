"""The gateway capability-defaults store IS the reasoning knob (operator
ruling 2026-08-04).

Reasoning effort for unpinned LLM calls — agora residents, scheduled targets,
any run that neither pins `thinking` per call nor carries `_runtime.thinking` —
comes from ONE place: the `reasoning` field on the text-generation capability
route in the AbstractCore store, edited through the gateway web console and
console-TUI (both already expose it) or `abstractcore config set-default
--reasoning`. Integration-specific knobs (an agora resident `thinking` field
was briefly one) are forbidden: agora is a standalone library, and LLM
parameters are not its config.

These tests pin the two halves that make the ruling real:

- the SEAM: a value saved through the gateway write path is served by the
  gateway payload, survives the runtime client's normalization, and is what
  the per-call cascade (`_with_capability_default_reasoning`) applies — the
  exact chain a resident's unpinned LLM call walks;
- the HTTP surface the web console uses: PUT carries `reasoning`, GET serves
  it back, `""` clears it.

Store-level field preservation is already pinned in
test_gateway_core_config_authority.py; per-call precedence (pin > `_runtime` >
route default) in abstractruntime tests/test_thinking_inheritance.py and
tests/test_capability_default_reasoning.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest


@pytest.fixture()
def scoped_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated AbstractCore store via the REAL `ABSTRACTCORE_CONFIG_FILE`
    resolution (the authority-suite pattern) — never the operator's file."""
    import abstractgateway.core_config as core_config

    path = tmp_path / "config" / "abstractcore.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"capability_defaults": {"routes": {}}}), encoding="utf-8")

    monkeypatch.delenv("ABSTRACTCORE_SERVER_BASE_URL", raising=False)
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(path))
    monkeypatch.setattr(core_config, "core_server_base_url", lambda: "")
    return path


def _text_route_row(payload: Dict[str, Any]) -> Dict[str, Any]:
    routes = payload.get("routes") or []
    for row in routes:
        if isinstance(row, dict) and row.get("key") in ("output.text", "input.text"):
            return row
    return {}


@pytest.mark.basic
def test_gateway_reasoning_default_reaches_the_runtime_cascade(
    scoped_store: Path, tmp_path: Path
) -> None:
    """The resident story in one composed assertion: gateway write -> gateway
    payload -> runtime client normalization -> per-call cascade. An unpinned
    call (no params.thinking, no `_runtime.thinking` — exactly an agora
    resident's react cycle) must receive the configured effort."""
    from abstractgateway.core_config import (
        gateway_capability_defaults_payload,
        save_gateway_capability_default,
    )
    from abstractruntime.integrations.abstractcore.llm_client import (
        _normalize_core_capability_defaults,
        _with_capability_default_reasoning,
    )
    from abstractruntime.integrations.abstractcore.output_specs import (
        capability_default_reasoning_for_text,
    )

    save_gateway_capability_default(
        "output", "text", provider="lmstudio", model="qwen3", reasoning="medium", base_dir=tmp_path
    )

    payload = gateway_capability_defaults_payload(base_dir=tmp_path)
    assert _text_route_row(payload).get("reasoning") == "medium"

    # What `set_default_provider_model` / `set_capability_defaults` store is the
    # NORMALIZED payload; what the cascade reads is that stored value.
    normalized = _normalize_core_capability_defaults(payload)
    assert capability_default_reasoning_for_text(normalized) == "medium"

    params: Dict[str, Any] = {}
    assert _with_capability_default_reasoning(params, normalized) == "medium"
    assert params["thinking"] == "medium"

    # An explicit per-call pin still outranks the operator default — including
    # False ("off" is a decision).
    pinned: Dict[str, Any] = {"thinking": False}
    assert _with_capability_default_reasoning(pinned, normalized) is False


@pytest.mark.basic
def test_clearing_the_gateway_reasoning_default_stops_the_cascade(
    scoped_store: Path, tmp_path: Path
) -> None:
    from abstractgateway.core_config import (
        gateway_capability_defaults_payload,
        save_gateway_capability_default,
    )
    from abstractruntime.integrations.abstractcore.llm_client import (
        _normalize_core_capability_defaults,
        _with_capability_default_reasoning,
    )

    save_gateway_capability_default(
        "output", "text", provider="lmstudio", model="qwen3", reasoning="medium", base_dir=tmp_path
    )
    save_gateway_capability_default("output", "text", reasoning="", base_dir=tmp_path)

    payload = gateway_capability_defaults_payload(base_dir=tmp_path)
    assert not _text_route_row(payload).get("reasoning")
    # Provider/model set through the same door survive the clear (field
    # preservation — the authority contract).
    assert _text_route_row(payload).get("provider") == "lmstudio"

    params: Dict[str, Any] = {}
    assert _with_capability_default_reasoning(params, _normalize_core_capability_defaults(payload)) is None
    assert "thinking" not in params


@pytest.mark.integration
def test_reasoning_default_http_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The surface the web console drives: PUT carries `reasoning`, GET serves
    it, `""` clears it while keeping provider/model."""
    from fastapi.testclient import TestClient

    core_file = tmp_path / "coreconfig" / "abstractcore.json"
    core_file.parent.mkdir(parents=True, exist_ok=True)
    core_file.write_text(json.dumps({"capability_defaults": {"routes": {}}}), encoding="utf-8")

    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)

    token = "t"
    monkeypatch.delenv("ABSTRACTCORE_SERVER_BASE_URL", raising=False)
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(core_file))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        put = client.put(
            "/api/gateway/config/capability-defaults/output/text",
            headers=headers,
            json={"provider": "lmstudio", "model": "qwen3", "reasoning": "medium"},
        )
        assert put.status_code == 200, put.text

        got = client.get("/api/gateway/config/capability-defaults", headers=headers)
        assert got.status_code == 200, got.text
        assert _text_route_row(got.json()).get("reasoning") == "medium"

        cleared = client.put(
            "/api/gateway/config/capability-defaults/output/text",
            headers=headers,
            json={"reasoning": ""},
        )
        assert cleared.status_code == 200, cleared.text

        got2 = client.get("/api/gateway/config/capability-defaults", headers=headers)
        row = _text_route_row(got2.json())
        assert not row.get("reasoning")
        assert row.get("provider") == "lmstudio"
        assert row.get("model") == "qwen3"
