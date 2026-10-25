"""Shared fakes of the push tests (framework backlog 1049): a fake relay on a scratch loopback port
and helpers that write attention events into a real user event stream."""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

TOKEN_A = "a" * 64
TOKEN_B = "b" * 64
REG = "0123456789abcdef"
WATCH_TOPIC = "ai.abstractframework.assistant.watchkitapp"
PHONE_TOPIC = "ai.abstractframework.assistant"


class FakeRelay:
    """`POST /v1/push` recorder; `answer(body) -> (status, payload)` decides the reply."""

    def __init__(self, answer: Optional[Callable[[Dict[str, Any]], Tuple[int, Dict[str, Any]]]] = None, topics: Tuple[str, ...] = ()) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.answer = answer or (lambda _b: (200, {"status": "sent"}))
        self.topics = topics
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def _reply(self, code: int, payload: Dict[str, Any]) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/v1/info":
                    self._reply(200, {"topics": list(relay.topics), "version": "test"})
                else:
                    self._reply(404, {})

            def do_POST(self) -> None:  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                relay.calls.append(body)
                code, payload = relay.answer(body)
                self._reply(code, payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeRelay":
        self._thread.start()
        return self

    def __exit__(self, *_a: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def raise_approval(stream: Any, n: int, *, run: str = "r") -> Dict[str, Any]:
    """An `attention.raised {kind: approval}` exactly as the runner writes it (`record_wait_raised`)."""
    from abstractgateway.events_stream import record_wait_raised

    item = {"run_id": f"{run}{n}", "wait_key": f"w{n}", "step_id": "s1", "kind": "approval", "wait_kind": "tool_approval",
            "record_key": f"step:{run}{n}:s1"}
    record_wait_raised(stream, item, session_id="sess")
    return item


def resolve_wait(stream: Any, item: Dict[str, Any]) -> None:
    from abstractgateway.events_stream import record_wait_seen

    record_wait_seen(stream, item, outcome="approved")
