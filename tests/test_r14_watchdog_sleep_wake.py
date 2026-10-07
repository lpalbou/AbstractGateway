"""Event-loop watchdog across sleep/wake (round 14, adversary AO-S1..S3).

A Mac waking from sleep can make the monotonic clock jump by minutes while the
event loop is alive. The watchdog must fire on a HANG (no loop progress), never
on a clock jump; its GIL-independent backstop must follow the same rule; and
when the watchdog does fire, the backstop must not race it (an exit 1 with no
incident file). Sleep is simulated by SIGSTOP/SIGCONT of the whole process group:
the monotonic clock keeps running while every thread is frozen, which is exactly
the jump a wake produces.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, List

import pytest

pytestmark = pytest.mark.basic

posix_only = pytest.mark.skipif(os.name != "posix", reason="SIGSTOP/SIGCONT and the backstop process are POSIX")


def test_check_fires_only_when_the_loop_made_no_progress() -> None:
    from abstractgateway.loop_watchdog import LoopWatchdog

    stream = io.StringIO()
    wd = LoopWatchdog(0.5, backstop=False, stream=stream)
    wd._last_tick = time.monotonic() - 100  # the clock jumped 100 s since the last tick

    def live_loop_ticks(_timeout: float) -> bool:
        wd._ticks += 1
        wd._last_tick = time.monotonic()
        return False

    assert wd.check(wait=live_loop_ticks) is None
    assert wd.resumed and wd.resumed[0] > 99
    assert "[WARN] gateway watchdog: the event loop resumed after" in stream.getvalue()

    wd._last_tick = time.monotonic() - 100
    age = wd.check(wait=lambda _t: False)  # no tick during the re-check: a hang
    assert age is not None and age > 99


def test_clock_jumps_with_a_live_loop_never_fire() -> None:
    """The loop keeps running while its last stamp is pushed 100 s into the past
    again and again (what the watcher sees right after a wake): no stall action."""
    from abstractgateway.loop_watchdog import LoopWatchdog

    fired: List[float] = []

    async def _main() -> LoopWatchdog:
        wd = LoopWatchdog(0.5, on_stall=lambda _wd, age: fired.append(age), backstop=False, stream=io.StringIO())
        wd.start()
        for _ in range(60):
            wd._last_tick -= 100.0
            await asyncio.sleep(0.03)
        wd.stop()
        return wd

    wd = asyncio.run(_main())
    assert fired == []
    assert wd.resumed, "the watcher never saw a jump (the test did not exercise the re-check)"


_SCRIPT = textwrap.dedent(
    """
    import asyncio, sys, time
    from pathlib import Path
    import abstractgateway.loop_watchdog as m
    m.BACKSTOP_GRACE_S = {grace}
    m.configure(1.0, incident_dir=Path({incidents!r}))
    {patch}

    def _deliberately_blocking_handler():
        time.sleep(60)

    async def main():
        wd = m.LoopWatchdog(1.0, backstop=True)
        wd.start()
        print("BACKSTOP", wd._backstop_proc.pid if wd._backstop_proc else 0, flush=True)
        await asyncio.sleep(0.5)
        if {hang}:
            _deliberately_blocking_handler()
        await asyncio.sleep(60)

    asyncio.run(main())
    """
)


def _spawn(tmp_path: Path, *, hang: bool, grace: float = 1.0, patch: str = "") -> subprocess.Popen:
    script = _SCRIPT.format(grace=grace, incidents=str(tmp_path / "incidents"), hang=hang, patch=patch)
    return subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True, env=dict(os.environ, PYTHONUNBUFFERED="1"))


@posix_only
def test_a_simulated_sleep_neither_trips_the_watchdog_nor_the_backstop(tmp_path: Path) -> None:
    """Freeze the whole process group (gateway + backstop process) for 4 s with a 1 s
    limit and a 2 s backstop budget, then wake it: both see the jump, both re-check,
    the loop is alive, nothing exits, no incident is written."""
    proc = _spawn(tmp_path, hang=False)
    try:
        line = proc.stdout.readline()
        child = int(line.split()[1])
        assert child > 0, line
        time.sleep(1.0)
        for pid in (proc.pid, child):  # freeze both: the machine sleeps
            os.kill(pid, signal.SIGSTOP)
        time.sleep(4.0)
        for pid in (child, proc.pid):  # wake both
            os.kill(pid, signal.SIGCONT)
        time.sleep(3.0)
        assert proc.poll() is None, (proc.returncode, proc.stderr.read())
    finally:
        proc.kill()
        _, err = proc.communicate(timeout=10)
    assert "[FATAL]" not in err and "Timeout (" not in err, err
    assert "[WARN] gateway watchdog: the event loop resumed after" in err, err
    assert not list((tmp_path / "incidents").glob("watchdog-*.json"))


@posix_only
def test_a_real_hang_exits_75_with_the_incident_even_when_writing_it_is_slow(tmp_path: Path) -> None:
    """A slow incident write (2 s) outlasts the backstop's budget (1.5 s): the watchdog
    stood the backstop down first, so the exit is 75 with the incident file, never the
    backstop's kill."""
    patch = (
        "_orig = m.write_incident\n"
        "def _slow(*a, **k):\n"
        "    time.sleep(2.0)\n"
        "    return _orig(*a, **k)\n"
        "m.write_incident = _slow\n"
    )
    proc = _spawn(tmp_path, hang=True, grace=0.5, patch=patch)
    t0 = time.monotonic()
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 75, (proc.returncode, err)
    assert "Timeout (" not in err
    files = list((tmp_path / "incidents").glob("watchdog-*.json"))
    assert len(files) == 1, files
    inc = json.loads(files[0].read_text())
    assert inc.get("kind") != "backstop" and "_deliberately_blocking_handler" in json.dumps(inc["loop_stack"])
    assert time.monotonic() - t0 < 15


