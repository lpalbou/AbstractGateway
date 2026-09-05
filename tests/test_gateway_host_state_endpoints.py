from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, str]]:
    token = "t"
    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractgateway.app import app

    return TestClient(app), {"Authorization": f"Bearer {token}"}


_MEMORY_SNAPSHOT: Dict[str, Any] = {
    "ts": 1756252800.0,
    "ram": {"total_bytes": 128_000, "available_bytes": 48_000, "used_bytes": 80_000, "percent": 62.5},
    "process": {"rss_bytes": 10_000},
    # `allocated_bytes` is PROCESS-LOCAL; `host_in_use_bytes` is the
    # cross-process accelerator HEAP (driver-allocated buffers — memory-mapped
    # GGUF weights never appear in it) and `wired_limit_bytes` the GPU
    # wired-memory ceiling. The gateway relays the device block verbatim — see
    # test_host_state_relays_the_device_memory_block_verbatim.
    #
    # This block is the MLX shape: a non-zero `allocated_bytes` because the
    # weights went through mlx's allocator. `_METAL_GGUF_DEVICE` below is the
    # OTHER real shape the wire serves, and the one this machine actually
    # reports — see test_host_state_relays_the_gguf_shaped_device_block.
    "device": {
        "backend": "metal",
        "allocated_bytes": 5_000,
        "total_bytes": 50_000,
        "free_bytes": 45_000,
        "host_in_use_bytes": 40_000,
        "wired_limit_bytes": 44_000,
    },
    "host": {"host_id": "h-1", "host_name": "studio.local", "platform": "darwin"},
}


# The GGUF shape, transcribed from a live `GET /api/gateway/host/state` on an
# Apple-silicon host holding a fully offloaded (`n_gpu_layers=-1`) 89.99 GB
# three-shard GGUF. BOTH device numbers read near zero while ~90 GB of weights
# are resident: `allocated_bytes` because llama.cpp does not use mlx's
# allocator, and `host_in_use_bytes` because llama.cpp mmaps the file and wraps
# the pages with `newBufferWithBytesNoCopy`, so they never become
# driver-allocated accelerator memory. The weights are visible as process RSS
# and as the model's own `est_weights_bytes` — nowhere else.
_METAL_GGUF_DEVICE: Dict[str, Any] = {
    "backend": "metal",
    "allocated_bytes": 0,
    "total_bytes": 137_438_953_472,
    "free_bytes": None,
    "host_in_use_bytes": 792_461_312,
    "wired_limit_bytes": 115_343_360_000,
}

_CACHE_ROW: Dict[str, Any] = {
    "key": "agw.pc.v1.s-sess1:session",
    "provider": "mlx",
    "model": "qwen",
    "runtime_id": "local:text_generation:mlx:qwen",
    "session_id": "sess1",
    "token_count": 100,
    "bytes": 4096,
    "created_at_s": 1.0,
    "last_used_at_s": 2.0,
    "meta": {},
}


