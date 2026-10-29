"""A fake OpenAI-compatible server for the model host tests (no model, no network).

`FakeOpenAIServer(delay_s=...)` answers `/v1/models` and `/v1/chat/completions` (plain and
`stream: true` server-sent events) on 127.0.0.1. Each reply says `reply:<last user text>`; a
streamed reply is sent as `chunks` pieces `delay_s` apart, so a test can act mid-generation.
`requests` records every chat request body.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List


class FakeOpenAIServer:
    def __init__(self, *, delay_s: float = 0.0, chunks: int = 4) -> None:
        self.delay_s = float(delay_s)
        self.chunks = int(chunks)
        self.requests: List[Dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # Headers and body are two writes: without this, Nagle + delayed ACK add ~40 ms on Linux.
            disable_nagle_algorithm = True

            def log_message(self, *args: Any) -> None:  # quiet
                return

            def _json(self, status: int, doc: Any) -> None:
                body = json.dumps(doc).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/").endswith("/models"):
                    return self._json(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
                return self._json(404, {"error": {"message": "not here"}})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                doc = json.loads(self.rfile.read(length) or b"{}")
                if not self.path.rstrip("/").endswith("/chat/completions"):
                    return self._json(404, {"error": {"message": "not here"}})
                outer.requests.append(doc)
                last = ""
                for m in doc.get("messages") or []:
                    if m.get("role") == "user":
                        c = m.get("content")
                        last = c if isinstance(c, str) else json.dumps(c)
                text = f"reply:{last}"
                if not doc.get("stream"):
                    if outer.delay_s:
                        time.sleep(outer.delay_s * outer.chunks)
                    return self._json(200, {
                        "id": "x", "object": "chat.completion", "created": int(time.time()), "model": doc.get("model"),
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                     "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                n = max(1, outer.chunks)
                size = max(1, -(-len(text) // n))
                pieces = [text[i:i + size] for i in range(0, len(text), size)]
                try:
                    for piece in pieces:
                        if outer.delay_s:
                            time.sleep(outer.delay_s)
                        chunk = {"id": "x", "object": "chat.completion.chunk", "model": doc.get("model"),
                                 "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                        self.wfile.flush()
                    end = {"id": "x", "object": "chat.completion.chunk", "model": doc.get("model"),
                           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
                    self.wfile.write(f"data: {json.dumps(end)}\n\ndata: [DONE]\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                self.close_connection = True

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = int(self._server.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-openai", daemon=True)

    def __enter__(self) -> "FakeOpenAIServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
