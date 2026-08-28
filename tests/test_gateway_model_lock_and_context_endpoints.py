"""Model-management wave: lock/unlock, context estimates, force-unload.

Facade contract (implemented by the parallel abstractruntime workstream):
- ``lock_model_residency(payload: dict)`` / ``unlock_model_residency(payload: dict)``
- ``get_context_estimate(payload: dict)``
- ``unload_model_residency(...)`` accepts ``force`` and may refuse ``model_locked``

The gateway reaches all of them duck-typed through the Runtime host facade and
degrades in-band at 200 when a method is missing (older runtimes).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

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


class _LockStubFacade:
    """Payload-dict style facade — EXACTLY the contract shape."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Dict[str, Any]]] = []

    def lock_model_residency(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(("lock", dict(payload)))
        return {
            "ok": True,
            "locked": True,
            "runtime_id": "local:text_generation:mlx:qwen",
            "provider": "mlx",
            "model": "qwen",
            "provider_side": {"supported": True, "applied": True},
        }

    def unlock_model_residency(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(("unlock", dict(payload)))
        return {"ok": True, "locked": False, "runtime_id": "local:text_generation:mlx:qwen"}

    def get_context_estimate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(("estimate", dict(payload)))
        return {
            "ok": True,
            "confidence": "calibrated",
            "predicted_max_context": 32768,
            "calibrated_context_length": 16384,
            "kv_bytes_per_token": 512,
            "est_kv_bytes": 8_388_608,
            "est_weights_bytes": 4_000_000_000,
            "geometry": {"n_layers": 28, "n_kv_heads": 8},
            "memory": {"available_bytes": 48_000},
            "notes": ["calibrated on this host"],
        }

    def unload_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
        # kwargs style: this method PREDATES the wave and keeps its spelling.
        self.calls.append(("unload", dict(kwargs)))
        if kwargs.get("model") == "locked-model" and not kwargs.get("force"):
            return {
                "ok": False,
                "error": "model_locked",
                "locked": True,
                "provider": kwargs.get("provider"),
                "model": kwargs.get("model"),
                "detail": "model is locked; unlock it or pass force",
            }
        return {"ok": True, "supported": True, "unloaded": True, "request": kwargs}


def _patch_facade(monkeypatch: pytest.MonkeyPatch, facade: Any) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (facade, None))


# ---------------------------------------------------------------------------
# POST /models/lock + /models/unlock
# ---------------------------------------------------------------------------


def test_lock_and_unlock_relay_payload_style_facade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    facade = _LockStubFacade()
    _patch_facade(monkeypatch, facade)

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        locked = client.post(
            "/api/gateway/models/lock",
            json={"provider": "mlx", "model": "qwen", "base_url": "http://core:8000", "timeout_s": 5.0},
            headers=headers,
        )
        unlocked = client.post("/api/gateway/models/unlock", json={"runtime_id": "local:text_generation:mlx:qwen"}, headers=headers)

    assert locked.status_code == 200, locked.text
    body = locked.json()
    assert body["ok"] is True
    assert body["locked"] is True
    assert body["provider_side"] == {"supported": True, "applied": True}
    assert body["operation"] == "lock"
    assert body["available"] is True
    assert body["route_available"] is True
    assert body["source"] == "abstractruntime.host_facade"

    assert unlocked.status_code == 200, unlocked.text
    body = unlocked.json()
    assert body["ok"] is True
    assert body["locked"] is False
    assert body["operation"] == "unlock"

    assert facade.calls == [
        ("lock", {"provider": "mlx", "model": "qwen", "base_url": "http://core:8000", "timeout_s": 5.0}),
        ("unlock", {"runtime_id": "local:text_generation:mlx:qwen"}),
    ]


def test_lock_tolerates_kwargs_style_facade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A facade written in the older kwargs idiom still relays (TypeError fallback)."""
    calls: list[Dict[str, Any]] = []

    class _KwargsFacade:
        def lock_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(dict(kwargs))
            return {"ok": True, "locked": True}

    _patch_facade(monkeypatch, _KwargsFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post("/api/gateway/models/lock", json={"provider": "mlx", "model": "qwen"}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["locked"] is True
    assert calls == [{"provider": "mlx", "model": "qwen"}]


def test_lock_and_unlock_degrade_when_method_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _OldFacade:
        def list_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
            _ = kwargs
            return {"ok": True, "models": []}

    _patch_facade(monkeypatch, _OldFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        locked = client.post("/api/gateway/models/lock", json={"provider": "mlx", "model": "qwen"}, headers=headers)
        unlocked = client.post("/api/gateway/models/unlock", json={"provider": "mlx", "model": "qwen"}, headers=headers)

    for resp, op in ((locked, "lock"), (unlocked, "unlock")):
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["available"] is False
        assert body["route_available"] is True
        assert body["code"] == "model_residency_unavailable"
        assert body["operation"] == op
        assert "does not expose" in body["error"]


def test_lock_degrades_when_facade_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(
        gateway_routes,
        "_gateway_abstractcore_host_facade",
        lambda: (None, "Gateway runtime is not wired to AbstractCore host controls."),
    )

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post("/api/gateway/models/lock", json={"provider": "mlx", "model": "qwen"}, headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["available"] is False
    assert body["code"] == "model_residency_unavailable"
    assert "host controls" in body["error"]


def test_lock_facade_exception_is_in_band(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _RaisingFacade:
        def lock_model_residency(self, payload: Dict[str, Any]) -> Dict[str, Any]:
            raise RuntimeError("provider control plane went away")

    _patch_facade(monkeypatch, _RaisingFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post("/api/gateway/models/lock", json={"provider": "mlx", "model": "qwen"}, headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["available"] is False
    assert body["code"] == "model_residency_error"
    assert "went away" in body["error"]


def test_lock_and_unlock_are_admin_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    _patch_facade(monkeypatch, _LockStubFacade())

    client, admin_headers = _client(tmp_path, monkeypatch)
    with client:
        created = client.post(
            "/api/gateway/admin/users",
            headers=admin_headers,
            json={"user_id": "mallory", "tenant_id": "default", "roles": ["user"]},
        )
        assert created.status_code == 200, created.text
        user_headers = {"Authorization": f"Bearer {created.json()['token']}"}

        for path in ("/api/gateway/models/lock", "/api/gateway/models/unlock"):
            denied = client.post(path, headers=user_headers, json={"provider": "mlx", "model": "qwen"})
            assert denied.status_code == 403, (path, denied.text)
            assert denied.json()["required_role"] == "admin"
            anonymous = client.post(path, json={"provider": "mlx", "model": "qwen"})
            assert anonymous.status_code == 401, path
            allowed = client.post(path, headers=admin_headers, json={"provider": "mlx", "model": "qwen"})
            assert allowed.status_code == 200, (path, allowed.text)


# ---------------------------------------------------------------------------
# POST /models/load with lock:true (load-then-lock convenience)
# ---------------------------------------------------------------------------


class _LoadLockStubFacade:
    """Load (kwargs style, pre-wave) + lock (payload style, wave contract)."""

    def __init__(self, *, load_ok: bool = True, with_lock: bool = True) -> None:
        self.calls: list[tuple[str, Dict[str, Any]]] = []
        self._load_ok = load_ok
        if not with_lock:
            # Older facade: no lock_model_residency at all.
            self.lock_model_residency = None  # type: ignore[assignment]

    def load_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(("load", dict(kwargs)))
        if not self._load_ok:
            return {"ok": False, "supported": True, "operation": "load", "error": "provider refused the load"}
        return {
            "ok": True,
            "supported": True,
            "loaded_new": True,
            "runtime": {"runtime_id": "local:text_generation:mlx:qwen"},
            "request": kwargs,
        }

    def lock_model_residency(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append(("lock", dict(payload)))
        return {"ok": True, "locked": True, "runtime_id": payload.get("runtime_id")}


def test_load_with_lock_true_locks_after_successful_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    facade = _LoadLockStubFacade()
    _patch_facade(monkeypatch, facade)

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post(
            "/api/gateway/models/load",
            json={"task": "text_generation", "provider": "mlx", "model": "qwen", "lock": True},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["loaded_new"] is True
    # Lock outcome rides additively; load success is untouched.
    assert body["lock"]["ok"] is True
    assert body["lock"]["locked"] is True
    assert body["lock"]["operation"] == "lock"
    # `lock` never reaches the load facade method; the lock call targets the
    # runtime_id the load answer minted.
    assert facade.calls == [
        ("load", {"task": "text_generation", "provider": "mlx", "model": "qwen", "pin": True}),
        ("lock", {"runtime_id": "local:text_generation:mlx:qwen"}),
    ]


def test_load_with_lock_true_reports_unsupported_lock_but_load_stays_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    facade = _LoadLockStubFacade(with_lock=False)
    _patch_facade(monkeypatch, facade)

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post(
            "/api/gateway/models/load",
            json={"provider": "mlx", "model": "qwen", "lock": True},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True  # the load itself succeeded
    assert body["loaded_new"] is True
    lock = body["lock"]
    assert lock["ok"] is False
    assert lock["available"] is False
    assert lock["supported"] is False
    assert lock["code"] == "model_residency_unavailable"
    assert [name for name, _ in facade.calls] == ["load"]


def test_load_without_lock_never_calls_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    facade = _LoadLockStubFacade()
    _patch_facade(monkeypatch, facade)

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        absent = client.post("/api/gateway/models/load", json={"provider": "mlx", "model": "qwen"}, headers=headers)
        explicit_false = client.post(
            "/api/gateway/models/load",
            json={"provider": "mlx", "model": "qwen", "lock": False},
            headers=headers,
        )

    assert absent.status_code == 200, absent.text
    assert explicit_false.status_code == 200, explicit_false.text
    assert "lock" not in absent.json()
    assert "lock" not in explicit_false.json()
    # ONLY load invocations — no second facade call, and no `lock` kwarg leaked.
    assert [name for name, _ in facade.calls] == ["load", "load"]
    assert all("lock" not in payload for _, payload in facade.calls)


def test_load_failure_with_lock_true_skips_the_lock_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    facade = _LoadLockStubFacade(load_ok=False)
    _patch_facade(monkeypatch, facade)

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post(
            "/api/gateway/models/load",
            json={"provider": "mlx", "model": "qwen", "lock": True},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "provider refused" in body["error"]
    assert "lock" not in body  # nothing was locked and nothing pretends otherwise
    assert [name for name, _ in facade.calls] == ["load"]


def test_load_and_unload_paths_never_gate_on_the_context_estimate() -> None:
    """The context estimate is ADVISORY ONLY: no gateway load/unload path
    consults it, gates on it, or blocks on it (source-level pin)."""
    import inspect

    import abstractgateway.routes.gateway as gateway_routes

    for handler in (gateway_routes.model_residency_load, gateway_routes.model_residency_unload):
        src = inspect.getsource(handler)
        assert "context_estimate" not in src
        assert "estimate_context_fit" not in src


# ---------------------------------------------------------------------------
# POST /models/unload: force passthrough + locked refusal -> 409
# ---------------------------------------------------------------------------


def test_unload_force_passthrough_and_default_omission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    facade = _LockStubFacade()
    _patch_facade(monkeypatch, facade)

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        plain = client.post("/api/gateway/models/unload", json={"provider": "mlx", "model": "qwen"}, headers=headers)
        forced = client.post(
            "/api/gateway/models/unload",
            json={"provider": "mlx", "model": "qwen", "force": True},
            headers=headers,
        )
        explicit_false = client.post(
            "/api/gateway/models/unload",
            json={"provider": "mlx", "model": "qwen", "force": False},
            headers=headers,
        )

    assert plain.status_code == 200, plain.text
    assert forced.status_code == 200, forced.text
    assert explicit_false.status_code == 200, explicit_false.text
    unload_calls = [payload for name, payload in facade.calls if name == "unload"]
    # Default/false NEVER reaches the facade (older facades reject unknown kwargs).
    assert unload_calls[0] == {"provider": "mlx", "model": "qwen"}
    assert unload_calls[1] == {"provider": "mlx", "model": "qwen", "force": True}
    assert unload_calls[2] == {"provider": "mlx", "model": "qwen"}


def test_unload_locked_refusal_is_409_and_force_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    facade = _LockStubFacade()
    _patch_facade(monkeypatch, facade)

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        refused = client.post("/api/gateway/models/unload", json={"provider": "mlx", "model": "locked-model"}, headers=headers)
        forced = client.post(
            "/api/gateway/models/unload",
            json={"provider": "mlx", "model": "locked-model", "force": True},
            headers=headers,
        )

    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["ok"] is False
    assert body["error"] == "model_locked"
    assert body["locked"] is True
    assert body["model"] == "locked-model"
    assert body["operation"] == "unload"

    assert forced.status_code == 200, forced.text
    assert forced.json()["ok"] is True
    assert forced.json()["unloaded"] is True


def test_unload_locked_refusal_shapes_never_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Adversarial refusal spellings: dict-valued error -> 409, success-only
    refusal (no `ok` key) -> 409, list-valued error -> in-band 200 (no crash)."""
    import datetime

    answers = {
        "dict-error": {"ok": False, "error": {"code": "model_locked", "detail": "pinned by operator"}},
        "success-only": {"success": False, "error": "model_locked"},
        "list-error": {"ok": False, "error": ["provider refused", "try later"]},
        "datetime-body": {"ok": False, "error": "model_locked", "locked_at": datetime.datetime(2026, 8, 27, 0, 0, 0)},
    }

    class _ShapesFacade:
        def unload_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
            return dict(answers[kwargs["model"]])

    _patch_facade(monkeypatch, _ShapesFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        dict_error = client.post("/api/gateway/models/unload", json={"provider": "mlx", "model": "dict-error"}, headers=headers)
        success_only = client.post("/api/gateway/models/unload", json={"provider": "mlx", "model": "success-only"}, headers=headers)
        list_error = client.post("/api/gateway/models/unload", json={"provider": "mlx", "model": "list-error"}, headers=headers)
        dated = client.post("/api/gateway/models/unload", json={"provider": "mlx", "model": "datetime-body"}, headers=headers)

    assert dict_error.status_code == 409, dict_error.text
    assert dict_error.json()["error"]["code"] == "model_locked"

    assert success_only.status_code == 409, success_only.text
    assert success_only.json()["error"] == "model_locked"

    # Not a locked refusal: stays the in-band 200 envelope, never a TypeError 500.
    assert list_error.status_code == 200, list_error.text
    assert list_error.json()["error"] == ["provider refused", "try later"]

    # The 409 lane must survive non-JSON-native values (FastAPI encoder).
    assert dated.status_code == 409, dated.text
    assert dated.json()["locked_at"] == "2026-08-27T00:00:00"


def test_lock_positional_or_keyword_facade_binds_kwargs_style(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A facade whose first param is positional-or-keyword (NOT named payload)
    must be called kwargs-style — never with the payload dict misbound into
    its first parameter."""
    calls: list[Dict[str, Any]] = []

    class _PokFacade:
        def lock_model_residency(self, runtime_id=None, provider=None, model=None, base_url=None, timeout_s=None):
            calls.append({"runtime_id": runtime_id, "provider": provider, "model": model})
            assert not isinstance(runtime_id, dict), "payload dict misbound into runtime_id"
            return {"ok": True, "locked": True, "provider": provider, "model": model}

    _patch_facade(monkeypatch, _PokFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post("/api/gateway/models/lock", json={"provider": "mlx", "model": "qwen"}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["locked"] is True
    assert resp.json()["provider"] == "mlx"
    assert calls == [{"runtime_id": None, "provider": "mlx", "model": "qwen"}]


def test_lock_typeerror_inside_facade_is_single_honest_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TypeError raised INSIDE the facade is a facade bug, not a signature
    mismatch: exactly ONE invocation (a mutation must never be double-fired by
    a convention probe) and an honest in-band error."""
    invocations: list[Dict[str, Any]] = []

    class _BuggyFacade:
        def lock_model_residency(self, payload: Dict[str, Any]) -> Dict[str, Any]:
            invocations.append(dict(payload))
            raise TypeError("unsupported operand deep inside the facade")

    _patch_facade(monkeypatch, _BuggyFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post("/api/gateway/models/lock", json={"provider": "mlx", "model": "qwen"}, headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["code"] == "model_residency_error"
    assert "deep inside the facade" in body["error"]
    assert len(invocations) == 1, "facade must be invoked exactly once"


def test_lock_success_only_facade_failure_is_not_stamped_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A facade that only speaks `success: false` must not come back `ok: true`."""

    class _SuccessOnlyFacade:
        def lock_model_residency(self, payload: Dict[str, Any]) -> Dict[str, Any]:
            _ = payload
            return {"success": False, "error": "no such runtime"}

    _patch_facade(monkeypatch, _SuccessOnlyFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post("/api/gateway/models/lock", json={"provider": "mlx", "model": "ghost"}, headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["success"] is False
    assert body["error"] == "no such runtime"


# ---------------------------------------------------------------------------
# GET /models/context_estimate
# ---------------------------------------------------------------------------


def test_context_estimate_relays_calibrated_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    facade = _LockStubFacade()
    _patch_facade(monkeypatch, facade)

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get(
            "/api/gateway/models/context_estimate?provider=mlx&model=qwen&context_length=16384",
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["confidence"] == "calibrated"
    assert body["predicted_max_context"] == 32768
    assert body["calibrated_context_length"] == 16384
    assert body["kv_bytes_per_token"] == 512
    assert body["geometry"] == {"n_layers": 28, "n_kv_heads": 8}
    assert body["notes"] == ["calibrated on this host"]
    assert body["operation"] == "context_estimate"
    assert body["available"] is True
    assert facade.calls == [("estimate", {"provider": "mlx", "model": "qwen", "context_length": 16384})]


@pytest.mark.parametrize(
    "estimate",
    [
        {"ok": True, "confidence": "estimated", "predicted_max_context": 8192, "est_kv_bytes": 1024, "notes": ["heuristic geometry"]},
        {"ok": True, "confidence": "unknown", "notes": ["no geometry available"]},
    ],
)
def test_context_estimate_relays_estimated_and_unknown_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, estimate: Dict[str, Any]
) -> None:
    class _EstimateFacade:
        def get_context_estimate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
            assert payload == {"provider": "ollama", "model": "phi"}  # no context_length param
            return dict(estimate)

    _patch_facade(monkeypatch, _EstimateFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/models/context_estimate?provider=ollama&model=phi", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["confidence"] == estimate["confidence"]
    assert body["notes"] == estimate["notes"]


def test_context_estimate_rejects_non_positive_context_length(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """context_length carries a ge=1 bound: zero/negative lengths are schema errors."""
    _patch_facade(monkeypatch, _LockStubFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        zero = client.get("/api/gateway/models/context_estimate?provider=mlx&model=qwen&context_length=0", headers=headers)
        negative = client.get("/api/gateway/models/context_estimate?provider=mlx&model=qwen&context_length=-1", headers=headers)

    assert zero.status_code == 422, zero.text
    assert negative.status_code == 422, negative.text


def test_context_estimate_degrades_when_method_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _OldFacade:
        pass

    _patch_facade(monkeypatch, _OldFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/models/context_estimate?provider=mlx&model=qwen", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["available"] is False
    assert body["route_available"] is True
    assert body["code"] == "context_estimate_unavailable"


def test_context_estimate_is_an_authenticated_read_not_admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    _patch_facade(monkeypatch, _LockStubFacade())

    client, admin_headers = _client(tmp_path, monkeypatch)
    with client:
        created = client.post(
            "/api/gateway/admin/users",
            headers=admin_headers,
            json={"user_id": "mallory", "tenant_id": "default", "roles": ["user"]},
        )
        assert created.status_code == 200, created.text
        user_headers = {"Authorization": f"Bearer {created.json()['token']}"}

        got = client.get("/api/gateway/models/context_estimate?provider=mlx&model=qwen", headers=user_headers)
        assert got.status_code == 200, got.text
        assert got.json()["confidence"] == "calibrated"
        assert client.get("/api/gateway/models/context_estimate?provider=mlx&model=qwen").status_code == 401


# ---------------------------------------------------------------------------
# row_v1: additive lock/modality/calibration fields
# ---------------------------------------------------------------------------


def test_row_v1_new_fields_extracted() -> None:
    import abstractgateway.routes.gateway as gateway_routes

    norm = gateway_routes._normalize_model_residency_row_v1

    row = norm(
        {
            "runtime_id": "r1",
            "locked": True,
            "lockable": True,
            "modalities": ["input.text", "output.text", "input.image"],
            "calibrated_context_length": 4096,
            "context_calibrated": True,
            "host_id": "h-1",
            "host_name": "studio.local",
        }
    )
    assert row is not None
    assert row["locked"] is True
    assert row["lockable"] is True
    assert row["modalities"] == ["input.text", "output.text", "input.image"]
    assert row["calibrated_context_length"] == 4096
    assert row["context_calibrated"] is True
    assert row["host_id"] == "h-1"
    assert row["host_name"] == "studio.local"
    # Raw record stays reachable through details.
    assert row["details"]["modalities"] == ["input.text", "output.text", "input.image"]

    # Absent = unknown (tri-state null), never guessed false/empty.
    empty = norm({})
    assert empty is not None
    for field in ("locked", "lockable", "modalities", "calibrated_context_length", "context_calibrated", "host_id", "host_name"):
        assert empty[field] is None, field


def test_row_v1_new_fields_junk_tolerance() -> None:
    import abstractgateway.routes.gateway as gateway_routes

    norm = gateway_routes._normalize_model_residency_row_v1

    # modalities: pass through ONLY a list of strings.
    assert norm({"modalities": "input.text"})["modalities"] is None
    assert norm({"modalities": {"input.text": True}})["modalities"] is None
    assert norm({"modalities": ["input.text", 3]})["modalities"] is None
    assert norm({"modalities": []})["modalities"] == []

    # calibrated_context_length rides the safe int coercion (never raises).
    assert norm({"calibrated_context_length": float("nan")})["calibrated_context_length"] is None
    assert norm({"calibrated_context_length": float("inf")})["calibrated_context_length"] is None
    assert norm({"calibrated_context_length": "not-a-number"})["calibrated_context_length"] is None
    assert norm({"calibrated_context_length": "8192"})["calibrated_context_length"] == 8192
    assert norm({"calibrated_context_length": True})["calibrated_context_length"] is None

    # locked/lockable are strict tri-state booleans.
    assert norm({"locked": "yes"})["locked"] is None
    assert norm({"locked": False})["locked"] is False
    assert norm({"lockable": 1})["lockable"] is None

    # host fields: non-string / blank -> null.
    assert norm({"host_id": 7})["host_id"] is None
    assert norm({"host_name": "  "})["host_name"] is None


# ---------------------------------------------------------------------------
# Contract descriptor: new endpoints + canonical modality palette
# ---------------------------------------------------------------------------


def test_contract_descriptor_advertises_lock_endpoints_and_modality_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_facade(monkeypatch, _LockStubFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/discovery/capabilities", headers=headers)

    assert resp.status_code == 200, resp.text
    residency = resp.json()["capabilities"]["contracts"]["common"]["model_residency"]
    assert residency["endpoints"]["lock"] == "/api/gateway/models/lock"
    assert residency["endpoints"]["unlock"] == "/api/gateway/models/unlock"
    assert residency["endpoints"]["context_estimate"] == "/api/gateway/models/context_estimate"
    assert residency["row_schema"] == "model_residency_row_v1"

    # The CANONICAL palette all clients must render modalities with
    # (aligned with abstractflow's PIN_COLORS).
    assert residency["modality_ui"] == {
        "version": 1,
        "colors": {
            "text_generation": {"color": "#00D2FF", "label": "Text"},
            "image_generation": {"color": "#19D3B8", "label": "Image"},
            "image_to_image": {"color": "#19D3B8", "label": "Image"},
            "image_upscale": {"color": "#19D3B8", "label": "Image"},
            "video_generation": {"color": "#A855F7", "label": "Video"},
            "text_to_video": {"color": "#A855F7", "label": "Video"},
            "image_to_video": {"color": "#A855F7", "label": "Video"},
            "tts": {"color": "#22D3EE", "label": "Voice"},
            "stt": {"color": "#22D3EE", "label": "Voice"},
            "music_generation": {"color": "#F59E0B", "label": "Music"},
            "scene3d_generation": {"color": "#9D4EDD", "label": "3D"},
            "text_to_scene3d": {"color": "#9D4EDD", "label": "3D"},
            "image_to_scene3d": {"color": "#9D4EDD", "label": "3D"},
            "embedding": {"color": "#94A3B8", "label": "Embedding"},
            "unknown": {"color": "#6B7280", "label": "Unknown"},
        },
    }
