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
    assert "${row.context_length.toLocaleString()} of ${row.advertised_context_length.toLocaleString()}; limited by memory${buf}" in html
    assert '"/api/gateway/models/context"' in html and "Set context" in html


def _route(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import abstractgateway.routes.gateway as g
    import abstractgateway.core_config as cc

    cfg = tmp_path / "abstractcore.json"
    monkeypatch.setattr(cc, "runtime_core_config_file", lambda base: cfg)
    monkeypatch.setattr(g, "_require_admin_principal", lambda request: None)
    monkeypatch.setattr(g, "get_gateway_service", lambda: SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path))))
    return g, cc, cfg


def test_set_context_goes_through_the_runtime_facade(tmp_path, monkeypatch):
    """Gateway ADR 0001: the write goes through AbstractRuntime's config facade (faked here: the
    released Runtime and Core predate it). MUTANT: the route writes nothing -> RED."""
    import asyncio

    g, cc, cfg = _route(tmp_path, monkeypatch)
    seen = []
    monkeypatch.setattr(cc.config_facade, "set_model_context",
                        lambda p, m, t, *, config_file=None: seen.append((p, m, t, config_file)) or {}, raising=False)
    out = asyncio.run(g.model_context_set(None, g._GatewayModelContextRequest(provider="huggingface", model="org/m-GGUF", context_length=32768)))
    assert out["ok"] and "32,768 tokens at its next load" in out["message"]
    assert seen == [("huggingface", "org/m-GGUF", 32768, cfg)]


def test_an_older_runtime_answers_501_in_words(tmp_path, monkeypatch):
    """MUTANT: a silent no-op (ok: true) when the facade lacks the call -> RED."""
    import asyncio

    g, cc, _ = _route(tmp_path, monkeypatch)
    monkeypatch.delattr(cc.config_facade, "set_model_context", raising=False)
    out = asyncio.run(g.model_context_set(None, g._GatewayModelContextRequest(provider="huggingface", model="org/m-GGUF", context_length=32768)))
    body = json.loads(out.body)
    assert out.status_code == 501 and body["reason"] == "abstractruntime_too_old"
    assert "Setting a model's context needs a newer AbstractRuntime" in body["message"]


def test_an_older_core_answers_501_in_words(tmp_path, monkeypatch):
    import asyncio

    g, cc, _ = _route(tmp_path, monkeypatch)

    def too_old(*a, **k):
        raise cc.CoreTooOld("Setting a model's context", "2.31.1", missing="abstractcore.config.model_contexts")

    monkeypatch.setattr(cc.config_facade, "set_model_context", too_old, raising=False)
    out = asyncio.run(g.model_context_set(None, g._GatewayModelContextRequest(provider="huggingface", model="org/m-GGUF", context_length=32768)))
    body = json.loads(out.body)
    assert out.status_code == 501 and body["reason"] == "abstractcore_too_old" and "Setting a model's context requires" in body["message"]


def test_set_context_is_an_admin_route():
    from abstractgateway.security.authorization import GATEWAY_ROUTE_POLICIES

    assert any("/api/gateway/models/context" in (p.exact or ()) and p.reason_code == "admin_required" for p in GATEWAY_ROUTE_POLICIES)
