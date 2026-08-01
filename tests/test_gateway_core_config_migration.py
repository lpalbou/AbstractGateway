"""THE MIGRATION that retires the second store, and what it may never destroy.

A machine that ran a user-auth Gateway has real configuration in
`<data_dir>/config/abstractcore.json`. Retiring that store cannot mean losing
it, and it cannot mean flattening the AbstractCore store it merges into either:
the legacy file is FULL of untouched framework defaults, and a naive
"legacy wins" would reset every value the operator ever set through
`abstractcore`.

So the rules under test are: what moves, what stays, what a route row does when
the two stores disagree about the provider, and that running it twice is
running it once.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic


@pytest.fixture()
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    data_dir = tmp_path / "runtime"
    (data_dir / "config").mkdir(parents=True, exist_ok=True)
    core = tmp_path / "home" / "abstractcore.json"
    core.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(core))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data_dir))
    return data_dir / "config" / "abstractcore.json", core


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _routes(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["capability_defaults"]["routes"]


def _migrate(**kwargs):
    from abstractgateway.core_config_migration import migrate_legacy_gateway_core_config

    return migrate_legacy_gateway_core_config(**kwargs)


# ------------------------------------------------------------------ the merge


def test_a_route_only_the_gateway_had_moves_across(stores) -> None:
    legacy, core = stores
    _write(legacy, {"capability_defaults": {"routes": {"input.voice": {"provider": "faster-whisper", "model": "large-v3"}}}})
    _write(core, {"capability_defaults": {"routes": {}}})

    report = _migrate()

    assert report["status"] == "migrated"
    assert _routes(core)["input.voice"] == {"provider": "faster-whisper", "model": "large-v3"}


def test_a_route_only_core_had_is_kept(stores) -> None:
    legacy, core = stores
    _write(legacy, {"capability_defaults": {"routes": {}}})
    _write(core, {"capability_defaults": {"routes": {"input.image": {"provider": "lmstudio", "model": "qwen"}}}})

    _migrate()

    assert _routes(core)["input.image"] == {"provider": "lmstudio", "model": "qwen"}


def test_a_different_provider_moves_the_WHOLE_row(stores) -> None:
    """A route row is ONE address. Half of each is an address nobody served.

    Core's `base_url` belonged to the LM Studio row; carrying it onto an
    `endpoint:airelay` row would point the operator's text route at the wrong
    machine -- the exact hazard that makes field-wise merging wrong here.
    """
    legacy, core = stores
    _write(legacy, {"capability_defaults": {"routes": {"input.text": {"provider": "endpoint:airelay", "model": "gpt-5.4"}}}})
    _write(
        core,
        {
            "capability_defaults": {
                "routes": {
                    "input.text": {
                        "provider": "lmstudio",
                        "model": "qwen/qwen3.5-9b",
                        "base_url": "http://localhost:1234/v1",
                    }
                }
            }
        },
    )

    _migrate()

    assert _routes(core)["input.text"] == {"provider": "endpoint:airelay", "model": "gpt-5.4"}


def test_the_same_provider_merges_field_by_field(stores) -> None:
    """Same provider: the fields ARE comparable, so Core's extras survive."""
    legacy, core = stores
    _write(
        legacy,
        {"capability_defaults": {"routes": {"output.voice": {"provider": "supertonic", "model": "supertonic-3", "options": {"voice": "M3"}}}}},
    )
    _write(
        core,
        {"capability_defaults": {"routes": {"output.voice": {"provider": "supertonic", "model": "supertonic-2", "options": {"voice": "M2", "speed": "1.0"}, "reasoning": "low"}}}},
    )

    _migrate()

    row = _routes(core)["output.voice"]
    assert row["model"] == "supertonic-3", "the legacy value wins the fields it states"
    assert row["options"] == {"voice": "M3", "speed": "1.0"}, "and only those fields"
    assert row["reasoning"] == "low", "a field the legacy row never named survives"


def test_a_section_the_gateway_left_at_its_default_never_wins(stores) -> None:
    """The three-way rule, stated where it bites.

    The legacy file carries a FULL default document. Without the baseline every
    one of those defaults would overwrite a value the operator set through
    AbstractCore -- `default_models`, a longer `tool_timeout`, the provider
    profiles -- and the migration would read as data loss.
    """
    legacy, core = stores
    _write(
        legacy,
        {
            "capability_defaults": {"routes": {}},
            "default_models": {"global_provider": None, "global_model": None, "chat_model": None, "code_model": None},
            "timeouts": {"default_timeout": 7200.0, "tool_timeout": 600.0},
            "provider_profiles": {"profiles": {}},
            "audio_strategy_explicit": False,
        },
    )
    _write(
        core,
        {
            "capability_defaults": {"routes": {"input.text": {"provider": "lmstudio", "model": "qwen"}}},
            "default_models": {"global_provider": "lmstudio", "global_model": "qwen3.6", "chat_model": None, "code_model": None},
            "timeouts": {"default_timeout": 7200.0, "tool_timeout": 7200.0},
            "provider_profiles": {"profiles": {"ovh": {"id": "ovh", "base_url": "https://x/v1"}}},
            "audio_strategy_explicit": True,
        },
    )

    _migrate()

    merged = json.loads(core.read_text(encoding="utf-8"))
    assert merged["default_models"]["global_provider"] == "lmstudio"
    assert merged["timeouts"]["tool_timeout"] == 7200.0
    assert merged["provider_profiles"]["profiles"]["ovh"]["base_url"] == "https://x/v1"
    assert merged["audio_strategy_explicit"] is True


