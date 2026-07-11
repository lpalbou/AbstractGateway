"""Declared address + handle rendering (plan item 5, GW-F — phase 2).

Laurent's consequences (c) and (d): handle = `<name>@<address>` with ONE
operator-declared address per door; address changes cost nothing engraved.
These tests pin both halves: the handle renders from the knob everywhere
the operator reads identity surfaces (list / inspect / card), and the
address is NEVER identity — nothing at rest (manifest, entity_id, marker
stream) contains it, a handle is not a lookup key, and flipping the knob
changes zero bytes on disk (localhost -> VPS = one config edit).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("yaml")

from abstractmemory import DEFAULT_SPARK_TEMPLATE  # noqa: E402

from abstractgateway.entities import EntityRegistry, render_handle  # noqa: E402


def _spark(name: str = "Castor") -> dict:
    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


@pytest.fixture()
def registry(tmp_path: Path) -> EntityRegistry:
    return EntityRegistry(data_dir=tmp_path / "runtime", embedder_factory=lambda: None)


def _home_bytes(registry: EntityRegistry, slug: str) -> bytes:
    """Every byte the door persists for one entity (home dir + host stream)."""
    chunks = []
    for root in (registry.entities_dir / slug, registry.entities_dir / ".host_stream"):
        if not root.exists():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            chunks.append(path.read_bytes())
    return b"".join(chunks)


def test_handle_renders_from_the_declared_address(registry: EntityRegistry, monkeypatch: pytest.MonkeyPatch):
    registry.create(name="Castor", spark=_spark())

    monkeypatch.setenv("ABSTRACTGATEWAY_DECLARED_ADDRESS", "127.0.0.1:8080")
    assert render_handle("castor") == "castor@127.0.0.1:8080"
    listed = registry.list_entities()
    assert listed[0]["handle"] == "castor@127.0.0.1:8080"
    assert registry.inspect("Castor")["handle"] == "castor@127.0.0.1:8080"

    # One config edit moves the door; the same surfaces follow instantly.
    monkeypatch.setenv("ABSTRACTGATEWAY_DECLARED_ADDRESS", "entities.example.org")
    assert registry.list_entities()[0]["handle"] == "castor@entities.example.org"


def test_no_declared_address_means_no_handle(registry: EntityRegistry, monkeypatch: pytest.MonkeyPatch):
    """The door never guesses its own address (it binds 0.0.0.0; clients
    dial many routes) — unset knob = handle absent, never a fabricated
    localhost default."""
    monkeypatch.delenv("ABSTRACTGATEWAY_DECLARED_ADDRESS", raising=False)
    registry.create(name="Castor", spark=_spark())
    assert render_handle("castor") is None
    assert registry.list_entities()[0]["handle"] is None
    assert registry.inspect("Castor")["handle"] is None


def test_address_is_never_identity(registry: EntityRegistry, monkeypatch: pytest.MonkeyPatch):
    """Core C1 pin + laurent's consequence (d): nothing AT REST derives
    from the address — creating under one address and reading under
    another changes zero persisted bytes; the handle is not a lookup key;
    entity_id/manifest never contain it."""
    monkeypatch.setenv("ABSTRACTGATEWAY_DECLARED_ADDRESS", "127.0.0.1:8080")
    created = registry.create(name="Castor", spark=_spark())
    assert "127.0.0.1" not in created.entity_id
    assert "8080" not in json.dumps(created.manifest)

    before = _home_bytes(registry, "castor")
    monkeypatch.setenv("ABSTRACTGATEWAY_DECLARED_ADDRESS", "10.0.0.9:9999")
    listed = registry.list_entities()  # renders the NEW address...
    assert listed[0]["handle"] == "castor@10.0.0.9:9999"
    after = _home_bytes(registry, "castor")
    assert before == after  # ...while zero persisted bytes changed

    # A handle is reachability, not a registry key.
    with pytest.raises((KeyError, ValueError)):
        registry.manifest_for("castor@10.0.0.9:9999")
