"""Explicit context (AbstractCore ADR 0010 point 8, operator ruling 2026-10-29): the Models row
says what the model advertises and why the loaded context differs; an admin sets it per model."""
from __future__ import annotations

import json


def test_row_v1_carries_advertised_context_and_its_source():
    import abstractgateway.routes.gateway as g

    row = g._normalize_model_residency_row_v1({"provider": "huggingface", "model": "m", "resident": True,
                                                "context_length": 65536, "advertised_context_length": 262144,
                                                "context_source": "memory"})
    assert row["advertised_context_length"] == 262144 and row["context_source"] == "memory"


def test_console_says_limited_by_memory_and_offers_the_setting():
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    assert "(model advertises ${row.advertised_context_length.toLocaleString()}; limited by memory)" in html
    assert '"/api/gateway/models/context"' in html and "Set context" in html


def test_set_context_writes_the_core_setting(tmp_path, monkeypatch):
    """MUTANT: the route writes nothing -> the model keeps the fit -> RED."""
    import asyncio
    from types import SimpleNamespace

    import abstractgateway.routes.gateway as g
    import abstractgateway.core_config as cc
    from abstractcore.config.model_contexts import configured_model_context

    cfg = tmp_path / "abstractcore.json"
    monkeypatch.setattr(cc, "runtime_core_config_file", lambda base: cfg)
    monkeypatch.setattr(g, "_require_admin_principal", lambda request: None)
    monkeypatch.setattr(g, "get_gateway_service", lambda: SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path))))
    out = asyncio.run(g.model_context_set(None, g._GatewayModelContextRequest(provider="huggingface", model="org/m-GGUF", context_length=32768)))
    assert out["ok"] and "32,768 tokens at its next load" in out["message"]
    assert configured_model_context("huggingface", "org/m-GGUF", config_file=cfg) == 32768
