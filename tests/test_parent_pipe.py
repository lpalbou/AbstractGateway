"""A child of the gateway learns that the gateway is gone from its stdin pipe closing.

Windows, 2026-11-17: the model host watched that pipe with a blocking read. The C runtime keeps a
descriptor locked for the whole of a read, so any other C runtime call on descriptor 0 waits for
it; `import numpy` (whose libraries touch the standard descriptors when they load) never
returned, the gateway's status probes piled up behind the model host, and every request was
refused with 429.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

SRC = str(Path(__file__).resolve().parents[1] / "src")

CHILD = r"""
import os, sys, threading, time
sys.path.insert(0, sys.argv[1])
from abstractgateway.parent_pipe import wait_closed

def watch():
    wait_closed(sys.stdin.buffer)
    os._exit(7)

threading.Thread(target=watch, daemon=True).start()
time.sleep(0.5)  # the watcher is waiting on the pipe now

def touch_stdin():
    os.close(os.dup(0))  # a C runtime call on descriptor 0, like a library makes when it loads
    sys.stdout.write("free\n")
    sys.stdout.flush()

threading.Thread(target=touch_stdin, daemon=True).start()
time.sleep(20)
os._exit(1)
"""


def test_watching_the_parent_pipe_leaves_stdin_usable_and_sees_it_close() -> None:
    proc = subprocess.Popen([sys.executable, "-c", CHILD, SRC], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert proc.stdout is not None and proc.stdin is not None
        assert proc.stdout.readline().strip() == b"free"  # behind a blocking read, the dup never returns on Windows
        proc.stdin.close()
        assert proc.wait(10) == 7
    finally:
        proc.kill()
        proc.wait(10)
