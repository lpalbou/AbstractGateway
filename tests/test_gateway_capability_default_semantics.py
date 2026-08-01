"""The console default is READ by name, and APPLIED to the live runtime.

Two separate defects, both operator-visible, both fixed here:

1. The text-generation default was found by iterating
   `for kind in ("output", "input")` over the route rows and taking the first
   text row. Correct only by iteration order, and it named no key, so nobody
   reading the code (or the config) could say what the default WAS. It now
   reads the canonical key `output.text` by name, with the legacy storage key
   `input.text` as a migration fallback, and reports WHICH key answered.

2. The default provider/model was resolved once at bundle load and baked into
   the pooled LLM client. Changing it in the console therefore did nothing
   until the process restarted -- reproduced live 2026-07-31: after a
   console-path PUT of the text default, the very next unpinned run still used
   the previous provider/model. The write path now refreshes the live runtime.

The cascade contract these pin (see `provider_defaults` module docstring):
explicit request pins > flow defaults > gateway console default >
flow-scanned bootstrap. A default never clobbers a pin, and applies whenever
nothing overrides.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ABSTRACTCORE_SERVER_BASE_URL",
        "ABSTRACTGATEWAY_DATA_DIR",
        "ABSTRACTGATEWAY_USER_AUTH",
        "ABSTRACTGATEWAY_LLM_PROVIDER",
        "ABSTRACTGATEWAY_LLM_MODEL",
        "ABSTRACTCORE_DEFAULT_PROVIDER",
        "ABSTRACTCORE_DEFAULT_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def _payload(routes: list[Dict[str, Any]], *, authority: str = "abstractcore.gateway_runtime") -> Dict[str, Any]:
    return {"ok": True, "authority": authority, "writable": True, "routes": routes, "errors": []}


def test_canonical_output_text_route_is_the_primary_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import provider_defaults
    import abstractgateway.core_config as capability_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(
        capability_defaults,
        "gateway_capability_defaults_payload",
        lambda **_kw: _payload(
            [
                {"key": "input.text", "kind": "input", "modality": "text", "provider": "ollama", "model": "legacy"},
                {"key": "output.text", "kind": "output", "modality": "text", "provider": "lmstudio", "model": "canonical"},
            ]
        ),
    )

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="test")
    assert (resolved.provider, resolved.model) == ("lmstudio", "canonical")
    assert resolved.source == "abstractcore.gateway_runtime:output.text"


def test_legacy_input_text_only_config_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Migration tolerance: a config carrying ONLY the legacy storage key works."""
    from abstractgateway import provider_defaults
    import abstractgateway.core_config as capability_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(
        capability_defaults,
        "gateway_capability_defaults_payload",
        lambda **_kw: _payload(
            [{"key": "input.text", "kind": "input", "modality": "text", "provider": "ollama", "model": "legacy-model"}]
        ),
    )

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="test")
    assert (resolved.provider, resolved.model) == ("ollama", "legacy-model")
    # The source names the LEGACY key, so an unmigrated host is visible.
    assert resolved.source == "abstractcore.gateway_runtime:input.text"


def test_rows_without_an_explicit_key_are_still_matched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route rows may omit `key`; kind+modality must still resolve the route."""
    from abstractgateway import provider_defaults
    import abstractgateway.core_config as capability_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(
        capability_defaults,
        "gateway_capability_defaults_payload",
        lambda **_kw: _payload(
            [{"kind": "output", "modality": "text", "task": "text_generation", "provider": "lmstudio", "model": "m1"}]
        ),
    )

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="test")
    assert (resolved.provider, resolved.model) == ("lmstudio", "m1")


def test_non_text_routes_never_answer_the_text_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old kind-scan could only ever look at text rows by accident of the
    filter order; naming the key makes it structural."""
    from abstractgateway import provider_defaults
    import abstractgateway.core_config as capability_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(
        capability_defaults,
        "gateway_capability_defaults_payload",
        lambda **_kw: _payload(
            [
                {"key": "embedding.text", "kind": "embedding", "modality": "text", "provider": "hf", "model": "minilm"},
                {"key": "output.voice", "kind": "output", "modality": "voice", "provider": "supertonic", "model": "s3"},
                {"key": "input.voice", "kind": "input", "modality": "voice", "provider": "whisper", "model": "large"},
            ]
        ),
    )

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="test")
    assert resolved.provider is None and resolved.model is None