class _FullStubHostFacade:
    """Implements the full agentic-OS facade contract."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Dict[str, Any]]] = []

    def get_memory_snapshot(self) -> Dict[str, Any]:
        self.calls.append(("memory", {}))
        return dict(_MEMORY_SNAPSHOT)

    def list_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(("list", dict(kwargs)))
        return {
            "ok": True,
            "supported": True,
            "models": [
                {
                    # camelCase aliases + `loaded` instead of `resident` + stringy size
                    "runtimeId": "local:text_generation:mlx:qwen",
                    "task": "text_generation",
                    "provider": "mlx",
                    "model": "qwen",
                    "source": "local",
                    "loaded": True,
                    "pinned": True,
                    "sizeBytes": "1024",
                    "size_vram_bytes": 2048,
                    "contextLength": 8192,
                    "expires_at": 1756252900.0,
                    "loadedAt": "2026-08-27T00:00:00Z",
                    "lastUsedAt": "2026-08-27T00:05:00Z",
                    # lock/modality/calibration wave fields (additive-optional)
                    "locked": True,
                    "lockable": True,
                    "modalities": ["input.text", "output.text"],
                    "calibrated_context_length": "4096",
                    "context_calibrated": True,
                    "host_id": "h-1",
                    "hostName": "studio.local",
                    # memory wave (additive-optional): per-model footprint.
                    "estWeightsBytes": 4096,
                    "cache_bytes": 512,
                }
            ],
        }

    def list_session_prompt_caches(self, session_id: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(("caches", {"session_id": session_id, **kwargs}))
        rows = [dict(_CACHE_ROW)]
        if session_id is not None:
            rows = [r for r in rows if r.get("session_id") == session_id]
        return {"ok": True, "caches": rows}

    def clear_session_prompt_caches(self, session_id: str, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(("clear_all", {"session_id": session_id, **kwargs}))
        return {"ok": True, "cleared": [dict(_CACHE_ROW)], "count": 1}


class _PartialStubHostFacade:
    """Residency only: no memory snapshot, no session-cache enumeration."""

    def list_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
        _ = kwargs
        return {"ok": True, "supported": True, "models": [{"runtime_id": "r1", "provider": "mlx", "model": "qwen"}]}


def _patch_gpu(monkeypatch: pytest.MonkeyPatch, payload: Dict[str, Any]) -> None:
    from abstractgateway import host_metrics

    monkeypatch.setattr(host_metrics, "get_host_gpu_metrics", lambda **_: dict(payload))


# ---------------------------------------------------------------------------
# GET /host/state
# ---------------------------------------------------------------------------


def test_host_state_full_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    facade = _FullStubHostFacade()
    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (facade, None))
    _patch_gpu(monkeypatch, {"ts": "2026-08-27T00:00:00+00:00", "supported": True, "source": "test", "utilization_gpu_pct": 7.0, "gpus": []})

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/host/state", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert isinstance(body["ts"], float)
    assert body["degraded"] == []
    assert body["memory"] == _MEMORY_SNAPSHOT
    # Host identity block is passed through from memory.host when present.
    assert body["host"] == _MEMORY_SNAPSHOT["host"]
    assert body["gpu"]["supported"] is True
    assert body["row_schema"] == "model_residency_row_v1"

    row = body["models"][0]
    assert row["runtime_id"] == "local:text_generation:mlx:qwen"
    assert row["resident"] is True  # coerced from `loaded`
    assert row["pinned"] is True
    assert row["size_bytes"] == 1024  # coerced from stringy camelCase
    assert row["size_vram_bytes"] == 2048
    assert row["context_length"] == 8192
    assert row["loaded_at"] == "2026-08-27T00:00:00Z"
    assert row["last_used_at"] == "2026-08-27T00:05:00Z"
    # lock/modality/calibration wave fields flow through the one normalizer.
    assert row["locked"] is True
    assert row["lockable"] is True
    assert row["modalities"] == ["input.text", "output.text"]
    assert row["calibrated_context_length"] == 4096  # coerced from string
    assert row["context_calibrated"] is True
    assert row["host_id"] == "h-1"
    assert row["host_name"] == "studio.local"  # camelCase alias
    # memory wave: both additive fields, camelCase alias + safe int coercion.
    assert row["est_weights_bytes"] == 4096
    assert row["cache_bytes"] == 512
    assert row["details"]["runtimeId"] == "local:text_generation:mlx:qwen"

    assert body["session_caches"] == [_CACHE_ROW]
    assert body["totals"] == {
        "models": 1,
        "models_resident": 1,
        # Display-size coalesce: size_bytes wins over est_weights_bytes.
        "model_bytes": 1024,
        # Per-model prompt-cache footprint, kept DISTINCT from the
        # session-cache enumeration total below.
        "cache_bytes_models": 512,
        "session_caches": 1,
        "session_cache_bytes": 4096,
    }


def test_host_state_omits_host_block_when_memory_snapshot_lacks_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runtime predating the host-identity block yields NO top-level `host`."""
    import abstractgateway.routes.gateway as gateway_routes

    class _NoHostFacade(_FullStubHostFacade):
        def get_memory_snapshot(self) -> Dict[str, Any]:
            snap = dict(_MEMORY_SNAPSHOT)
            snap.pop("host")
            return snap

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_NoHostFacade(), None))
    _patch_gpu(monkeypatch, {"ts": "2026-08-27T00:00:00+00:00", "supported": True, "source": "test", "utilization_gpu_pct": 1.0, "gpus": []})

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/host/state", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "host" not in body
    assert body["memory"]["ram"] == _MEMORY_SNAPSHOT["ram"]

    # Junk tolerance: a non-dict memory.host must be dropped, not passed through.
    class _JunkHostFacade(_FullStubHostFacade):
        def get_memory_snapshot(self) -> Dict[str, Any]:
            return {**_MEMORY_SNAPSHOT, "host": "studio.local"}

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_JunkHostFacade(), None))
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/host/state", headers=headers)
    assert resp.status_code == 200, resp.text
    assert "host" not in resp.json()


