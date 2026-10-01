"""The console Sandbox on the kit chat island, in a real browser (DESIGN-v3 §8, round 3).

The island (islands `mountSandboxChat`: panel-chat's thread + composer) must be mounted, every
output mode must still be a button around it, a text send must go through POST
/api/gateway/sandbox/generate and render the reply in the thread, an unconfigured mode must say
why it cannot send, and Clear must empty the thread — against a hermetic gateway started here
whose input.text route points at a fake OpenAI-compatible server (no model, loopback only).

Opt-in (it needs Chromium through playwright-core): ABSTRACTGATEWAY_BROWSER_TESTS=1, with
ABSTRACTGATEWAY_PLAYWRIGHT_NODE_MODULES pointing at a node_modules that holds playwright-core
(default: the monorepo's abstractcode/web/node_modules).
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from node_requirement import require_node
from test_gateway_console_browser_state_toggles import _call, _playwright_modules, _start, _stop

pytestmark = pytest.mark.e2e

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "browser" / "sandbox_chat.mjs"


def _free_port(start: int, stop: int) -> int:
    for port in range(start, stop):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    pytest.fail(f"no free port in {start}-{stop - 1}", pytrace=False)


class _FakeOpenAI(BaseHTTPRequestHandler):
    requests: list = []

    def log_message(self, *args) -> None:  # quiet
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
        return self._json(404, {"error": {"message": self.path}})

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0) or 0)
        req = json.loads(raw or b"{}")
        type(self).requests.append({"path": self.path, "body": req})
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": {"message": self.path}})
        last = next((m for m in reversed(req.get("messages") or []) if m.get("role") == "user"), {})
        content = last.get("content")
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        content = str(content or "").split("</runtime_metadata>")[-1].strip()
        return self._json(200, {
            "id": "chatcmpl-fake", "object": "chat.completion", "model": req.get("model") or "fake-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": f"**Fake reply** to: {content}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 7, "total_tokens": 16},
        })


@pytest.fixture()
def sandbox_gateway(tmp_path: Path):
    if os.getenv("ABSTRACTGATEWAY_BROWSER_TESTS", "").strip() not in {"1", "true", "yes"}:
        pytest.skip("browser test: set ABSTRACTGATEWAY_BROWSER_TESTS=1 (needs playwright-core + Chromium)")
    _FakeOpenAI.requests = []
    fake = ThreadingHTTPServer(("127.0.0.1", _free_port(18396, 18400)), _FakeOpenAI)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    port = _free_port(18393, 18396)
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
        admin = re.findall(r"Gateway admin token: (\S+)$", log.read_text(), flags=re.M)[-1]
        assert _call(base, "POST", "/host/first-run", admin, {"outcome": "skipped"})[0] == 200
        code, out = _call(base, "POST", "/config/provider-endpoint-profiles", admin, {
            "id": "fake", "display_name": "Fake", "provider_family": "openai-compatible",
            "base_url": f"http://127.0.0.1:{fake.server_address[1]}/v1", "api_key": "fake-key", "scope": "gateway", "capabilities": ["text"],
        })
        assert code == 200, out
        code, out = _call(base, "PUT", "/config/capability-defaults/input/text", admin, {"provider": "endpoint:fake", "model": "fake-model"})
        assert code == 200, out
        yield base, admin
    finally:
        _stop(proc)
        fake.shutdown()


def test_console_sandbox_chat_island_in_a_browser(sandbox_gateway) -> None:
    node = require_node()
    modules = _playwright_modules()
    base, admin = sandbox_gateway
    proc = subprocess.run([node, str(SCRIPT), base, admin, str(modules)], capture_output=True, text=True, timeout=600, check=False)
    assert proc.returncode == 0, proc.stderr[-4000:]
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["failures"] == [], out["failures"]
    assert out["checks"] >= 18
    # The send reached the fake model through the gateway (never an operator stack).
    assert any(r["path"].endswith("/chat/completions") for r in _FakeOpenAI.requests), _FakeOpenAI.requests
