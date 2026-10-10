"""R18: the Spoken language preference in the web console, in a real browser — Accounts →
Preferences → Spoken language (a NON-ADMIN, alice, on her own account) and the Multimodal page's
own-account line (the admin). tests/browser/r18_spoken_language.mjs has the checks.

Opt-in like test_r16w2_console_time_zone_browser.py (ABSTRACTGATEWAY_BROWSER_TESTS=1,
playwright-core from ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES or abstractcode/web/node_modules);
the gateway runs from this checkout with a scratch HOME and data dir, no provider keys, on a
free loopback port. R18_SHOTS=<dir> also writes light/dark shots (1440/834/390).
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
SCRIPT = HERE / "browser" / "r18_spoken_language.mjs"
ALICE = "r18-alice-browser-0001"


@pytest.fixture()
def spoken_gateway(tmp_path: Path):
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
        yield base, admin
    finally:
        _stop(proc)


def test_spoken_language_in_preferences_and_on_the_multimodal_page(spoken_gateway) -> None:
    base, admin = spoken_gateway
    node = require_node()
    modules = _playwright_modules()
    argv = [node, str(SCRIPT), base, ALICE, admin, str(modules)]
    shots = os.getenv("R18_SHOTS", "").strip()
    if shots:
        argv.append(shots)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=900, check=False)
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-4000:])
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    # 32 behaviour checks + 12 layout checks (3 widths x 2 themes x 2 surfaces).
    assert out["checks"] >= 44, out["checks"]