def test_host_state_partial_facade_degrades_sections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_PartialStubHostFacade(), None))
    _patch_gpu(monkeypatch, {"ts": "2026-08-27T00:00:00+00:00", "supported": True, "source": "test", "utilization_gpu_pct": 1.0, "gpus": []})

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/host/state", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["memory"] is None
    assert "host" not in body  # no memory snapshot -> no host identity block
    assert body["session_caches"] is None
    assert sorted(body["degraded"]) == ["memory", "session_caches"]
    assert body["models"][0]["runtime_id"] == "r1"
    assert body["totals"]["models"] == 1
    # The row reports no residency at all (tri-state null) — it must NOT be
    # counted as resident: "N loaded" is provider-verified rows only.
    assert body["totals"]["models_resident"] == 0
    assert body["totals"]["model_bytes"] is None
    assert body["totals"]["cache_bytes_models"] is None
    assert body["totals"]["session_caches"] == 0
    assert body["totals"]["session_cache_bytes"] is None


def test_host_state_everything_unavailable_still_200(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(
        gateway_routes,
        "_gateway_abstractcore_host_facade",
        lambda: (None, "Gateway runtime is not wired to AbstractCore host controls."),
    )
    _patch_gpu(monkeypatch, {"ts": "2026-08-27T00:00:00+00:00", "supported": False, "reason": "nvidia-smi not available"})

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/host/state", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["memory"] is None
    assert body["models"] is None
    assert body["session_caches"] is None
    assert body["gpu"]["supported"] is False  # gpu keeps its in-band reason payload
    assert sorted(body["degraded"]) == ["gpu", "memory", "models", "session_caches"]
    assert "host controls" in body["reasons"]["memory"]
    assert body["totals"] == {
        "models": 0,
        "models_resident": 0,
        "model_bytes": None,
        "cache_bytes_models": None,
        "session_caches": 0,
        "session_cache_bytes": None,
    }


def test_host_state_raising_facade_sections_degrade_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each section survives its own probe RAISING (not just missing methods)."""
    import abstractgateway.routes.gateway as gateway_routes
    from abstractgateway import host_metrics

    class _RaisingFacade:
        def get_memory_snapshot(self) -> Dict[str, Any]:
            raise RuntimeError("psutil exploded")

        def list_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("residency probe exploded")

        def list_session_prompt_caches(self, **kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("cache probe exploded")

    def _raising_gpu(**_: Any) -> Dict[str, Any]:
        raise RuntimeError("gpu probe exploded")

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_RaisingFacade(), None))
    monkeypatch.setattr(host_metrics, "get_host_gpu_metrics", _raising_gpu)

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/host/state", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["memory"] is None
    assert body["gpu"] is None
    assert body["models"] is None
    assert body["session_caches"] is None
    assert sorted(body["degraded"]) == ["gpu", "memory", "models", "session_caches"]
    assert "psutil exploded" in body["reasons"]["memory"]
    assert "gpu probe exploded" in body["reasons"]["gpu"]
    assert "residency probe exploded" in body["reasons"]["models"]
    assert "cache probe exploded" in body["reasons"]["session_caches"]


def test_host_state_non_dict_memory_snapshot_degrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    class _WeirdFacade:
        def get_memory_snapshot(self) -> Any:
            return ["not", "a", "dict"]

        def list_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
            _ = kwargs
            return {"ok": True, "supported": True, "models": []}

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_WeirdFacade(), None))
    _patch_gpu(monkeypatch, {"ts": "2026-08-27T00:00:00+00:00", "supported": True, "source": "test", "utilization_gpu_pct": 1.0, "gpus": []})

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/host/state", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["memory"] is None
    assert "memory" in body["degraded"]
    assert "non-dict" in body["reasons"]["memory"]
    assert body["models"] == []


def test_host_state_non_dict_gpu_payload_is_nulled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes
    from abstractgateway import host_metrics

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_FullStubHostFacade(), None))
    monkeypatch.setattr(host_metrics, "get_host_gpu_metrics", lambda **_: "42% utilized")

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/host/state", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["gpu"] is None
    assert body["degraded"] == ["gpu"]
    assert "non-dict" in body["reasons"]["gpu"]


def test_host_state_in_band_cache_failure_degrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A facade answering `ok: false` (no raise) must degrade, not report an empty list."""
    import abstractgateway.routes.gateway as gateway_routes

    class _RefusingFacade(_PartialStubHostFacade):
        def get_memory_snapshot(self) -> Dict[str, Any]:
            return dict(_MEMORY_SNAPSHOT)

        def list_session_prompt_caches(self, **kwargs: Any) -> Dict[str, Any]:
            _ = kwargs
            return {"ok": False, "error": "provider control plane refused"}

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_RefusingFacade(), None))
    _patch_gpu(monkeypatch, {"ts": "2026-08-27T00:00:00+00:00", "supported": True, "source": "test", "utilization_gpu_pct": 1.0, "gpus": []})

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/host/state", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_caches"] is None
    assert body["degraded"] == ["session_caches"]
    assert "refused" in body["reasons"]["session_caches"]


