"""R15 D1 (operator ruling: no "Advanced" disclosure anywhere) in a real browser and in the markup.

The web console hid five groups of fields behind "Advanced" disclosures; each is now a visible
section named for its content, with the names the console-TUI uses (COORD "R15 SECTION NAMES"):
"Recipients and limits", "Sign-in app", "Runtime and tenant", "Visible models", "Optional
configuration", and the Network page's origins + proxy trust inside "Reached through another
address?". tests/browser/r15_no_advanced.mjs opens every one of those surfaces (light and dark)
and asserts the sections are visible and no <summary>/visible text says "Advanced".
Opt-in like the other console browser tests (ABSTRACTGATEWAY_BROWSER_TESTS=1). R15_SHOTS=<dir>
writes the screenshots. The static markup test runs always.
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

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "browser" / "r15_no_advanced.mjs"
SECTION_NAMES = ("Recipients and limits", "Sign-in app", "Runtime and tenant", "Visible models", "Optional configuration")


def test_the_console_markup_has_no_advanced_disclosure() -> None:
    from abstractgateway.console import gateway_console_html

    page = gateway_console_html()  # the served page: markup, CSS and every console script
    for source in (page,):
        assert not re.search(r"<summary[^>]*>(?:<[^>]+>)*\s*Advanced", source), "an Advanced disclosure is back"
        assert "ui-net-proxy__title\">Advanced" not in source
    for name in SECTION_NAMES:
        assert f">{name}</h" in page, name
    assert "Other workflow types</h3>" in page


@pytest.fixture()
def admin_gateway(tmp_path: Path):
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
        "OLLAMA_BASE_URL": "http://127.0.0.1:9", "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
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


@pytest.mark.e2e
def test_every_former_advanced_group_is_a_visible_named_section(admin_gateway) -> None:
    base, admin = admin_gateway
    argv = [require_node(), str(SCRIPT), base, admin, str(_playwright_modules())]
    shots = os.getenv("R15_SHOTS", "").strip()
    if shots:
        argv.append(shots)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=900, check=False)
    assert proc.returncode == 0, (proc.stdout[-3000:], proc.stderr[-4000:])
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 36
