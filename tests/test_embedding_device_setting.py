"""The embedding model's device is the person's choice (operator ruling 2026-10-29): the
route's `options.device` reaches AbstractCore, and the route editor has a Device control."""
from __future__ import annotations

from pathlib import Path

import abstractgateway.embeddings_config as ec


def test_route_device_reaches_the_embedding_manager(monkeypatch, tmp_path):
    captured = {}

    class FakeClient:
        def __init__(self, *, provider, model, manager_kwargs):
            captured.update(manager_kwargs)

    import abstractruntime.integrations.abstractcore.embeddings_client as rc
    monkeypatch.setattr(rc, "AbstractCoreEmbeddingsClient", FakeClient)
    route = ec.EmbeddingRouteConfig(provider="huggingface", model="qwen3-embedding", device="cpu")
    ec.build_embedding_client(route, cache_dir=tmp_path)
    assert captured["device"] == "cpu", "MUTANT: device dropped -> the GPU default regardless -> RED"
    ec.build_embedding_client(ec.EmbeddingRouteConfig(provider="huggingface", model="m"), cache_dir=tmp_path)


def test_route_device_is_read_from_the_route_options():
    assert ec._route_device({"options": {"device": "GPU"}}) == "gpu"
    assert ec._route_device({"options": {"device": "tpu"}}) is None
    assert ec._route_device({}) is None


def test_the_route_editor_offers_device_for_embedding_routes():
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    assert 'id="modal-default-device"' in html and "GPU when there is room (default)" in html
    assert "loadDefaultDevice(row);" in html and "options.device = dev" in html
