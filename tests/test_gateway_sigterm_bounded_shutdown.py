"""A SIGTERM stops a real `serve` within a bounded time and exits 0, even with a live stream open.

Before (2026-10-29, the operator's Mac): one open `/me/events/stream` held a SIGTERM for the whole
graceful-shutdown timeout (120 s) and the gateway had to be SIGKILLed. `shutdown_drain.py`: streams
end the moment the stop begins, requests in flight get 10 s, the process exits 0.
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

TOKEN = "sigterm-bounded-token-0123456789abcdef"
BUDGET_S = 5.0

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX signals")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_sigterm_with_a_live_stream_exits_zero_within_the_budget(tmp_path: Path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    (home / "tmp").mkdir(parents=True)
    data.mkdir()
    port = _free_port()
    env = {
        "HOME": str(home), "TMPDIR": str(home / "tmp"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(Path(__import__("abstractgateway").__file__).resolve().parent.parent),
        "LANG": "en_US.UTF-8", "HF_HOME": str(home / "hf"), "HF_HUB_OFFLINE": "1", "NO_COLOR": "1",
        "OLLAMA_BASE_URL": "http://127.0.0.1:9/", "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", "ABSTRACTGATEWAY_AUTH_TOKEN": TOKEN,
        "ABSTRACTGATEWAY_STORE_BACKEND": "file",
    }
    log = tmp_path / "gateway.log"
    gw = subprocess.Popen([sys.executable, "-m", "abstractgateway", "serve", "--data-dir", str(data),
                           "--host", "127.0.0.1", "--port", str(port), "--no-tray"],
                          env=env, stdout=log.open("w"), stderr=subprocess.STDOUT, cwd=str(tmp_path))
    base = f"http://127.0.0.1:{port}"
    try:
        end = time.monotonic() + 120
        while time.monotonic() < end:
            assert gw.poll() is None, log.read_text()[-3000:]
            try:
                if httpx.get(f"{base}/api/health", timeout=2, trust_env=False).json().get("status") == "healthy":
                    break
            except Exception:  # noqa: BLE001 - not up yet
                pass
            time.sleep(0.2)
        else:
            raise AssertionError("the gateway did not come up\n" + log.read_text()[-3000:])

        got: list = []
        ended = threading.Event()

        def _stream() -> None:
            try:
                with httpx.stream("GET", f"{base}/api/gateway/me/events/stream", params={"heartbeat_s": 1},
                                  headers={"Authorization": f"Bearer {TOKEN}"}, timeout=None, trust_env=False) as r:
                    got.append(r.status_code)
                    for chunk in r.iter_bytes():
                        got.append(chunk)
            except Exception as exc:  # noqa: BLE001 - the server ending the stream is the point
                got.append(repr(exc))
            finally:
                ended.set()

        threading.Thread(target=_stream, daemon=True).start()
        end = time.monotonic() + 10
        while time.monotonic() < end and len(got) < 2:
            time.sleep(0.05)
        assert got and got[0] == 200, got

        t0 = time.monotonic()
        gw.send_signal(signal.SIGTERM)
        code = gw.wait(timeout=60)
        took = time.monotonic() - t0
        print(f"\nSIGTERM with a live stream: exit {code} after {took:.2f} s")
        assert took < BUDGET_S, f"stopped after {took:.1f} s"
        assert code == 0, log.read_text()[-3000:]
        assert ended.wait(5), "the stream was not ended by the stop"
        assert "survived" not in log.read_text(), log.read_text()[-3000:]
    finally:
        if gw.poll() is None:
            gw.kill()
            gw.wait(timeout=10)


def test_get_requests_see_the_client_leave_when_the_stop_begins() -> None:
    from abstractgateway import shutdown_drain

    shutdown_drain.reset()
    seen: list = []

    async def app(scope, receive, send):
        seen.append(await receive())

    async def receive():
        await asyncio.sleep(3600)  # a client that never sends anything else

    async def main():
        mw = shutdown_drain.EndStreamsOnShutdown(app)
        task = asyncio.ensure_future(mw({"type": "http", "method": "GET"}, receive, None))
        await asyncio.sleep(0.05)
        shutdown_drain.begin("SIGTERM")
        await asyncio.wait_for(task, 2)
        # A POST is not cut: its body must be read whole.
        posted: list = []

        async def post_app(scope, rcv, send):
            posted.append(await rcv())

        async def body():
            return {"type": "http.request", "body": b"x", "more_body": False}

        await shutdown_drain.EndStreamsOnShutdown(post_app)({"type": "http", "method": "POST"}, body, None)
        return posted

    try:
        posted = asyncio.run(main())
    finally:
        shutdown_drain.reset()
    assert seen == [{"type": "http.disconnect"}]
    assert posted == [{"type": "http.request", "body": b"x", "more_body": False}]
