"""`/v1/chat/completions` on the gateway reuses the model its runtime pool holds (backlog 0962).

Fresh-Linux release test (2026-10-28, QA 4): with a 2.5 GB GGUF loaded through the gateway's model
pool, every `/v1/chat/completions` built a SECOND llama.cpp instance (Core's server route did not know
the pool's), failing with "Failed to create llama_context" on a 16 GB GPU; on Metal each request added
~11 GB. AbstractCore 2.31.1 (its backlog 0859) lends the process's resident huggingface instance to the
request; this checks the gateway's mounted `/v1` (CoreEndpoint -> Runtime's server facade -> Core's
route) gets that behaviour, i.e. the gateway's Core floor carries the fix.

Hermetic: the pool's resident instance is a stand-in registered where a real HuggingFaceProvider
registers itself (`abstractcore.providers.hf_residency`); `create_llm` counts any second copy.
"""
from __future__ import annotations

import importlib
import sys
import types
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from abstractgateway import core_endpoint as ce

pytestmark = pytest.mark.basic

MODEL = "unsloth/Qwen3.5-4B-GGUF"


def _resident(model: str) -> Any:
    from abstractcore.core.types import GenerateResponse
    from abstractcore.providers.base import BaseProvider

    class Resident(BaseProvider):
        """Stands in for the pool's HuggingFaceProvider: alive (`llm` set), named by `model`."""

        def __init__(self) -> None:
            super().__init__(model=model, enable_tracing=False)
            self.llm = types.SimpleNamespace(model_path=f"/cache/{model.split('/')[-1]}/model-Q4_K_M.gguf")
            self.calls = 0

        def get_capabilities(self) -> List[str]:
            return ["chat"]

        def list_available_models(self, **kwargs: Any) -> List[str]:
            return [self.model]

        def unload_model(self, model_name: str) -> None:
            raise AssertionError("a /v1 request unloaded the pool's model")

        def _generate_internal(self, prompt: str, messages: Optional[List[Dict[str, Any]]] = None, **kwargs: Any):
            self.calls += 1
            return GenerateResponse(content="pong", model=self.model, finish_reason="stop",
                                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

    return Resident()


@pytest.fixture
def gateway_v1(tmp_path, monkeypatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ce, "gateway_data_dir_from_env", lambda: tmp_path)
    monkeypatch.setattr(ce, "resolve_network_setting", lambda path: {"mode": "lan"})
    monkeypatch.setattr(ce.ne, "live_reverse_proxy", lambda path: types.SimpleNamespace(trust_proxy=False))
    monkeypatch.setattr(ce.ne, "effective_bind", lambda path, **kw: {})
    monkeypatch.setattr(ce.ne, "tailscale_status", lambda: None)
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ACEMUSIC_API_KEY", "ELEVENLABS_API_KEY",
                "ABSTRACTCORE_AUTH_TOKEN", "ABSTRACTCORE_SERVER_ALLOW_UNAUTHENTICATED"):
        monkeypatch.delenv(key, raising=False)
    # Core counts the huggingface backend as loaded once its provider module is imported (the pool did).
    if "abstractcore.providers.huggingface_provider" not in sys.modules:
        monkeypatch.setitem(sys.modules, "abstractcore.providers.huggingface_provider", types.ModuleType("hf_stub"))
    core = importlib.import_module("abstractcore.server.app")
    copies: List[Any] = []

    def second_copy(provider: str, model: str, **kwargs: Any) -> Any:
        llm = _resident(model)
        copies.append(llm)
        return llm

    monkeypatch.setattr(core, "create_llm", second_copy)
    app = FastAPI()
    app.mount("/v1", ce.CoreEndpoint())
    token = ce.change_settings(tmp_path, enabled=True, reach="network").token
    http = TestClient(app, client=("192.168.1.20", 50000))

    def chat(**extra: Any) -> Dict[str, Any]:
        doc = {"model": f"huggingface/{MODEL}", "messages": [{"role": "user", "content": "Say pong."}], "max_tokens": 8, **extra}
        resp = http.post("/v1/chat/completions", json=doc, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        return resp.json()

    return chat, copies


def test_v1_chat_uses_the_pools_resident_model_not_a_second_copy(gateway_v1) -> None:
    from abstractcore.providers import hf_residency

    chat, copies = gateway_v1
    pool_model = _resident(MODEL)
    hf_residency.register_provider(pool_model)
    try:
        for temperature in (0.1, 0.7, 0.0):  # request settings do not make a copy either
            assert chat(temperature=temperature)["choices"][0]["message"]["content"] == "pong"
        assert copies == [], f"/v1 built {len(copies)} second copies of a resident model"
        assert pool_model.calls == 3
    finally:
        hf_residency._HF_PROVIDERS.discard(pool_model)


def test_v1_chat_without_a_resident_model_builds_its_own(gateway_v1) -> None:
    chat, copies = gateway_v1
    assert chat()["choices"][0]["message"]["content"] == "pong"
    assert len(copies) == 1 and copies[0].calls == 1