# ---------------------------------------------------------------------------
# GET /host/metrics/memory
# ---------------------------------------------------------------------------


def test_host_memory_metrics_happy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_FullStubHostFacade(), None))

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/host/metrics/memory", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["supported"] is True
    assert body["ram"] == _MEMORY_SNAPSHOT["ram"]
    assert body["device"]["backend"] == "metal"


def test_host_memory_metrics_degrades_without_facade_method(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_PartialStubHostFacade(), None))

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/host/metrics/memory", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["supported"] is False
    assert "memory snapshot" in body["reason"]


# ---------------------------------------------------------------------------
# model_residency_row_v1 normalization
# ---------------------------------------------------------------------------


def test_models_loaded_carries_row_v1_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_FullStubHostFacade(), None))

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/models/loaded", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Existing keys stay untouched (raw runtime records).
    assert body["models"][0]["runtimeId"] == "local:text_generation:mlx:qwen"
    assert body["row_schema"] == "model_residency_row_v1"
    row = body["rows"][0]
    assert set(row) == {
        "runtime_id",
        "task",
        "provider",
        "model",
        "source",
        "resident",
        "state",
        "pinned",
        "default",
        "size_bytes",
        "size_vram_bytes",
        # additive-optional memory wave fields
        "est_weights_bytes",
        "cache_bytes",
        "expires_at",
        "context_length",
        "loaded_at",
        "last_used_at",
        # additive-optional lock/modality/calibration wave fields
        "locked",
        "lockable",
        "modalities",
        "calibrated_context_length",
        "context_calibrated",
        "host_id",
        "host_name",
        "details",
    }
    assert row["runtime_id"] == "local:text_generation:mlx:qwen"
    assert row["resident"] is True
    assert row["state"] is None
    assert row["default"] is None
    assert row["size_bytes"] == 1024
    assert row["expires_at"] == 1756252900.0


def test_row_v1_alias_and_state_coercions() -> None:
    import abstractgateway.routes.gateway as gateway_routes

    norm = gateway_routes._normalize_model_residency_row_v1

    assert norm({"state": "provider_loaded"})["resident"] is True
    assert norm({"provider_state": "loaded"})["resident"] is True
    # A state STRING never proves absence: unknown stays null, never guessed false.
    assert norm({"state": "unloaded"})["resident"] is None
    assert norm({"provider_resident": False, "state": "provider_loaded"})["resident"] is False
    assert norm({"provider_loaded": True})["resident"] is True
    # Provider truth outranks runtime-lease booleans in BOTH directions.
    assert norm({"loaded": True, "provider_resident": False})["resident"] is False
    assert norm({"resident": False, "provider_loaded": True})["resident"] is True
    assert norm({"loaded": True})["resident"] is True
    assert norm({"loadId": "L1"})["runtime_id"] == "L1"
    assert norm({"id": "X"})["runtime_id"] == "X"
    assert norm({})["resident"] is None
    assert norm({"is_default": True})["default"] is True
    assert norm({"size_bytes": True})["size_bytes"] is None  # bools are not sizes
    assert norm("not-a-dict") is None


