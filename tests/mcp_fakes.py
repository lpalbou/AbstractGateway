"""Tiny fake MCP servers for gateway tests: a strict Streamable HTTP server (session id,
notifications/initialized required, paginated tools/list, optional bearer check) and a stdio
script with the same rules."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

TOOLS = [
    {"name": "search_docs", "description": "Search the documentation."},
    {"name": "read_page", "description": "Read one page."},
    {"name": "list_spaces", "description": "List the spaces."},
]


class FakeHttpMcp:
    def __init__(self, *, require_bearer: Optional[str] = None) -> None:
        self.require_bearer = require_bearer
        self.log: List[Dict[str, Any]] = []
        self.sessions: Dict[str, bool] = {}
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
                fake.log.append({"method": req.get("method"), "session": sid, "authorization": self.headers.get("Authorization")})
                if fake.require_bearer and self.headers.get("Authorization") != f"Bearer {fake.require_bearer}":
                    self._json(401, {"error": "unauthorized"})
                    return
                method, rid = req.get("method"), req.get("id")
                if method == "initialize":
                    new = f"s{len(fake.sessions) + 1}"
                    fake.sessions[new] = False
                    self._json(200, {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                               "serverInfo": {"name": "fake-docs", "version": "2.1.0"}}}, {"MCP-Session-Id": new})
                    return
                if sid not in fake.sessions:
                    self._json(400, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "missing session"}})
                    return
                if rid is None:
                    if method == "notifications/initialized":
                        fake.sessions[sid] = True
                    self._json(202)
                    return
                if not fake.sessions[sid]:
                    self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "not initialized"}})
                    return
                if method == "tools/list":
                    cursor = (req.get("params") or {}).get("cursor")
                    result = {"tools": TOOLS[:2], "nextCursor": "p2"} if cursor is None else {"tools": TOOLS[2:]}
                    self._json(200, {"jsonrpc": "2.0", "id": rid, "result": result})
                    return
                self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeHttpMcp":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


STDIO_SCRIPT = r'''
import json, os, sys
TOOLS = [{"name": "add", "description": "Add two integers."}, {"name": "echo", "description": "Echo a text."}]
pidfile = os.environ.get("FAKE_PIDFILE") or (sys.argv[1] if len(sys.argv) > 1 else "")
if pidfile:
    open(pidfile, "w").write(str(os.getpid()))
mode = sys.argv[2] if len(sys.argv) > 2 else ""
ready = False
def send(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    req = json.loads(line)
    m, rid = req.get("method"), req.get("id")
    if mode == "hang":
        continue
    if m == "initialize":
        if mode == "refuse":
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Unsupported protocol version"}}); continue
        send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
              "serverInfo": {"name": "fake-stdio", "version": "0.3"}, }}); continue
    if rid is None:
        ready = ready or m == "notifications/initialized"; continue
    if not ready:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "not initialized"}}); continue
    if m == "tools/list":
        c = (req.get("params") or {}).get("cursor")
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS[:1], "nextCursor": "n"} if c is None else {"tools": TOOLS[1:]}}); continue
    send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
'''


def write_stdio_fake(dir_: Path) -> Path:
    path = Path(dir_) / "fake_mcp_server.py"
    path.write_text(STDIO_SCRIPT, encoding="utf-8")
    return path
