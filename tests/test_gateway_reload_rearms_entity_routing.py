"""Regression pin: a bundle reload must not disarm entity routing.

Live defect (2026-07-24, flow c5209/c5227): `reload_bundles_from_disk`
builds a NEW host via `load_from_dir` and swaps `self.runtime` in place —
a brand-new Runtime carrying only load-time handlers. `install_entity_routing`
runs once in the service factory, so ANY catalog publish/reload replaced the
armed runtime with an unarmed one and every subsequent entity run failed
"No effect handler registered for memory_recall". A process bounce did not
help because acceptance flows publish their bundle (triggering a reload)
before summoning.

The fix: the factory registers a runtime-rebuild hook on the host; the
reload re-arms the NEW runtime BEFORE the swap publishes it (no tick can
observe an unarmed runtime).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abstractruntime.core.models import EffectType
from abstractruntime.integrations.abstractmemory import ENTITY_HOME_EFFECT_TYPES

# ONE SOURCE (flow c5237): the pin covers the FULL entity-home set the
# runtime binds in-process — originally seven (seam + diary), grown to
# eleven by the brain wave (consolidate/probe/life_query/tend). Importing
# the set means the NEXT effect type extends this pin automatically instead
# of re-opening the lane gap.
ENTITY_EFFECTS = tuple(sorted(ENTITY_HOME_EFFECT_TYPES, key=lambda e: str(e.value)))


def test_entity_effect_set_covers_the_brain_quartet() -> None:
    """The live gap (c5237): the door routed 7 while the brain is 11."""
    for etype in (
        EffectType.MEMORY_CONSOLIDATE,
        EffectType.MEMORY_PROBE,
        EffectType.LIFE_QUERY,
        EffectType.MEMORY_TEND,
    ):
        assert etype in ENTITY_HOME_EFFECT_TYPES, f"{etype.value} missing from the one-source set"


def _handlers(runtime) -> dict:
    return getattr(runtime, "_handlers", {}) or {}


@pytest.mark.basic
def test_reload_bundles_keeps_entity_routing_armed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact live defect: publish/reload swapped in an unarmed runtime."""
    from abstractgateway.config import GatewayHostConfig
    from abstractgateway.service import create_default_gateway_service

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    svc = create_default_gateway_service(config=GatewayHostConfig.from_env())
    assert svc.entity_registry is not None, "entity subsystem must boot for this pin"
    old_runtime = svc.host.runtime
    for etype in ENTITY_EFFECTS:
        assert etype in _handlers(old_runtime), f"boot must arm {etype.value}"

    result = svc.host.reload_bundles_from_disk()
    assert result.get("ok") is True
    assert result.get("warnings") is None or result.get("warnings") == [], (
        f"re-arm hook must succeed on reload: {result.get('warnings')}"
    )

    new_runtime = svc.host.runtime
    # The reload really swaps the runtime — the pin is meaningless otherwise.
    assert new_runtime is not old_runtime, "reload_bundles_from_disk must have rebuilt the runtime"
    for etype in ENTITY_EFFECTS:
        assert etype in _handlers(new_runtime), (
            f"reload disarmed {etype.value}: entity runs would fail "
            "'No effect handler registered' after any catalog publish"
        )


@pytest.mark.basic
def test_reload_survives_repeated_reloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-arm must be idempotent across reload cycles (each reload arms a
    FRESH runtime, so refuse-to-shadow never fires against our own routers)."""
    from abstractgateway.config import GatewayHostConfig
    from abstractgateway.service import create_default_gateway_service

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    svc = create_default_gateway_service(config=GatewayHostConfig.from_env())
    assert svc.entity_registry is not None
    for _ in range(3):
        result = svc.host.reload_bundles_from_disk()
        assert result.get("ok") is True
        assert not result.get("warnings"), result.get("warnings")
        assert EffectType.MEMORY_RECALL in _handlers(svc.host.runtime)


@pytest.mark.basic
def test_rebuild_hook_failure_is_loud_not_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing hook must not kill the reload, but must surface a labeled
    warning (the runtime may be missing handlers — say so, never silently)."""
    from abstractgateway.config import GatewayHostConfig
    from abstractgateway.service import create_default_gateway_service

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    svc = create_default_gateway_service(config=GatewayHostConfig.from_env())

    def _boom(rt) -> None:
        raise RuntimeError("injected hook failure")

    svc.host.add_runtime_rebuild_hook(_boom)
    result = svc.host.reload_bundles_from_disk()
    assert result.get("ok") is True, "reload itself must survive a hook failure"
    warnings = result.get("warnings") or []
    assert any("#FALLBACK" in w and "injected hook failure" in w for w in warnings), warnings