def test_row_v1_int_coercion_survives_junk_numerics() -> None:
    import abstractgateway.routes.gateway as gateway_routes

    norm = gateway_routes._normalize_model_residency_row_v1

    # Non-finite numerics must coerce to None, never raise (never-500 contract).
    assert norm({"size_bytes": float("nan")})["size_bytes"] is None
    assert norm({"size_bytes": float("inf")})["size_bytes"] is None
    assert norm({"size_bytes": float("-inf")})["size_bytes"] is None
    assert norm({"size_bytes": "inf"})["size_bytes"] is None
    assert norm({"size_bytes": "nan"})["size_bytes"] is None
    assert norm({"size_bytes": "not-a-number"})["size_bytes"] is None
    assert norm({"size_bytes": "12.5"})["size_bytes"] == 12
    assert norm({"size_bytes": "nan", "sizeBytes": 7})["size_bytes"] == 7  # falls through to next alias


def test_row_v1_memory_wave_fields_share_the_size_coercion() -> None:
    """est_weights_bytes/cache_bytes are additive fields with the SAME safe int
    coercion as size_bytes: bools and non-finite numerics are not values."""
    import abstractgateway.routes.gateway as gateway_routes

    norm = gateway_routes._normalize_model_residency_row_v1

    assert norm({"est_weights_bytes": 93_000_000_000})["est_weights_bytes"] == 93_000_000_000
    assert norm({"estWeightsBytes": "4096"})["est_weights_bytes"] == 4096  # camelCase + stringy
    assert norm({"cache_bytes": 512})["cache_bytes"] == 512
    assert norm({"cacheBytes": 512})["cache_bytes"] == 512
    assert norm({"est_weights_bytes": True})["est_weights_bytes"] is None
    assert norm({"cache_bytes": True})["cache_bytes"] is None
    assert norm({"est_weights_bytes": float("nan")})["est_weights_bytes"] is None
    assert norm({"cache_bytes": float("inf")})["cache_bytes"] is None
    assert norm({"cache_bytes": "not-a-number"})["cache_bytes"] is None
    # Absent means UNKNOWN, never 0.
    assert norm({})["est_weights_bytes"] is None
    assert norm({})["cache_bytes"] is None
    # `cache_bytes: 0` is a KNOWN empty store, not unknown.
    assert norm({"cache_bytes": 0})["cache_bytes"] == 0


# ---------------------------------------------------------------------------
# Display-size coalesce rule (the contract every residency UI shares)
# ---------------------------------------------------------------------------


def test_display_size_coalesce_picks_the_first_known_field() -> None:
    import abstractgateway.routes.gateway as gateway_routes

    coalesce = gateway_routes._model_residency_display_size_bytes

    assert gateway_routes.MODEL_RESIDENCY_DISPLAY_SIZE_FIELD_ORDER == (
        "size_bytes",
        "size_vram_bytes",
        "est_weights_bytes",
    )
    # First KNOWN wins, in order.
    assert coalesce({"size_bytes": 10, "size_vram_bytes": 20, "est_weights_bytes": 30}) == 10
    assert coalesce({"size_vram_bytes": 20, "est_weights_bytes": 30}) == 20
    # The case this rule exists for: an in-process MLX/GGUF runtime has no
    # server reporting a size, so only the provider's weight estimate carries
    # the row (it used to render as "0 B" / "size unknown").
    assert coalesce({"est_weights_bytes": 93_000_000_000}) == 93_000_000_000
    # camelCase aliases and stringy numerics ride the row_v1 coercion.
    assert coalesce({"sizeBytes": "1024"}) == 1024
    assert coalesce({"estWeightsBytes": 4096}) == 4096
    # Junk is not a value: fall through to the next field, then to unknown.
    assert coalesce({"size_bytes": float("nan"), "est_weights_bytes": 7}) == 7
    assert coalesce({"size_bytes": True}) is None
    assert coalesce({}) is None
    assert coalesce("not-a-dict") is None
    # cache_bytes is deliberately NOT a size: it is a separate footprint.
    assert coalesce({"cache_bytes": 4096}) is None


