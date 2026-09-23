"""Windows runner singleton lock (first-run, 2026-09-23).

Windows has no `fcntl`, and the runner used to fall back to "flock unsupported
on this platform (no mutual exclusion)": two gateways on one data dir both
ticked every run. It now uses `msvcrt.locking` (one byte at offset 0). These
tests exercise that branch on any OS with a fake `msvcrt` whose byte-range
locks behave like Windows' (exclusive across file handles, EACCES on
conflict, released by LK_UNLCK or by closing the handle).
"""

from __future__ import annotations

import errno
import os
import sys
import types
from pathlib import Path

import pytest

from abstractgateway import runner as runner_mod

pytestmark = pytest.mark.basic


class _FakeMsvcrt(types.ModuleType):
    LK_UNLCK = 0
    LK_LOCK = 1
    LK_NBLCK = 2

    def __init__(self) -> None:
        super().__init__("msvcrt")
        self.held: dict = {}  # (dev, ino, offset) -> fd
        self.calls: list = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        st = os.fstat(fd)
        key = (st.st_dev, st.st_ino, os.lseek(fd, 0, os.SEEK_CUR))
        self.calls.append((mode, nbytes, key[2]))
        if mode == self.LK_UNLCK:
            if self.held.get(key) == fd:
                del self.held[key]
            return
        owner = self.held.get(key)
        if owner is not None and owner != fd:
            try:
                os.fstat(owner)
            except OSError:  # the owning handle was closed: Windows frees the lock
                del self.held[key]
            else:
                raise OSError(errno.EACCES, "Permission denied")
        self.held[key] = fd


def _make_runner(base_dir: Path):
    from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    class _Host:
        run_store = InMemoryRunStore()
        ledger_store = InMemoryLedgerStore()
        artifact_store = None

        def runtime_and_workflow_for_run(self, run_id: str):  # pragma: no cover
            raise KeyError(run_id)

    return GatewayRunner(base_dir=base_dir, host=_Host(), config=GatewayRunnerConfig(poll_interval_s=0.05))


@pytest.fixture()
def fake_msvcrt(monkeypatch: pytest.MonkeyPatch) -> _FakeMsvcrt:
    fake = _FakeMsvcrt()
    monkeypatch.setattr(runner_mod, "_singleton_file_locker", lambda: runner_mod._SingletonFileLocker("msvcrt", fake))
    return fake


def test_locker_selection_falls_back_to_msvcrt_without_fcntl(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "fcntl", None)  # import fcntl -> ImportError, as on Windows
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    locker = runner_mod._singleton_file_locker()
    assert locker is not None and locker.kind == "msvcrt"


def test_posix_still_uses_flock() -> None:
    pytest.importorskip("fcntl")
    locker = runner_mod._singleton_file_locker()
    assert locker is not None and locker.kind == "fcntl"


def test_windows_branch_gives_mutual_exclusion(tmp_path: Path, fake_msvcrt: _FakeMsvcrt) -> None:
    first = _make_runner(tmp_path)
    second = _make_runner(tmp_path)
    try:
        assert first._acquire_singleton_lock() is True
        assert first._lock_held is True
        assert "no mutual exclusion" not in str(first._last_lock_error or "")
        # Offset 0, one byte, non-blocking.
        assert (fake_msvcrt.LK_NBLCK, 1, 0) in fake_msvcrt.calls
        # A second runner on the same data dir is REFUSED (it used to also "hold" it).
        assert second._acquire_singleton_lock() is False
        assert second._lock_held is False and second._lock_refused_flag is True
        # Release (LK_UNLCK then close) lets the next one in.
        first._release_singleton_lock()
        assert (fake_msvcrt.LK_UNLCK, 1, 0) in fake_msvcrt.calls
        assert second._acquire_singleton_lock() is True
        text = (tmp_path / "gateway_runner.lock").read_text(encoding="utf-8")
        assert f"pid={os.getpid()}" in text
    finally:
        first._release_singleton_lock()
        second._release_singleton_lock()
