"""R16.5 in a real browser: the creator configures their entity (operator ruling 2026-10-08).

alice (member) created Nova in her runtime; bob (member) did not; Vega was created by the admin in
the admin's runtime. The script (tests/browser/r16w6_creator.mjs) proves: alice's Nova row offers
Manage, Archive and a usable Active switch; her Manage dialog shows the mind and voice pickers,
the tools matrix (execute_command refused with the served sentence as the kit tooltip) and editable
instructions, while sleep/wake, freeze and the memory rebuild stay hidden; a tool tick saves; an
unoffered model is refused naming the offered set and an offered one saves; she suspends, resumes,
archives, finds it under Show archived, unarchives (back inactive) and turns it on; bob never sees
Nova and his write answers like a missing entity; the admin has every control on Vega.

Opt-in like the other browser tests: ABSTRACTGATEWAY_BROWSER_TESTS=1 (playwright-core + Chromium,
ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES or the monorepo's abstractcode/web/node_modules). The
gateway runs from this checkout with a scratch HOME and data dir, no provider keys, on a free
loopback port in 18350-18359; the mind list is an endpoint profile with a static model list
(nothing is probed, no model loads). Shots: ABSTRACTGATEWAY_BROWSER_SHOTS=<dir>.
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
SCRIPT = HERE / "browser" / "r16w6_creator.mjs"

_SEED = """
import copy, json, os, sys
from pathlib import Path
from abstractmemory import DEFAULT_SPARK_TEMPLATE
from abstractgateway.entities import EntityRegistry
from abstractgateway.users import GatewayUserRegistry

class _Embedder:  # fixed vectors: born without any model
    model = "browser-test-embedder"
    def embed_texts(self, texts):
        return [[0.25] * 8 for _ in texts]

data = Path(sys.argv[1])
users = GatewayUserRegistry()
tokens = {}
for uid in ("alice", "bob"):
    _r, tokens[uid] = users.create_user(user_id=uid, roles=["user"])
def born(plane, name, creator):
    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE)); spark["name"] = name; spark["spark"] = 1
    plane.mkdir(parents=True, exist_ok=True)
    EntityRegistry(data_dir=plane, embedder_factory=lambda: _Embedder(), users_registry_path=data / "auth" / "users.json").create(
        name=name, spark=spark, created_by={"tenant_id": "default", "user_id": creator})
born(data / "users" / "default" / "alice" / "runtime", "Nova", "alice")
born(data, "Vega", "admin")
print(json.dumps(tokens))
"""


def _playwright_modules() -> Path:
    raw = os.getenv("ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES", "").strip()
    p = Path(raw) if raw else HERE.parents[2] / "abstractcode" / "web" / "node_modules"
    if not (p / "playwright-core" / "package.json").is_file():
        pytest.fail(f"playwright-core not found under {p}: set ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES", pytrace=False)
    return p


def _free_port() -> int:
    for port in range(18350, 18360):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    pytest.fail("no free port in 18350-18359", pytrace=False)


def _call(base: str, method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{base}/api/gateway{path}", method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture()
def creator_gateway(tmp_path: Path):
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
        "OLLAMA_BASE_URL": "http://127.0.0.1:9/", "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
    }
    seeded = subprocess.run([sys.executable, "-c", _SEED, str(data)], env=env, capture_output=True, text=True, timeout=120)
    assert seeded.returncode == 0, seeded.stderr[-3000:]
    tokens = json.loads(seeded.stdout.strip().splitlines()[-1])
    log = tmp_path / "gateway.log"
    out = open(log, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "abstractgateway", "serve", "--host", "127.0.0.1", "--port", str(port), "--print-token", "--no-tray"],
        env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 120
        while True:
            if proc.poll() is not None:
                pytest.fail(f"gateway exited: {log.read_text()[-3000:]}", pytrace=False)
            try:
                with urllib.request.urlopen(f"{base}/api/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                if time.time() > deadline:
                    pytest.fail(f"gateway did not come up: {log.read_text()[-3000:]}", pytrace=False)
                time.sleep(0.5)
        m = re.findall(r"Gateway admin token: (\S+)$", log.read_text(), flags=re.M)
        assert m, log.read_text()[-2000:]
        tokens["admin"] = m[-1]
        assert _call(base, "POST", "/host/first-run", tokens["admin"], {"outcome": "skipped"})[0] == 200
        status, _ = _call(base, "POST", "/config/provider-endpoint-profiles", tokens["admin"], {
            "id": "demo", "display_name": "Demo", "provider_family": "openai-compatible",
            "base_url": "http://127.0.0.1:9/v1", "allowed_models": ["demo-small", "demo-large"], "scope": "gateway",
        })
        assert status == 200
        tok_file = tmp_path / "tokens.json"
        tok_file.write_text(json.dumps(tokens))
        os.chmod(tok_file, 0o600)
        yield base, tok_file
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_the_creator_configures_their_entity_in_a_browser(creator_gateway) -> None:
    node = require_node()
    modules = _playwright_modules()
    base, tok_file = creator_gateway
    shots = os.getenv("ABSTRACTGATEWAY_BROWSER_SHOTS", "").strip()
    args = [node, str(SCRIPT), base, str(tok_file), str(modules)] + ([shots] if shots else [])
    proc = subprocess.run(args, capture_output=True, text=True, timeout=900, check=False)
    assert proc.returncode == 0, proc.stderr[-4000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 45
