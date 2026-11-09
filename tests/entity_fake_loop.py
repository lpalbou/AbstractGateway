"""A stand-in own-time loop for the kill-switch tests: a real child process whose command line is
this home's loop invocation (`-m abstractruntime.identity.life --home <home>`), with the incarnation
token (its OS start time) stamped in the status — exactly what the runtime's hard stop verifies before
it signals anything."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path


def stamp(pid: int) -> str:
    from abstractruntime.identity.life import _pid_start_time

    for _ in range(50):
        token = _pid_start_time(pid)
        if token:
            return token
        time.sleep(0.05)
    raise AssertionError(f"no start time for pid {pid}")


_STUB_ROOT: list = []


def _stub_root() -> str:
    """A throwaway package tree whose `abstractruntime.identity.life` only sleeps: the child runs
    `python -m abstractruntime.identity.life --home <home>` (the real loop's command line) without
    running a loop."""
    if not _STUB_ROOT:
        import tempfile

        root = Path(tempfile.mkdtemp(prefix="fake-own-time-loop-"))
        pkg = root / "abstractruntime" / "identity"
        pkg.mkdir(parents=True)
        (root / "abstractruntime" / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "life.py").write_text(
            "import sys, time\nsecs = float(sys.argv[sys.argv.index('--seconds') + 1]) if '--seconds' in sys.argv else 120\n"
            "time.sleep(secs)\n", encoding="utf-8")
        _STUB_ROOT.append(str(root))
    return _STUB_ROOT[0]


def spawn(home_dir: Path, phase: str = "day", seconds: int = 120, reap: bool = True, **status) -> subprocess.Popen:
    import os

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = _stub_root()
    proc = subprocess.Popen([sys.executable, "-m", "abstractruntime.identity.life", "--home", str(home_dir),
                             "--seconds", str(seconds)], env=env)
    if reap:
        threading.Thread(target=proc.wait, daemon=True).start()
    write_status(home_dir, proc.pid, phase, **status)
    return proc


def write_status(home_dir: Path, pid: int, phase: str, **extra) -> None:
    Path(home_dir, "loop_status").write_text(
        json.dumps({"phase": phase, "pid": pid, "pid_started_at": stamp(pid), **extra}), encoding="utf-8")
