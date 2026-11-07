"""The console's kit islands = abstractuic's real components, vendored + drift-pinned.

Operator order (2026-09-24, mission L): the console's theme selector and
connect/disconnect control are the abstractuic ui-kit's AfAppearanceDialog
(ThemeSelect inside) and AfTopBarActions, not hand-drawn copies. The kit
bundles them (`ui-kit/scripts/build_islands.mjs`); `console_islands_sync`
vendors the bundle + the kit's component CSS into `console_islands.py`.

These pins make the copy impossible to rot silently: the kit-source hash is
recomputed from the released kit (a kit change without a re-sync fails here),
the bundle is rebuilt from the released sources and byte-compared, and the
served page is checked to carry and mount the islands. With an abstractuic
checkout present, nothing here skips: no tag for the pinned version, no
installed kit packages or no built bundle under ABSTRACTUIC_SRC fails with the
fix. Only a checkout without abstractuic beside it (CI, a wheel) skips.
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
from kit_release import build_islands_bundle, explicit_kit_src, no_release_message, released_ui_kit

pytestmark = pytest.mark.basic


def _pinned_kit(tmp_path: Path) -> Path:
    """The kit the console pins: ABSTRACTUIC_SRC as given, else the abstractuic checkout's files at
    the tag that released the vendored ui-kit version (a failure when no tag carries it)."""
    kit = console_islands_sync.locate_kit()
    if kit is None:
        pytest.skip("abstractuic ui-kit not present (non-monorepo checkout)")
    if explicit_kit_src():
        return kit
    pinned = ISLANDS_PROVENANCE["kit_version"]
    released = released_ui_kit(kit, pinned, tmp_path / "released")
    if released is None:
        pytest.fail(no_release_message(console_islands_sync.kit_version(kit), pinned), pytrace=False)
    return released


def test_vendored_islands_match_the_kit_sources(tmp_path: Path) -> None:
    """THE DRIFT PIN, against the RELEASED kit the console pins (the abstractuic tag that released
    that ui-kit version), never a checkout that moved ahead of it."""
    kit = _pinned_kit(tmp_path)
    assert console_islands_sync.kit_version(kit) == ISLANDS_PROVENANCE["kit_version"]
    assert console_islands_sync.kit_sources_sha256(kit) == ISLANDS_PROVENANCE["kit_sources_sha256"], (
        "the abstractuic kit changed since the console islands were vendored: build the islands from the "
        "kit release (`node scripts/build_islands.mjs` in its ui-kit) and run "
        "`ABSTRACTUIC_SRC=<that ui-kit>/src python -m abstractgateway.console_islands_sync`, then re-verify the console"
    )


def test_vendored_islands_match_the_bundle_built_from_the_kit(tmp_path: Path) -> None:
    """The vendored module is exactly what the sync writes from the released kit's bundle. The
    bundle is not in git: it is rebuilt here from the released sources with the checkout's installed
    packages (a checkout's own `islands/dist` can predate its sources). Under ABSTRACTUIC_SRC the
    person's kit must have been built."""
    kit = _pinned_kit(tmp_path)
    if explicit_kit_src():
        if not (kit / console_islands_sync.BUNDLE_RELPATH).is_file():
            pytest.fail(
                f"{kit / console_islands_sync.BUNDLE_RELPATH} is not built: run `node scripts/build_islands.mjs` in {kit}",
                pytrace=False,
            )
    else:
        checkout = console_islands_sync.locate_kit()
        node_modules = next((d for d in (checkout / "node_modules", checkout.parent / "node_modules") if (d / "esbuild").is_dir()), None)
        if node_modules is None:
            pytest.fail(f"abstractuic's packages are not installed (no esbuild under {checkout.parent}/node_modules): run `npm ci` in {checkout.parent}", pytrace=False)
        build_islands_bundle(kit, node_modules, require_node())
    regenerated = console_islands_sync.generate_console_islands_module(kit)
    current = (Path(console_islands_sync.__file__).parent / console_islands_sync.GENERATED_MODULE).read_text(encoding="utf-8")
    assert current == regenerated, "console_islands.py is STALE vs the bundle built from the released kit: re-run the sync"


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
    assert got["api"] == ISLANDS_PROVENANCE["api_version"] == "2"
    assert got["kit"] == ISLANDS_PROVENANCE["kit_version"]
    assert got["fns"] == ["mountTopBar", "mountAppearance", "applyAppearance"]
    # The islands' ThemeSelect offers exactly the themes the console styles.
    assert got["themes"] == [s["id"] for s in KIT_THEME_SPECS]
