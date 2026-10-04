"""R13.1 in a real browser: the console's Resources page (Gateway card) shows
"Gateway restarted at <time> after a hang — <reason>" with the stack dump's path,
read from the incident file the watchdog wrote before the previous process exited.

Opt-in like the other browser tests (ABSTRACTGATEWAY_BROWSER_TESTS=1, playwright-core
from ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES or abstractcode/web/node_modules); the
gateway runs from this checkout with a scratch HOME and data dir, the runner off, on a
free loopback port. ABSTRACTGATEWAY_BROWSER_SHOTS=<dir> also writes screenshots
(1440 / 390, dark and light).
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
SCRIPT = HERE / "browser" / "r13_last_restart.mjs"

INCIDENT = {
    "schema": "abstractgateway.watchdog_incident.v1",
    "at": "2026-10-04T19:23:57+00:00",
    "blocked_s": 30.9,
    "exit_code": 75,
    "pid": 12345,
    "limit_s": 30.0,
    "top_frame": {"file": "starlette/responses.py", "line": 245, "function": "listen_for_disconnect"},
    "gateway_frame": {"file": "abstractgateway/security/gateway_security.py", "line": 1443, "function": "__call__"},
    "requests_in_flight": [{"method": "POST", "path": "/api/gateway/runs/c45d73be/voice/tts/stream", "age_s": 31.2}],
    "loop_stack": [],
}


@pytest.fixture()
def r13_gateway(tmp_path: Path):
    if os.getenv("ABSTRACTGATEWAY_BROWSER_TESTS", "").strip() not in {"1", "true", "yes"}:
        pytest.skip("browser test: set ABSTRACTGATEWAY_BROWSER_TESTS=1 (needs playwright-core + Chromium)")
    port = _free_port()
    home, data = tmp_path / "home", tmp_path / "data"
    (home / "tmp").mkdir(parents=True)
    incidents = data / "incidents"
    incidents.mkdir(parents=True)
    (incidents / "watchdog-20261004T192357Z.json").write_text(
        json.dumps(dict(INCIDENT, dump_path=str(incidents / "watchdog-20261004T192357Z.threads.txt")))
    )
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


def test_console_resources_shows_the_last_watchdog_restart(r13_gateway) -> None:
    node = require_node()
    modules = _playwright_modules()
    base, admin = r13_gateway
    code, runner = _call(base, "GET", "/host/runner", admin)
    assert code == 200 and runner["last_hang"]["at"] == INCIDENT["at"], runner
    args = [node, str(SCRIPT), base, admin, str(modules)]
    shots = os.getenv("ABSTRACTGATEWAY_BROWSER_SHOTS", "").strip()
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
        args.append(shots)
    proc = subprocess.run(args, capture_output=True, text=True, timeout=600, check=False)
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-4000:])
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 20
