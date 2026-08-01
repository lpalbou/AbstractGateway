"""Fresh-install contract for the shipped default workflows.

The wheel force-include list in pyproject.toml IS the out-of-box registry: on
a pip install, `_default_flows_dir()` resolves to the packaged bundles dir and
the host loads exactly those artifacts. These tests boot the host on that
exact set (pins read from pyproject, so a version bump cannot drift past
them) and assert the promise users get:

- every force-included bundle loads at its pinned version — a bundle the
  compile/min-runtime gates silently DROP is a packaging bug, not a wheel
  detail;
- the flagship workflows serve out of the box: coding-agent's `coder`
  (abstractcode-tui's default agent when installed, resolved by bundle id +
  flow id + the abstractcode.agent.v1 interface), deep-research, and
  co-scientist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from abstractruntime.storage.artifacts import InMemoryArtifactStore
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

pytestmark = pytest.mark.basic

ROOT = Path(__file__).resolve().parents[1]


def _force_included_bundle_files() -> list[Path]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    files = [ROOT / src for src in force if src.startswith("flows/bundles/")]
    assert files, "wheel force-include lists no shipped bundles — packaging is broken"
    return files


def _load_shipped_host(tmp_path: Path):
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    bundles_dir = tmp_path / "flows"
    bundles_dir.mkdir(parents=True)
    for src in _force_included_bundle_files():
        assert src.is_file(), f"force-included bundle artifact missing from repo: {src}"
        (bundles_dir / src.name).write_bytes(src.read_bytes())

    return WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=bundles_dir,
        data_dir=tmp_path / "runtime",
        run_store=InMemoryRunStore(),
        ledger_store=InMemoryLedgerStore(),
        artifact_store=InMemoryArtifactStore(),
    )


def _entrypoint(host, bundle_id: str, flow_id: str):
    version = host.latest_bundle_versions[bundle_id]
    manifest = host.bundles[bundle_id][version].manifest
    for ep in manifest.entrypoints:
        if ep.flow_id == flow_id:
            return version, ep
    raise AssertionError(f"{bundle_id}@{version} has no entrypoint for flow '{flow_id}'")


def test_every_force_included_bundle_loads_at_its_pinned_version(tmp_path: Path) -> None:
    host = _load_shipped_host(tmp_path)
    for src in _force_included_bundle_files():
        stem = src.name[: -len(".flow")]
        bundle_id, _, version = stem.partition("@")
        assert bundle_id in host.bundles, f"shipped bundle '{bundle_id}' did not load"
        if version:
            assert version in host.bundles[bundle_id], (
                f"shipped bundle '{bundle_id}' loaded but not at its pinned version {version}"
            )


def test_fresh_install_serves_coder_deep_research_and_co_scientist(tmp_path: Path) -> None:
    host = _load_shipped_host(tmp_path)

    # The coder abstractcode-tui resolves by default (saved preference →
    # coding-agent:coder → basic-agent): it must serve AND declare the chat
    # agent interface the TUI filters on.
    version, coder = _entrypoint(host, "coding-agent", "coder")
    assert f"coding-agent@{version}:coder" in host.specs
    assert "abstractcode.agent.v1" in (coder.interfaces or [])

    version, _ = _entrypoint(host, "deep-research", "deep-research")
    assert f"deep-research@{version}:deep-research" in host.specs

    version, _ = _entrypoint(host, "co-scientist", "co-scientist")
    assert f"co-scientist@{version}:co-scientist" in host.specs

    # basic-agent stays the universal fallback of that resolution chain.
    version, fallback = _entrypoint(host, "basic-agent", host.bundles["basic-agent"][
        host.latest_bundle_versions["basic-agent"]
    ].manifest.default_entrypoint)
    assert "abstractcode.agent.v1" in (fallback.interfaces or [])
