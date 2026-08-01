"""THE CONTROL PLANE MUST NOT HIDE A BROKEN STORE, AND MUST FLAG A TYPO.

Two failures a new operator meets and cannot diagnose from the console:

  1. A CORRUPT `abstractcore.json` reached the capability-defaults payload as
     `ok: true, errors: [], every route not_configured` -- byte-for-byte what a
     fresh install looks like. The operator is then told to configure a default
     they configured months ago, and the next save overwrites the recoverable
     file. AbstractCore already backs the file up; the control plane has to say
     so.

  2. An unrecognized provider on the TEXT route saved with HTTP 200 and total
     silence; the operator learned about the typo at the first run. We
     ACCEPT-AND-WARN rather than refuse: media routes legitimately name plugin
     backends (`mlx-gen`, `supertonic`) and endpoint profiles can appear after
     this process started, so refusing unknown names would break more than it
     fixes -- but AbstractCore's text registry is closed, so an unknown name
     there is worth reporting at save time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic


def test_a_corrupt_store_is_reported_in_the_payload_not_hidden(tmp_path: Path) -> None:
    from abstractgateway.core_config import _unreadable_store_errors

    path = tmp_path / "abstractcore.json"
    path.write_text('{"capability_defaults": {"routes": {"input.text": {"provider": "lms', encoding="utf-8")
    (tmp_path / "abstractcore.json.corrupt-20260801-000000.bak").write_text("{}", encoding="utf-8")

    errors = _unreadable_store_errors(str(path))
    assert len(errors) == 1
    assert "could not be parsed" in errors[0]
    assert "DEFAULTS, not what you configured" in errors[0]
    assert "abstractcore.json.corrupt-20260801-000000.bak" in errors[0], "name the recoverable copy"
    assert "a save overwrites it" in errors[0]


@pytest.mark.parametrize(
    "content",
    [
        '{"capability_defaults": {"routes": {}}}',
        "{}",
    ],
)
def test_a_healthy_store_reports_nothing(tmp_path: Path, content: str) -> None:
    from abstractgateway.core_config import _unreadable_store_errors

    path = tmp_path / "abstractcore.json"
    path.write_text(content, encoding="utf-8")
    assert _unreadable_store_errors(str(path)) == []


def test_a_store_that_does_not_exist_yet_is_not_an_error(tmp_path: Path) -> None:
    """A fresh install has no file. That is a first run, not a corruption."""
    from abstractgateway.core_config import _unreadable_store_errors

    assert _unreadable_store_errors(str(tmp_path / "never-written.json")) == []
    assert _unreadable_store_errors(None) == []
    assert _unreadable_store_errors("") == []


def test_the_payload_carries_the_error_when_the_store_is_corrupt(tmp_path: Path, monkeypatch) -> None:
    import abstractgateway.core_config as core_config

    path = tmp_path / "abstractcore.json"
    path.write_text("{ truncated", encoding="utf-8")
    monkeypatch.setattr(core_config, "core_server_base_url", lambda: "")
    monkeypatch.setattr(core_config.config_facade, "capability_default_config_file", lambda *a, **k: str(path))
    monkeypatch.setattr(core_config.config_facade, "list_capability_defaults", lambda *a, **k: [])

    payload = core_config.gateway_capability_defaults_payload()
    assert payload["errors"], "a corrupt store must never serve a silent empty grid"
    assert "could not be parsed" in payload["errors"][0]


# --- accept-and-warn on provider names ---------------------------------------


@pytest.mark.parametrize(
    "kind,modality,task,provider,expect_warning",
    [
        ("output", "text", None, "notaprovider", True),
        ("input", "text", None, "notaprovider", True),
        ("output", "text", None, "lmstudio", False),          # a real registry provider
        ("output", "text", None, "endpoint:airelay", False),  # an endpoint profile reference
        ("output", "voice", None, "supertonic", False),       # media plugin backend
        ("output", "image", "text_to_image", "mlx-gen", False),
        ("embedding", "text", None, "whatever", False),       # not the text-generation route
        ("output", "text", None, "", False),                  # omitted field: nothing to judge
    ],
)
def test_only_an_unknown_text_provider_warns(kind, modality, task, provider, expect_warning) -> None:
    from abstractgateway.core_config import text_route_provider_warnings

    warnings = text_route_provider_warnings(kind, modality, task=task, provider=provider)
    assert bool(warnings) is expect_warning, f"{kind}.{modality} / {provider!r}"
    if expect_warning:
        assert "not a known AbstractCore text provider" in warnings[0]
        assert "It was saved" in warnings[0], "the value stays written; this is an advisory"


def test_an_unavailable_registry_degrades_to_silence_not_a_false_alarm(monkeypatch) -> None:
    """Silence is the failure mode of choice: warning about every provider
    because the registry could not be imported would be worse than nothing."""
    import abstractgateway.core_config as core_config

    monkeypatch.setattr(core_config.config_facade, "list_llm_provider_names", lambda: [])
    assert core_config.text_route_provider_warnings("output", "text", provider="notaprovider") == []

    def _boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(core_config.config_facade, "list_llm_provider_names", _boom)
    assert core_config.text_route_provider_warnings("output", "text", provider="notaprovider") == []


def test_the_warning_rides_the_payload_without_failing_the_save() -> None:
    from abstractgateway.routes.gateway import _with_capability_default_warnings

    saved = {"ok": True, "routes": []}
    out = _with_capability_default_warnings(dict(saved), "output", "text", provider="notaprovider")
    assert out["ok"] is True, "an advisory never turns a successful save into a failure"
    assert out["warnings"] and "not a known AbstractCore text provider" in out["warnings"][0]

    clean = _with_capability_default_warnings(dict(saved), "output", "text", provider="lmstudio")
    assert "warnings" not in clean, "no key at all when there is nothing to say"


def test_the_two_entry_points_agree_on_which_provider_names_are_sane(tmp_path: Path) -> None:
    """The AbstractCore CLI and the Gateway PUT write ONE store; if they
    disagreed about which values deserve a warning, that would be a second
    truth in a different costume."""
    from abstractcore.config.main import _text_route_provider_warnings as core_warnings
    from abstractgateway.core_config import text_route_provider_warnings as gateway_warnings

    for route, provider in (
        ("output.text", "notaprovider"),
        ("output.text", "lmstudio"),
        ("output.text", "endpoint:airelay"),
        ("output.voice", "supertonic"),
    ):
        kind, _, modality = route.partition(".")
        assert bool(core_warnings(route, provider)) == bool(
            gateway_warnings(kind, modality, provider=provider)
        ), f"{route} / {provider}"


def test_the_stored_row_is_unchanged_by_an_advisory(tmp_path: Path, monkeypatch) -> None:
    """Accept-and-warn means ACCEPT: the value the operator typed is what the
    store holds afterwards."""
    import abstractgateway.core_config as core_config

    path = tmp_path / "config" / "abstractcore.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"capability_defaults": {"routes": {}}}), encoding="utf-8")
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(path))
    monkeypatch.setattr(core_config, "core_server_base_url", lambda: "")

    core_config.save_gateway_capability_default(
        "output", "text", provider="notaprovider", model="nope-1", base_dir=tmp_path
    )
    stored = json.loads(path.read_text(encoding="utf-8"))["capability_defaults"]["routes"]["input.text"]
    assert stored["provider"] == "notaprovider" and stored["model"] == "nope-1"


# --- the write-path client contract (gateway-TUI bug, sibling 2026-08-01) -----


@pytest.fixture()
def _store(tmp_path: Path, monkeypatch):
    """One AbstractCore config file, reached only through the seam."""
    import abstractgateway.core_config as core_config

    path = tmp_path / "config" / "abstractcore.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"capability_defaults": {"routes": {}}}), encoding="utf-8")
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(path))
    monkeypatch.setattr(core_config, "core_server_base_url", lambda: "")
    return path


def _routes(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))["capability_defaults"]["routes"]


def test_omitting_a_field_keeps_it_and_an_empty_string_clears_it(_store: Path) -> None:
    """THE CLIENT CONTRACT, stated for the clients.

    A gateway-TUI bug (2026-08-01): the client OMITTED base_url when its field
    was blank, meaning "no base URL" -- but omission means "keep what is
    stored", so a previously-set base_url silently came back. Omitted and empty
    are two different instructions and every client has to know which it sent.
    """
    from abstractgateway.core_config import save_gateway_capability_default

    base = _store.parent.parent
    save_gateway_capability_default(
        "output", "text", provider="lmstudio", model="qwen3-0.6b",
        base_url="http://localhost:1234/v1", reasoning="high", base_dir=base,
    )

    # OMITTED: every unnamed field survives.
    save_gateway_capability_default("output", "text", model="qwen3.5-0.8b", base_dir=base)
    row = _routes(_store)["input.text"]
    assert row["base_url"] == "http://localhost:1234/v1", "omission is 'keep', never 'clear'"
    assert row["reasoning"] == "high"
    assert row["model"] == "qwen3.5-0.8b"

    # EMPTY STRING: exactly the named field is cleared, nothing else.
    save_gateway_capability_default("output", "text", base_url="", base_dir=base)
    row = _routes(_store)["input.text"]
    assert not row.get("base_url"), "a blank field a client SENDS must clear the stored value"
    assert row["reasoning"] == "high" and row["model"] == "qwen3.5-0.8b"

    # And a cleared field stays cleared across a later unrelated save.
    save_gateway_capability_default("output", "text", provider="lmstudio", base_dir=base)
    assert not _routes(_store)["input.text"].get("base_url"), "a cleared value must not resurrect"


@pytest.mark.parametrize("field", ["provider", "model", "base_url", "reasoning"])
def test_every_route_field_obeys_the_same_omitted_versus_empty_rule(_store: Path, field: str) -> None:
    from abstractgateway.core_config import save_gateway_capability_default

    base = _store.parent.parent
    seed = {
        "provider": "lmstudio",
        "model": "qwen3-0.6b",
        "base_url": "http://localhost:1234/v1",
        "reasoning": "high",
    }
    save_gateway_capability_default("output", "text", base_dir=base, **seed)

    save_gateway_capability_default("output", "text", base_dir=base)  # names nothing
    assert {k: _routes(_store)["input.text"].get(k) for k in seed} == seed, "a no-op save changes nothing"

    save_gateway_capability_default("output", "text", base_dir=base, **{field: ""})
    row = _routes(_store)["input.text"]
    assert not row.get(field), f"{field}='' must clear {field}"
    for other, value in seed.items():
        if other != field:
            assert row.get(other) == value, f"clearing {field} must not touch {other}"
