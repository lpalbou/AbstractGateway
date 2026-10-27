"""Split mode: the API process and the runner process (opt-in, gateway load 2026-10-26).

`abstractgateway serve --no-runner` serves HTTP only; `abstractgateway runner` ticks the runs and
holds the model. Who does what:

| Work                                                   | combined `serve` | split: API process | split: runner process |
|--------------------------------------------------------|------------------|--------------------|-----------------------|
| HTTP API, console, SSE, `/api/health`                  | yes              | yes                | no                    |
| Run ticking (every plane's runner)                     | yes              | no                 | yes                   |
| Text model (runs and the `/v1` OpenAI-compatible API)  | yes              | no: forwards `/v1` | yes (model host)      |
| Mail watchers, feed polling, notification outbox       | yes              | no                 | yes                   |
| Push dispatcher                                        | yes              | no                 | yes                   |
| Telegram and agora bridges                             | yes              | no                 | yes                   |

The model host: the runner process serves AbstractCore's OpenAI-compatible app on a loopback port
(`127.0.0.1`, a port the system picks) and records `{url, pid, secret}` in
`<data dir>/split/model_host.json` (mode 0600). The API process's `/v1` reads that file and
forwards each admitted request there, streaming both ways; it never imports Core's server, so
it never loads a text model (one model at a time, framework rule). The secret is per runner
boot: a request without it is refused, so another local program cannot use the port to skip
the API's key and reach checks. No environment variable is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("abstractgateway.split")

ADDRESS_FILE = Path("split") / "model_host.json"
SECRET_HEADER = b"x-abstractgateway-split-secret"
ALLOW_OPEN_HEADER = b"x-abstractgateway-allow-unauthenticated"
_HOP_BY_HOP = frozenset({
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization", b"te", b"trailer",
    b"transfer-encoding", b"upgrade", b"host", b"content-length",
})
_RESPONSE_DROP = frozenset({b"connection", b"keep-alive", b"transfer-encoding"})

_said: set = set()
_said_lock = threading.Lock()


# ---- Process role ------------------------------------------------------------------------------

def role() -> str:
    """`combined` (one `serve`), `api` (`serve --no-runner`) or `runner` (`abstractgateway runner`)."""
    from .live_deltas import process_role

    return process_role()


def is_api_process() -> bool:
    from .live_deltas import ROLE_API

    return role() == ROLE_API


def mode() -> str:
    from .live_deltas import ROLE_COMBINED

    return "combined" if role() == ROLE_COMBINED else "split"


def background_work_here(what: str) -> bool:
    """Whether this process runs `what` (mail, push, a bridge): never the split API process, whose
    runner process runs them. Logged once per kind, so the operator sees where it went."""
    if not is_api_process():
        return True
    with _said_lock:
        first = what not in _said
        _said.add(what)
    if first:
        logger.info("split mode: %s runs in the runner process (`abstractgateway runner`), not here", what)
    return False


# ---- The address file --------------------------------------------------------------------------

def address_path(data_dir: Path) -> Path:
    return Path(data_dir) / ADDRESS_FILE


def _pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_address(data_dir: Path) -> Optional[Dict[str, Any]]:
    """The runner's model host `{url, pid, secret, started_at}`, or None (no file, unreadable, or
    its process is gone)."""
    try:
        doc = json.loads(address_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not doc.get("url") or not doc.get("secret"):
        return None
    if not _pid_alive(doc.get("pid")):
        return None
    return doc


def _write_address(data_dir: Path, doc: Dict[str, Any]) -> None:
    path = address_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, path)


def runner_process_snapshot(data_dir: Path) -> Dict[str, Any]:
    """What `/api/health` says about the split runner process (no address, no secret)."""
    doc = read_address(data_dir)
    if doc is None:
        return {"model_host": "down"}
    return {"model_host": "up", "pid": int(doc.get("pid") or 0)}


# ---- Runner side: the model host ---------------------------------------------------------------

def _header(scope: dict, name: bytes) -> Optional[bytes]:
    for k, v in scope.get("headers") or []:
        if bytes(k).lower() == name:
            return bytes(v)
    return None


async def _json(send, status: int, doc: Dict[str, Any]) -> None:
    body = json.dumps(doc).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class ModelHost:
    """AbstractCore's OpenAI-compatible app on a loopback port of the runner process."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.secret = secrets.token_urlsafe(32)
        self.port: Optional[int] = None
        self._server: Any = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> Optional[str]:
        return None if self.port is None else f"http://127.0.0.1:{self.port}"

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope.get("type") != "http":
            return
        given = _header(scope, SECRET_HEADER) or b""
        if not hmac.compare_digest(given, self.secret.encode()):
            return await _json(send, 403, {"error": {"message": "This port serves the gateway's API process only.",
                                                     "type": "permission_error", "code": "split_secret_required"}})
        allow_open = _header(scope, ALLOW_OPEN_HEADER) == b"1"
        headers = [(k, v) for k, v in scope.get("headers") or [] if bytes(k).lower() not in (SECRET_HEADER, ALLOW_OPEN_HEADER)]
        from .core_endpoint import read_settings
        from abstractruntime.integrations.abstractcore.server_facade import serve_core_request

        settings = await asyncio.to_thread(read_settings, self.data_dir)
        await serve_core_request(dict(scope, headers=headers), receive, send, token=settings.token,
                                 allow_unauthenticated=allow_open)

    def start(self, *, timeout_s: float = 15.0) -> "ModelHost":
        import uvicorn

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.set_inheritable(False)
        self.port = int(sock.getsockname()[1])
        config = uvicorn.Config(app=self, lifespan="off", log_level="warning", access_log=False,
                                timeout_graceful_shutdown=5)
        self._server = uvicorn.Server(config)

        def _serve() -> None:
            try:
                self._server.run(sockets=[sock])
            except Exception:  # noqa: BLE001 - loud: /v1 on the API answers 503 runner_unavailable
                logger.exception("split mode: the model host stopped")

        self._thread = threading.Thread(target=_serve, name="gateway-model-host", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout_s
        while not getattr(self._server, "started", False):
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the model host did not start on 127.0.0.1")
            time.sleep(0.02)
        _write_address(self.data_dir, {"url": self.url, "pid": os.getpid(), "secret": self.secret,
                                       "started_at": time.time()})
        logger.info("split mode: model host on %s (the API process forwards /v1 here)", self.url)
        return self

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            doc = json.loads(address_path(self.data_dir).read_text(encoding="utf-8"))
            if isinstance(doc, dict) and doc.get("secret") == self.secret:
                address_path(self.data_dir).unlink()
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10.0)


