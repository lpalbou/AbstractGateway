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

pytestmark = pytest.mark.e2e


def test_catalog_loading_versions_and_provider_cards(tmp_path):
    if os.getenv("ABSTRACTGATEWAY_BROWSER_TESTS") != "1":
        pytest.skip("set ABSTRACTGATEWAY_BROWSER_TESTS=1 for Chromium validation")
    html = tmp_path / "console.html"
    html.write_text(gateway_console_html(), encoding="utf-8")
    catalog = _catalog(3)
    for i, row in enumerate(catalog["rows"]):
        row["artifacts"] = [_art(i, j) for j in range(18)]
    fixture = tmp_path / "catalog.json"
    fixture.write_text(json.dumps(catalog), encoding="utf-8")
    shots = Path(os.getenv("ABSTRACTGATEWAY_BROWSER_SHOTS", str(tmp_path / "shots")))
    shots.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([
        require_node(), str(Path(__file__).parent / "browser" / "catalog_cards.mjs"),
        str(html), str(_playwright_modules()), str(shots), str(fixture),
    ], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, (result.stdout[-4000:], result.stderr[-6000:])
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert not report["failures"], report
    assert report["checks"] >= 35
