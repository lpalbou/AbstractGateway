"""Sync the gateway console's themes from the abstractuic kit (uic card 0023).

The console is served HTML from Python with no npm build, so it cannot
import @abstractframework/ui-kit at runtime — the operator caught the
consequence live (2026-07-15 16:56): a hand-copied 6-theme fork while the
kit serves 21. The ruled fix shape (uic backlog 0023, gateway-owned): the
kit's `theme.ts` (THEME_SPECS) and `theme.css` (per-theme token blocks) are
GENERATED into `console_themes.py` at sync time, and a drift-pin test
regenerates from the kit source and fails loud on any divergence — the
copy exists (packaging necessity: wheels have no kit checkout) but it can
never rot silently (the pc-chat class-set belt, applied to themes).

Run: `python -m abstractgateway.console_theme_sync` (or
`scripts/sync_console_themes.py`) from a checkout where abstractuic sits
beside abstractgateway (or set ABSTRACTUIC_SRC to the ui-kit/src dir).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

GENERATED_MODULE = "console_themes.py"

_SPEC_RE = re.compile(
    r"\{\s*id:\s*\"(?P<id>[a-z0-9-]+)\",\s*label:\s*\"(?P<label>[^\"]+)\",\s*"
    r"group:\s*\"(?P<group>dark|light)\",\s*swatches:\s*\[(?P<swatches>[^\]]*)\]",
)


def locate_kit_src(repo_root: Optional[Path] = None) -> Optional[Path]:
    """The kit's ui-kit/src directory: ABSTRACTUIC_SRC env, else the
    abstractuic checkout beside this repo (monorepo layout)."""
    env = str(os.getenv("ABSTRACTUIC_SRC") or "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    root = repo_root if repo_root is not None else Path(__file__).resolve().parents[3]
    candidate = root / "abstractuic" / "ui-kit" / "src"
    return candidate if candidate.is_dir() else None


def parse_theme_specs(theme_ts_text: str) -> List[Dict[str, Any]]:
    """Extract THEME_SPECS entries (id/label/group/swatches) from theme.ts.

    The regex is deliberately narrow: it parses the kit's literal-array
    style and REFUSES (empty result -> caller raises) rather than guessing
    if the kit ever reshapes the file — the drift pin then names the sync
    module as the thing to update."""
    specs: List[Dict[str, Any]] = []
    for m in _SPEC_RE.finditer(theme_ts_text):
        swatches = [s.strip().strip('"') for s in m.group("swatches").split(",") if s.strip()]
        specs.append(
            {
                "id": m.group("id"),
                "label": m.group("label"),
                "group": m.group("group"),
                "swatches": swatches,
            }
        )
    return specs


def parse_theme_css_blocks(theme_css_text: str) -> List[Tuple[str, str]]:
    """Every `{selector} {{ ... }}` block whose selector names a
    `:root.theme-*` class, verbatim, in source order. Returns
    (selector, full_block_text) pairs. Kit theme blocks are FLAT css —
    a `{` inside a matched block means the format changed and the sync
    must be revisited, so that raises."""
    blocks: List[Tuple[str, str]] = []
    idx = 0
    text = theme_css_text
    while True:
        brace = text.find("{", idx)
        if brace < 0:
            break
        # Selector = the text between the previous block/comment end and the brace.
        sel_start = max(text.rfind("}", 0, brace), text.rfind("*/", 0, brace))
        selector = text[sel_start + 1 if sel_start >= 0 else 0 : brace].strip().lstrip("/").strip()
        end = text.find("\n}", brace)
        if end < 0:
            break
        body = text[brace + 1 : end]
        if ":root.theme-" in selector:
            if "{" in body:
                raise ValueError(
                    f"kit theme block for {selector!r} is not flat CSS — update console_theme_sync's parser"
                )
            blocks.append((selector, f"{selector} {{{body}\n}}"))
        idx = end + 2
    return blocks


def generate_console_themes_module(kit_src: Path) -> str:
    """Render the generated console_themes.py content from the kit source."""
    theme_ts = (kit_src / "theme.ts").read_text(encoding="utf-8")
    theme_css = (kit_src / "theme.css").read_text(encoding="utf-8")

    specs = parse_theme_specs(theme_ts)
    if len(specs) < 10:
        raise ValueError(
            f"parsed only {len(specs)} THEME_SPECS from {kit_src / 'theme.ts'} — "
            "the kit file shape changed; update console_theme_sync"
        )
    blocks = parse_theme_css_blocks(theme_css)
    if not blocks:
        raise ValueError(f"no :root.theme-* blocks parsed from {kit_src / 'theme.css'}")

    block_ids = set()
    for selector, _ in blocks:
        block_ids.update(re.findall(r":root\.theme-([a-z0-9-]+)", selector))
    # "dark" is the kit's :root default — the ONLY spec allowed to have no
    # class block. Anything else missing means the copy would lie.
    missing = [s["id"] for s in specs if s["id"] != "dark" and s["id"] not in block_ids]
    if missing:
        raise ValueError(f"kit theme.css has no blocks for spec ids {missing} — refusing to generate a lying list")

    light_ids = [s["id"] for s in specs if s["group"] == "light"]
    css_text = "\n\n".join(block for _, block in blocks)
    provenance = {
        "theme_ts_sha256": hashlib.sha256(theme_ts.encode("utf-8")).hexdigest(),
        "theme_css_sha256": hashlib.sha256(theme_css.encode("utf-8")).hexdigest(),
    }

    specs_literal = json.dumps(specs, ensure_ascii=False, indent=4)
    light_literal = json.dumps(light_ids, ensure_ascii=False)
    prov_literal = json.dumps(provenance, ensure_ascii=False, indent=4)
    return (
        '"""GENERATED by console_theme_sync — DO NOT EDIT.\n'
        "\n"
        "Source of truth: abstractuic ui-kit/src/theme.ts (THEME_SPECS) and\n"
        "theme.css (per-theme token blocks), copied verbatim so the served\n"
        "console offers exactly the framework's themes (uic card 0023; the\n"
        "operator caught the 6-vs-21 fork live 2026-07-15). Regenerate with:\n"
        "    python -m abstractgateway.console_theme_sync\n"
        "The drift-pin test (test_gateway_console_theme_sync.py) regenerates\n"
        "from the kit checkout and fails loud when this file is stale.\n"
        '"""\n'
        "\n"
        "# fmt: off\n"
        f"KIT_THEME_SPECS = {specs_literal}\n"
        "\n"
        f"KIT_LIGHT_THEME_IDS = {light_literal}\n"
        "\n"
        f"KIT_SOURCE_PROVENANCE = {prov_literal}\n"
        "\n"
        f"KIT_THEME_CSS = {css_text!r}\n"
        "# fmt: on\n"
    )


def sync(repo_src_dir: Optional[Path] = None) -> Path:
    """Write the generated module beside this one. Returns the path."""
    kit_src = locate_kit_src()
    if kit_src is None:
        raise FileNotFoundError(
            "abstractuic kit source not found — set ABSTRACTUIC_SRC to the "
            "ui-kit/src directory or run from the framework monorepo checkout"
        )
    target_dir = repo_src_dir if repo_src_dir is not None else Path(__file__).resolve().parent
    out_path = target_dir / GENERATED_MODULE
    out_path.write_text(generate_console_themes_module(kit_src), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    print(f"wrote {sync()}")
