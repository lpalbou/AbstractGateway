"""Runner scan gate + replay chunk batching (perf incident, commons c2394).

The incident: a 3,241-file / 659MB root FileRunStore with ZERO active runs
pegged the gateway at ~100% CPU forever — every 0.25s poll ran three
scarce-match scans, each parsing the whole store because matches were
scarce and the store's 512-entry LRU cannot hold 3,241 entries (the scan
itself evicts everything it caches). Compounding it, the entity replay
route sent ONE envelope per ASGI chunk (threadpool hop + middleware
crossing + send per line: a 14-20 MB/s ceiling that served a 12.5MB life
in ~14s while its generator produced it in ~1.1s).

These tests pin the two gateway-side fixes:
- the scheduling pass is GATED on a cheap mtime fingerprint + a wait
  deadline horizon (quiet store = no full-store parse);
- replay responses batch lines into ~256KB sends with byte-identical
  content.
"""

from __future__ import annotations

import datetime
import json
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

from abstractruntime.core.models import RunState, RunStatus, WaitReason, WaitState  # noqa: E402
from abstractruntime.storage.in_memory import InMemoryRunStore  # noqa: E402
from abstractruntime.storage.json_files import JsonFileRunStore  # noqa: E402

from abstractgateway.runner import (  # noqa: E402
    GatewayRunner,
    GatewayRunnerConfig,
    _epoch_from_iso,
    _file_store_base,
    file_store_fingerprint,
)
from abstractgateway.routes.entity_replay import _batch_bytes  # noqa: E402


def _run(run_id: str, *, status: RunStatus = RunStatus.RUNNING, until: str | None = None) -> RunState:
    waiting = None
    if until is not None:
        waiting = WaitState(reason=WaitReason.UNTIL, wait_key=None, until=until)
        status = RunStatus.WAITING
    return RunState(
        run_id=run_id,
        workflow_id="wf",
        status=status,
        current_node="n",
        vars={},
        waiting=waiting,
        actor_id="gateway",
    )


class _Host:
    """Minimal GatewayHost: just enough for the runner's store access."""

    def __init__(self, run_store) -> None:
        self._rs = run_store

    @property
    def run_store(self):
        return self._rs

    @property
    def ledger_store(self):  # pragma: no cover - unused in these tests
        return None

    @property
    def artifact_store(self):  # pragma: no cover - unused
        return None

    def runtime_and_workflow_for_run(self, run_id: str):  # pragma: no cover - unused
        raise KeyError(run_id)


def _runner(tmp_path: Path, run_store) -> GatewayRunner:
    return GatewayRunner(
        base_dir=tmp_path,
        host=_Host(run_store),
        config=GatewayRunnerConfig(scan_gate_idle_interval_s=0.05),
        enable=False,
    )


# ---------------------------------------------------------------------------
# Fingerprint + base resolution
# ---------------------------------------------------------------------------


def test_fingerprint_moves_on_any_save_create_delete(tmp_path: Path):
    store = JsonFileRunStore(tmp_path)
    fp0 = file_store_fingerprint(tmp_path)

    store.save(_run("run_a"))
    fp1 = file_store_fingerprint(tmp_path)
    assert fp1 != fp0

    # Same run re-saved: atomic tmp->replace always bumps mtime.
    time.sleep(0.002)
    store.save(_run("run_a"))
    fp2 = file_store_fingerprint(tmp_path)
    assert fp2 != fp1

    store.delete("run_a")
    fp3 = file_store_fingerprint(tmp_path)
    assert fp3 != fp2


def test_file_store_base_resolves_through_wrappers_and_refuses_others(tmp_path: Path):
    file_store = JsonFileRunStore(tmp_path)
    assert _file_store_base(file_store) == tmp_path

    class _Wrapper:
        def __init__(self, inner) -> None:
            self.inner = inner

    assert _file_store_base(_Wrapper(file_store)) == tmp_path
    # Non-file stores DISABLE the gate (None): their scans are cheap and a
    # constant fingerprint would skip them forever.
    assert _file_store_base(InMemoryRunStore()) is None


# ---------------------------------------------------------------------------
# Gate semantics
# ---------------------------------------------------------------------------


def test_quiet_file_store_skips_scans_and_changes_wake_it(tmp_path: Path):
    store = JsonFileRunStore(tmp_path)
    store.save(_run("run_seed", status=RunStatus.COMPLETED))
    r = _runner(tmp_path, store)

    assert r._scan_pass_due() is True  # first pass always scans
    r._note_next_due([])  # a real pass would record "no deadlines"

    time.sleep(0.06)
    assert r._scan_pass_due() is True  # first probe captures the fingerprint
    r._note_next_due([])

    # Quiet store: probes come back false from now on.
    time.sleep(0.06)
    assert r._scan_pass_due() is False
    time.sleep(0.06)
    assert r._scan_pass_due() is False

    # Any write wakes the next probe.
    time.sleep(0.002)
    store.save(_run("run_new"))
    time.sleep(0.06)
    assert r._scan_pass_due() is True


def test_probe_throttle_blocks_back_to_back_probes(tmp_path: Path):
    store = JsonFileRunStore(tmp_path)
    r = _runner(tmp_path, store)
    assert r._scan_pass_due() is True
    r._note_next_due([])
    time.sleep(0.06)
    assert r._scan_pass_due() is True  # fingerprint capture probe
    # Immediately after a probe, the throttle refuses another (no sleep).
    assert r._scan_pass_due() is False