def test_request_pin_is_never_clobbered_by_the_console_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cascade tier 1 beats tier 3: an app override always wins."""
    from abstractgateway import provider_defaults
    import abstractgateway.core_config as capability_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(
        capability_defaults,
        "gateway_capability_defaults_payload",
        lambda **_kw: _payload(
            [{"key": "output.text", "kind": "output", "modality": "text", "provider": "lmstudio", "model": "console"}]
        ),
    )

    pinned = provider_defaults.resolve_gateway_provider_model(
        provider="openai", model="app-chosen", flow_defaults=("ollama", "scanned"), purpose="test"
    )
    assert (pinned.provider, pinned.model, pinned.source) == ("openai", "app-chosen", "request")


def test_console_default_outranks_the_flow_scanned_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cascade tier 3 beats tier 4: a scanned guess must not hijack Auto nodes."""
    from abstractgateway import provider_defaults
    import abstractgateway.core_config as capability_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(
        capability_defaults,
        "gateway_capability_defaults_payload",
        lambda **_kw: _payload(
            [{"key": "output.text", "kind": "output", "modality": "text", "provider": "lmstudio", "model": "console"}]
        ),
    )

    resolved = provider_defaults.resolve_gateway_provider_model(
        flow_defaults=("ollama", "scanned-from-a-stale-bundle"), purpose="test"
    )
    assert (resolved.provider, resolved.model) == ("lmstudio", "console")

    # ... but with NO console default, the bootstrap still saves a fresh install.
    monkeypatch.setattr(capability_defaults, "gateway_capability_defaults_payload", lambda **_kw: _payload([]))
    bootstrap = provider_defaults.resolve_gateway_provider_model(
        flow_defaults=("ollama", "scanned-from-a-stale-bundle"), purpose="test"
    )
    assert (bootstrap.provider, bootstrap.model, bootstrap.source) == (
        "ollama",
        "scanned-from-a-stale-bundle",
        "flow_defaults",
    )


class _FakePool:
    """Stands in for MultiLocalAbstractCoreLLMClient's default-identity half."""

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        self.llm_kwargs: Dict[str, Any] = {}
        self.capability_defaults: Any = None
        self.calls = 0

    def set_default_provider_model(
        self,
        *,
        provider: Optional[str],
        model: Optional[str],
        llm_kwargs: Optional[Dict[str, Any]] = None,
        capability_defaults: Any = None,
    ) -> bool:
        self.calls += 1
        changed = provider != self.provider or model != self.model
        self.provider = str(provider or "")
        self.model = str(model or "")
        if llm_kwargs is not None:
            self.llm_kwargs = dict(llm_kwargs)
        self.capability_defaults = capability_defaults
        return changed


class _FakeRuntime:
    """Carries BOTH truths a host must keep in step: the pooled LLM client
    (what plain llm_call nodes use) and RuntimeConfig (what `start()` seeds
    into `_runtime.provider|model`, which Auto Agent nodes read)."""

    def __init__(self, pool: _FakePool) -> None:
        self._abstractcore_llm_client = pool
        self.config_provider: Optional[str] = "stale-provider"
        self.config_model: Optional[str] = "stale-model"

    def set_default_provider_model(
        self,
        *,
        provider: Optional[str],
        model: Optional[str],
        model_capabilities: Optional[Dict[str, Any]] = None,
    ) -> bool:
        del model_capabilities
        changed = provider != self.config_provider or model != self.config_model
        self.config_provider = provider
        self.config_model = model
        return changed


