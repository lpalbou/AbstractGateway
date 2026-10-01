"""The abstractuic kit the console vendors, at the RELEASED version it pins.

The islands/theme drift pins compare the vendored copies with the kit. A
moving checkout (abstractuic main, or a worktree ahead of the release) is the
wrong reference: the console pins a released kit (`ISLANDS_PROVENANCE
["kit_version"]`). When the auto-located checkout has the tag `v<version>`,
the pins read that tag's files; an explicit `ABSTRACTUIC_SRC` is taken as the
person's choice and read as-is.
"""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Optional


def explicit_kit_src() -> bool:
    return bool(str(os.getenv("ABSTRACTUIC_SRC") or "").strip())


def released_ui_kit(kit_ui_dir: Path, version: str, dest: Path) -> Optional[Path]:
    """`ui-kit/` of the kit tag `v<version>`, extracted from the checkout that
    holds `kit_ui_dir`; None when that checkout has no such tag."""
    top = subprocess.run(["git", "-C", str(kit_ui_dir), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        return None
    repo = top.stdout.strip()
    tag = f"v{version}"
    if subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}"], capture_output=True).returncode != 0:
        return None
    # panel-chat rides along: the islands bundle and CSS carry it (mountSandboxChat).
    archive = subprocess.run(["git", "-C", repo, "archive", tag, "ui-kit", "panel-chat"], capture_output=True)
    if archive.returncode != 0:
        return None
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tf:
        tf.extractall(dest, filter="data")
    return dest / "ui-kit"


def skip_reason(found: str, pinned: str) -> str:
    return (
        f"the abstractuic checkout is at ui-kit {found}, the console vendors the released {pinned}, and the "
        f"checkout has no v{pinned} tag: set ABSTRACTUIC_SRC=<abstractuic v{pinned} checkout>/ui-kit/src to run this pin"
    )
