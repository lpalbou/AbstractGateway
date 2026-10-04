"""R13.1: the watchdog leaves an incident file; the next process shows it.

Before exiting, the watchdog writes `<data_dir>/incidents/watchdog-<stamp>.json`
(+ `.threads.txt`, every thread's stack). The next gateway reads the newest at
startup; `/api/gateway/host/runner` gives admins `last_hang`, which the
console's Resources page and the terminal console show as
"Gateway restarted at <time> after a hang — <reason>".
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from abstractgateway import loop_watchdog

pytestmark = pytest.mark.basic


def _blocking_handler_for_the_test() -> None:
    time.sleep(1.2)


def test_a_stall_writes_an_incident_naming_the_blocking_frame_and_the_request(tmp_path: Path) -> None:
    written: list = []

    async def scenario() -> None:
        def on_stall(wd, age):
            written.append(loop_watchdog.write_incident(wd, age, directory=tmp_path / "incidents"))

        wd = loop_watchdog.LoopWatchdog(0.5, on_stall=on_stall, backstop=False)
        wd.start()
        loop_watchdog._inflight[1] = ("POST", "/api/gateway/runs/r1/voice/tts/stream", time.monotonic())
        try:
            await asyncio.sleep(0.2)
            _blocking_handler_for_the_test()  # the loop thread is stuck here
            await asyncio.sleep(0.1)
        finally:
            loop_watchdog._inflight.pop(1, None)
            wd.stop()

    asyncio.run(scenario())
    assert written and written[0] is not None, "no incident written"
    path = written[0]
    data = json.loads(path.read_text())
    assert path.name.startswith("watchdog-") and path.suffix == ".json"
    assert data["schema"] == loop_watchdog.INCIDENT_SCHEMA and data["exit_code"] == 75
    assert data["top_frame"]["function"] == "_blocking_handler_for_the_test"
    assert data["blocked_s"] >= 0.5
    assert data["requests_in_flight"][0]["path"] == "/api/gateway/runs/r1/voice/tts/stream"
    assert "_blocking_handler_for_the_test" in data["reason"]
    assert "while serving POST /api/gateway/runs/r1/voice/tts/stream" in data["reason"]
    dump = Path(data["dump_path"])
    assert dump.is_file() and "Thread" in dump.read_text()
    # No home directory in what the console shows.
    assert str(Path.home()) not in json.dumps(data["loop_stack"])


def _incident(directory: Path, stamp: str, reason: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"watchdog-{stamp}.json"
    path.write_text(json.dumps({
        "schema": loop_watchdog.INCIDENT_SCHEMA, "at": "2026-10-04T19:23:57+00:00", "blocked_s": 30.9,
        "exit_code": 75, "reason": reason, "dump_path": str(directory / f"watchdog-{stamp}.threads.txt"),
        "top_frame": {"file": "starlette/responses.py", "line": 245, "function": "listen_for_disconnect"},
    }))
    return path


def test_the_newest_incident_is_read_and_rendered_as_one_line(tmp_path: Path) -> None:
    d = tmp_path / "incidents"
    _incident(d, "20261003T101010Z", "older")
    newest = _incident(d, "20261004T192357Z", "the event loop was blocked in starlette/responses.py:245 listen_for_disconnect")
    (d / "watchdog-20261005T000000Z.json").write_text("{not json")  # a torn write is skipped
    got = loop_watchdog.read_last_incident(d)
    assert got["file"] == str(newest)
    loop_watchdog.configure(None, incident_dir=d)
    try:
        loop_watchdog.load_last_incident()
        view = loop_watchdog.last_incident_view()
    finally:
        loop_watchdog.configure(None)
        loop_watchdog._last_incident = None
    assert view["line"] == (
        "Gateway restarted at 2026-10-04T19:23:57+00:00 after a hang — "
        "the event loop was blocked in starlette/responses.py:245 listen_for_disconnect"
    )
    assert view["dump_path"].endswith("watchdog-20261004T192357Z.threads.txt")
    assert loop_watchdog.read_last_incident(tmp_path / "none") is None


def test_host_runner_gives_admins_the_last_hang(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    flows = tmp_path / "flows"
    flows.mkdir()
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "r13-admin")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    d = tmp_path / "runtime" / "incidents"
    _incident(d, "20261004T192357Z", "the event loop was blocked in starlette/responses.py:245 listen_for_disconnect")
    loop_watchdog.configure(None, incident_dir=d)
    from abstractgateway.app import app

    try:
        with TestClient(app) as client:
            body = client.get("/api/gateway/host/runner", headers={"Authorization": "Bearer r13-admin"}).json()
    finally:
        loop_watchdog.configure(None)
        loop_watchdog._last_incident = None
    assert body["last_hang"]["line"].startswith("Gateway restarted at 2026-10-04T19:23:57+00:00 after a hang — the event loop was blocked")
    assert body["last_hang"]["blocked_s"] == 30.9


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


_LIVE = textwrap.dedent(
    """
    import sys, time
    import abstractgateway.loop_watchdog as wd

    _orig = wd.start_configured

    def _deliberately_blocking_handler():
        time.sleep(3600)

    def _start_and_schedule_a_stall():
        import asyncio
        out = _orig()
        asyncio.get_running_loop().call_later(3.0, _deliberately_blocking_handler)
        return out

    wd.start_configured = _start_and_schedule_a_stall
    from abstractgateway.cli import main
    main(sys.argv[1:])
    """
)


def test_real_serve_writes_the_incident_into_the_data_dir_before_exiting(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["ABSTRACTGATEWAY_DATA_DIR"] = str(tmp_path / "data")
    env["ABSTRACTGATEWAY_AUTH_TOKEN"] = "watchdog-test-token"
    env.pop("ABSTRACTGATEWAY_ADMIN_TOKEN", None)
    port = _free_port()
    args = ["serve", "--host", "127.0.0.1", "--port", str(port), "--no-tray", "--no-runner", "--watchdog-seconds", "2"]
    proc = subprocess.run([sys.executable, "-c", _LIVE, *args], capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))
    assert proc.returncode == loop_watchdog.WATCHDOG_EXIT_CODE, proc.stderr[-4000:]
    files = sorted((tmp_path / "data" / "incidents").glob("watchdog-*.json"))
    assert len(files) == 1, proc.stderr[-4000:]
    data = json.loads(files[0].read_text())
    assert data["top_frame"]["function"] == "_deliberately_blocking_handler"
    assert f"incident written to {files[0]}" in proc.stderr
