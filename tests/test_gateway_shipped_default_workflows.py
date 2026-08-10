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
  co-scientist;
- every pinned artifact is actually committable. `.gitignore` ignores
  `flows/bundles/*` and re-admits shipped bundles one by one, so a version
  bump that updates pyproject but not the negation leaves the new artifact
  untracked — the wheel builds on the author's machine and fails from a clean
  clone. Three bundles were already in that state when this test was written.
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


@pytest.mark.parametrize(
    "dockerfile_name",
    sorted(p.name for p in (ROOT / "docker" / "abstractgateway-server").glob("Dockerfile*")),
)
def test_dockerfile_copies_every_force_included_artifact(dockerfile_name: str) -> None:
    """Container images are a release surface too, and there is more than one
    of them (base + nvidia). Their `local` install mode builds the wheel from
    a context assembled by COPY lines, so any force-included artifact a
    Dockerfile does not copy fails that build — at the COPY step if the path
    is stale, or in hatchling if it is merely absent. The published `pypi`
    mode installs the built wheel and is unaffected, which is exactly why this
    drifts unnoticed. Parametrized over every Dockerfile so a new variant
    cannot silently skip the check."""
    import re

    dockerfile = ROOT / "docker" / "abstractgateway-server" / dockerfile_name
    joined = re.sub(r"\\\s*\n\s*", " ", dockerfile.read_text(encoding="utf-8"))
    copied: set[str] = set()
    for line in joined.splitlines():
        if not line.startswith("COPY ") or "/tmp/abstractgateway-src" not in line:
            continue
        copied.update(line.split()[1:-1])  # drop COPY and the destination

    if not copied:
        pytest.skip(f"{dockerfile_name} does not build from a source context")

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    not_copied = sorted(src for src in force if src not in copied)
    assert not not_copied, (
        f"{dockerfile_name} does not copy these force-included artifacts, so a "
        f"local-mode image build cannot produce the wheel: {not_copied}"
    )

    stale = sorted(p for p in copied if p.startswith("flows/") and not (ROOT / p).exists())
    assert not stale, (
        f"{dockerfile_name} copies paths that no longer exist: {stale} — "
        "`docker build` fails at the COPY step"
    )


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


def _manifest(host, bundle_id: str):
    version = host.latest_bundle_versions[bundle_id]
    return version, host.bundles[bundle_id][version].manifest


def _entrypoint(host, bundle_id: str, flow_id: str):
    version, manifest = _manifest(host, bundle_id)
    for ep in manifest.entrypoints:
        if ep.flow_id == flow_id:
            return version, ep
    raise AssertionError(f"{bundle_id}@{version} has no entrypoint for flow '{flow_id}'")


def _default_entrypoint(host, bundle_id: str):
    _, manifest = _manifest(host, bundle_id)
    return _entrypoint(host, bundle_id, manifest.default_entrypoint)


def test_every_force_included_bundle_is_tracked_by_git() -> None:
    """A clean clone must CONTAIN every pinned artifact. Trackedness is the
    invariant, not the ignore rule: `.gitignore` ignores `flows/bundles/*` and
    re-admits shipped bundles one negation at a time, so an artifact that was
    never added stays absent from a fresh clone and the wheel build fails
    there while succeeding on the author's machine."""
    import subprocess

    rel = [str(p.relative_to(ROOT)) for p in _force_included_bundle_files()]
    proc = subprocess.run(
        ["git", "ls-files", "--", *rel],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"git unavailable here: {proc.stderr.strip()}")

    tracked = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    untracked = sorted(set(rel) - tracked)
    assert not untracked, (
        "these shipped bundles are pinned in pyproject but not tracked by git, so "
        f"a clean clone cannot build the wheel: {untracked}. Add each one "
        "(`git add -f`) and give it a `!flows/bundles/<name>` negation in "
        ".gitignore — the pyproject pin and the negation must move together."
    )


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

    # basic-agent stays the universal fallback of that resolution chain, so its
    # default entrypoint must keep declaring the same chat contract.
    version, fallback = _default_entrypoint(host, "basic-agent")
    assert "abstractcode.agent.v1" in (fallback.interfaces or [])
