"""The entity Manage modal in a real browser (round 3 DESIGN-v3 §12): it opens as the kit's
af-modal af-modal--wide over the Accounts page (role=dialog, aria-modal, blurred backdrop, focus
trapped, Esc and backdrop close, focus back on the row, no navigation, the Accounts table stays
behind), its on/off states are switches labelled by the feature (no Wake/Sleep/Stop verb buttons),
it has no Save buttons and nothing that duplicates the Accounts row, a failed save shows the API
message, labels keep the type scale, and on a phone it is a full-screen sheet.

Opt-in like the other browser test: ABSTRACTGATEWAY_BROWSER_TESTS=1 (playwright-core + Chromium,
ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES or the monorepo's abstractcode/web/node_modules). The
gateway runs from this checkout with a scratch HOME and data dir, no provider keys, on a free
loopback port in 18340-18349; one entity (castor) is born offline with a fixed-vector embedder.
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
SCRIPT = HERE / "browser" / "entity_manage.mjs"


def _playwright_modules() -> Path:
    raw = os.getenv("ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES", "").strip()
    p = Path(raw) if raw else HERE.parents[2] / "abstractcode" / "web" / "node_modules"
    if not (p / "playwright-core" / "package.json").is_file():
        pytest.fail(f"playwright-core not found under {p}: set ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES", pytrace=False)
    return p


def _free_port() -> int:
    for port in range(18340, 18350):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    pytest.fail("no free port in 18340-18349", pytrace=False)


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


_SEED_ENTITY = """
import copy, sys
from pathlib import Path
from abstractmemory import DEFAULT_SPARK_TEMPLATE
from abstractgateway.entities import EntityRegistry

class _Embedder:  # a fixed-vector embedder: the entity is born vectored without any model
    model = "browser-test-embedder"
    def embed_texts(self, texts):
        return [[0.25] * 8 for _ in texts]

data = Path(sys.argv[1])
spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE)); spark["name"] = "Castor"; spark["spark"] = 1
EntityRegistry(data_dir=data, embedder_factory=lambda: _Embedder(), users_registry_path=data / "auth" / "users.json").create(name="Castor", spark=spark)
"""


def _start(port: int, env: dict, log: Path) -> subprocess.Popen:
    out = open(log, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "abstractgateway", "serve", "--host", "127.0.0.1", "--port", str(port), "--print-token"],
        env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"gateway exited: {log.read_text()[-3000:]}", pytrace=False)
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            time.sleep(0.5)
    proc.kill()
    pytest.fail(f"gateway did not come up: {log.read_text()[-3000:]}", pytrace=False)


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture()
def entity_gateway(tmp_path: Path):
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
    seeded = subprocess.run([sys.executable, "-c", _SEED_ENTITY, str(data)], env=env, capture_output=True, text=True, timeout=120)
    assert seeded.returncode == 0, seeded.stderr[-3000:]
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


def test_entity_manage_modal_in_a_browser(entity_gateway) -> None:
    node = require_node()
    modules = _playwright_modules()
    from abstractgateway import console_islands_sync

    kit = console_islands_sync.locate_kit()
    base, admin = entity_gateway
    proc = subprocess.run([node, str(SCRIPT), base, admin, str(modules), str(kit or "")], capture_output=True, text=True, timeout=600, check=False)
    assert proc.returncode == 0, proc.stderr[-4000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 50
