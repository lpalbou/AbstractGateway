"""The console's engine and cloud provider cards show each brand's official mark, from the kit.

The seam, read live on both sides (never a list kept in this test): every engine the gateway
offers (`engines_install.ENGINE_IDS`) and every cloud provider AbstractCore's registry lists
(`local_provider` False) has, in the VENDORED kit bundle (`console_islands.ISLANDS_JS`), either an
inlined mark carrying its source, licence and date, or a documented text fallback with its reason.
A provider added to AbstractCore without a mark fails here, not silently as initials.
"""

from __future__ import annotations

import json
import subprocess
import tempfile

import pytest
from node_requirement import require_node

from abstractgateway.console_islands import ISLANDS_JS
from abstractgateway.engines_install import ENGINE_IDS

pytestmark = pytest.mark.basic


def _cloud_provider_ids() -> list[str]:
    from abstractcore.providers.registry import get_provider_registry

    reg = get_provider_registry()
    return sorted(name for name in reg.list_provider_names() if not reg.get_provider_info(name).local_provider)


def _kit_marks(ids: list[str]) -> dict:
    node = require_node()
    probe = (
        "const vm = require('node:vm'); const fs = require('node:fs');"
        "const sb = { console }; sb.globalThis = sb; sb.window = sb; vm.createContext(sb);"
        "vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sb);"
        "const m = sb.AfConsoleIslands.brandMarks; const ids = JSON.parse(process.argv[2]);"
        "console.log(JSON.stringify({ cardIds: m.cardIds, marks: Object.fromEntries(ids.map((id) => [id, { info: m.info(id), html: m.html(id) }])) }));"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
        f.write(ISLANDS_JS)
        bundle = f.name
    out = subprocess.run([node, "-e", probe, bundle, json.dumps(ids)], capture_output=True, text=True, check=True, timeout=60)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_every_engine_and_cloud_provider_has_an_official_mark_or_a_documented_fallback() -> None:
    cloud = _cloud_provider_ids()
    assert {"openai", "anthropic", "openrouter", "portkey"} <= set(cloud), cloud
    ids = sorted(set(ENGINE_IDS) | set(cloud))
    got = _kit_marks(ids)
    card_ids = set(got["cardIds"]["engines"]) | set(got["cardIds"]["providers"])
    assert set(ids) <= card_ids, f"ids the cards show but the kit does not list: {sorted(set(ids) - card_ids)}"
    for id_ in ids:
        info = got["marks"][id_]["info"]
        html = got["marks"][id_]["html"]
        assert info["kind"] in {"icon", "text"}, f"{id_}: no mark and no documented fallback ({info})"
        if info["kind"] == "icon":
            icon = info["icon"]
            for field in ("source", "terms", "licence", "retrieved"):
                assert icon.get(field), f"{id_}: the mark has no {field}"
            assert html.startswith(f'<span class="af-brand-mark" data-brand="{id_}"') and "<svg viewBox=" in html
            assert "http" not in icon["body"], f"{id_}: a mark must not reference any host"
        else:
            fb = info["fallback"]
            assert fb["text"] and len(fb["reason"]) >= 20, f"{id_}: a text fallback needs its reason"
            assert "af-brand-mark--text" in html


def test_engine_cards_draw_the_kit_marks_not_two_letter_badges() -> None:
    from abstractgateway.console_ui import CONSOLE_UI_JS

    assert "ENGINE_MARKS" not in CONSOLE_UI_JS
    assert CONSOLE_UI_JS.count("${uiBrandMark(") >= 2