def test_nudge_and_command_force_bypass_the_gate(tmp_path: Path):
    store = JsonFileRunStore(tmp_path)
    r = _runner(tmp_path, store)
    assert r._scan_pass_due() is True
    r._note_next_due([])
    time.sleep(0.06)
    assert r._scan_pass_due() is True
    time.sleep(0.06)
    assert r._scan_pass_due() is False

    r.nudge()  # what the run-start route calls
    assert r._scan_pass_due() is True

    time.sleep(0.06)
    assert r._scan_pass_due() is False
    r._scan_force = True  # what _poll_commands sets after applying commands
    assert r._scan_pass_due() is True


def test_non_file_store_always_scans(tmp_path: Path):
    r = _runner(tmp_path, InMemoryRunStore())
    for _ in range(5):
        assert r._scan_pass_due() is True


# ---------------------------------------------------------------------------
# Deadline horizon: time passing must wake the gate without any file change
# ---------------------------------------------------------------------------


def _iso_in(seconds: float) -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    ).isoformat()


def test_due_deadline_wakes_a_quiet_store(tmp_path: Path):
    store = JsonFileRunStore(tmp_path)
    store.save(_run("run_wait", until=_iso_in(0.15)))
    r = _runner(tmp_path, store)

    assert r._scan_pass_due() is True
    r._note_next_due(store.list_runs(status=RunStatus.WAITING, limit=200))
    time.sleep(0.06)
    assert r._scan_pass_due() is True  # fingerprint capture
    r._note_next_due(store.list_runs(status=RunStatus.WAITING, limit=200))

    time.sleep(0.06)
    assert r._scan_pass_due() is False  # deadline still in the future

    time.sleep(0.15)  # deadline passed; NO file changed
    assert r._scan_pass_due() is True


def test_note_next_due_handles_horizon_shapes(tmp_path: Path):
    r = _runner(tmp_path, JsonFileRunStore(tmp_path))

    r._note_next_due([])
    assert r._next_due_epoch is None  # no deadlines: sleep on the fingerprint

    future = _iso_in(3600)
    r._note_next_due([_run("a", until=future)])
    assert r._next_due_epoch == pytest.approx(_epoch_from_iso(future), abs=0.01)

    # Unparseable deadline: degrade to periodic scanning, never skip forever.
    r._note_next_due([_run("b", until="not-a-date")])
    assert r._next_due_epoch is not None
    assert r._next_due_epoch <= time.time() + 1.0

    # Truncated list (>= run_scan_limit rows): same periodic degrade.
    many = [_run(f"c{i}", until=None, status=RunStatus.WAITING) for i in range(r._cfg.run_scan_limit)]
    r._note_next_due(many)
    assert r._next_due_epoch is not None


def test_epoch_from_iso_variants():
    assert _epoch_from_iso("2026-07-15T10:00:00+00:00") == pytest.approx(
        datetime.datetime(2026, 7, 15, 10, tzinfo=datetime.timezone.utc).timestamp()
    )
    assert _epoch_from_iso("2026-07-15T10:00:00Z") == _epoch_from_iso("2026-07-15T10:00:00+00:00")
    # Naive strings are UTC by the runtime's WAIT_UNTIL invariant.
    assert _epoch_from_iso("2026-07-15T10:00:00") == _epoch_from_iso("2026-07-15T10:00:00Z")
    assert _epoch_from_iso("garbage") is None


# ---------------------------------------------------------------------------
# End-to-end through the real loop pass
# ---------------------------------------------------------------------------


def test_schedule_pass_records_horizon_and_still_submits(tmp_path: Path):
    store = JsonFileRunStore(tmp_path)
    store.save(_run("run_go"))  # RUNNING, gateway-owned
    until = _iso_in(3600)
    store.save(_run("run_hold", until=until))
    r = _runner(tmp_path, store)

    submitted: list[str] = []
    r._submit_tick = lambda rid: submitted.append(rid)  # type: ignore[method-assign]

    r._schedule_ticks()
    assert submitted == ["run_go"]
    assert r._next_due_epoch == pytest.approx(_epoch_from_iso(until), abs=0.01)


# ---------------------------------------------------------------------------
# Replay chunk batching (fix B)
# ---------------------------------------------------------------------------


def test_batch_bytes_preserves_content_and_bounds_sends():
    lines = [
        (json.dumps({"seq": i, "pad": uuid.uuid4().hex * 40}) + "\n").encode("utf-8")
        for i in range(500)
    ]
    chunks = list(_batch_bytes(iter(lines), chunk_bytes=64 * 1024))
    assert b"".join(chunks) == b"".join(lines)  # byte-identical on the wire
    assert len(chunks) < len(lines) / 10  # actually batched
    for c in chunks[:-1]:
        assert len(c) >= 64 * 1024  # flush at the threshold
    assert all(len(c) > 0 for c in chunks)


def test_batch_bytes_flushes_a_final_partial_buffer():
    lines = [b"a\n", b"b\n"]
    assert list(_batch_bytes(iter(lines), chunk_bytes=1024)) == [b"a\nb\n"]
    assert list(_batch_bytes(iter([]), chunk_bytes=1024)) == []
