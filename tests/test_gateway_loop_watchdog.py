"""Event-loop watchdog (2026-10-04): a gateway whose loop is blocked exits so
its service manager restarts it.

The 03:15 incident: a TTS stream blocked the loop for 10+ minutes; the
process stayed alive, so neither launchd (`KeepAlive` restarts exits, not
hangs) nor the local supervisor restarted it. These tests block the loop on
purpose and require: the watchdog fires, names the blocking code, exits with
WATCHDOG_EXIT_CODE (non-zero, which `SuccessfulExit: false` /
`Restart=on-failure` act on) — up to a real `abstractgateway serve`.
"""

from __future__ import annotations

import asyncio
import io
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, List

import pytest

pytestmark = pytest.mark.basic


def _deliberately_blocking_handler(seconds: float) -> None:
    time.sleep(seconds)  # synchronous work on the event loop: the incident's shape


def test_watchdog_fires_on_a_blocked_loop_and_names_the_blocking_code() -> None:
    from abstractgateway.loop_watchdog import LoopWatchdog

    fired: List[Any] = []

    def _on_stall(wd: LoopWatchdog, age: float) -> None:
        fired.append((age, wd.format_loop_stack()))

    async def _main() -> None:
        wd = LoopWatchdog(0.5, on_stall=_on_stall, backstop=False, stream=io.StringIO())
        wd.start()
        await asyncio.sleep(0.2)
        _deliberately_blocking_handler(1.6)
        await asyncio.sleep(0)
        wd.stop()

    asyncio.run(_main())
    assert fired, "the watchdog did not fire while the loop was blocked for 1.6s with a 0.5s limit"
    age, stack = fired[0]
    assert age > 0.5
    assert "_deliberately_blocking_handler" in stack and "time.sleep" in stack


def test_watchdog_stays_quiet_on_a_responsive_loop() -> None:
    from abstractgateway.loop_watchdog import LoopWatchdog

    fired: List[float] = []

    async def _main() -> float:
        wd = LoopWatchdog(0.5, on_stall=lambda _wd, age: fired.append(age), backstop=False, stream=io.StringIO())
        wd.start()
        for _ in range(15):
            await asyncio.sleep(0.1)
        age = wd.last_tick_age_s()
        wd.stop()
        return age

    age = asyncio.run(_main())
    assert fired == []
    assert age < 0.5


_STANDALONE = textwrap.dedent(
    """
    import asyncio, time
    from abstractgateway.loop_watchdog import LoopWatchdog

    def _deliberately_blocking_handler():
        time.sleep(60)

    async def main():
        LoopWatchdog(1.0, backstop={backstop}).start()
        await asyncio.sleep(0.3)
        _deliberately_blocking_handler()

    asyncio.run(main())
    """
)


def test_blocked_loop_exits_with_the_watchdog_code_and_dumps_stacks() -> None:
    from abstractgateway.loop_watchdog import WATCHDOG_EXIT_CODE

    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", _STANDALONE.format(backstop="True")], capture_output=True, text=True, timeout=30)
    assert proc.returncode == WATCHDOG_EXIT_CODE != 0, proc.stderr
    assert time.monotonic() - t0 < 20
    assert "[FATAL] gateway watchdog: the event loop has not run for" in proc.stderr
    assert "the event-loop thread is blocked here" in proc.stderr
    assert "_deliberately_blocking_handler" in proc.stderr
    assert "all threads (faulthandler)" in proc.stderr


