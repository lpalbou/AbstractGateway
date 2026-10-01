"""Fake MCP servers with a `tools/call` echo for the mcp-runs tests: Streamable HTTP (session id,
notifications/initialized required, optional bearer check, switchable initialize failure) and a stdio
script with the same rules. Every request is logged so tests can prove what reached the server."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

ECHO = {"name": "echo", "description": "Echo the text back.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "description": "What to echo."}}, "required": ["text"]}}


class FakeHttpEcho:
    def __init__(self, *, require_bearer: Optional[str] = None) -> None:
        self.require_bearer = require_bearer
        self.fail_initialize = False
        self.log: List[Dict[str, Any]] = []
        sessions: Dict[str, bool] = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                return

            def _json(self, status: int, body: Any = None, headers: Optional[Dict[str, str]] = None) -> None:
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self) -> None:  # noqa: N802
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                sid = self.headers.get("MCP-Session-Id")
                fake.log.append({"method": req.get("method"), "params": req.get("params"), "authorization": self.headers.get("Authorization")})
                if fake.require_bearer and self.headers.get("Authorization") != f"Bearer {fake.require_bearer}":
                    self._json(401, {"error": "unauthorized"})
                    return
                method, rid = req.get("method"), req.get("id")
                if method == "initialize":
                    if fake.fail_initialize:
                        self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "maintenance"}})
                        return
                    new = f"s{len(sessions) + 1}"
                    sessions[new] = False
                    self._json(200, {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                               "serverInfo": {"name": "fake-echo", "version": "1.0"}}}, {"MCP-Session-Id": new})
                    return
                if sid not in sessions:
                    self._json(400, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "missing session"}})
                    return
                if rid is None:
                    if method == "notifications/initialized":
                        sessions[sid] = True
                    self._json(202)
                    return
                if not sessions[sid]:
                    self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "not initialized"}})
                    return
                if method == "tools/list":
                    self._json(200, {"jsonrpc": "2.0", "id": rid, "result": {"tools": [ECHO]}})
                    return
                if method == "tools/call":
                    text = ((req.get("params") or {}).get("arguments") or {}).get("text")
                    self._json(200, {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": f"echo: {text}"}]}})
                    return
                self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def calls(self) -> List[Dict[str, Any]]:
        return [e for e in self.log if e["method"] == "tools/call"]

    def __enter__(self) -> "FakeHttpEcho":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


STDIO_SCRIPT = r'''
import json, sys
log = open(sys.argv[1], "a")
initialized = False
for line in sys.stdin:
    req = json.loads(line)
    rid, m = req.get("id"), req.get("method")
    log.write(json.dumps({"method": m, "params": req.get("params")}) + "\n"); log.flush()
    if rid is None:
        if m == "notifications/initialized":
            initialized = True
        continue
    if m == "initialize":
        res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-echo-stdio", "version": "1.0"}}
    elif not initialized:
        out = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "not initialized"}}
        sys.stdout.write(json.dumps(out) + "\n"); sys.stdout.flush(); continue
    elif m == "tools/list":
        res = {"tools": [TOOL]}
    elif m == "tools/call":
        res = {"content": [{"type": "text", "text": "echo: " + str(req["params"]["arguments"].get("text"))}]}
    else:
        out = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
        sys.stdout.write(json.dumps(out) + "\n"); sys.stdout.flush(); continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}) + "\n"); sys.stdout.flush()
'''


def write_stdio_server(folder: Path) -> tuple:
    """(script path, log path) of a stdio echo server that appends every request to the log."""
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "fake_echo_stdio.py"
    script.write_text("TOOL = " + json.dumps(ECHO) + "\n" + STDIO_SCRIPT, encoding="utf-8")
    log = folder / "stdio_requests.jsonl"
    return script, log


def stdio_calls(log: Path) -> List[Dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(x) for x in log.read_text().splitlines() if x.strip() and json.loads(x)["method"] == "tools/call"]
