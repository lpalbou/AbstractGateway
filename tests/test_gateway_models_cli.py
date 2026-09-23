"""Pins `abstractgateway models loaded|load|unload` (2026-09-23).

The shell face of the console's model-residency routes: it must call the SAME
routes (GET /api/gateway/models/loaded, POST /api/gateway/models/load|unload)
with the bearer token, and its exit code must be the gateway's verdict (a
200 answer carrying ok:false is a failure, a 401 is a failure).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List

import pytest

from abstractgateway.cli import main


def _server(answers: Dict[str, Any]):
    seen: List[Dict[str, Any]] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"null")
            seen.append({"method": self.command, "path": self.path, "body": body,
                         "auth": self.headers.get("Authorization")})
            status, payload = answers.get(self.path.split("?")[0], (404, {"detail": "nope"}))
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = _reply
        do_POST = _reply

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def _run(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    return exc.value.code


def test_models_cli_calls_the_console_routes_with_the_token(capsys):
    srv, seen = _server({
        "/api/gateway/models/loaded": (200, {"ok": True, "models": []}),
        "/api/gateway/models/load": (200, {"ok": True, "loaded_new": True}),
        "/api/gateway/models/unload": (200, {"ok": True, "unloaded": True}),
    })
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert _run(["models", "loaded", "--url", url, "--token", "t0k", "--provider", "ollama"]) == 0
        assert _run(["models", "load", "--url", url, "--token", "t0k", "--provider", "ollama", "--model", "m"]) == 0
        assert _run(["models", "unload", "--url", url, "--token", "t0k", "--provider", "ollama", "--model", "m", "--force"]) == 0
    finally:
        srv.shutdown()
        srv.server_close()
    assert [(s["method"], s["path"].split("?")[0]) for s in seen] == [
        ("GET", "/api/gateway/models/loaded"),
        ("POST", "/api/gateway/models/load"),
        ("POST", "/api/gateway/models/unload"),
    ]
    assert seen[0]["path"].endswith("?provider=ollama")
    assert all(s["auth"] == "Bearer t0k" for s in seen)
    assert seen[1]["body"] == {"provider": "ollama", "model": "m"}
    assert seen[2]["body"] == {"provider": "ollama", "model": "m", "force": True}


def test_models_cli_exit_code_is_the_gateway_verdict():
    srv, _ = _server({
        "/api/gateway/models/unload": (200, {"ok": False, "error": "model_residency unload failed"}),
        "/api/gateway/models/load": (401, {"detail": "Unauthorized"}),
    })
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert _run(["models", "unload", "--url", url, "--token", "x", "--provider", "mlx", "--model", "m"]) == 1
        assert _run(["models", "load", "--url", url, "--token", "x", "--provider", "mlx", "--model", "m"]) == 1
    finally:
        srv.shutdown()
        srv.server_close()