def test_host_state_model_bytes_coalesces_over_resident_rows_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`totals.model_bytes` sums the COALESCED display size over rows the
    provider verified RESIDENT. An est_weights-only resident row counts (the
    bug: a 93 GB GGUF summed to nothing); a cold row never does."""
    import abstractgateway.routes.gateway as gateway_routes

    class _MixedFacade(_FullStubHostFacade):
        def list_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
            _ = kwargs
            return {
                "ok": True,
                "supported": True,
                "models": [
                    # server-reported size, resident
                    {"runtime_id": "r1", "provider": "ollama", "model": "a", "resident": True, "size_bytes": 1_000, "cache_bytes": 10},
                    # in-process runtime: only the provider's weight estimate
                    {"runtime_id": "r2", "provider": "mlx", "model": "b", "resident": True, "est_weights_bytes": 2_000, "cache_bytes": 20},
                    # vram-only, resident
                    {"runtime_id": "r3", "provider": "ollama", "model": "c", "resident": True, "size_vram_bytes": 4_000},
                    # NOT resident: configured/cold weights are not in memory
                    {"runtime_id": "r4", "provider": "mlx", "model": "d", "resident": False, "size_bytes": 8_000, "cache_bytes": 40},
                    # residency unknown (tri-state null) — never counted
                    {"runtime_id": "r5", "provider": "mlx", "model": "e", "size_bytes": 16_000},
                ],
            }

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_MixedFacade(), None))
    _patch_gpu(monkeypatch, {"ts": "2026-08-27T00:00:00+00:00", "supported": True, "source": "test", "gpus": []})

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/host/state", headers=headers)

    assert resp.status_code == 200, resp.text
    totals = resp.json()["totals"]
    assert totals["models"] == 5
    assert totals["models_resident"] == 3
    assert totals["model_bytes"] == 1_000 + 2_000 + 4_000
    # cache_bytes_models sums every KNOWN row figure (residency is a separate
    # question from what a row's prompt-cache store holds).
    assert totals["cache_bytes_models"] == 10 + 20 + 40
    # ...and stays DISTINCT from the session-cache enumeration total.
    assert totals["session_cache_bytes"] == 4096


def test_host_state_relays_the_device_memory_block_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The device block is Core-owned truth relayed UNTOUCHED.

    `allocated_bytes` is PROCESS-LOCAL — a model resident in another process,
    or a llama.cpp/GGUF model in this one, is invisible to it — so clients need
    `host_in_use_bytes` (the cross-process accelerator heap) and
    `wired_limit_bytes` (the GPU ceiling) to say anything true about
    accelerator memory. No normalizer may drop them.
    """
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_FullStubHostFacade(), None))
    _patch_gpu(monkeypatch, {"ts": "2026-08-27T00:00:00+00:00", "supported": True, "source": "test", "gpus": []})

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        state = client.get("/api/gateway/host/state", headers=headers)
        metrics = client.get("/api/gateway/host/metrics/memory", headers=headers)

    assert state.status_code == 200, state.text
    device = state.json()["memory"]["device"]
    assert device == _MEMORY_SNAPSHOT["device"]
    assert device["host_in_use_bytes"] == 40_000
    assert device["wired_limit_bytes"] == 44_000
    # The dedicated memory route carries the same block.
    assert metrics.status_code == 200, metrics.text
    assert metrics.json()["device"] == _MEMORY_SNAPSHOT["device"]


