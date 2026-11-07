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


def test_no_declared_address_falls_back_to_the_lan_ip(registry: EntityRegistry, monkeypatch: pytest.MonkeyPatch):
    """Laurent's ruling (DM 2026-07-15 20:32) SUPERSEDES the earlier
    never-guess posture for the RENDERING path: entity id =
    <name>@<gateway lan ip> ("ip is the current lan ip of the gateway;
    gateway is their home"). Unset knob => the detected LAN IP renders;
    the declared knob remains the override; loopback is never rendered."""
    monkeypatch.delenv("ABSTRACTGATEWAY_DECLARED_ADDRESS", raising=False)
    import abstractgateway.config as config

    monkeypatch.setattr(config, "detected_lan_ip", lambda: "192.168.1.146")
    registry.create(name="Castor", spark=_spark())
    assert render_handle("castor") == "castor@192.168.1.146"
    assert registry.list_entities()[0]["handle"] == "castor@192.168.1.146"
    assert registry.inspect("Castor")["handle"] == "castor@192.168.1.146"


def test_offline_box_with_no_declared_address_has_no_handle(registry: EntityRegistry, monkeypatch: pytest.MonkeyPatch):
    """No knob AND no detectable non-loopback interface: the handle is
    honestly absent — a fabricated 127.0.0.1 would be a reachability lie."""
    monkeypatch.delenv("ABSTRACTGATEWAY_DECLARED_ADDRESS", raising=False)
    import abstractgateway.config as config

    monkeypatch.setattr(config, "detected_lan_ip", lambda: None)
    registry.create(name="Castor", spark=_spark())
    assert render_handle("castor") is None
    assert registry.list_entities()[0]["handle"] is None


def _address_leaves(value, host: str, port: int, path: str = "$") -> list:
    """Every field of `value` that carries the declared address: a string holding the host (alone or
    in `host:port`, a URL, a handle) or the port as a value of its own ("8080" or 8080). Never a
    substring of the whole document: a timestamp's microseconds (`…38.248080`) or a SHA-256 can
    contain "8080" by coincidence (backlog 1128 item 4)."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += _address_leaves(item, host, port, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            found += _address_leaves(item, host, port, f"{path}[{i}]")
    elif isinstance(value, str):
        if host in value or value.strip() == str(port):
            found.append(path)
    elif isinstance(value, int) and not isinstance(value, bool) and value == port:
        found.append(path)
    return found


def test_the_address_check_finds_an_address_and_ignores_coincidences():
    """The check above goes red on an address written into a manifest, never on a timestamp."""
    clean = {"created_at": "2026-11-04T13:55:38.248080+00:00", "sha256": "ab8080cd" * 8, "name": "Castor"}
    assert _address_leaves(clean, "127.0.0.1", 8080) == []
    assert _address_leaves({"handle": "castor@127.0.0.1:8080"}, "127.0.0.1", 8080) == ["$.handle"]
    assert _address_leaves({"reach": {"url": "http://127.0.0.1:8080/x"}}, "127.0.0.1", 8080) == ["$.reach.url"]
    assert _address_leaves({"net": [{"port": 8080}, {"port": "8080"}]}, "127.0.0.1", 8080) == ["$.net[0].port", "$.net[1].port"]


def test_address_is_never_identity(registry: EntityRegistry, monkeypatch: pytest.MonkeyPatch):
    """Core C1 pin + laurent's consequence (d): nothing AT REST derives
    from the address — creating under one address and reading under
    another changes zero persisted bytes; the handle is not a lookup key;
    entity_id/manifest never contain it."""
    monkeypatch.setenv("ABSTRACTGATEWAY_DECLARED_ADDRESS", "127.0.0.1:8080")
    created = registry.create(name="Castor", spark=_spark())
    assert "127.0.0.1" not in created.entity_id
    assert _address_leaves(created.manifest, "127.0.0.1", 8080) == []

    before = _home_bytes(registry, "castor")
    monkeypatch.setenv("ABSTRACTGATEWAY_DECLARED_ADDRESS", "10.0.0.9:9999")
    listed = registry.list_entities()  # renders the NEW address...
    assert listed[0]["handle"] == "castor@10.0.0.9:9999"
    after = _home_bytes(registry, "castor")
    assert before == after  # ...while zero persisted bytes changed

    # A handle is reachability, not a registry key.
    with pytest.raises((KeyError, ValueError)):
        registry.manifest_for("castor@10.0.0.9:9999")
