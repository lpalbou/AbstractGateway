"""Root backlog 1041: the console's Triggers page in a real browser (tests/browser/triggers_console.mjs).

A hermetic scratch gateway (scratch HOME and data dir, its own port, never :8080) serves the console;
the trigger-connection routes are served to the page by the script's Playwright routing with the
exact W2 contract shapes, so the page is proven whether or not this gateway build serves them yet.

Opt-in like the other browser tests (Chromium through playwright-core):

    ABSTRACTGATEWAY_BROWSER_TESTS=1 python -m pytest tests/test_gateway_console_browser_triggers.py -q

`ABSTRACTGATEWAY_TRIGGERS_SHOTS=<dir>` keeps the screenshots.
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

pytestmark = pytest.mark.basic

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "browser" / "triggers_console.mjs"


@pytest.fixture()
def triggers_gateway(tmp_path: Path):
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
        m = re.findall(r"Gateway admin token: (\S+)$", log.read_text(), flags=re.M)
        assert m, log.read_text()[-2000:]
        admin = m[-1]
        assert _call(base, "POST", "/host/first-run", admin, {"outcome": "skipped"})[0] == 200
        yield base, admin
    finally:
        _stop(proc)


def test_triggers_page_in_a_browser(triggers_gateway) -> None:
    node = require_node()
    modules = _playwright_modules()
    base, admin = triggers_gateway
    shots = os.getenv("ABSTRACTGATEWAY_TRIGGERS_SHOTS", "").strip()
    proc = subprocess.run(
        [node, str(SCRIPT), base, admin, str(modules)] + ([shots] if shots else []),
        capture_output=True, text=True, timeout=900, check=False,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 45, out["checks"]
