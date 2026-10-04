"""Round 11 (DESIGN.md R11.1 FINAL / R11.2) console Workspaces modals in a real browser, on the
gateway's own routes: "Eligible workspaces" (the kit WorkspaceChooser at level "gateway": posture,
caps, built-in refusals, one PUT per change, a refused path shows the gateway's sentence + "Not
saved.") and the per-account modal from the workspace icon on EVERY Accounts row (level "account":
"Gateway: <gateway_summary>" verbatim, "Follow the gateway policy" ON = {configured:false}, a mode
above the cap disabled with the kit tooltip "The gateway allows this workspace read-only" and
reachable by keyboard, a path outside the eligible set refused inline), the entity's and the
admin's own modals, and a non-admin's own (`me`). The checks live in tests/browser/r11w2.mjs.

Opt-in like test_gateway_console_browser_r9w2.py (ABSTRACTGATEWAY_BROWSER_TESTS=1, playwright-core
from ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES or the monorepo's abstractcode/web/node_modules); the
gateway runs from this checkout with a scratch HOME and data dir, no provider keys, no tray, on a
free loopback port.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from node_requirement import require_node
from test_gateway_console_browser_state_toggles import (
    _SEED_ENTITY,
    _call,
    _free_port,
    _playwright_modules,
    _start,
    _stop,
)

pytestmark = pytest.mark.e2e

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "browser" / "r11w2.mjs"
ALICE = "alice-r11w2-browser-token-01"


def seed(base: str, admin: str) -> None:
    assert _call(base, "POST", "/host/first-run", admin, {"outcome": "skipped"})[0] == 200
    for body in (
        {"user_id": "alice", "roles": ["user"], "token": ALICE, "email": "alice@fastmail.com"},
        {"user_id": "bob", "roles": ["user"], "token": "bob-r11w2-browser-token-001"},
    ):
        code, out = _call(base, "POST", "/admin/users", admin, body)
        assert code == 200, out


@pytest.fixture()
def r11_gateway(tmp_path: Path):
    if os.getenv("ABSTRACTGATEWAY_BROWSER_TESTS", "").strip() not in {"1", "true", "yes"}:
        pytest.skip("browser test: set ABSTRACTGATEWAY_BROWSER_TESTS=1 (needs playwright-core + Chromium)")
    port = _free_port()
    home, data, dirs = tmp_path / "home", tmp_path / "data", (tmp_path / "dirs").resolve()
    (home / "tmp").mkdir(parents=True)
    data.mkdir()
    for name in ("projects", "notes", "secrets", "lab", "pictures"):
        (dirs / name).mkdir(parents=True)
    env = {
        "HOME": str(home), "TMPDIR": str(home / "tmp"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join([str(HERE.parent / "src")] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]),
        "PYTHONUNBUFFERED": "1", "LANG": "en_US.UTF-8", "HF_HOME": str(home / "hf"),
        "ABSTRACTGATEWAY_DATA_DIR": str(data), "ABSTRACTGATEWAY_USER_AUTH": "1", "ABSTRACTGATEWAY_RUNNER": "0",
        "ABSTRACTGATEWAY_ALLOWED_ORIGINS": f"http://127.0.0.1:{port},http://localhost:{port}",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", "HF_HUB_OFFLINE": "1", "NO_COLOR": "1",
    }
    seeded = subprocess.run([sys.executable, "-c", _SEED_ENTITY, str(data)], env=env, capture_output=True, text=True, timeout=120)
    assert seeded.returncode == 0, seeded.stderr[-3000:]
    log = tmp_path / "gateway.log"
    base = f"http://127.0.0.1:{port}"
    proc = _start(port, env, log)
    try:
        admin = re.findall(r"Gateway admin token: (\S+)$", log.read_text(), flags=re.M)[-1]
        seed(base, admin)
        yield base, admin, dirs
    finally:
        _stop(proc)


def test_console_r11_workspaces_modals(r11_gateway) -> None:
    node = require_node()
    modules = _playwright_modules()
    base, admin, dirs = r11_gateway
    proc = subprocess.run([node, str(SCRIPT), base, admin, str(modules), str(dirs), ALICE, "real"], capture_output=True, text=True, timeout=600, check=False)
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-4000:])
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["mode"] == "real"
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 44
