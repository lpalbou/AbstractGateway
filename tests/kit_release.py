"""The abstractuic kit the console vendors, at the RELEASED version it pins.

The islands/theme drift pins compare the vendored copies with the kit. A
moving checkout (abstractuic main, or a worktree ahead of the release) is the
wrong reference: the console pins a released kit (`ISLANDS_PROVENANCE
["kit_version"]`, the ui-kit package version). abstractuic tags its REPO
version (`v0.10.3` released ui-kit 0.9.16), so the release is the OLDEST tag
whose `ui-kit/package.json` carries the pinned version (`release_tag`); the
pins read that tag's files. An explicit `ABSTRACTUIC_SRC` is taken as the
person's choice and read as-is.

No tag carrying the pinned version, or no way to build the bundle, is a
FAILURE with the fix, never a skip: a pin that skips checks nothing.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Optional


def explicit_kit_src() -> bool:
    return bool(str(os.getenv("ABSTRACTUIC_SRC") or "").strip())


def release_tag(repo: Path, version: str) -> Optional[str]:
    """The abstractuic tag that released ui-kit `version`: the oldest `v*` tag (version order) whose
    `ui-kit/package.json` carries it. Later tags that did not bump ui-kit carry the same version;
    the oldest is the one the package was published from. None when no tag carries it."""
    refs = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "--sort=version:refname", "--format=%(refname:short)", "refs/tags/v*"],
        capture_output=True, text=True,
    )
    tags = [t for t in refs.stdout.split() if t] if refs.returncode == 0 else []
    if not tags:
        return None
    batch = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input="".join(f"{t}:ui-kit/package.json\n" for t in tags).encode("utf-8"), capture_output=True,
    )
    if batch.returncode != 0:
        return None
    out, pos = batch.stdout, 0
    for tag in tags:
        end = out.index(b"\n", pos)
        header = out[pos:end].decode("utf-8", "replace").split()
        pos = end + 1
        if len(header) < 3 or header[1] == "missing":
            continue
        size = int(header[2])
        blob, pos = out[pos:pos + size], pos + size + 1
        try:
            found = str(json.loads(blob.decode("utf-8")).get("version") or "")
        except ValueError:
            continue
        if found == version:
            return tag
    return None


def released_ui_kit(kit_ui_dir: Path, version: str, dest: Path) -> Optional[Path]:
    """`ui-kit/` (with panel-chat beside it when the tag has it) at the tag that released ui-kit
    `version`, extracted from the checkout that holds `kit_ui_dir`; None when no tag carries it."""
    top = subprocess.run(["git", "-C", str(kit_ui_dir), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        return None
    repo = top.stdout.strip()
    tag = release_tag(Path(repo), version)
    if tag is None:
        return None
    # panel-chat rides along when the tag has it: the islands bundle and CSS
    # carry it (mountSandboxChat, round 3). Older tags predate it: ui-kit alone.
    # tsconfig.base.json: ui-kit's tsconfig extends it, and esbuild reads it (its `strict` makes the
    # bundle start with "use strict"); without it the bundle built here differs from the vendored one.
    paths = ["ui-kit"]
    for optional in ("panel-chat", "tsconfig.base.json"):
        if subprocess.run(["git", "-C", repo, "cat-file", "-e", f"{tag}:{optional}"], capture_output=True).returncode == 0:
            paths.append(optional)
    archive = subprocess.run(["git", "-C", repo, "archive", tag, *paths], capture_output=True)
    if archive.returncode != 0:
        return None
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tf:
        tf.extractall(dest, filter="data")
    return dest / "ui-kit"


def build_islands_bundle(kit: Path, node_modules: Path, node: str) -> Path:
    """Build the islands bundle of an extracted kit (`kit/islands/dist/af-console-islands.js`) with
    the checkout's installed packages; the bundle is not in git, so the released sources are built
    here rather than trusting a checkout's `dist/`, which may predate its sources."""
    link = kit.parent / "node_modules"
    if not link.exists():
        link.symlink_to(node_modules, target_is_directory=True)
    proc = subprocess.run([node, "scripts/build_islands.mjs"], cwd=str(kit), capture_output=True, text=True, timeout=300)
    bundle = kit / "islands" / "dist" / "af-console-islands.js"
    if proc.returncode != 0 or not bundle.is_file():
        raise RuntimeError(f"building the islands bundle in {kit} failed:\n{proc.stdout}\n{proc.stderr}")
    return bundle


def no_release_message(found: str, pinned: str) -> str:
    return (
        f"no abstractuic tag carries ui-kit {pinned} (the version the console vendors; the checkout is at "
        f"ui-kit {found}): fetch abstractuic's tags (`git -C abstractuic fetch --tags`), or set "
        f"ABSTRACTUIC_SRC=<abstractuic checkout of the tag that released ui-kit {pinned}>/ui-kit/src"
    )
