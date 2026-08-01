"""ONE STORE: the Gateway seam and AbstractCore resolve the SAME file.

The operator's ruling of 2026-08-01: AbstractCore holds the configuration for
provider, model, reasoning and routes, the Gateway is a layer on top, and a
value changed from a Gateway console changes it AT THE SOURCE. The failure this
file exists to prevent is the one the operator photographed: a Gateway serving
`endpoint:airelay/gpt-5.4` while `abstractcore config defaults` on the same
machine answered `lmstudio/qwen3.5-9b`, because the Gateway kept a base of its
own under its data dir.

So the pin is a PATH pin, in both directions and in both auth modes, plus the
retirement of the second store and the migration that empties it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic


def _core_manager_path(**kwargs) -> Path:
    """Where AbstractCore itself resolves its store. The other side of the pin."""
    from abstractcore.config.manager import ConfigurationManager

    return Path(ConfigurationManager(**kwargs).config_file)


def _seam_path() -> Path:
    from abstractgateway import core_config

    return Path(core_config.core_config_file())


# ------------------------------------------------------------------ the path


def test_the_seam_and_abstractcore_resolve_the_same_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default mode: no override, no data dir involved."""
    monkeypatch.delenv("ABSTRACTCORE_CONFIG_FILE", raising=False)
    monkeypatch.delenv("ABSTRACTCORE_CONFIG_DIR", raising=False)

    assert _seam_path() == _core_manager_path()
    assert _seam_path().name == "abstractcore.json"


def test_the_same_file_under_user_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """USER AUTH DOES NOT MOVE THE BASE. It only adds an overlay above it.

    This is the regression itself: user auth used to switch the Gateway onto
    `<data_dir>/config/abstractcore.json`, and from then on the two entry points
    described different machines.
    """
    from abstractgateway import core_config

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    assert _seam_path() == _core_manager_path()
    assert core_config._scoped_core_config_path(tmp_path / "runtime") is None, (
        "the gateway ROOT is not a scope: an admin write goes to the source"
    )
    payload = core_config.gateway_capability_defaults_payload(base_dir=tmp_path / "runtime")
    assert Path(payload["config_file"]) == _core_manager_path()
    assert "gateway_config_file" not in payload, "the retired gateway base must not be advertised"


def test_an_abstractcore_env_override_moves_the_seam_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`ABSTRACTCORE_CONFIG_FILE` means the same thing on both sides."""
    target = tmp_path / "elsewhere" / "abstractcore.json"
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(target))

    assert _seam_path() == target
    assert _core_manager_path() == target


def test_a_per_user_overlay_is_still_a_separate_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The overlay contract survives: a USER runtime keeps a file of its own."""
    from abstractgateway import core_config

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    alice = tmp_path / "runtime" / "users" / "default" / "alice" / "runtime"
    overlay = core_config._scoped_core_config_path(alice)
    assert overlay == alice.resolve() / "config" / "abstractcore.json"
    assert overlay != _core_manager_path()


def test_no_overlay_without_user_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import core_config

    monkeypatch.delenv("ABSTRACTGATEWAY_USER_AUTH", raising=False)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))

    assert core_config._scoped_core_config_path(tmp_path / "somewhere") is None


# ------------------------------------------------------------- both directions


def test_a_gateway_write_is_visible_to_abstractcore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(a) The Gateway console writes; the AbstractCore entry point reads it."""
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway.core_config import save_gateway_capability_default

    store = tmp_path / "abstractcore.json"
    store.write_text(json.dumps({"capability_defaults": {"routes": {}}}), encoding="utf-8")
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(store))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    save_gateway_capability_default("output", "text", provider="endpoint:airelay", model="gpt-5.4")

    rows = {row["key"]: row for row in ConfigurationManager().list_capability_defaults()}
    assert (rows["input.text"]["provider"], rows["input.text"]["model"]) == ("endpoint:airelay", "gpt-5.4")


def test_an_abstractcore_write_is_visible_to_the_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """(b) `abstractcore config set-default` writes; the Gateway grid serves it."""
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway.core_config import capability_default_rows

    store = tmp_path / "abstractcore.json"
    store.write_text(json.dumps({"capability_defaults": {"routes": {}}}), encoding="utf-8")
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(store))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    assert ConfigurationManager().set_capability_default(
        "output", "voice", provider="supertonic", model="supertonic-3", options={"voice": "M3"}
    )

    row = capability_default_rows().get("output.voice", {})
    assert (row.get("provider"), row.get("model")) == ("supertonic", "supertonic-3")
    assert row.get("options") == {"voice": "M3"}


def test_the_signature_watches_the_core_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The freshness fingerprint must stat the file the CLI writes."""
    from abstractgateway.core_config import capability_defaults_config_signature

    store = tmp_path / "abstractcore.json"
    store.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(store))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    watched = {entry[0] for entry in capability_defaults_config_signature(base_dir=tmp_path / "runtime")}
    assert str(store) in watched
    assert str((tmp_path / "runtime" / "config" / "abstractcore.json").resolve()) not in watched


