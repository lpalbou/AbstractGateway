"""The Workspaces page and the Runtimes account filter in a real browser (DESIGN.md round 8, R8.2):
"Workspaces" right after Accounts in the sidebar, nothing of the policy left on Accounts, the one-line
summary, the segmented access mode, the launch-folder trust switch, folder rows that apply on BLUR
(an invalid path says why and is NOT saved; the API changes only after the blur), per-account
policies (Own policy on / inline confirm to drop), a user's own policy, the Accounts Workspace icon
and Runtime link, and the Runtimes chip (`#runtimes?account=<id>`, survives a reload, × clears it).

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
SCRIPT = HERE / "browser" / "workspaces.mjs"
ALICE = "alice-workspaces-browser-01"


@pytest.fixture()
def workspaces_gateway(tmp_path: Path):
    if os.getenv("ABSTRACTGATEWAY_BROWSER_TESTS", "").strip() not in {"1", "true", "yes"}:
        pytest.skip("browser test: set ABSTRACTGATEWAY_BROWSER_TESTS=1 (needs playwright-core + Chromium)")
    port = _free_port()
    home, data, folders = tmp_path / "home", tmp_path / "data", (tmp_path / "folders").resolve()
    (home / "tmp").mkdir(parents=True)
    data.mkdir()
    for name in ("projects", "notes", "private", "alice-lab", "secrets", "extra"):
        (folders / name).mkdir(parents=True)
    (folders / "a-file.txt").write_text("not a folder\n")
    env = {
        "HOME": str(home), "TMPDIR": str(home / "tmp"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join([str(HERE.parent / "src")] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]),
        "PYTHONUNBUFFERED": "1", "LANG": "en_US.UTF-8", "HF_HOME": str(home / "hf"),
        "ABSTRACTGATEWAY_DATA_DIR": str(data), "ABSTRACTGATEWAY_USER_AUTH": "1",
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
        assert _call(base, "POST", "/host/first-run", admin, {"outcome": "skipped"})[0] == 200
        for body in (
            {"user_id": "alice", "roles": ["user"], "token": ALICE, "email": "alice@fastmail.com"},
            {"user_id": "bob", "roles": ["user"], "token": "bob-workspaces-browser-001"},
        ):
            code, out = _call(base, "POST", "/admin/users", admin, body)
            assert code == 200, out
        code, out = _call(base, "POST", "/admin/runtime-config", admin, {
            "workspace_allowed_paths": [str(folders / "projects"), str(folders / "notes")],
            "workspace_blocked_paths": [str(folders / "secrets")],
        })
        assert code == 200, out
        code, out = _call(base, "PUT", "/admin/user-workspace-policy?tenant_id=default&user_id=alice", admin, {"policy": {
            "mode": "whitelist", "trust_client_launch_folder": False,
            "workspace_allowed_paths": [str(folders / "alice-lab")], "workspace_blocked_paths": [str(folders / "private")],
        }})
        assert code == 200 and out.get("customized") is True, out
        yield base, admin, folders
    finally:
        _stop(proc)


def test_console_workspaces_in_a_browser(workspaces_gateway) -> None:
    node = require_node()
    modules = _playwright_modules()
    base, admin, folders = workspaces_gateway
    proc = subprocess.run([node, str(SCRIPT), base, admin, str(modules), str(folders), ALICE], capture_output=True, text=True, timeout=600, check=False)
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-4000:])
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 40
