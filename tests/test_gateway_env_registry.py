"""Env-registry phase 0 (env-kill lead, dm#177/c4174): the enforced inventory.

The design adversary's risk 4: a behavior env with no registry entry is
invisible to the console, the scanner, and the migration ratchet. This pin
greps every env read in src/ and fails on any name the registry does not
classify — the inventory stops being a one-time audit and becomes an
enforced invariant. New env reads must land WITH their classification row.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

_SRC = Path(__file__).resolve().parents[1] / "src" / "abstractgateway"


def _env_names_read_in_src() -> set[str]:
    """Every UPPER_SNAKE name passed to an env-reading call in src/.

    Two passes: direct single-arg reads (os.getenv/environ.get/_env with a
    literal first arg), then multi-key chains (_env_first/_env_bool with
    several literals). String literals elsewhere are NOT env reads — only
    call sites count, which keeps the pin precise.
    """
    names: set[str] = set()
    # Pass 1: reader( "NAME" — the literal immediately after the call open.
    out = subprocess.run(
        ["rg", "-o", r"(?:os\.getenv|os\.environ\.get|os\.environ\.pop|_env_first|_env_bool|_env_float|_env)\(\s*[\"']([A-Z][A-Z0-9_]{3,})[\"']",
         str(_SRC), "-r", "$1", "--no-filename"],
        capture_output=True, text=True,
    ).stdout
    names.update(n for n in out.split() if n)
    # Pass 2: the remaining keys of multi-key chains — capture full call args.
    out2 = subprocess.run(
        ["rg", "-o", r"(?:_env_first|_env_bool)\(([^)]*)\)", str(_SRC), "-r", "$1", "--no-filename", "-U"],
        capture_output=True, text=True,
    ).stdout
    for line in out2.splitlines():
        names.update(re.findall(r"[\"']([A-Z][A-Z0-9_]{3,})[\"']", line))
    return names


def test_every_env_read_is_declared_in_the_registry() -> None:
    from abstractgateway.env_registry import classify_env_var

    read = _env_names_read_in_src()
    assert read, "the grep found no env reads — the pin's extraction broke"
    undeclared = sorted(n for n in read if classify_env_var(n) is None)
    assert not undeclared, (
        "env reads with NO registry classification (add a row to "
        f"src/abstractgateway/env_registry.py with class/scope/console_path): {undeclared}"
    )


def test_registry_classes_are_coherent() -> None:
    from abstractgateway.env_registry import (
        BEHAVIOR,
        LEGACY_ALIAS,
        SECRET,
        classify_env_var,
    )

    # Behavior rows must name a console destination (the migration target).
    read = _env_names_read_in_src()
    missing_console = sorted(
        n for n in read
        if (spec := classify_env_var(n)) is not None
        and spec.klass == BEHAVIOR
        and not spec.console_path
    )
    assert not missing_console, f"BEHAVIOR rows without a console_path: {missing_console}"

    # Alias rows must name what they alias.
    bad_alias = sorted(
        n for n in read
        if (spec := classify_env_var(n)) is not None
        and spec.klass == LEGACY_ALIAS
        and not spec.alias_of
    )
    assert not bad_alias, f"LEGACY_ALIAS rows without alias_of: {bad_alias}"

    # The ruled key family stays SECRET (dm#201) — a reclassification of an
    # API key to behavior would force the migration laurent explicitly
    # exempted.
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "PORTKEY_API_KEY"):
        spec = classify_env_var(key)
        assert spec is not None and spec.klass == SECRET, f"{key} must stay a dm#201 secret row"


def test_known_rulings_are_encoded() -> None:
    """Spot pins for the operator/adversary rulings the classification came
    from — a silent table edit that flips one of these is a contract break."""
    from abstractgateway.env_registry import BEHAVIOR, DEPLOYMENT, FOREIGN, classify_env_var

    assert classify_env_var("ABSTRACTGATEWAY_TOOL_MODE").klass == BEHAVIOR  # security posture → console
    assert classify_env_var("ABSTRACTGATEWAY_RUNNER").klass == DEPLOYMENT  # process-role test
    assert classify_env_var("ABSTRACTGATEWAY_USER_AUTH").klass == DEPLOYMENT  # circular (P0-4)
    assert classify_env_var("ABSTRACTCORE_SERVER_BASE_URL").klass == DEPLOYMENT  # config authority address
    assert classify_env_var("ABSTRACTVOICE_TTS_ENGINE").klass == FOREIGN  # core+voice own execution
    assert classify_env_var("ABSTRACTGATEWAY_GRACEFUL_SHUTDOWN_S").klass == DEPLOYMENT  # supervisor pair
    assert classify_env_var("ABSTRACTGATEWAY_EAGER_REHYDRATE_MAX").klass == BEHAVIOR  # adversary hardest-ten
    assert classify_env_var("ABSTRACT_TELEGRAM_MODEL").klass == BEHAVIOR  # bridge policy
    assert classify_env_var("ABSTRACT_TELEGRAM_BOT_TOKEN").klass == "secret"
