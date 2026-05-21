from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
import re

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
    def __init__(
        self,
        provider: str,
        model: str,
        llm_kwargs: Optional[Dict[str, Any]] = None,
        artifact_store: Any = None,
        **kwargs: Any,
    ):
        _ = (provider, model, llm_kwargs, artifact_store, kwargs)
        self._provider = _StubPromptCacheProvider(model=model)
        self._llm = self._provider
        root_dir = kwargs.get("prompt_cache_export_root_dir")
        self._prompt_cache_export_root_dir = Path(root_dir) if root_dir is not None else (Path.cwd() / "prompt_cache_exports")
        self._exports: Dict[str, Dict[str, Any]] = {}

    def _prompt_cache_caps(self) -> Dict[str, Any]:
        return self._provider.get_prompt_cache_capabilities().to_dict()

    def _export_name(self, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("name is required")
        text = text.replace("/", "-").replace("\\", "-")
        text = re.sub(r"[^A-Za-z0-9._-]+", "-", text)
        text = text.strip("._-")
        return text or "prompt-cache-export"

    def _export_paths(self, *, provider: str, model: str, name: str) -> tuple[str, Path, Path]:
        normalized_name = self._export_name(name)
        export_dir = self._prompt_cache_export_root_dir / str(provider or "unknown-provider") / str(model or "unknown-model").replace("/", "__")
        artifact_path = export_dir / f"{normalized_name}.safetensors"
        meta_path = export_dir / f"{artifact_path.name}.meta.json"
        return normalized_name, artifact_path, meta_path

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

    def list_prompt_cache_exports(
        self,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        root_dir = Path(kwargs.get("prompt_cache_export_root_dir") or self._prompt_cache_export_root_dir)
        target_provider = str(provider or "stub").strip() or "stub"
        target_model = str(model or self._provider.model).strip() or str(self._provider.model)
        items = [
            dict(item)
            for item in self._exports.values()
            if str(item.get("provider") or "") == target_provider and str(item.get("model") or "") == target_model
        ]
        items.sort(key=lambda item: str(item.get("saved_at") or item.get("name") or ""), reverse=True)
        return {
            "supported": True,
            "ok": True,
            "operation": "list_exports",
            "local_only": True,
            "provider": target_provider,
            "model": target_model,
            "root_dir": str(root_dir),
            "items": items,
            "capabilities": self._prompt_cache_caps(),
        }

    def prompt_cache_export(
        self,
        *,
        name: str,
        key: str,
        q8: bool = False,
        meta: Optional[Dict[str, Any]] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        _ = kwargs
        target_provider = str(provider or "stub").strip() or "stub"
        target_model = str(model or self._provider.model).strip() or str(self._provider.model)
        known_keys = set((self._provider.get_prompt_cache_stats() or {}).get("keys") or [])
        if str(key or "").strip() not in known_keys:
            return {
                "supported": False,
                "operation": "export",
                "code": "not_found",
                "error": f"No in-memory cache found for key '{key}'",
                "capabilities": self._prompt_cache_caps(),
            }
        normalized_name, artifact_path, meta_path = self._export_paths(
            provider=target_provider,
            model=target_model,
            name=name,
        )
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("stub prompt cache export", encoding="utf-8")
        record: Dict[str, Any] = {
            "schema": "abstractruntime-prompt-cache-export/v1",
            "name": normalized_name,
            "provider": target_provider,
            "model": target_model,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "key": str(key or "").strip(),
            "artifact_filename": artifact_path.name,
            "artifact_extension": artifact_path.suffix,
            "artifact_format": "stub",
            "token_count": len(known_keys),
            "provider_meta": dict(meta or {}),
        }
        meta_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        item = {
            "name": normalized_name,
            "provider": target_provider,
            "model": target_model,
            "saved_at": record["saved_at"],
            "token_count": record["token_count"],
            "key": record["key"],
            "artifact_filename": artifact_path.name,
            "artifact_path": str(artifact_path),
            "artifact_exists": True,
            "artifact_extension": artifact_path.suffix,
            "artifact_format": "stub",
            "meta_path": str(meta_path),
            "meta": record,
        }
        self._exports[normalized_name] = item
        return {
            "supported": True,
            "ok": True,
            "operation": "export",
            "local_only": True,
            "provider": target_provider,
            "model": target_model,
            "name": normalized_name,
            "artifact_filename": artifact_path.name,
            "artifact_path": str(artifact_path),
            "meta_path": str(meta_path),
            "capabilities": self._prompt_cache_caps(),
            "meta": record,
            "provider_response": {"ok": True, "key": str(key or "").strip(), "q8": bool(q8)},
        }

    def prompt_cache_import(
        self,
        *,
        name: str,
        key: Optional[str] = None,
        make_default: bool = True,
        clear_existing: bool = False,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        _ = kwargs
        target_provider = str(provider or "stub").strip() or "stub"
        target_model = str(model or self._provider.model).strip() or str(self._provider.model)
        normalized_name = self._export_name(name)
        item = self._exports.get(normalized_name)
        if item is None:
            return {
                "supported": False,
                "operation": "import",
                "code": "not_found",
                "error": f"Prompt cache export '{normalized_name}' was not found for {target_provider}/{target_model}.",
                "capabilities": self._prompt_cache_caps(),
            }
        if bool(clear_existing):
            self._provider.prompt_cache_clear(None)
        loaded_key = str(key or "").strip() or "loaded:auto"
        self._provider.prompt_cache_set(loaded_key, make_default=bool(make_default))
        self._provider.prompt_cache_update(loaded_key, prompt=f"loaded:{normalized_name}")
        return {
            "supported": True,
            "ok": True,
            "operation": "import",
            "local_only": True,
            "provider": target_provider,
            "model": target_model,
            "name": normalized_name,
            "key": loaded_key,
            "make_default": bool(make_default),
            "clear_existing": bool(clear_existing),
            "artifact_filename": str(item.get("artifact_filename") or ""),
            "artifact_path": str(item.get("artifact_path") or ""),
            "capabilities": self._prompt_cache_caps(),
            "meta": dict(item.get("meta") or {}),
            "provider_response": {"ok": True, "key": loaded_key},
        }

    def upsert_text_bloc(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "upsert_text", "record": {"bloc_id": 1, "sha256": "stub-sha"}}

    def get_bloc_record(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "record", "record": {"bloc_id": 1, "sha256": "stub-sha"}}

    def list_blocs(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "list", "records": [{"bloc_id": 1, "sha256": "stub-sha"}]}

    def get_bloc_kv_manifest(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "kv_manifest", "manifest": {"binding_id": "bind-stub", "key": "work:stub"}}

    def ensure_bloc_kv_artifact(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "kv_ensure", "artifact": {"artifact_path": "/tmp/stub.kv", "binding_id": "bind-stub"}}

    def load_bloc_kv_artifact(self, **kwargs: Any) -> Dict[str, Any]:
        key = kwargs.get("key") or kwargs.get("stable_cache_key") or "work:stub"
        return {
            "ok": True,
            "operation": "kv_load",
            "artifact": {"key": key, "prompt_cache_binding": {"binding_id": "bind-stub", "key": key}},
        }

    def list_bloc_kv_artifacts(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "kv_list", "artifacts": [{"artifact_path": "/tmp/stub.kv", "provider": "stub", "model": "stub-model"}]}

    def delete_bloc_kv_artifact(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "kv_delete", "result": {"deleted": True}}

    def prune_bloc_kv_artifacts(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "kv_prune", "results": [{"deleted": True, "artifact_path": "/tmp/stub.kv"}]}

    def delete_bloc(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "delete", "result": {"deleted": True, "record": {"bloc_id": 1, "sha256": "stub-sha"}}}

    def list_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "list_loaded", "models": []}

    def load_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "load"}

    def unload_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "unload"}

    def generate(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"content": "ok", "tool_calls": []}

class _ProtocolOnlyGatewayLLMClient(_StubGatewayLLMClient):
    def get_provider_instance(self, *, provider: str, model: str) -> Any:
        raise AssertionError("gateway prompt-cache routes should not require provider instance access")


class _KeyedGatewayLLMClient:
    def __init__(
        self,
        provider: str,
        model: str,
        llm_kwargs: Optional[Dict[str, Any]] = None,
        artifact_store: Any = None,
        **kwargs: Any,
    ):
        _ = (provider, model, llm_kwargs, artifact_store, kwargs)
        self._llm = self

    def get_model_capabilities(self) -> Dict[str, Any]:
        return {"max_tokens": 1024, "max_output_tokens": 256}

    def generate(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"content": "ok", "tool_calls": []}

    def get_prompt_cache_capabilities(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {
            "supported": True,
            "operation": "capabilities",
            "capabilities": {
                "supported": True,
                "mode": "keyed",
                "supports_set": True,
                "supports_clear": False,
                "supports_update": False,
                "supports_fork": False,
                "supports_prepare_modules": False,
                "supports_stats": False,
                "supports_save": False,
                "supports_load": False,
                "supports_ttl": False,
                "notes": ["server-managed keyed cache"],
            },
        }

    def get_prompt_cache_stats(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        caps = self.get_prompt_cache_capabilities()["capabilities"]
        return {"supported": True, "operation": "stats", "capabilities": caps, "stats": {"keys": []}}

    def prompt_cache_set(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"supported": False, "operation": "set", "capabilities": self.get_prompt_cache_capabilities()["capabilities"]}

    def prompt_cache_update(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"supported": False, "operation": "update", "capabilities": self.get_prompt_cache_capabilities()["capabilities"]}

    def prompt_cache_fork(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"supported": False, "operation": "fork", "capabilities": self.get_prompt_cache_capabilities()["capabilities"]}

    def prompt_cache_clear(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"supported": False, "operation": "clear", "capabilities": self.get_prompt_cache_capabilities()["capabilities"]}

    def prompt_cache_prepare_modules(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {
            "supported": False,
            "operation": "prepare_modules",
            "capabilities": self.get_prompt_cache_capabilities()["capabilities"],
        }

    def list_prompt_cache_exports(
        self,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        _ = kwargs
        return {
            "supported": True,
            "ok": True,
            "operation": "list_exports",
            "local_only": True,
            "provider": str(provider or "remote").strip() or "remote",
            "model": str(model or "server-model").strip() or "server-model",
            "root_dir": str(Path.cwd() / "prompt_cache_exports"),
            "items": [],
            "capabilities": self.get_prompt_cache_capabilities()["capabilities"],
        }

    def prompt_cache_export(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {
            "supported": False,
            "operation": "export",
            "code": "prompt_cache_unsupported",
            "capabilities": self.get_prompt_cache_capabilities()["capabilities"],
        }

    def prompt_cache_import(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {
            "supported": False,
            "operation": "import",
            "code": "prompt_cache_unsupported",
            "capabilities": self.get_prompt_cache_capabilities()["capabilities"],
        }

    def upsert_text_bloc(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "upsert_text", "record": {"bloc_id": 1, "sha256": "stub-sha"}}

    def get_bloc_record(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "record", "record": {"bloc_id": 1, "sha256": "stub-sha"}}

    def list_blocs(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "list", "records": [{"bloc_id": 1, "sha256": "stub-sha"}]}

    def get_bloc_kv_manifest(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "kv_manifest", "manifest": {"binding_id": "bind-stub", "key": "work:stub"}}

    def ensure_bloc_kv_artifact(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "kv_ensure", "artifact": {"artifact_path": "/tmp/stub.kv", "binding_id": "bind-stub"}}

    def load_bloc_kv_artifact(self, **kwargs: Any) -> Dict[str, Any]:
        key = kwargs.get("key") or kwargs.get("stable_cache_key") or "work:stub"
        return {
            "ok": True,
            "operation": "kv_load",
            "artifact": {"key": key, "prompt_cache_binding": {"binding_id": "bind-stub", "key": key}},
        }

    def list_bloc_kv_artifacts(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "kv_list", "artifacts": [{"artifact_path": "/tmp/stub.kv", "provider": "stub", "model": "stub-model"}]}

    def delete_bloc_kv_artifact(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "kv_delete", "result": {"deleted": True}}

    def prune_bloc_kv_artifacts(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "kv_prune", "results": [{"deleted": True, "artifact_path": "/tmp/stub.kv"}]}

    def delete_bloc(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "delete", "result": {"deleted": True, "record": {"bloc_id": 1, "sha256": "stub-sha"}}}

    def list_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "list_loaded", "models": []}

    def load_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "load"}

    def unload_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "operation": "unload"}


class _UnsupportedGatewayLLMClient(_KeyedGatewayLLMClient):
    def get_prompt_cache_capabilities(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {
            "supported": False,
            "operation": "capabilities",
            "capabilities": {"supported": False, "mode": "none"},
        }


class _RemotePromptCacheGatewayLLMClient(_KeyedGatewayLLMClient):
    def __init__(self) -> None:
        super().__init__(provider="remote", model="server-model")

    def get_prompt_cache_capabilities(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {
            "supported": False,
            "operation": "capabilities",
            "capabilities": {"supported": False, "mode": "none"},
        }

    def list_prompt_cache_exports(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {
            "supported": False,
            "operation": "list_exports",
            "code": "prompt_cache_local_only",
            "error": (
                "Prompt cache export/import admin is local-only. "
                "Remote and hybrid runtimes do not expose a host-local prompt-cache export root."
            ),
            "capabilities": {"supported": False, "mode": "none"},
        }

    def prompt_cache_export(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {
            "supported": False,
            "operation": "export",
            "code": "prompt_cache_local_only",
            "error": (
                "Prompt cache export/import admin is local-only. "
                "Remote and hybrid runtimes do not expose a host-local prompt-cache export root."
            ),
            "capabilities": {"supported": False, "mode": "none"},
        }

    def prompt_cache_import(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {
            "supported": False,
            "operation": "import",
            "code": "prompt_cache_local_only",
            "error": (
                "Prompt cache export/import admin is local-only. "
                "Remote and hybrid runtimes do not expose a host-local prompt-cache export root."
            ),
            "capabilities": {"supported": False, "mode": "none"},
        }


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


def _make_remote_client(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setenv("ABSTRACTCORE_SERVER_BASE_URL", "http://core.test/v1")
    monkeypatch.delenv("ABSTRACTGATEWAY_PROVIDER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_MODEL", raising=False)

    from abstractruntime import Runtime, WorkflowRegistry
    from abstractruntime.integrations.abstractcore import factory as ac_factory

    def _fake_create_remote_runtime(**kwargs: Any) -> Runtime:
        runtime = Runtime(
            run_store=kwargs["run_store"],
            ledger_store=kwargs["ledger_store"],
            workflow_registry=WorkflowRegistry(),
            artifact_store=kwargs["artifact_store"],
            effect_handlers={},
        )
        setattr(runtime, "_abstractcore_llm_client", _RemotePromptCacheGatewayLLMClient())
        return runtime

    monkeypatch.setattr(ac_factory, "create_remote_runtime", _fake_create_remote_runtime)

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


def test_gateway_prompt_cache_export_import_aliases_use_runtime_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _make_client(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        llm_client_cls=_ProtocolOnlyGatewayLLMClient,
    )
    expected_root = tmp_path / "runtime" / "prompt_cache_exports"
    with client:
        seeded = client.post(
            "/api/gateway/prompt_cache/set",
            json={"provider": "stub", "model": "stub-model", "key": "orbit"},
            headers=headers,
        )
        assert seeded.status_code == 200, seeded.text
        assert seeded.json()["ok"] is True

        exported = client.post(
            "/api/gateway/prompt_cache/save",
            json={"provider": "stub", "model": "stub-model", "name": "Orbit Cache", "key": "orbit", "q8": True},
            headers=headers,
        )
        assert exported.status_code == 200, exported.text
        export_body = exported.json()
        assert export_body["supported"] is True
        assert export_body["operation"] == "export"
        assert export_body["local_only"] is True
        assert export_body["source"] == "abstractruntime.host_facade"
        assert export_body["name"] == "Orbit-Cache"
        assert Path(export_body["artifact_path"]).is_file()
        assert Path(export_body["artifact_path"]).is_relative_to(expected_root)

        listed = client.get("/api/gateway/prompt_cache/saved?provider=stub&model=stub-model", headers=headers)
        assert listed.status_code == 200, listed.text
        listed_body = listed.json()
        assert listed_body["supported"] is True
        assert listed_body["operation"] == "list_exports"
        assert listed_body["local_only"] is True
        assert listed_body["root_dir"] == str(expected_root)
        assert len(listed_body["items"]) == 1
        assert listed_body["items"][0]["artifact_path"] == export_body["artifact_path"]

        imported = client.post(
            "/api/gateway/prompt_cache/load",
            json={"provider": "stub", "model": "stub-model", "name": "Orbit Cache", "key": "loaded:orbit", "make_default": True, "clear_existing": True},
            headers=headers,
        )
        assert imported.status_code == 200, imported.text
        import_body = imported.json()
        assert import_body["supported"] is True
        assert import_body["operation"] == "import"
        assert import_body["local_only"] is True
        assert import_body["key"] == "loaded:orbit"
        assert import_body["clear_existing"] is True


def test_gateway_prompt_cache_export_import_aliases_are_local_only_for_remote_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, headers = _make_remote_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        listed = client.get("/api/gateway/prompt_cache/saved?provider=stub&model=stub-model", headers=headers)
        assert listed.status_code == 200, listed.text
        listed_body = listed.json()
        assert listed_body["supported"] is False
        assert listed_body["operation"] == "list_exports"
        assert listed_body["code"] == "prompt_cache_local_only"
        assert listed_body["source"] == "abstractruntime.host_facade"

        exported = client.post(
            "/api/gateway/prompt_cache/save",
            json={"provider": "stub", "model": "stub-model", "name": "remote-export", "key": "orbit"},
            headers=headers,
        )
        assert exported.status_code == 200, exported.text
        export_body = exported.json()
        assert export_body["supported"] is False
        assert export_body["operation"] == "export"
        assert export_body["code"] == "prompt_cache_local_only"

        imported = client.post(
            "/api/gateway/prompt_cache/load",
            json={"provider": "stub", "model": "stub-model", "name": "remote-export"},
            headers=headers,
        )
        assert imported.status_code == 200, imported.text
        import_body = imported.json()
        assert import_body["supported"] is False
        assert import_body["operation"] == "import"
        assert import_body["code"] == "prompt_cache_local_only"


def test_session_prompt_cache_namespace_is_stable_and_bounded() -> None:
    from abstractgateway.routes.gateway import _session_prompt_cache_identity

    first = _session_prompt_cache_identity(
        session_id="../session/with/slashes",
        bundle_id="bundle/example",
        bundle_version="0.0.0",
        flow_id="root:flow",
        provider="mlx",
        model="mlx-community/Qwen3-4B-4bit",
        template_id="assistant.default",
        version=1,
    )
    second = _session_prompt_cache_identity(
        session_id="../session/with/slashes",
        bundle_id="bundle/example",
        bundle_version="0.0.0",
        flow_id="root:flow",
        provider="mlx",
        model="mlx-community/Qwen3-4B-4bit",
        template_id="assistant.default",
        version=1,
    )

    assert first == second
    assert first["namespace"].startswith("agw.pc.v1.")
    assert "/" not in first["namespace"]
    assert ".." not in first["namespace"]
    assert len(first["namespace"]) <= 240
    assert first["prompt_cache_key"].endswith(":session")


def test_gateway_session_prompt_cache_lifecycle_local_control_plane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _make_client(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        llm_client_cls=_ProtocolOnlyGatewayLLMClient,
    )
    with client:
        status = client.get(
            "/api/gateway/sessions/chat-1/prompt_cache/status?provider=stub&model=stub-model&bundle_id=bundle-cache&flow_id=root&template_id=assistant.default",
            headers=headers,
        )
        assert status.status_code == 200, status.text
        status_body = status.json()
        assert status_body["supported"] is True
        assert status_body["mode"] == "local_control_plane"
        assert status_body["runtime_hint"]["prompt_cache_key"] == status_body["prompt_cache_key"]

        prepared = client.post(
            "/api/gateway/sessions/chat-1/prompt_cache/prepare",
            json={
                "provider": "stub",
                "model": "stub-model",
                "bundle_id": "bundle-cache",
                "flow_id": "root",
                "template_id": "assistant.default",
                "system_prompt": "You are helpful.",
                "tools": [{"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}],
                "make_default": True,
            },
            headers=headers,
        )
        assert prepared.status_code == 200, prepared.text
        body = prepared.json()
        assert body["supported"] is True
        assert body["ok"] is True
        assert body["prepared"] is True
        assert body["mode"] == "local_control_plane"
        assert body["prefix_cache_key"]
        assert body["provider_responses"]["prepare_modules"]["final_cache_key"] == body["prefix_cache_key"]
        assert body["provider_responses"]["fork"]["ok"] is True

        stats = client.get("/api/gateway/prompt_cache/stats?provider=stub&model=stub-model", headers=headers)
        assert stats.status_code == 200, stats.text
        keys = (stats.json().get("stats") or {}).get("keys") or []
        assert body["prompt_cache_key"] in keys

        cleared = client.post(
            "/api/gateway/sessions/chat-1/prompt_cache/clear",
            json={"provider": "stub", "model": "stub-model", "bundle_id": "bundle-cache", "flow_id": "root", "template_id": "assistant.default"},
            headers=headers,
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["ok"] is True

        rebuilt = client.post(
            "/api/gateway/sessions/chat-1/prompt_cache/rebuild",
            json={
                "provider": "stub",
                "model": "stub-model",
                "bundle_id": "bundle-cache",
                "flow_id": "root",
                "template_id": "assistant.default",
                "modules": [{"module_id": "system", "system_prompt": "You are helpful."}],
            },
            headers=headers,
        )
        assert rebuilt.status_code == 200, rebuilt.text
        assert rebuilt.json()["operation"] == "rebuild"
        assert rebuilt.json()["ok"] is True


def test_gateway_session_prompt_cache_keyed_provider_returns_runtime_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch, llm_client_cls=_KeyedGatewayLLMClient)
    with client:
        prepared = client.post(
            "/api/gateway/sessions/chat-keyed/prompt_cache/prepare",
            json={"provider": "remote", "model": "server-model", "bundle_id": "basic-agent", "flow_id": "root"},
            headers=headers,
        )
        assert prepared.status_code == 200, prepared.text
        body = prepared.json()
        assert body["supported"] is True
        assert body["ok"] is True
        assert body["mode"] == "keyed"
        assert body["prepared"] is False
        assert body["code"] == "prompt_cache_key_hint_only"
        assert body["runtime_hint"]["prompt_cache_key"] == body["prompt_cache_key"]

        cleared = client.post(
            "/api/gateway/sessions/chat-keyed/prompt_cache/clear",
            json={"provider": "remote", "model": "server-model", "bundle_id": "basic-agent", "flow_id": "root"},
            headers=headers,
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["ok"] is False
        assert cleared.json()["code"] == "prompt_cache_clear_unsupported"


def test_gateway_session_prompt_cache_unsupported_provider_is_structured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch, llm_client_cls=_UnsupportedGatewayLLMClient)
    with client:
        status = client.get(
            "/api/gateway/sessions/chat-none/prompt_cache/status?provider=none&model=none-model&bundle_id=basic-agent&flow_id=root",
            headers=headers,
        )
        assert status.status_code == 200, status.text
        body = status.json()
        assert body["supported"] is False
        assert body["ok"] is False
        assert body["mode"] == "unsupported"
        assert body["code"] == "prompt_cache_unsupported"
