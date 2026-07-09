"""ONE mind substrate per entity (maintainer ruling 2026-07-09 06:32).

The choice persists in the home (substrate.yaml), is exposed by GET/PUT
/{name}/substrate, and is resolved identically by visits and the own-time
loop: request override > home file > operator env > loud refusal. Never a
code default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

from abstractgateway.entity_chat import (  # noqa: E402
    ChatOpenRefused,
    read_entity_substrate,
    resolve_substrate,
    write_entity_substrate,
)


def test_resolution_order_request_beats_home_beats_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "castor"
    home.mkdir()
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", raising=False)

    # Nothing anywhere: loud refusal (no code default).
    with pytest.raises(ChatOpenRefused):
        resolve_substrate(None, None, home_dir=home)

    # Operator env answers when the home carries no choice.
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", "env-provider")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", "env-model")
    assert resolve_substrate(None, None, home_dir=home) == ("env-provider", "env-model")

    # The home's persisted choice beats env (one substrate per entity).
    write_entity_substrate(home, provider="endpoint:ovh-provider", model="gpt-oss-120b")
    assert read_entity_substrate(home) == {"provider": "endpoint:ovh-provider", "model": "gpt-oss-120b"}
    assert resolve_substrate(None, None, home_dir=home) == ("endpoint:ovh-provider", "gpt-oss-120b")

    # An explicit request override beats everything (still explicit).
    assert resolve_substrate("lmstudio", "tiny", home_dir=home) == ("lmstudio", "tiny")


def test_partial_choices_never_mix_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A request naming only a provider fills the model from the SAME
    resolution chain — and refuses when no complete pair exists."""
    home = tmp_path / "e"
    home.mkdir()
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", raising=False)
    with pytest.raises(ChatOpenRefused):
        resolve_substrate("lmstudio", None, home_dir=home)
    write_entity_substrate(home, provider="p1", model="m1")
    # Provider-only override + home model: explicit pieces, no silence.
    assert resolve_substrate("lmstudio", None, home_dir=home) == ("lmstudio", "m1")


def test_write_requires_both_fields(tmp_path: Path) -> None:
    home = tmp_path / "e2"
    home.mkdir()
    with pytest.raises(ValueError):
        write_entity_substrate(home, provider="p", model="")
    # A malformed file reads as unset, never a crash.
    (home / "substrate.yaml").write_text("- not-a-mapping\n", encoding="utf-8")
    assert read_entity_substrate(home) == {}