def test_host_refresh_repoints_the_live_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The console write path must reach the RUNNING runtime, not just the file."""
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway.hosts import bundle_host as bundle_host_mod

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    manager = ConfigurationManager()
    assert manager.set_capability_default("output.text", provider="lmstudio", model="first-choice")

    pool = _FakePool("stale-provider", "stale-model")
    runtime = _FakeRuntime(pool)
    host = object.__new__(bundle_host_mod.WorkflowBundleGatewayHost)
    import threading

    host._lock = threading.RLock()
    host.runtime = runtime
    host.data_dir = tmp_path / "runtime"
    host.catalog_root_data_dir = None
    host._flow_scanned_llm_defaults = None

    out = host.refresh_capability_defaults()
    assert out["ok"] is True and out["changed"] is True
    assert (pool.provider, pool.model) == ("lmstudio", "first-choice")
    # BOTH truths move together: llm_call nodes (client) and Auto Agent nodes
    # (RuntimeConfig -> _runtime.provider|model) must not disagree.
    assert (runtime.config_provider, runtime.config_model) == ("lmstudio", "first-choice")
    assert out["client_changed"] is True and out["config_changed"] is True

    # The operator changes their mind in the console: the live host follows.
    assert manager.set_capability_default("output.text", provider="ollama", model="second-choice")
    out2 = host.refresh_capability_defaults()
    assert out2["changed"] is True
    assert (pool.provider, pool.model) == ("ollama", "second-choice")
    assert (runtime.config_provider, runtime.config_model) == ("ollama", "second-choice")

    # A refresh with nothing changed is a no-op, not a client rebuild.
    out3 = host.refresh_capability_defaults()
    assert out3["changed"] is False
    assert (pool.provider, pool.model) == ("ollama", "second-choice")


def test_capability_defaults_write_route_reports_the_runtime_refresh() -> None:
    """The PUT/DELETE responses carry `runtime_refresh` so a console (or a
    script) can SEE that the live host took the new default."""
    from abstractgateway.routes import gateway as gateway_routes

    class _Cfg:
        data_dir = "/tmp/does-not-exist-capability-defaults-test"

    class _Host:
        def refresh_capability_defaults(self) -> Dict[str, Any]:
            return {"ok": True, "changed": True, "provider": "lmstudio", "model": "m"}

    class _Svc:
        config = _Cfg()
        host = _Host()

    out = gateway_routes._apply_capability_defaults_to_live_runtime(_Svc())
    assert out["runtime_refresh"] == {"ok": True, "changed": True, "provider": "lmstudio", "model": "m"}
    assert "routes" in out


def test_capability_defaults_write_route_survives_a_refresh_failure() -> None:
    """A refresh failure must never fail a save that already landed on disk."""
    from abstractgateway.routes import gateway as gateway_routes

    class _Cfg:
        data_dir = "/tmp/does-not-exist-capability-defaults-test"

    class _Host:
        def refresh_capability_defaults(self) -> Dict[str, Any]:
            raise RuntimeError("runtime is mid-reload")

    class _Svc:
        config = _Cfg()
        host = _Host()

    out = gateway_routes._apply_capability_defaults_to_live_runtime(_Svc())
    assert out["runtime_refresh"]["ok"] is False
    assert "mid-reload" in out["runtime_refresh"]["error"]


def test_route_key_helper_ignores_descriptive_task_field() -> None:
    from abstractgateway.core_config import _row_key as _route_key

    assert _route_key({"kind": "output", "modality": "text", "task": "text_generation"}) == "output.text"
    assert _route_key({"key": "output.image.image_upscale"}) == "output.image.image_upscale"
    assert _route_key({"modality": "text"}) == ""
    assert _route_key(json.loads('{"key": "  OUTPUT.TEXT "}')) == "output.text"
