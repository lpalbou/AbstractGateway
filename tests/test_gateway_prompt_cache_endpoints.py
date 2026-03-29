from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pytest
from fastapi.testclient import TestClient


def _write_llm_bundle(*, bundles_dir: Path, bundle_id: str, flow_id: str) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)

    flow = {
        "id": flow_id,
        "name": "llm",
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
                    "effectConfig": {"provider": "stub", "model": "stub-model"},
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

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-25T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }

    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, indent=2))


@dataclass
class _FakeCache:
    chunks: List[Dict[str, Any]]


class _StubPromptCacheProvider:
    """A minimal AbstractCore-like provider surface for gateway prompt-cache endpoints."""

    def __init__(self, model: str = "stub-model"):
        from abstractcore.providers.base import BaseProvider
        from abstractcore.core.types import GenerateResponse

        class _Impl(BaseProvider):
            def supports_prompt_cache(self) -> bool:
                return True

            def _prompt_cache_backend_create(self) -> Optional[Any]:
                return _FakeCache(chunks=[])

            def _prompt_cache_backend_clone(self, cache_value: Any) -> Optional[Any]:
                if not isinstance(cache_value, _FakeCache):
                    return None
                return _FakeCache(chunks=list(cache_value.chunks))

            def _prompt_cache_backend_append(
                self,
                cache_value: Any,
                *,
                prompt: str = "",
                messages: Optional[List[Dict[str, Any]]] = None,
                system_prompt: Optional[str] = None,
                tools: Optional[List[Dict[str, Any]]] = None,
                add_generation_prompt: bool = False,
                **kwargs: Any,
            ) -> bool:
                _ = kwargs
                if not isinstance(cache_value, _FakeCache):
                    return False
                cache_value.chunks.append(
                    {
                        "prompt": prompt,
                        "messages": messages,
                        "system_prompt": system_prompt,
                        "tools": tools,
                        "add_generation_prompt": bool(add_generation_prompt),
                    }
                )
                return True

            def _prompt_cache_backend_token_count(self, cache_value: Any) -> Optional[int]:
                if not isinstance(cache_value, _FakeCache):
                    return None
                return len(cache_value.chunks)

            def _generate_internal(
                self,
                prompt: str,
                messages: Optional[List[Dict[str, str]]] = None,
                system_prompt: Optional[str] = None,
                tools: Optional[List[Dict[str, Any]]] = None,
                media: Optional[List[Any]] = None,
                stream: bool = False,
                **kwargs: Any,
            ) -> GenerateResponse | Iterator[GenerateResponse]:
                _ = (prompt, messages, system_prompt, tools, media, stream, kwargs)
                return GenerateResponse(content="ok", model=self.model, finish_reason="stop")

            def get_capabilities(self) -> List[str]:
                return ["chat"]

            def unload_model(self, model_name: str) -> None:
                _ = model_name

            @classmethod
            def list_available_models(cls, **kwargs: Any) -> List[str]:
                _ = kwargs
                return ["stub-model"]

        self._impl = _Impl(model=model)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._impl, name)


