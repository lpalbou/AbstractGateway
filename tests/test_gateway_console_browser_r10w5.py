"""R10.6 Apps page in a real browser: the Assistant card offers "Update to
0.14.0" beside Open with the gateway's tooltip in the kit tooltip (no native
title on the card's buttons), one click posts /apps/assistant/update; a
browser app's Update likewise; an app started outside the gateway shows
"Latest 0.8.0 · Started outside the gateway — update it where it was
installed" and no update action; 1440 and 390 px, dark and light, no
horizontal scroll. GET /apps and the update POST are answered by the page
route with a FIXTURE in the gateway's shape (nothing is installed).

Opt-in like test_gateway_console_browser_r9w2.py (ABSTRACTGATEWAY_BROWSER_TESTS=1,
playwright-core from ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES or the monorepo's
abstractcode/web/node_modules); the gateway runs from this checkout with a
scratch HOME and data dir, no provider keys, the runner off, on a free
loopback port. ABSTRACTGATEWAY_BROWSER_SHOTS=<dir> also writes screenshots.
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
SCRIPT = HERE / "browser" / "r10w5_apps.mjs"


@pytest.fixture()
def r10w5_gateway(tmp_path: Path):
    if os.getenv("ABSTRACTGATEWAY_BROWSER_TESTS", "").strip() not in {"1", "true", "yes"}:
        pytest.skip("browser test: set ABSTRACTGATEWAY_BROWSER_TESTS=1 (needs playwright-core + Chromium)")
    port = _free_port()
    home, data = tmp_path / "home", tmp_path / "data"
    (home / "tmp").mkdir(parents=True)
    data.mkdir()
    env = {
        "HOME": str(home), "TMPDIR": str(home / "tmp"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join([str(HERE.parent / "src")] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]),
        "PYTHONUNBUFFERED": "1", "LANG": "en_US.UTF-8", "HF_HOME": str(home / "hf"),
        "ABSTRACTGATEWAY_DATA_DIR": str(data), "ABSTRACTGATEWAY_USER_AUTH": "1", "ABSTRACTGATEWAY_RUNNER": "0",
        "ABSTRACTGATEWAY_ALLOWED_ORIGINS": f"http://127.0.0.1:{port},http://localhost:{port}",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", "HF_HUB_OFFLINE": "1", "NO_COLOR": "1",
    }
    log = tmp_path / "gateway.log"
    base = f"http://127.0.0.1:{port}"
    proc = _start(port, env, log)
    try:
        admin = re.findall(r"Gateway admin token: (\S+)$", log.read_text(), flags=re.M)[-1]
        assert _call(base, "POST", "/host/first-run", admin, {"outcome": "skipped"})[0] == 200
        yield base, admin
    finally:
        _stop(proc)


def test_console_apps_updates(r10w5_gateway) -> None:
    node = require_node()
    modules = _playwright_modules()
    base, admin = r10w5_gateway
    args = [node, str(SCRIPT), base, admin, str(modules)]
    shots = os.getenv("ABSTRACTGATEWAY_BROWSER_SHOTS", "").strip()
    args.append(shots or "")
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(args, capture_output=True, text=True, timeout=600, check=False)
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-4000:])
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 35
