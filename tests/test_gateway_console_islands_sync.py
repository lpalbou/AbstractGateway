"""The console's kit islands = abstractuic's real components, vendored + drift-pinned.

Operator order (2026-09-24, mission L): the console's theme selector and
connect/disconnect control are the abstractuic ui-kit's AfAppearanceDialog
(ThemeSelect inside) and AfTopBarActions, not hand-drawn copies. The kit
bundles them (`ui-kit/scripts/build_islands.mjs`); `console_islands_sync`
vendors the bundle + the kit's component CSS into `console_islands.py`.

These pins make the copy impossible to rot silently: the kit-source hash is
recomputed from the checkout (a kit change without a re-sync fails here),
the built bundle is byte-compared when the kit has been built, and the
served page is checked to carry and mount the islands.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from node_requirement import require_node

from abstractgateway import console_islands_sync
from abstractgateway.console import gateway_console_html
from abstractgateway.console_islands import ISLANDS_CSS, ISLANDS_JS, ISLANDS_PROVENANCE
from abstractgateway.console_themes import KIT_THEME_SPECS

pytestmark = pytest.mark.basic


def test_vendored_islands_match_the_kit_sources() -> None:
    """THE DRIFT PIN. Skips honestly outside the monorepo (no kit checkout)."""
    kit = console_islands_sync.locate_kit()
    if kit is None:
        pytest.skip("abstractuic ui-kit not present (non-monorepo checkout)")
    assert console_islands_sync.kit_sources_sha256(kit) == ISLANDS_PROVENANCE["kit_sources_sha256"], (
        "the abstractuic kit changed since the console islands were vendored: run "
        "`(cd abstractuic/ui-kit && node scripts/build_islands.mjs)` then "
        "`python -m abstractgateway.console_islands_sync`, and re-verify the console"
    )
    assert console_islands_sync.kit_version(kit) == ISLANDS_PROVENANCE["kit_version"]
    if (kit / console_islands_sync.BUNDLE_RELPATH).is_file():
        regenerated = console_islands_sync.generate_console_islands_module(kit)
        current = (Path(console_islands_sync.__file__).parent / console_islands_sync.GENERATED_MODULE).read_text(
            encoding="utf-8"
        )
        assert current == regenerated, "console_islands.py is STALE vs the kit's built bundle — re-run the sync"


def test_vendored_copy_is_internally_consistent() -> None:
    assert hashlib.sha256(ISLANDS_JS.encode("utf-8")).hexdigest() == ISLANDS_PROVENANCE["bundle_sha256"]
    assert hashlib.sha256(ISLANDS_CSS.encode("utf-8")).hexdigest() == ISLANDS_PROVENANCE["css_sha256"]
    assert ISLANDS_JS.startswith(f"/*! @abstractframework/ui-kit {ISLANDS_PROVENANCE['kit_version']} console islands")
    # The component CSS the islands render, minus the per-theme blocks that
    # console_themes.py already carries (one copy of each theme block).
    for cls in (".af-topbar__pill", ".af-select-trigger", ".af-appearance", ".af-theme-swatch"):
        assert cls in ISLANDS_CSS, cls
    assert ":root.theme-" not in ISLANDS_CSS
    assert "/*" not in ISLANDS_CSS


def test_served_console_carries_and_mounts_the_islands() -> None:
    html = gateway_console_html()
    assert html.count('<script id="af-console-islands">') == 1
    assert html.count('<style id="af-kit-css">') == 1
    body = re.search(r'<script id="af-console-islands">(.*?)</script>', html, flags=re.S).group(1)
    # Nothing inside the bundle can open or close a script element.
    lowered = body.lower()
    assert "<script" not in lowered and "</script" not in lowered and "<!--" not in body
    # The islands load BEFORE the console script that mounts them.
    assert html.index('<script id="af-console-islands">') < html.index("function mountConsoleIslands(")
    for call in ("lib.mountTopBar(", "lib.mountAppearance(", "islands.lib.applyAppearance("):
        assert call in html, call
    # The static cluster stays as the no-bundle fallback, hidden once mounted.
    assert 'id="af-topbar-root"' in html and 'id="topbar-static"' in html and 'id="af-appearance-root"' in html


def test_islands_bundle_defines_the_documented_api() -> None:
    node = require_node()
    probe = (
        "const vm = require('node:vm'); const fs = require('node:fs');"
        "const sb = { console }; sb.globalThis = sb; sb.window = sb; vm.createContext(sb);"
        "vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sb);"
        "const a = sb.AfConsoleIslands;"
        "console.log(JSON.stringify({ api: a.apiVersion, kit: a.kitVersion, themes: a.themes.map((t) => t.id),"
        " fns: ['mountTopBar', 'mountAppearance', 'applyAppearance'].filter((f) => typeof a[f] === 'function') }));"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
        f.write(ISLANDS_JS)
        bundle = f.name
    out = subprocess.run([node, "-e", probe, bundle], capture_output=True, text=True, check=True, timeout=60)
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["api"] == ISLANDS_PROVENANCE["api_version"] == "1"
    assert got["kit"] == ISLANDS_PROVENANCE["kit_version"]
    assert got["fns"] == ["mountTopBar", "mountAppearance", "applyAppearance"]
    # The islands' ThemeSelect offers exactly the themes the console styles.
    assert got["themes"] == [s["id"] for s in KIT_THEME_SPECS]
