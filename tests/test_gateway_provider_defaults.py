from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _portable_host(monkeypatch):
    """These tests pin the PORTABLE text recommendation (LM Studio
    qwen/qwen3.5-9b), so AbstractCore's host probe reports a non-Apple host.
    On a Mac the text row follows the unified-memory tiers instead
    (tests/test_gateway_recommended_text_tiers.py)."""

    from abstractcore.utils import host_profile as hp

    gib = 1024**3
    host = {
        "schema": "host_profile_v1", "os": "linux", "arch": "x86_64", "accelerator": "cuda",
        "gpu_name": "synthetic", "gpu_count": 1, "unified_memory": False, "ram_bytes": 64 * gib,
        "vram_bytes": 24 * gib, "ceiling_bytes": 24 * gib, "ceiling_source": "cuda_total",
        "free_now_bytes": 22 * gib, "disk": {}, "notes": [],
    }
    monkeypatch.setattr(hp, "host_profile", lambda **_k: dict(host))

pytestmark = pytest.mark.basic


def _empty_core_store(path: Path) -> Path:
    """An EXISTING AbstractCore store that configures nothing.

    "No default is configured" is a store that exists and carries no routes --
    NOT an absent file. Since the operator ruling of 2026-08-01 an absent file
    means a truly fresh install, and AbstractCore seeds it with the recommended
    stack (text `lmstudio/qwen/qwen3.5-9b`, voice `supertonic/supertonic-3`,
    image `mlx-gen/AbstractFramework/flux.2-klein-4b-8bit`). A test that means
    "nothing is configured" therefore has to say so by materializing the store,
    exactly as AbstractCore's own `tests/config` do; leaving the file absent
    would silently assert the fresh-install journey instead.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    return path


def _empty_home_core_store(home: Path) -> Path:
    return _empty_core_store(home / ".abstractcore" / "config" / "abstractcore.json")


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "ABSTRACTGATEWAY_PROVIDER",
        "ABSTRACTGATEWAY_MODEL",
        "ABSTRACTFLOW_PROVIDER",
        "ABSTRACTFLOW_MODEL",
        "ABSTRACTGATEWAY_USER_AUTH",
        "ABSTRACTGATEWAY_MULTI_USER",
        "ABSTRACTFLOW_GATEWAY_USER_AUTH",
        "ABSTRACTGATEWAY_AUTH_MODE",
        "ABSTRACTCORE_CONFIG_FILE",
        "ABSTRACTCORE_CONFIG_DIR",
        "ABSTRACTCORE_SERVER_BASE_URL",
        "ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_AUTH_TOKEN",
        "ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_API_KEY",
        "ABSTRACTCORE_AUTH_TOKEN",
        "ABSTRACTCORE_SERVER_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def _write_min_bundle(*, bundles_dir: Path) -> tuple[str, str]:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = "bundle-provider-defaults"
    flow_id = "root"
    flow = {
        "id": flow_id,
        "name": "root",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 32, "y": 128},
                "data": {"nodeType": "on_flow_start", "inputs": [], "outputs": [{"id": "exec-out", "type": "execution"}]},
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 288, "y": 128},
                "data": {"nodeType": "on_flow_end", "inputs": [{"id": "exec-in", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "e", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"}],
        "entryNode": "start",
    }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-05-08T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    with zipfile.ZipFile(bundles_dir / f"{bundle_id}.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow))
    return bundle_id, flow_id


def test_provider_model_resolver_prefers_request_then_capability_route_then_flow_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # Tier 4 only gets a turn when tier 3 said nothing, so this half of the
    # cascade needs a store that exists and configures nothing.
    _empty_home_core_store(tmp_path / "home")

    req = provider_defaults.resolve_gateway_provider_model(provider="OLLAMA", model="llama3", purpose="test").require()
    assert req == ("ollama", "llama3")

    flow = provider_defaults.resolve_gateway_provider_model(
        flow_defaults=("ollama", "qwen3:8b"),
        purpose="test",
    )
    assert (flow.provider, flow.model, flow.source) == ("ollama", "qwen3:8b", "flow_defaults")

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home-route"))
    manager = ConfigurationManager()
    assert manager.set_capability_default("output.text", provider="lmstudio", model="qwen-local")

    route = provider_defaults.resolve_gateway_provider_model(flow_defaults=("ollama", "stale-flow-model"), purpose="test")
    assert (route.provider, route.model, route.source) == (
        "lmstudio",
        "qwen-local",
        # The source now NAMES the route key that answered, so a run's
        # evidence says where the default came from (canonical output.text,
        # or the legacy input.text storage key on an unmigrated config).
        "abstractcore.capability_defaults:output.text",
    )


def test_provider_model_resolver_does_not_cross_fill_partial_pairs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    manager = ConfigurationManager()
    assert manager.set_capability_default("output.text", provider="ollama", model="route-model")

    provider_only = provider_defaults.resolve_gateway_provider_model(
        provider="openai",
        flow_defaults=("lmstudio", "flow-model"),
        purpose="test",
    )
    assert provider_only.provider == "openai"
    assert provider_only.model is None
    assert "No provider/model is configured for test" in str(provider_only.error)

    model_only = provider_defaults.resolve_gateway_provider_model(
        model="request-model",
        flow_defaults=("lmstudio", "flow-model"),
        purpose="test",
    )
    assert model_only.provider is None
    assert model_only.model == "request-model"
    assert "No provider/model is configured for test" in str(model_only.error)


def test_provider_model_resolver_ignores_partial_capability_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    manager = ConfigurationManager()
    assert manager.set_capability_default("output.text", provider="lmstudio", model=None)

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="test")

    assert resolved.provider is None
    assert resolved.model is None
    assert "No provider/model is configured for test" in str(resolved.error)


def test_core_server_token_accepts_core_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.core_config import core_server_token

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("ABSTRACTCORE_AUTH_TOKEN", "core-token")

    assert core_server_token() == "core-token"


def test_provider_model_resolver_reports_clear_config_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The error path is still reachable -- from an EXISTING store with no routes.

    A host whose operator cleared the text default (or who never had one and
    has a store from some earlier write) gets the named, actionable error. An
    ABSENT store is a different journey now: see
    `test_a_fresh_install_resolves_to_the_recommended_text_default`.
    """
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    _empty_home_core_store(tmp_path)

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="summary helper")
    assert resolved.provider is None
    assert resolved.model is None
    assert "No provider/model is configured for summary helper" in str(resolved.error)