def test_gil_independent_backstop_exits_non_zero_when_the_python_watcher_cannot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Native code holding the GIL starves the Python watcher thread; the
    faulthandler timer (a C thread) must still dump and exit. Simulated by a
    watcher whose stall action never exits."""
    script = _STANDALONE.format(backstop="True").replace(
        "LoopWatchdog(1.0, backstop=True)",
        "LoopWatchdog(1.0, backstop=True, on_stall=lambda wd, age: None)",
    ).replace("import asyncio, time", "import asyncio, time\nimport abstractgateway.loop_watchdog as m\nm.BACKSTOP_GRACE_S = 1.0")
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0, proc.stderr
    assert "_deliberately_blocking_handler" in proc.stderr  # faulthandler's dump


def test_service_definitions_restart_a_watchdog_exit() -> None:
    """The watchdog exit is only useful if the service manager restarts on it."""
    import plistlib

    from abstractgateway.loop_watchdog import WATCHDOG_EXIT_CODE
    from abstractgateway.os_service import render_launchd_plist, render_systemd_unit

    assert WATCHDOG_EXIT_CODE != 0
    home = Path("/Users/someone")
    plist = plistlib.loads(render_launchd_plist(exe_argv=["/x/abstractgateway"], host="127.0.0.1", port=8080, data_dir=home / "d", home=home).encode())
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    unit = render_systemd_unit(exe_argv=["/x/abstractgateway"], host="127.0.0.1", port=8080, data_dir=home / "d", home=home)
    assert "Restart=on-failure" in unit


def test_health_reports_the_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from abstractgateway import loop_watchdog
    from abstractgateway.app import app

    loop_watchdog.configure(None)
    with TestClient(app) as client:
        body = client.get("/api/health").json()
    assert body["watchdog"] == {"enabled": False, "limit_s": None, "last_tick_age_s": None}

    loop_watchdog.configure(600)  # long enough never to fire inside the test
    try:
        with TestClient(app) as client:
            time.sleep(0.2)
            body = client.get("/api/health").json()
    finally:
        loop_watchdog.configure(None)
    wd = body["watchdog"]
    assert wd["enabled"] is True and wd["limit_s"] == 600
    assert isinstance(wd["last_tick_age_s"], float) and 0 <= wd["last_tick_age_s"] < 5
    assert loop_watchdog.health_snapshot()["enabled"] is False  # lifespan shutdown stopped it


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# The adversary's reproduction recipe, as a test: a REAL `abstractgateway
# serve`, its loop blocked 3 s after startup by a synchronous call.
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


def test_real_serve_with_a_blocked_loop_exits_with_the_watchdog_code(tmp_path: Path) -> None:
    from abstractgateway.loop_watchdog import WATCHDOG_EXIT_CODE

    env = dict(os.environ)
    env["ABSTRACTGATEWAY_DATA_DIR"] = str(tmp_path / "data")
    env["ABSTRACTGATEWAY_AUTH_TOKEN"] = "watchdog-test-token"
    env.pop("ABSTRACTGATEWAY_ADMIN_TOKEN", None)
    port = _free_port()
    args = ["serve", "--host", "127.0.0.1", "--port", str(port), "--no-tray", "--no-runner", "--watchdog-seconds", "2"]
    proc = subprocess.run([sys.executable, "-c", _LIVE, *args], capture_output=True, text=True, timeout=120, env=env, cwd=str(tmp_path))
    err = proc.stderr
    assert "Event-loop watchdog: exits with code 75 when the event loop is blocked for 2s" in err, err[-4000:]
    assert proc.returncode == WATCHDOG_EXIT_CODE, err[-4000:]
    assert "_deliberately_blocking_handler" in err


def test_serve_flag_configures_the_watchdog_for_the_run_only(monkeypatch: pytest.MonkeyPatch) -> None:
    import tests.test_gateway_cli_serve_host_controls as hc

    from abstractgateway import host_control, loop_watchdog

    seen: List[Any] = []
    orig_run = hc._FakeServer.run

    def _run(self: Any) -> None:
        seen.append(loop_watchdog.configured_limit_s())
        return orig_run(self)

    monkeypatch.setattr(hc._FakeServer, "run", _run)
    for extra, expected in (([], 30.0), (["--watchdog-seconds", "5"], 5.0), (["--watchdog-seconds", "0"], None)):
        host_control._reset_for_tests()
        hc._serve(monkeypatch, extra_args=extra, decision_start=False)
        assert seen[-1] == expected, (extra, seen)
        assert loop_watchdog.configured_limit_s() is None  # reset once serve returns