# ---- API side: forwarding ----------------------------------------------------------------------

class RunnerUnavailable(Exception):
    """No live model host recorded by a runner process."""


async def _read_all(receive) -> Tuple[bytes, bool]:
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return bytes(body), True
        body.extend(message.get("body") or b"")
        if not message.get("more_body"):
            return bytes(body), False


async def _until_disconnect(receive) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


async def forward_core_request(scope, receive, send, *, data_dir: Path, allow_unauthenticated: bool) -> None:
    """Serve an admitted `/v1` request through the runner process's model host. Raises
    RunnerUnavailable before anything was sent when no runner answers (the caller answers 503)."""
    import httpx

    from .work_pools import off_loop

    doc = await off_loop("light", read_address, data_dir)
    if doc is None:
        raise RunnerUnavailable("no runner process has recorded a model host")
    body, gone = await _read_all(receive)
    if gone:
        return
    query = scope.get("query_string") or b""
    url = str(doc["url"]) + str(scope.get("path") or "/") + (("?" + query.decode("latin-1")) if query else "")
    headers: List[Tuple[bytes, bytes]] = [(bytes(k), bytes(v)) for k, v in scope.get("headers") or []
                                          if bytes(k).lower() not in _HOP_BY_HOP
                                          and bytes(k).lower() not in (SECRET_HEADER, ALLOW_OPEN_HEADER)]
    headers.append((SECRET_HEADER, str(doc["secret"]).encode()))
    if allow_unauthenticated:
        headers.append((ALLOW_OPEN_HEADER, b"1"))
    timeout = httpx.Timeout(connect=5.0, read=None, write=60.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        request = client.build_request(str(scope.get("method") or "GET"), url, headers=headers, content=body)
        try:
            response = await client.send(request, stream=True)
        except httpx.TransportError as exc:
            raise RunnerUnavailable(f"the runner's model host did not answer: {type(exc).__name__}") from exc

        async def _relay() -> None:
            await send({"type": "http.response.start", "status": response.status_code,
                        "headers": [(k, v) for k, v in response.headers.raw if k.lower() not in _RESPONSE_DROP]})
            async for chunk in response.aiter_raw():
                if chunk:
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b""})  # more_body defaults to False: the end

        relay = asyncio.ensure_future(_relay())
        watch = asyncio.ensure_future(_until_disconnect(receive))
        try:
            done, _ = await asyncio.wait({relay, watch}, return_when=asyncio.FIRST_COMPLETED)
            if relay in done:
                relay.result()  # a send error (the client left, the endpoint stopped) surfaces here
            # else: the client left: closing the upstream response stops the generation there.
        finally:
            for task in (relay, watch):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
            await response.aclose()
