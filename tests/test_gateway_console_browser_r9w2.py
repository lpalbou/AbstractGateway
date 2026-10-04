"""Round 9 (DESIGN.md R9.2) console in a real browser: no Workspaces page (`#workspaces` folds into
Accounts), the admin-only "Shared workspace & allowed folders" modal (every row path-checked and
applied on blur, switches apply at once, a reopen shows the stored values), the per-account
"Workspace folders" modal from the folder icon on EVERY Accounts row (user, entity, the admin's
own; the kit WorkspaceChooser; My folders only while Allow any folder is on; Follow the gateway
policy), a non-admin's own modal, and the kit tooltip (150 ms delay, keyboard focus, Escape, the
R9.2 sentences, inside the viewport at 390 px).

Opt-in like test_gateway_console_browser_state_toggles.py (ABSTRACTGATEWAY_BROWSER_TESTS=1,
playwright-core from ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES or the monorepo's
abstractcode/web/node_modules); the gateway runs from this checkout with a scratch HOME and data dir,
no provider keys, on a free loopback port.
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
SCRIPT = HERE / "browser" / "r9w2.mjs"
ALICE = "alice-r9w2-browser-token-01"


def seed(base: str, admin: str, folders: Path) -> None:
    """Accounts alice (user) and bob; the gateway allows <folders>/projects and never <folders>/secrets."""
    assert _call(base, "POST", "/host/first-run", admin, {"outcome": "skipped"})[0] == 200
    for body in (
        {"user_id": "alice", "roles": ["user"], "token": ALICE, "email": "alice@fastmail.com"},
        {"user_id": "bob", "roles": ["user"], "token": "bob-r9w2-browser-token-001"},
    ):
        code, out = _call(base, "POST", "/admin/users", admin, body)
        assert code == 200, out
    code, out = _call(base, "PUT", "/workspace/policy", admin, {
        "allowed_folders": [str(folders / "projects")], "never_allowed": [str(folders / "secrets")], "allow_any_folder": False,
    })
    assert code == 200, out


@pytest.fixture()
def r9_gateway(tmp_path: Path):
    if os.getenv("ABSTRACTGATEWAY_BROWSER_TESTS", "").strip() not in {"1", "true", "yes"}:
        pytest.skip("browser test: set ABSTRACTGATEWAY_BROWSER_TESTS=1 (needs playwright-core + Chromium)")
    port = _free_port()
    home, data, folders = tmp_path / "home", tmp_path / "data", (tmp_path / "folders").resolve()
    (home / "tmp").mkdir(parents=True)
    data.mkdir()
    for name in ("projects", "notes", "secrets", "alice-lab", "shared"):
        (folders / name).mkdir(parents=True)
    (folders / "a-file.txt").write_text("not a folder\n")
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
        seed(base, admin, folders)
        yield base, admin, folders
    finally:
        _stop(proc)


def test_console_r9_workspace_modals_and_tooltips(r9_gateway) -> None:
    node = require_node()
    modules = _playwright_modules()
    base, admin, folders = r9_gateway
    proc = subprocess.run([node, str(SCRIPT), base, admin, str(modules), str(folders), ALICE], capture_output=True, text=True, timeout=600, check=False)
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-4000:])
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 45
