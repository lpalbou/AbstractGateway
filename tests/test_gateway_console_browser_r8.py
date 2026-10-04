"""Round 8 (R8.1) in a real browser: the Apps page (no disclosures; Continuum's settings behind
the gear on its card, "Apps settings" behind the toolbar gear, rows applied on blur / switch with
no Save), the Skills shelf row, the Workflows rows (not expandable; Export / Open / Archive as
icon buttons with tooltips in one row that never wraps; inline description editing for the
owner only) and "Email for everyone" (three switches in the card) — tests/browser/r8_console.mjs
against a hermetic gateway started here.

Opt-in like the other browser tests (Chromium through playwright-core):
ABSTRACTGATEWAY_BROWSER_TESTS=1. ABSTRACTGATEWAY_R8_SHOTS=<dir> also writes the screenshots
(1440 / 834 / 390, light and dark). The gateway runs from this checkout with a scratch HOME and
data dir, no provider keys, on a free loopback port >= 18120; users admin and alice, alice owning
one imported workflow (two versions).
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import urllib.request
import uuid
import zipfile
from pathlib import Path

import pytest
from node_requirement import require_node
from test_gateway_console_browser_state_toggles import _call, _free_port, _playwright_modules, _start, _stop
from test_gateway_workflow_registry_honesty import _min_flow

pytestmark = pytest.mark.e2e

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "browser" / "r8_console.mjs"
ALICE = "alice-r8-browser-token-0001"


def _bundle_bytes(bundle_id: str, version: str) -> bytes:
    buf = io.BytesIO()
    manifest = {
        "bundle_format_version": "1", "bundle_id": bundle_id, "bundle_version": version,
        "created_at": "2026-10-04T00:00:00Z",
        "entrypoints": [{"flow_id": "root", "name": "Weekly digest", "description": "From the file.", "interfaces": []}],
        "default_entrypoint": "root", "flows": {"root": "flows/root.json"}, "metadata": {},
    }
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("flows/root.json", json.dumps(_min_flow("root")))
    return buf.getvalue()


def _upload(base: str, token: str, name: str, content: bytes) -> tuple[int, dict]:
    boundary = uuid.uuid4().hex
    parts = []
    for field, value in (("overwrite", "false"), ("reload", "true")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"{base}/api/gateway/bundles/upload", method="POST", data=b"".join(parts),
        headers={"Authorization": f"Bearer {token}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture()
def r8_gateway(tmp_path: Path):
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
        assert _call(base, "POST", "/admin/users", admin, {"user_id": "alice", "roles": ["user"], "token": ALICE})[0] == 200
        for version in ("1.0.0", "1.1.0"):
            code, out = _upload(base, ALICE, f"r8-alice-wf@{version}.flow", _bundle_bytes("r8-alice-wf", version))
            assert code == 200 and out.get("loaded") is not False, out
        yield base, admin
    finally:
        _stop(proc)


def test_round8_console_in_a_browser(r8_gateway) -> None:
    node = require_node()
    modules = _playwright_modules()
    base, admin = r8_gateway
    shots = os.getenv("ABSTRACTGATEWAY_R8_SHOTS", "").strip()
    proc = subprocess.run(
        [node, str(SCRIPT), base, admin, ALICE, str(modules)] + ([shots] if shots else []),
        capture_output=True, text=True, timeout=900, check=False,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 60, out["checks"]