# --------------------------------------------------------- the seed contract


def test_a_fresh_install_has_no_file_at_the_core_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE SEED IS PER INSTALL. Reading a fresh store must not create a second one."""
    from abstractgateway.core_config import gateway_capability_defaults_payload

    store = tmp_path / "fresh" / "abstractcore.json"
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(store))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    payload = gateway_capability_defaults_payload(base_dir=tmp_path / "runtime")

    assert payload.get("seeded") == "recommended-v1"
    assert Path(payload["config_file"]) == store
    assert not (tmp_path / "runtime" / "config" / "abstractcore.json").exists(), (
        "a fresh read must never materialize the retired gateway store"
    )


# --------------------------------------------------------- provider profiles


def test_a_gateway_endpoint_profile_lands_in_core_provider_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A profile is provider config, so BOTH consoles see the one row."""
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway.provider_endpoint_profiles import ProviderEndpointProfileStore

    store_path = tmp_path / "abstractcore.json"
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(store_path))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))

    store = ProviderEndpointProfileStore(base_dir=tmp_path / "runtime")
    assert store.core_backed is True
    store.upsert_profile(
        profile_id="airelay",
        display_name="airelay",
        base_url="http://127.0.0.1:8317/v1",
        api_key="secret-key",
        scope="gateway",
    )

    assert not store.path.exists(), "the gateway root must keep no profile file of its own"
    resolved = ConfigurationManager().resolve_provider_profile("endpoint:airelay")
    assert resolved is not None
    assert resolved.base_url == "http://127.0.0.1:8317/v1"
    assert resolved.api_key == "secret-key"
    assert resolved.scope == "gateway"

    # ...and the Gateway reads back what Core holds.
    back = store.get_profile("airelay")
    assert back is not None and back.base_url == "http://127.0.0.1:8317/v1"

    assert store.delete_profile("airelay") is True
    assert ConfigurationManager().resolve_provider_profile("endpoint:airelay") is None


def test_a_core_created_profile_is_visible_to_the_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction: `abstractcore` creates it, the Gateway resolves it."""
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway.provider_endpoint_profiles import resolve_effective_endpoint_profile

    store_path = tmp_path / "abstractcore.json"
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(store_path))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))

    ConfigurationManager().set_provider_profile(
        "ovh-provider", base_url="https://oai.example/v1", api_key="ovh-key", provider_family="openai-compatible"
    )

    profile = resolve_effective_endpoint_profile(
        "endpoint:ovh-provider", base_dir=tmp_path / "runtime", root_base_dir=tmp_path / "runtime"
    )
    assert profile is not None
    assert profile.base_url == "https://oai.example/v1"
    assert profile.api_key == "ovh-key"


def test_a_per_user_profile_store_still_keeps_its_own_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway-only scoping survives where it belongs: a per-USER overlay."""
    from abstractgateway.provider_endpoint_profiles import ProviderEndpointProfileStore

    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(tmp_path / "abstractcore.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))

    alice = tmp_path / "runtime" / "users" / "default" / "alice" / "runtime"
    store = ProviderEndpointProfileStore(base_dir=alice)
    assert store.core_backed is False
    store.upsert_profile(profile_id="mine", display_name="mine", base_url="http://127.0.0.1:9/v1", scope="user")

    assert store.path.exists()
    assert not (tmp_path / "abstractcore.json").exists(), "a user's private profile must not reach the Core store"