def test_a_fresh_install_resolves_to_the_recommended_text_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """THE FRESH-INSTALL JOURNEY, end to end through the Gateway resolver.

    Operator ruling 2026-08-01: an install where no AbstractCore config file
    has ever existed works out of the box on the recommended stack instead of
    refusing until configured. So a Gateway on a brand-new host resolves the
    text default rather than raising the "No provider/model is configured"
    error it used to -- and the resolution names the ordinary route key that
    answered, because the seed writes ordinary rows and nothing else.
    """
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert not (tmp_path / ".abstractcore" / "config" / "abstractcore.json").exists()

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="test")

    assert (resolved.provider, resolved.model) == ("lmstudio", "qwen/qwen3.5-9b")
    assert resolved.source == "abstractcore.capability_defaults:output.text"
    assert resolved.error is None

    # A request pin still beats it -- the seed is a default, not a policy.
    pinned = provider_defaults.resolve_gateway_provider_model(provider="ollama", model="granite", purpose="test")
    assert (pinned.provider, pinned.model, pinned.source) == ("ollama", "granite", "request")


def test_a_fresh_install_serves_the_three_recommended_routes_with_their_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same journey at the payload the console renders.

    All three recommended routes are ordinary, configured rows in the grid, and
    the payload carries the `seeded` marker so a surface can label them
    "recommended" rather than implying the operator picked them.
    """
    from abstractgateway.core_config import gateway_capability_defaults_payload

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert not (tmp_path / ".abstractcore" / "config" / "abstractcore.json").exists()

    payload = gateway_capability_defaults_payload()
    rows = {row.get("key"): row for row in payload["routes"] if isinstance(row, dict)}

    for key, provider, model in (
        ("output.text", "lmstudio", "qwen/qwen3.5-9b"),
        ("output.voice", "supertonic", "supertonic-3"),
        ("output.image", "mlx-gen", "AbstractFramework/flux.2-klein-4b-8bit"),
    ):
        assert rows[key]["configured"] is True, f"{key} must be an ordinary configured row"
        assert rows[key]["provider"] == provider
        assert rows[key]["model"] == model

    assert payload.get("seeded") == "recommended-v1", "the payload must carry the seed provenance"


def test_provider_model_resolver_uses_execution_host_capability_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    manager = ConfigurationManager()
    assert manager.set_capability_default("output.text", provider="lmstudio", model="qwen-local")

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="test")

    assert (resolved.provider, resolved.model, resolved.source) == (
        "lmstudio",
        "qwen-local",
        # The source now NAMES the route key that answered, so a run's
        # evidence says where the default came from (canonical output.text,
        # or the legacy input.text storage key on an unmigrated config).
        "abstractcore.capability_defaults:output.text",
    )


def test_provider_model_resolver_ignores_legacy_gateway_defaults_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("HOME", str(tmp_path))

    legacy = tmp_path / "runtime" / "config" / "capability_defaults.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps({"version": 1, "routes": {"output.text": {"provider": "legacy-provider", "model": "legacy-model"}}}),
        encoding="utf-8",
    )
    # The live store exists and is empty, so "nothing resolves" can only mean
    # the legacy overlay was ignored -- not that a fresh install got seeded.
    # ONE STORE (2026-08-01): the live store is Core's, not a Gateway base.
    _empty_home_core_store(tmp_path)

    resolved = provider_defaults.resolve_gateway_provider_model(base_dir=tmp_path / "runtime", purpose="test")

    assert resolved.provider is None
    assert resolved.model is None
    assert "No provider/model is configured for test" in str(resolved.error)


def test_provider_model_resolver_falls_back_to_abstractcore_capability_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    manager = ConfigurationManager()
    assert manager.set_capability_default("output.text", provider="ollama", model="qwen3:8b")

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="test")

    assert (resolved.provider, resolved.model, resolved.source) == (
        "ollama",
        "qwen3:8b",
        # The source now NAMES the route key that answered, so a run's
        # evidence says where the default came from (canonical output.text,
        # or the legacy input.text storage key on an unmigrated config).
        "abstractcore.capability_defaults:output.text",
    )


def test_provider_model_resolver_does_not_use_gateway_host_core_config_when_remote_core_is_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from abstractcore.config.manager import ConfigurationManager
    import abstractgateway.core_config as capability_defaults
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ABSTRACTCORE_SERVER_BASE_URL", "http://core.example/v1")

    manager = ConfigurationManager()
    assert manager.set_capability_default("output.text", provider="lmstudio", model="local-gateway-host-model")

    def fail_urlopen(*_args, **_kwargs):
        raise capability_defaults.urllib.error.URLError("remote core unavailable")

    monkeypatch.setattr(capability_defaults.urllib.request, "urlopen", fail_urlopen)

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="test")

    assert resolved.provider is None
    assert resolved.model is None
    assert "No provider/model is configured for test" in str(resolved.error)


def test_summary_helper_rejects_missing_provider_model_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.provider_defaults as provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    # An UNCONFIGURED host is an existing store with no routes; an absent store
    # would be a fresh install, which now has a recommended default to run with.
    _empty_home_core_store(tmp_path)

    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_min_bundle(bundles_dir=bundles_dir)
    token = "t"

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        started = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {}},
            headers=headers,
        )
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]

        resp = client.post(f"/api/gateway/runs/{run_id}/summary", json={}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert "No provider/model is configured for run summary generation" in resp.text


def test_discovery_providers_reports_default_error_without_hardcoded_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import abstractgateway.provider_defaults as provider_defaults
    from abstractgateway.routes import gateway as gateway_routes

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Discovery reports the resolver's own answer. With an existing, empty
    # store that answer is the error -- and the point of the test is that it is
    # the ERROR and not some hardcoded provider baked into the route.
    _empty_home_core_store(tmp_path)

    class StubDiscoveryFacade:
        def list_providers(self, *, include_models: bool = False, **_kwargs):
            assert include_models is False
            return {"items": []}

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (StubDiscoveryFacade(), None))

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    with client:
        resp = client.get("/api/gateway/discovery/providers", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["default_provider"] is None
    assert body["default_model"] is None
    assert "No provider/model is configured" in body["default_error"]
