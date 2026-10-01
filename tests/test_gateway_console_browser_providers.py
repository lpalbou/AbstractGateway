"""The merged Providers page in a real browser (DESIGN-v3 §7, §13.8; ADVERSARY A11): "Engines" is gone
from the sidebar, `#engines` (and a persisted "engines" tab) lands on Providers, every local provider
card carries the engine actions of 0.10.0's Engines page (Install / Start / Stop / Cancel / Continue /
Re-check, Browse models, Learn more) plus its connection, the remote presets show their connection
state with key fingerprints only, and the Available Providers table is still there.

Opt-in like the other browser test: ABSTRACTGATEWAY_BROWSER_TESTS=1 (playwright-core + Chromium,
ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES or the monorepo's abstractcode/web/node_modules). The
gateway runs from this checkout with a scratch HOME and data dir, no provider keys; GET /engines is
answered by a FIXTURE inside the page (page.route) so no local engine server is ever probed, and
every engine POST is intercepted and recorded, never sent: nothing is installed, started or stopped.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from pathlib import Path

import pytest
from node_requirement import require_node
from test_gateway_console_browser_state_toggles import _call, _playwright_modules, _start, _stop

pytestmark = pytest.mark.e2e

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "browser" / "providers.mjs"
FAKE_KEY = "sk-browser-fixture-not-a-real-key-0001"


def _free_port() -> int:
    for port in list(range(18382, 18390)) + list(range(18200, 18300)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    pytest.fail("no free loopback port", pytrace=False)


@pytest.fixture()
def providers_gateway(tmp_path: Path):
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
        for body in (
            {"id": "openai", "display_name": "OpenAI", "provider_family": "openai", "api_key": FAKE_KEY, "scope": "gateway", "enabled": True},
            {"id": "lmstudio-lan", "display_name": "LM Studio (studio Mac)", "provider_family": "lmstudio", "base_url": "http://192.168.1.20:1234/v1", "scope": "gateway", "enabled": True},
        ):
            code, out = _call(base, "POST", "/config/provider-endpoint-profiles", admin, body)
            assert code == 200, out
        yield base, admin
    finally:
        _stop(proc)


def test_providers_page_merges_engines_in_a_browser(providers_gateway) -> None:
    node = require_node()
    modules = _playwright_modules()
    base, admin = providers_gateway
    proc = subprocess.run([node, str(SCRIPT), base, admin, str(modules), FAKE_KEY], capture_output=True, text=True, timeout=600, check=False)
    assert proc.returncode == 0, proc.stderr[-4000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 40, out