def test_host_state_relays_the_gguf_shaped_device_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OTHER real device shape: both accelerator numbers near zero.

    Transcribed from a live host holding a fully offloaded 89.99 GB three-shard
    GGUF. `allocated_bytes` is 0 (llama.cpp does not use mlx's allocator) AND
    `host_in_use_bytes` is 0.79 GB (llama.cpp mmaps the file and wraps the pages
    with `newBufferWithBytesNoCopy`, so the weights are never driver-allocated
    accelerator memory). ~90 GB is resident and NEITHER device figure shows it.

    The fixture exists so nobody "fixes" a renderer by assuming a resident
    model must move `host_in_use_bytes`. It must not, and a UI that renders
    this block as the host's memory use reports 0.7% beside a 90 GB model row.
    """
    import abstractgateway.routes.gateway as gateway_routes

    class _GgufHostFacade(_FullStubHostFacade):
        def get_memory_snapshot(self) -> Dict[str, Any]:
            snapshot = dict(_MEMORY_SNAPSHOT)
            snapshot["device"] = dict(_METAL_GGUF_DEVICE)
            snapshot["process"] = {"rss_bytes": 73_245_212_672}
            return snapshot

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_GgufHostFacade(), None))
    _patch_gpu(monkeypatch, {"ts": "2026-08-27T00:00:00+00:00", "supported": True, "source": "test", "gpus": []})

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        state = client.get("/api/gateway/host/state", headers=headers)

    assert state.status_code == 200, state.text
    memory = state.json()["memory"]
    assert memory["device"] == _METAL_GGUF_DEVICE
    # The whole point: a zero here is NOT evidence of an empty accelerator.
    assert memory["device"]["allocated_bytes"] == 0
    assert memory["device"]["host_in_use_bytes"] == 792_461_312
    # Where the mmapped weights ARE visible.
    assert memory["process"]["rss_bytes"] == 73_245_212_672


def test_host_state_does_not_clobber_an_explicit_sweep_lockable_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit `lockable: false` SURVIVES the relay.

    Sweep rows (`source: "provider_server"`) are normally lockable — locking
    ADOPTS them — and core stamps `lockable: True` on the rows it synthesizes.
    But when a runtime states `lockable: false` for a specific row, that is a
    REFUSAL and the gateway must relay it, not overwrite it with the general
    rule. Observed live on this host: an LM Studio `provider_server` row served
    `lockable: false` while a sibling row served `lockable: true`.

    This matters to the UI contract: adopt WORDING is chosen by
    `source == "provider_server"`, but the lock GATE still honours
    `lockable is False`. Clobber this to `True` and the console offers a lock
    the runtime already refused.
    """
    import abstractgateway.routes.gateway as gateway_routes

    class _SweepRefusalFacade(_FullStubHostFacade):
        def list_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
            return {
                "ok": True,
                "supported": True,
                "models": [
                    {
                        "provider": "lmstudio",
                        "model": "qwen/qwen3-vl-4b",
                        "source": "provider_server",
                        "resident": True,
                        "size_bytes": 3_109_915_433,
                        "lockable": False,
                    },
                    {
                        "runtime_id": "local:text_generation:huggingface:gguf",
                        "provider": "huggingface",
                        "model": "unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q3_K_XL",
                        "source": "abstractruntime.local",
                        "resident": True,
                        "est_weights_bytes": 89_986_353_824,
                        "lockable": True,
                    },
                ],
            }

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_SweepRefusalFacade(), None))
    _patch_gpu(monkeypatch, {"ts": "2026-08-27T00:00:00+00:00", "supported": True, "source": "test", "gpus": []})

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        state = client.get("/api/gateway/host/state", headers=headers)

    assert state.status_code == 200, state.text
    rows = state.json()["models"]
    sweep = next(row for row in rows if row["source"] == "provider_server")
    managed = next(row for row in rows if row["source"] == "abstractruntime.local")

    # The refusal is relayed, NOT rewritten to the sweep default.
    assert sweep["lockable"] is False
    assert managed["lockable"] is True
    # And the sharded-GGUF weight total rides through as the row's display size.
    assert managed["est_weights_bytes"] == 89_986_353_824
    assert state.json()["totals"]["model_bytes"] == 3_109_915_433 + 89_986_353_824