@posix_only
def test_a_real_hang_exits_within_the_limit_plus_two_ticks(tmp_path: Path) -> None:
    from abstractgateway.loop_watchdog import LoopWatchdog

    fired: List[Any] = []

    async def _main() -> float:
        wd = LoopWatchdog(0.5, on_stall=lambda _wd, age: fired.append((time.monotonic(), age)), backstop=False, stream=io.StringIO())
        wd.start()
        await asyncio.sleep(0.2)
        t_block = time.monotonic()
        time.sleep(1.5)
        await asyncio.sleep(0)
        wd.stop()
        return t_block

    t_block = asyncio.run(_main())
    assert fired, "a 1.5 s block with a 0.5 s limit did not fire"
    when, age = fired[0]
    assert when - t_block <= 0.5 + 2 * 0.05 + 0.15, when - t_block  # limit + 2 ticks (+ scheduling slack)


@posix_only
def test_the_backstop_process_dies_with_the_gateway(tmp_path: Path) -> None:
    proc = _spawn(tmp_path, hang=False)
    line = proc.stdout.readline()
    child = int(line.split()[1])
    assert child > 0
    os.kill(proc.pid, signal.SIGKILL)
    proc.communicate(timeout=10)
    for _ in range(50):
        try:
            os.kill(child, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the backstop process outlived the gateway")


_FORK_HOLDER = textwrap.dedent(
    """
    import asyncio, os, time
    import abstractgateway.loop_watchdog as m

    async def main():
        wd = m.LoopWatchdog(1.0, backstop=True)
        wd.start()
        holder = os.fork()  # fork without exec (multiprocessing 'fork', a library's os.fork): keeps the pipe open
        if holder == 0:
            time.sleep(30)
            os._exit(0)
        print("PIDS", wd._backstop_proc.pid, holder, flush=True)
        await asyncio.sleep(0.5)
        os._exit(3)  # the gateway crashes; the heartbeat pipe stays open in the holder

    asyncio.run(main())
    """
)


@posix_only
def test_the_backstop_leaves_when_the_gateway_dies_even_if_a_fork_holds_the_pipe() -> None:
    """The backstop knows the gateway as its PARENT, not as a pid number: when the
    gateway dies while a forked child keeps the heartbeat pipe open (no EOF), the
    backstop exits within a couple of ticks instead of later signalling a pid that may
    have been reused."""
    proc = subprocess.Popen([sys.executable, "-c", _FORK_HOLDER], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = proc.stdout.readline()
    backstop, holder = (int(x) for x in line.split()[1:3])
    try:
        assert proc.wait(timeout=10) == 3  # (the holder keeps stdout open: wait, not communicate)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3.0:
            try:
                os.kill(backstop, 0)
            except OSError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the backstop outlived its gateway while a fork held the pipe")
        assert time.monotonic() - t0 < 1.0  # within a few ticks (tick 0.1 s)
    finally:
        try:
            os.kill(holder, signal.SIGKILL)
        except OSError:
            pass
        proc.stdout.close()
        proc.stderr.close()