class _StubGatewayLLMClient:
    def __init__(self, provider: str, model: str, llm_kwargs: Optional[Dict[str, Any]] = None, artifact_store: Any = None):
        _ = (provider, model, llm_kwargs, artifact_store)
        self._provider = _StubPromptCacheProvider(model=model)
        self._llm = self._provider

    def get_provider_instance(self, *, provider: str, model: str) -> Any:
        _ = (provider, model)
        return self._provider

    def get_model_capabilities(self) -> Dict[str, Any]:
        return {"max_tokens": 1024, "max_output_tokens": 256}

    def get_prompt_cache_capabilities(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        caps = self._provider.get_prompt_cache_capabilities()
        return {"supported": bool(caps.supported), "operation": "capabilities", "capabilities": caps.to_dict()}

    def get_prompt_cache_stats(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        caps = self._provider.get_prompt_cache_capabilities()
        return {
            "supported": True,
            "operation": "stats",
            "capabilities": caps.to_dict(),
            "stats": self._provider.get_prompt_cache_stats(),
        }

    def prompt_cache_set(self, *, key: str, make_default: bool = True, ttl_s: Optional[float] = None, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        caps = self._provider.get_prompt_cache_capabilities()
        ok = self._provider.prompt_cache_set(key, make_default=make_default, ttl_s=ttl_s)
        return {"supported": True, "operation": "set", "ok": bool(ok), "capabilities": caps.to_dict()}

    def prompt_cache_update(
        self,
        *,
        key: str,
        prompt: Optional[str] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        add_generation_prompt: bool = False,
        ttl_s: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        _ = kwargs
        caps = self._provider.get_prompt_cache_capabilities()
        ok = self._provider.prompt_cache_update(
            key,
            prompt=str(prompt or ""),
            messages=messages,
            system_prompt=system_prompt,
            tools=tools,
            add_generation_prompt=add_generation_prompt,
            ttl_s=ttl_s,
        )
        return {"supported": True, "operation": "update", "ok": bool(ok), "capabilities": caps.to_dict()}

    def prompt_cache_fork(
        self,
        *,
        from_key: str,
        to_key: str,
        make_default: bool = False,
        ttl_s: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        _ = kwargs
        caps = self._provider.get_prompt_cache_capabilities()
        ok = self._provider.prompt_cache_fork(from_key, to_key, make_default=make_default, ttl_s=ttl_s)
        return {"supported": True, "operation": "fork", "ok": bool(ok), "capabilities": caps.to_dict()}

    def prompt_cache_clear(self, *, key: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        caps = self._provider.get_prompt_cache_capabilities()
        ok = self._provider.prompt_cache_clear(key)
        return {"supported": True, "operation": "clear", "ok": bool(ok), "capabilities": caps.to_dict()}

    def prompt_cache_prepare_modules(
        self,
        *,
        namespace: str,
        modules: List[Dict[str, Any]],
        make_default: bool = False,
        ttl_s: Optional[float] = None,
        version: int = 1,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        _ = kwargs
        result = self._provider.prompt_cache_prepare_modules(
            namespace=namespace,
            modules=modules,
            make_default=make_default,
            ttl_s=ttl_s,
            version=version,
        )
        result.setdefault("operation", "prepare_modules")
        result.setdefault("capabilities", self._provider.get_prompt_cache_capabilities().to_dict())
        return result

    def generate(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"content": "ok", "tool_calls": []}

class _ProtocolOnlyGatewayLLMClient(_StubGatewayLLMClient):
    def get_provider_instance(self, *, provider: str, model: str) -> Any:
        raise AssertionError("gateway prompt-cache routes should not require provider instance access")


def _make_client(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm_client_cls: type[_StubGatewayLLMClient] = _StubGatewayLLMClient,
) -> tuple[TestClient, dict[str, str]]:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_llm_bundle(bundles_dir=bundles_dir, bundle_id="bundle-cache", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    # Avoid loading real models: replace the in-process LLM client with a stub.
    from abstractruntime.integrations.abstractcore import factory as ac_factory

    monkeypatch.setattr(ac_factory, "MultiLocalAbstractCoreLLMClient", llm_client_cls)

    from abstractgateway.app import app

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    return client, headers


def test_gateway_prompt_cache_control_plane_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        caps = client.get("/api/gateway/prompt_cache/capabilities?provider=stub&model=stub-model", headers=headers)
        assert caps.status_code == 200, caps.text
        assert caps.json()["supported"] is True
        assert caps.json()["capabilities"]["mode"] == "local_control_plane"

        stats0 = client.get("/api/gateway/prompt_cache/stats?provider=stub&model=stub-model", headers=headers)
        assert stats0.status_code == 200, stats0.text
        assert stats0.json()["supported"] is True
        assert stats0.json()["operation"] == "stats"

        r = client.post("/api/gateway/prompt_cache/set", json={"provider": "stub", "model": "stub-model", "key": "k1"}, headers=headers)
        assert r.status_code == 200, r.text
        assert r.json()["supported"] is True
        assert r.json()["ok"] is True

        r2 = client.post(
            "/api/gateway/prompt_cache/update",
            json={"provider": "stub", "model": "stub-model", "key": "k1", "prompt": "hello"},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        assert r2.json()["supported"] is True
        assert r2.json()["ok"] is True

        s = client.get("/api/gateway/prompt_cache/stats?provider=stub&model=stub-model", headers=headers)
        assert s.status_code == 200, s.text
        body = s.json()
        assert body["supported"] is True
        keys = (body.get("stats") or {}).get("keys") or []
        assert "k1" in keys

        rc = client.post("/api/gateway/prompt_cache/clear", json={"provider": "stub", "model": "stub-model", "key": "k1"}, headers=headers)
        assert rc.status_code == 200, rc.text
        assert rc.json()["supported"] is True
        assert rc.json()["ok"] is True


def test_gateway_prompt_cache_routes_use_runtime_client_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _make_client(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        llm_client_cls=_ProtocolOnlyGatewayLLMClient,
    )
    with client:
        caps = client.get("/api/gateway/prompt_cache/capabilities?provider=stub&model=stub-model", headers=headers)
        assert caps.status_code == 200, caps.text
        assert caps.json()["supported"] is True

        prepared = client.post(
            "/api/gateway/prompt_cache/prepare_modules",
            json={
                "provider": "stub",
                "model": "stub-model",
                "namespace": "tenant:stub-model",
                "modules": [{"module_id": "system", "system_prompt": "You are helpful"}],
            },
            headers=headers,
        )
        assert prepared.status_code == 200, prepared.text
        body = prepared.json()
        assert body["supported"] is True
        assert body["operation"] == "prepare_modules"
        assert body["capabilities"]["mode"] == "local_control_plane"
