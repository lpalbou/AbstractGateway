"""The console's table layer in a real browser: cells stay in their header's column (an actions
cell laid out as a flex box left the column grid on the Providers table), and a table turned into
cards becomes a table again when there is room (the layer kept the width recorded when it stacked).
Checks in tests/browser/r15_table_layer.mjs. Opt-in (ABSTRACTGATEWAY_BROWSER_TESTS=1); hermetic
scratch gateway, the Multimodal routes of test_r14w7_multimodal_layout_browser.py.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from node_requirement import require_node
from test_gateway_console_browser_state_toggles import _playwright_modules
from test_r14w7_multimodal_layout_browser import multimodal_gateway  # noqa: F401 (fixture)

pytestmark = pytest.mark.e2e
SCRIPT = Path(__file__).resolve().parent / "browser" / "r15_table_layer.mjs"


def test_cells_stay_in_their_columns_and_cards_return_to_a_table(multimodal_gateway) -> None:  # noqa: F811
    base, admin = multimodal_gateway
    proc = subprocess.run([require_node(), str(SCRIPT), base, admin, str(_playwright_modules())], capture_output=True, text=True, timeout=900, check=False)
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-4000:])
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 13
