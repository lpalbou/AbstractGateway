"""The runner's scan gate compares the run store's own change token, never the directory
(2026-10-26: a glob + stat of 11,500 run files every 0.5 s, 0.18 s each, 13-36% of a core on
the operator's idle gateway)."""

from __future__ import annotations

import builtins
import os
import threading
import time
from pathlib import Path

import pytest

from timing import timing_budget  # noqa: E402 - tests/timing.py (wall-clock budgets)

from abstractruntime.core.models import RunState, RunStatus
from abstractruntime.storage.json_files import JsonFileRunStore

from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig, _store_change_token


class _Host:
    def __init__(self, run_store) -> None:
        self.run_store = run_store
        self.ledger_store = None
        self.artifact_store = None


class _Wrapper:
    def __init__(self, inner) -> None:
        self._inner = inner


def _run(rid: str, status: RunStatus) -> RunState:
    return RunState(run_id=rid, workflow_id="wf", status=status, current_node="n", vars={"x": "y" * 200}, actor_id="gateway")


def _runner(tmp_path: Path, store) -> GatewayRunner:
    return GatewayRunner(base_dir=tmp_path, host=_Host(store), config=GatewayRunnerConfig(scan_gate_idle_interval_s=0.05), enable=False)


def _probe(r: GatewayRunner) -> bool:
    r._scan_last_probe = 0.0  # past the idle throttle: every call probes
    return r._scan_pass_due()


def test_gate_uses_the_store_token_through_wrappers(tmp_path: Path) -> None:
    store = JsonFileRunStore(tmp_path)
    assert _store_change_token(_Wrapper(store)) == store.change_token
    r = _runner(tmp_path, _Wrapper(store))
    assert r._scan_pass_due() is True  # first pass
    assert _probe(r) is True  # captures the token
    assert _probe(r) is False
    store.save(_run("a", RunStatus.RUNNING))
    assert _probe(r) is True  # a save moves the token
    assert _probe(r) is False
    store.save(_run("a", RunStatus.RUNNING))  # the per-tick save of a running run
    assert _probe(r) is True
    store.save(_run("a", RunStatus.COMPLETED))
    assert _probe(r) is True
    JsonFileRunStore(tmp_path).save(_run("b", RunStatus.RUNNING))  # another process starts a run
    assert _probe(r) is True
    assert _probe(r) is False


def test_benchmark_11500_quiet_runs_probe_under_1ms_and_reads_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = JsonFileRunStore(tmp_path)
    for i in range(11_500):
        store.save(_run(f"r{i:05d}", RunStatus.COMPLETED if i % 800 else RunStatus.WAITING))
    r = _runner(tmp_path, store)
    r._scan_pass_due()
    _probe(r)
    calls = {"open": 0, "scandir": 0, "stat": 0}
    real_open, real_scandir, real_stat = builtins.open, os.scandir, os.stat
    prober = threading.get_ident()

    def counting(name, real):
        # Only the probing thread's file access counts (backlog 0966): the patch is process-wide, and a
        # thread left by another test (or the store's background reconciliation) stat-ing at the same time
        # made this read 202 <= 200 on a CI job. What it pins is that the PROBE reads no file.
        def f(*a, **k):
            if threading.get_ident() == prober:
                calls[name] += 1
            return real(*a, **k)
        return f

    monkeypatch.setattr(builtins, "open", counting("open", real_open))
    monkeypatch.setattr(os, "scandir", counting("scandir", real_scandir))
    monkeypatch.setattr(os, "stat", counting("stat", real_stat))
    passes = 200
    noise_stop = threading.Event()

    def other_thread_stats() -> None:  # another thread's file access, as on a busy CI process
        while not noise_stop.is_set():
            os.stat(tmp_path)
            time.sleep(0.0005)

    noise = threading.Thread(target=other_thread_stats, daemon=True)
    noise.start()
    t = time.perf_counter()
    due = [_probe(r) for _ in range(passes)]
    per_pass = (time.perf_counter() - t) / passes
    noise_stop.set()
    noise.join(5)
    monkeypatch.undo()
    assert not any(due)
    assert calls["open"] == 0 and calls["scandir"] == 0
    assert calls["stat"] <= passes  # one stat of the creation log per probe
    timing_budget(per_pass, 0.001, "change-token probe per pass")