def test_host_state_relays_unknown_device_fields_a_future_core_adds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The relay is field-agnostic: a device figure this Gateway has never
    heard of reaches clients anyway (no allowlist to fall behind Core)."""
    import abstractgateway.routes.gateway as gateway_routes

    class _FutureFacade(_FullStubHostFacade):
        def get_memory_snapshot(self) -> Dict[str, Any]:
            snap = dict(_MEMORY_SNAPSHOT)
            snap["device"] = {**snap["device"], "some_future_bytes": 123}
            return snap

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_FutureFacade(), None))
    _patch_gpu(monkeypatch, {"ts": "2026-08-27T00:00:00+00:00", "supported": True, "source": "test", "gpus": []})

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/host/state", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["memory"]["device"]["some_future_bytes"] == 123


def test_models_loaded_rows_empty_when_facade_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(
        gateway_routes,
        "_gateway_abstractcore_host_facade",
        lambda: (None, "Gateway runtime is not wired to AbstractCore host controls."),
    )

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/models/loaded", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["rows"] == []
    assert body["row_schema"] == "model_residency_row_v1"


# ---------------------------------------------------------------------------
# Session-cache enumeration lane
# ---------------------------------------------------------------------------


def test_session_prompt_caches_list_and_clear_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    facade = _FullStubHostFacade()
    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (facade, None))

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        listed_all = client.get("/api/gateway/sessions/prompt_cache", headers=headers)
        listed_one = client.get("/api/gateway/sessions/prompt_cache?session_id=sess1", headers=headers)
        listed_miss = client.get("/api/gateway/sessions/prompt_cache?session_id=other", headers=headers)
        cleared = client.post("/api/gateway/sessions/sess1/prompt_cache/clear_all", headers=headers)

    assert listed_all.status_code == 200, listed_all.text
    assert listed_all.json()["ok"] is True
    assert listed_all.json()["available"] is True
    assert listed_all.json()["caches"] == [_CACHE_ROW]

    assert listed_one.json()["caches"] == [_CACHE_ROW]
    assert listed_miss.json()["caches"] == []

    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["ok"] is True
    assert body["available"] is True
    assert body["count"] == 1
    assert body["cleared"] == [_CACHE_ROW]

    assert ("caches", {"session_id": None}) in facade.calls
    assert ("caches", {"session_id": "sess1"}) in facade.calls
    assert ("clear_all", {"session_id": "sess1"}) in facade.calls


def test_session_prompt_caches_degrade_without_facade_methods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_PartialStubHostFacade(), None))

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        listed = client.get("/api/gateway/sessions/prompt_cache", headers=headers)
        cleared = client.post("/api/gateway/sessions/sess1/prompt_cache/clear_all", headers=headers)

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["ok"] is False
    assert body["available"] is False
    assert body["route_available"] is True
    assert body["code"] == "session_caches_unavailable"
    assert body["caches"] == []

    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["ok"] is False
    assert body["available"] is False
    assert body["code"] == "session_caches_unavailable"
    assert body["cleared"] == []
    assert body["count"] == 0


def test_session_prompt_caches_facade_error_is_in_band(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    class _BrokenFacade:
        def list_session_prompt_caches(self, **kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("provider control plane went away")

        def clear_session_prompt_caches(self, **kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("clear blew up mid-flight")

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_BrokenFacade(), None))

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        listed = client.get("/api/gateway/sessions/prompt_cache", headers=headers)
        cleared = client.post("/api/gateway/sessions/sess1/prompt_cache/clear_all", headers=headers)

    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["ok"] is False
    assert body["available"] is False
    assert body["code"] == "session_caches_error"
    assert "went away" in body["error"]

    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["ok"] is False
    assert body["available"] is False
    assert body["code"] == "session_caches_error"
    assert "mid-flight" in body["error"]
    assert body["cleared"] == []
    assert body["count"] == 0


# ---------------------------------------------------------------------------
# Authorization split on the real app: reads for authenticated users,
# mutations admin-only.
# ---------------------------------------------------------------------------


def test_agentic_os_reads_are_user_level_and_mutations_stay_admin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_FullStubHostFacade(), None))
    _patch_gpu(monkeypatch, {"ts": "2026-08-27T00:00:00+00:00", "supported": True, "source": "test", "utilization_gpu_pct": 3.0, "gpus": []})

    client, admin_headers = _client(tmp_path, monkeypatch)
    with client:
        created = client.post(
            "/api/gateway/admin/users",
            headers=admin_headers,
            json={"user_id": "mallory", "tenant_id": "default", "roles": ["user"]},
        )
        assert created.status_code == 200, created.text
        user_headers = {"Authorization": f"Bearer {created.json()['token']}"}

        # READ surfaces: authenticated non-admin passes.
        for path in (
            "/api/gateway/models/loaded",
            "/api/gateway/models/context_estimate?provider=mlx&model=qwen",
            "/api/gateway/host/state",
            "/api/gateway/host/metrics/memory",
            "/api/gateway/host/metrics/gpu",
            "/api/gateway/sessions/prompt_cache",
        ):
            got = client.get(path, headers=user_headers)
            assert got.status_code == 200, (path, got.text)
            # ... but anonymous stays refused.
            assert client.get(path).status_code == 401, path

        # MUTATIONS: non-admin refused, admin passes.
        mutations = [
            ("/api/gateway/models/load", {"task": "text_generation", "provider": "mlx", "model": "qwen"}),
            ("/api/gateway/models/unload", {"provider": "mlx", "model": "qwen"}),
            ("/api/gateway/models/download", {"provider": "mlx", "model": "qwen"}),
            ("/api/gateway/models/lock", {"provider": "mlx", "model": "qwen"}),
            ("/api/gateway/models/unlock", {"provider": "mlx", "model": "qwen"}),
            ("/api/gateway/sessions/sess1/prompt_cache/clear_all", None),
        ]
        for path, payload in mutations:
            denied = client.post(path, headers=user_headers, json=payload)
            assert denied.status_code == 403, (path, denied.text)
            assert denied.json()["required_role"] == "admin"
        admin_cleared = client.post("/api/gateway/sessions/sess1/prompt_cache/clear_all", headers=admin_headers)
        assert admin_cleared.status_code == 200, admin_cleared.text
        assert admin_cleared.json()["ok"] is True
