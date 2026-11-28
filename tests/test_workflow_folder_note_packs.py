"""12. AbstractNote's packed workflows (rule R4, as re-reviewed 2026-11-28).

Every pack on disk loads, the installed version's first. Two packs of one OLDER version with
different bytes (its flows edited without a version bump): the pack whose folder name sorts first is
served, and one warning names both; file times do not change the choice (a copied data folder loses
them). An older pack that cannot be read is skipped by itself and the installed pack still loads.

In-process, through the seam (`abstractnote_seam.note_bundle_paths`) and the registry compile the
host runs (`WorkflowBundleGatewayHost._compile_workflows`, as tests/test_abstractnote_seam.py does).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import zipfile
from pathlib import Path

import pytest

from abstractgateway import abstractnote_seam as seam

pytestmark = pytest.mark.note_workflows


def _repack(current: Path, dest: Path, *, version: str, extra: str = "") -> Path:
    """A pack of `version` made from the installed one (an `extra` entry makes other bytes)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(current) as src, zipfile.ZipFile(dest, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "manifest.json":
                manifest = json.loads(data)
                manifest["bundle_version"] = version
                data = json.dumps(manifest).encode("utf-8")
            dst.writestr(name, data)
        if extra:
            dst.writestr("NOTE.txt", extra)
    return dest


def _compiled(tmp_path: Path):
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    empty = tmp_path / "bundles"
    empty.mkdir(exist_ok=True)
    return WorkflowBundleGatewayHost._compile_workflows(
        base=empty, framework_base=None, catalog_base=None, catalog_root=tmp_path, data_root=tmp_path,
        catalog_tenant="default", dynamic_dir=tmp_path / "dynamic_flows", email_tools_listed=False,
    )


@pytest.fixture()
def installed(tmp_path: Path) -> Path:
    pytest.importorskip("abstractnote.flows.bundle", reason="abstractnote is not installed")
    paths = seam.note_bundle_paths(tmp_path)
    assert len(paths) == 1, paths
    return paths[0]


def _served_path(tmp_path: Path, version: str) -> str:
    compiled = _compiled(tmp_path)
    return str(compiled.bundle_sources["abstractnote"][version]["path"])


def test_12_every_pack_loads_and_the_installed_one_first(tmp_path: Path, installed: Path) -> None:
    root = tmp_path / "extensions" / "abstractnote"
    a = _repack(installed, root / "1111111111111111" / "abstractnote@0.0.1.flow", version="0.0.1")
    b = _repack(installed, root / "2222222222222222" / "abstractnote@0.0.2.flow", version="0.0.2")
    paths = seam.note_bundle_paths(tmp_path)
    assert paths[0] == installed and set(paths[1:]) == {a, b}, paths
    compiled = _compiled(tmp_path)
    version = installed.name[len("abstractnote@"):-len(".flow")]
    for v in (version, "0.0.1", "0.0.2"):
        assert compiled.workflow_registry.get(f"abstractnote@{v}:note-drain") is not None, v
    assert compiled.latest_versions["abstractnote"] == version


def test_12_two_packs_of_one_older_version_serve_the_first_folder_whatever_the_file_times(
        tmp_path: Path, installed: Path, caplog: pytest.LogCaptureFixture) -> None:
    root = tmp_path / "extensions" / "abstractnote"
    first = _repack(installed, root / "0000000000000000" / "abstractnote@0.0.1.flow", version="0.0.1", extra="first")
    last = _repack(installed, root / "ffffffffffffffff" / "abstractnote@0.0.1.flow", version="0.0.1", extra="last")
    assert first.read_bytes() != last.read_bytes()
    for older, newer in ((first, last), (last, first)):  # file times both ways round
        os.utime(older, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        os.utime(newer, ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            paths = seam.note_bundle_paths(tmp_path)
        assert paths[0] == installed and first in paths, paths
        assert _served_path(tmp_path, "0.0.1") == str(first)
        naming = [r for r in caplog.records if r.levelno >= logging.WARNING and str(first) in r.getMessage() and str(last) in r.getMessage()]
        assert len(naming) == 1, [r.getMessage() for r in caplog.records]


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="needs POSIX permissions, not root")
def test_12_an_unreadable_older_pack_is_skipped_and_the_installed_one_loads(tmp_path: Path, installed: Path) -> None:
    root = tmp_path / "extensions" / "abstractnote"
    version = installed.name[len("abstractnote@"):-len(".flow")]
    # One unreadable alone, and one unreadable beside a readable pack of the same version.
    lone = _repack(installed, root / "1111111111111111" / "abstractnote@0.0.1.flow", version="0.0.1")
    twin_bad = _repack(installed, root / "2222222222222222" / "abstractnote@0.0.2.flow", version="0.0.2", extra="a")
    twin_ok = _repack(installed, root / "3333333333333333" / "abstractnote@0.0.2.flow", version="0.0.2", extra="b")
    corrupt = root / "4444444444444444" / "abstractnote@0.0.3.flow"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not a zip")
    lone.chmod(0)
    twin_bad.chmod(0)
    try:
        paths = seam.note_bundle_paths(tmp_path)
        assert paths and paths[0] == installed, paths
        compiled = _compiled(tmp_path)
        assert compiled.workflow_registry.get(f"abstractnote@{version}:note-drain") is not None
        assert compiled.workflow_registry.get("abstractnote@0.0.2:note-drain") is not None
        assert str(compiled.bundle_sources["abstractnote"]["0.0.2"]["path"]) == str(twin_ok)
        assert compiled.latest_versions["abstractnote"] == version
    finally:
        lone.chmod(0o644)
        twin_bad.chmod(0o644)
