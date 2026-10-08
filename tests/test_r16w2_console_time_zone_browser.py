"""R16.1: Accounts → Preferences → Time zone in a real browser, signed in as a NON-ADMIN editing
her own time zone through the kit AfTimeZonePicker island (tests/browser/r16w2_time_zone.mjs has
the checks).

Opt-in like test_gateway_console_browser_accounts.py (ABSTRACTGATEWAY_BROWSER_TESTS=1,
playwright-core from ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES or abstractcode/web/node_modules);
the gateway runs from this checkout with a scratch HOME and data dir, no provider keys, on a
free loopback port. R16W2_SHOTS=<dir> also writes the light/dark shots (1440/834/390) of the modal.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from node_requirement import require_node
from test_gateway_console_browser_state_toggles import _call, _free_port, _playwright_modules, _start, _stop

pytestmark = pytest.mark.e2e

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "browser" / "r16w2_time_zone.mjs"
ALICE = "r16w2-alice-browser-0001"


@pytest.fixture()
def prefs_gateway(tmp_path: Path):
    if os.getenv("ABSTRACTGATEWAY_BROWSER_TESTS", "").strip() not in {"1", "true", "yes"}:
        pytest.skip("browser test: set ABSTRACTGATEWAY_BROWSER_TESTS=1 (needs playwright-core + Chromium)")
    port = _free_port()
    home, data = tmp_path / "home", tmp_path / "data"
    (home / "tmp").mkdir(parents=True)
    data.mkdir()
    env = {
        "HOME": str(home), "TMPDIR": str(home / "tmp"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join([str(HERE.parent / "src")] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]),
        "PYTHONUNBUFFERED": "1", "LANG": "en_US.UTF-8",
        "ABSTRACTGATEWAY_DATA_DIR": str(data), "ABSTRACTGATEWAY_USER_AUTH": "1",
        "ABSTRACTGATEWAY_ALLOWED_ORIGINS": f"http://127.0.0.1:{port},http://localhost:{port}",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", "HF_HUB_OFFLINE": "1", "NO_COLOR": "1",
    }
    log = tmp_path / "gateway.log"
    base = f"http://127.0.0.1:{port}"
    proc = _start(port, env, log)
    try:
        admin = re.findall(r"Gateway admin token: (\S+)$", log.read_text(), flags=re.M)[-1]
        assert _call(base, "POST", "/host/first-run", admin, {"outcome": "skipped"})[0] == 200
        code, out = _call(base, "POST", "/admin/users", admin, {"user_id": "alice", "roles": ["user"], "token": ALICE})
        assert code == 200, out
        yield base
    finally:
        _stop(proc)


def test_non_admin_edits_her_own_time_zone_in_the_console(prefs_gateway) -> None:
    node = require_node()
    modules = _playwright_modules()
    argv = [node, str(SCRIPT), prefs_gateway, ALICE, str(modules)]
    shots = os.getenv("R16W2_SHOTS", "").strip()
    if shots:
        argv.append(shots)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=600, check=False)
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-4000:])
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    # 18 behaviour checks + 6 layout checks (3 widths x 2 themes), with or without R16W2_SHOTS.
    assert out["checks"] >= 24, out["checks"]
