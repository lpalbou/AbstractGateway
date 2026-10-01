"""The Accounts page in a real browser (DESIGN-v3 §1, §2, §3.2; adversary A1/A2/A3/A5): the table never
scrolls sideways (1280-2560 px: no page overflow, no inner horizontal scroller, the Actions cell inside the
table; below the computed breakpoint the rows are flat cards), only the actions that apply are rendered
(no disabled row buttons, no reasons paragraph, no Delete; menu items per kind), Archive asks inline and
Unarchive brings the account back inactive, "Show archived" lists archived rows, and an entity's Email
opens the same account email UI on /accounts/<id>/email.

Opt-in like test_gateway_console_browser_state_toggles.py (ABSTRACTGATEWAY_BROWSER_TESTS=1, playwright-core
from ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES or the monorepo's abstractcode/web/node_modules); the gateway
runs from this checkout with a scratch HOME and data dir, no provider keys, on a free loopback port.
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
SCRIPT = HERE / "browser" / "accounts.mjs"
LONG_ID = "alexandra-konstantinopoulou-research-laboratory"
LONG_MAIL = "alexandra.konstantinopoulou@very-long-research-institute-department.example.org"


@pytest.fixture()
def accounts_gateway(tmp_path: Path):
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
        for body in (
            {"user_id": "alice", "roles": ["user"], "token": "alice-accounts-browser-01", "email": "alice@fastmail.com"},
            {"user_id": "bob", "roles": ["user"], "token": "bob-accounts-browser-0001"},
            {"user_id": LONG_ID, "roles": ["user"], "token": "long-accounts-browser-01", "email": LONG_MAIL},
            {"user_id": "dave", "roles": ["user"], "token": "dave-accounts-browser-01", "email": "dave@example.org"},
        ):
            code, out = _call(base, "POST", "/admin/users", admin, body)
            assert code == 200, out
        code, out = _call(base, "POST", "/admin/accounts/dave/archive", admin)
        assert code == 200 and out.get("archived") is True, out
        _stop(proc)
        seeded = subprocess.run([sys.executable, "-c", _SEED_ENTITY, str(data)], env=env, capture_output=True, text=True, timeout=120)
        assert seeded.returncode == 0, seeded.stderr[-3000:]
        proc = _start(port, env, log)
        admin = re.findall(r"Gateway admin token: (\S+)$", log.read_text(), flags=re.M)[-1]
        yield base, admin
    finally:
        _stop(proc)


def test_console_accounts_in_a_browser(accounts_gateway) -> None:
    node = require_node()
    modules = _playwright_modules()
    base, admin = accounts_gateway
    proc = subprocess.run([node, str(SCRIPT), base, admin, str(modules), LONG_ID], capture_output=True, text=True, timeout=600, check=False)
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-4000:])
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 40
