"""API-key precedence: config supersedes env, env inherited by default.

Operator ruling (laurent dm#201, 2026-07-21): cloud API keys (openai,
anthropic, openrouter, portkey, ...) are the RULED exception to behavior-var
elimination — they inherit from env by default (they exist for other apps
too), no migration forced. BUT a config-set key ALWAYS supersedes the env
key. The gateway's resolver already reads config-first; these pins hold that
invariant so a future edit can't silently reintroduce env-always-wins (the
class core had to invert).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic


def _write_core_config_key(base_dir: Path, attr: str, value: str) -> None:
    """Set a provider key in THE AbstractCore store.

    ONE STORE (operator ruling 2026-08-01): API keys are Core-owned, so "the
    config" a key is set in is AbstractCore's config file -- the same one
    `abstractcore --set-api-key` writes -- and the Gateway reads it in every
    mode. `base_dir` is retained for callers that still name a scope; the
    Gateway data dir is no longer a base of its own.
    """
    from abstractruntime.integrations.abstractcore import config_facade

    _ = base_dir
    cfg = Path(os.environ["ABSTRACTCORE_CONFIG_FILE"])
    cfg.parent.mkdir(parents=True, exist_ok=True)
    # Use the facade's own writer path if present; else a minimal JSON the
    # reader understands (read_config_api_key reads api_keys.<attr>).
    import json

    existing = {}
    if cfg.exists():
        try:
            existing = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing.setdefault("api_keys", {})[attr] = value
    cfg.write_text(json.dumps(existing), encoding="utf-8")
    # Sanity: the facade must actually read it back (guards a schema drift).
    assert config_facade.read_config_api_key(cfg, attr) == value


def test_config_key_supersedes_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.provider_connections import configured_provider_api_key

    monkeypatch.setenv("OPENAI_API_KEY", "env-key-value")
    _write_core_config_key(tmp_path, "openai", "config-key-value")

    key, source = configured_provider_api_key(
        "openai", current_base_dir=tmp_path, root_base_dir=tmp_path
    )
    assert key == "config-key-value", "a config-set key must supersede the env key (dm#201)"
    assert source != "environment"


def test_env_key_inherited_when_no_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.provider_connections import configured_provider_api_key

    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-anthropic")
    key, source = configured_provider_api_key(
        "anthropic", current_base_dir=tmp_path, root_base_dir=tmp_path
    )
    assert key == "env-anthropic", "keys inherit from env by default (dm#201 — no forced migration)"
    assert source == "environment"


def test_include_env_false_ignores_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The strict lane (include_env=False) is unchanged: config-only, env
    never consulted — used where a deployment wants no ambient key leakage."""
    from abstractgateway.provider_connections import configured_provider_api_key

    monkeypatch.setenv("OPENROUTER_API_KEY", "env-openrouter")
    key, source = configured_provider_api_key(
        "openrouter", current_base_dir=tmp_path, root_base_dir=tmp_path, include_env=False
    )
    assert key == ""
    assert source == ""
