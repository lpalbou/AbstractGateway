"""Whole served console: delayed catalog, version disclosure and shared provider cards."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from abstractgateway.console import gateway_console_html
from node_requirement import require_node
from test_gateway_console_browser_state_toggles import _playwright_modules
from test_gateway_console_catalog import _art, _catalog
from test_gateway_console_discovered_assets import asset_rows

pytestmark = pytest.mark.e2e


def test_catalog_loading_versions_and_provider_cards(tmp_path):
    if os.getenv("ABSTRACTGATEWAY_BROWSER_TESTS") != "1":
        pytest.skip("set ABSTRACTGATEWAY_BROWSER_TESTS=1 for Chromium validation")
    html = tmp_path / "console.html"
    html.write_text(gateway_console_html(), encoding="utf-8")
    catalog = _catalog(3)
    catalog["rows"][0].update(starter=True, vendor="Qwen", capabilities={"text": True, "thinking": True, "tools": "native", "vision": True})
    catalog["rows"][1].update(display_name="A longer model title", vendor="Meta", params_total=31_000_000_000, params_active=3_300_000_000)
    for i, row in enumerate(catalog["rows"]):
        row["artifacts"] = [_art(i, j) for j in range(18)]
        for j, art in enumerate(row["artifacts"]):
            art["artifact"] += f"-variant-{j}"
    catalog["rows"][0]["artifacts"][0]["fit"] = {
        "verdict":"tight", "need_bytes":112e9,"usable_bytes":113e9,"free_now_bytes":59.3e9,
        "fits_now":False,"disk_ok":True,"max_context":67834,"confidence":"rough",
        "notes":["unrecognised quantization 'oq4e'","not enough disk: stale download warning","already installed"],
    }
    catalog["rows"][1]["artifacts"][0]["presence"]["status"] = "installed"
    fixture = tmp_path / "catalog.json"
    fixture.write_text(json.dumps(catalog), encoding="utf-8")
    assets = tmp_path / "assets.json"
    assets.write_text(json.dumps(asset_rows()), encoding="utf-8")
    shots = Path(os.getenv("ABSTRACTGATEWAY_BROWSER_SHOTS", str(tmp_path / "shots")))
    shots.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([
        require_node(), str(Path(__file__).parent / "browser" / "catalog_cards.mjs"),
        str(html), str(_playwright_modules()), str(shots), str(fixture), str(assets),
    ], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, (result.stdout[-4000:], result.stderr[-6000:])
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert not report["failures"], report
    assert report["checks"] >= 35