def test_a_section_the_gateway_actually_changed_does_win(stores) -> None:
    legacy, core = stores
    _write(legacy, {"capability_defaults": {"routes": {}}, "logging": {"console_level": "DEBUG"}})
    _write(core, {"capability_defaults": {"routes": {}}, "logging": {"console_level": "ERROR"}})

    _migrate()

    assert json.loads(core.read_text(encoding="utf-8"))["logging"]["console_level"] == "DEBUG"


# ---------------------------------------------------------------- the safety


def test_both_files_are_backed_up_and_the_legacy_one_is_renamed(stores) -> None:
    legacy, core = stores
    _write(legacy, {"capability_defaults": {"routes": {"output.sound": {"provider": "stable-audio-3", "model": "sfx"}}}})
    _write(core, {"capability_defaults": {"routes": {}}})
    legacy_bytes = legacy.read_bytes()
    core_bytes = core.read_bytes()

    report = _migrate()

    assert len(report["backups"]) == 2
    contents = [Path(p).read_bytes() for p in report["backups"]]
    assert core_bytes in contents and legacy_bytes in contents
    assert not legacy.exists(), "the legacy store must leave the field entirely"
    renamed = Path(report["renamed_to"])
    assert renamed.exists() and ".migrated-" in renamed.name
    assert renamed.read_bytes() == legacy_bytes


def test_running_it_twice_is_running_it_once(stores) -> None:
    legacy, core = stores
    _write(legacy, {"capability_defaults": {"routes": {"input.voice": {"provider": "faster-whisper", "model": "large-v3"}}}})
    _write(core, {"capability_defaults": {"routes": {}}})

    first = _migrate()
    after_first = core.read_bytes()
    second = _migrate()

    assert first["status"] == "migrated"
    assert second["status"] == "no-legacy-store"
    assert second["changes"] == []
    assert core.read_bytes() == after_first


def test_a_dry_run_touches_nothing(stores) -> None:
    legacy, core = stores
    _write(legacy, {"capability_defaults": {"routes": {"input.voice": {"provider": "faster-whisper", "model": "large-v3"}}}})
    _write(core, {"capability_defaults": {"routes": {}}})
    before = (legacy.read_bytes(), core.read_bytes())

    report = _migrate(dry_run=True)

    assert report["status"] == "dry-run"
    assert report["changes"], "a dry run still has to SAY what it would do"
    assert (legacy.read_bytes(), core.read_bytes()) == before


def test_an_identical_legacy_store_is_retired_without_a_rewrite(stores) -> None:
    legacy, core = stores
    document = {"capability_defaults": {"routes": {"input.voice": {"provider": "faster-whisper", "model": "large-v3"}}}}
    _write(legacy, document)
    _write(core, document)
    before = core.read_bytes()

    report = _migrate()

    assert report["status"] == "retired-identical"
    assert core.read_bytes() == before
    assert not legacy.exists()


def test_an_unreadable_legacy_store_is_refused_loudly(stores) -> None:
    legacy, core = stores
    legacy.write_text("{ truncated", encoding="utf-8")
    _write(core, {"capability_defaults": {"routes": {}}})

    report = _migrate()

    assert report["ok"] is False
    assert report["status"] == "error"
    assert legacy.exists(), "a store that could not be read must not be renamed away"
    assert any("not valid JSON" in error for error in report["errors"])


def test_a_split_server_deployment_is_left_alone(stores, monkeypatch: pytest.MonkeyPatch) -> None:
    """The store lives on another machine; nothing here may be merged or moved."""
    legacy, core = stores
    _write(legacy, {"capability_defaults": {"routes": {"input.voice": {"provider": "faster-whisper", "model": "large-v3"}}}})
    monkeypatch.setenv("ABSTRACTCORE_SERVER_BASE_URL", "http://core.example")

    report = _migrate()

    assert report["status"] == "skipped-split-server"
    assert legacy.exists()
    assert not core.exists()


def test_no_legacy_store_is_a_silent_no_op(stores) -> None:
    from abstractgateway.core_config_migration import format_migration_report

    report = _migrate()

    assert report["status"] == "no-legacy-store"
    assert format_migration_report(report) == []


# ------------------------------------------------- profiles, the same journey


def test_legacy_endpoint_profiles_move_into_core(stores) -> None:
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway.core_config_migration import migrate_legacy_gateway_provider_profiles

    legacy, core = stores
    profiles = legacy.with_name("provider_endpoint_profiles.json")
    _write(
        profiles,
        {
            "version": 1,
            "profiles": [
                {
                    "id": "airelay",
                    "display_name": "airelay",
                    "provider_family": "openai-compatible",
                    "base_url": "http://127.0.0.1:8317/v1",
                    "api_key": "airelay-key",
                    "scope": "gateway",
                    "capabilities": ["text"],
                    "enabled": True,
                }
            ],
        },
    )

    report = migrate_legacy_gateway_provider_profiles()

    assert report["status"] == "migrated"
    assert not profiles.exists()
    assert ".migrated-" in Path(report["renamed_to"]).name
    resolved = ConfigurationManager().resolve_provider_profile("endpoint:airelay")
    assert resolved is not None and resolved.api_key == "airelay-key"
    assert resolved.scope == "gateway"

    again = migrate_legacy_gateway_provider_profiles()
    assert again["status"] == "no-legacy-store"
