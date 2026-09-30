"""The web console in a real browser (DESIGN 2026-09-30 §2–§6): switches, the sign-in card and
its recovery flow, the users table, the account page, and the type scale measured with the kit's
checkLabelScale (computed styles: every label, switch label and field caption <= 15 px and
weight <= 600) — against a hermetic gateway started here.

Opt-in (it needs Chromium through playwright-core): ABSTRACTGATEWAY_BROWSER_TESTS=1, with
ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES pointing at a node_modules that holds playwright-core
(default: the monorepo's abstractcode/web/node_modules). Once opted in, a missing Playwright
fails. The gateway runs from this checkout with a scratch HOME and data dir, no provider keys,
the key-file vault, on a free loopback port >= 18120; users admin / alice (email address + a
mailbox record on .invalid hosts that never connects) / bob (no email address; an old per-user
override pins his agent email tools off).
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from node_requirement import require_node

pytestmark = pytest.mark.e2e

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "browser" / "state_toggles.mjs"
ALICE, BOB = "alice-browser-test-token-01", "bob-browser-test-token-001"


def _playwright_modules() -> Path:
    raw = os.getenv("ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES", "").strip()
    p = Path(raw) if raw else HERE.parents[2] / "abstractcode" / "web" / "node_modules"
    if not (p / "playwright-core" / "package.json").is_file():
        pytest.fail(f"playwright-core not found under {p}: set ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES", pytrace=False)
    return p


def _free_port() -> int:
    for port in range(18120, 18200):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    pytest.fail("no free port in 18120-18199", pytrace=False)


def _call(base: str, method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{base}/api/gateway{path}", method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture()
def scratch_gateway(tmp_path: Path):
    if os.getenv("ABSTRACTGATEWAY_BROWSER_TESTS", "").strip() not in {"1", "true", "yes"}:
        pytest.skip("browser test: set ABSTRACTGATEWAY_BROWSER_TESTS=1 (needs playwright-core + Chromium)")
    port = _free_port()
    home, data = tmp_path / "home", tmp_path / "data"
    (home / "tmp").mkdir(parents=True)
    data.mkdir()
    env = {
        "HOME": str(home), "TMPDIR": str(home / "tmp"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(HERE.parent / "src"), "PYTHONUNBUFFERED": "1", "LANG": "en_US.UTF-8",
        "ABSTRACTGATEWAY_DATA_DIR": str(data), "ABSTRACTGATEWAY_USER_AUTH": "1",
        "ABSTRACTGATEWAY_ALLOWED_ORIGINS": f"http://127.0.0.1:{port},http://localhost:{port}",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", "HF_HUB_OFFLINE": "1", "NO_COLOR": "1",
    }
    log = tmp_path / "gateway.log"
    with open(log, "wb") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "abstractgateway", "serve", "--host", "127.0.0.1", "--port", str(port), "--print-token"],
            env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 120
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"gateway exited: {log.read_text()[-3000:]}", pytrace=False)
            try:
                with urllib.request.urlopen(f"{base}/api/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.5)
        else:
            pytest.fail(f"gateway did not come up: {log.read_text()[-3000:]}", pytrace=False)
        m = re.findall(r"Gateway admin token: (\S+)$", log.read_text(), flags=re.M)
        assert m, log.read_text()[-2000:]
        admin = m[-1]
        assert _call(base, "POST", "/host/first-run", admin, {"outcome": "skipped"})[0] == 200
        assert _call(base, "POST", "/admin/users", admin, {"user_id": "alice", "roles": ["user"], "token": ALICE, "email": "alice@fastmail.com"})[0] == 200
        assert _call(base, "POST", "/admin/users", admin, {"user_id": "bob", "roles": ["user"], "token": BOB})[0] == 200
        code, out = _call(base, "PUT", "/me/email", ALICE, {
            "address": "alice@fastmail.com", "password": "not-a-real-password",
            "imap": {"host": "imap.alice.invalid", "port": 993, "security": "ssl"},
            "smtp": {"host": "smtp.alice.invalid", "port": 465, "security": "ssl"}, "test": False,
        })
        assert code == 200, out
        # An old per-user override (what the capabilities v3 migration pins): bob's agent email tools off.
        code, out = _call(base, "PUT", "/admin/users/bob/email", admin, {"agent_tools": False})
        assert code == 200, out
        yield base, admin
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_console_state_toggles_in_a_browser(scratch_gateway) -> None:
    node = require_node()
    modules = _playwright_modules()
    from abstractgateway import console_islands_sync

    kit = console_islands_sync.locate_kit()
    base, admin = scratch_gateway
    proc = subprocess.run(
        [node, str(SCRIPT), base, admin, ALICE, str(modules), str(kit or "")],
        capture_output=True, text=True, timeout=600, check=False,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 38
