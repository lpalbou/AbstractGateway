"""Event-driven ledger streaming (backlog 0075) + replay-equivalence pins (0082).

The operator ruling (c4089): the SSE ledger stream's poll shape — full-file
count every 0.25s per client, full re-parse on every news event — is the
100%-CPU class (c2394) and its fix is TOP PRIORITY. The fix rides incremental
tail readers (`abstractgateway/ledger_tail.py`) + the previously-dormant
ObservableLedgerStore.subscribe as a coalescing wakeup.

0082's invariants gate the change (streaming is only an optimization over
replay — it must never invent, drop, or reorder records):

- I1  replay completeness: tail-from-0 == list() on both backends.
- I2  streaming ⊆ replay: records delivered over SSE equal a replay from the
      same starting cursor.
- I3  cursor stability: a reconnect from a held cursor (query param or
      Last-Event-ID) resumes exactly — no misses, no duplicates.
- I4  recovery preserved: concatenated-JSON lines recover identically in the
      tail reader and list() (the #FALLBACK class).
- I5  no terminal hang: a terminal run always ends with a `done` frame after
      one final drain.

Plus the cost-model pins: an idle JSONL poll is one stat() (no read of file
content); torn (partial-line) appends are never emitted or dropped; a file
that shrinks under a live tail resets honestly.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from abstractgateway.ledger_tail import (
    JsonlLedgerTail,
    ListSliceTail,
    SeqLedgerTail,
    find_observable,
    resolve_ledger_tail,
)


def _mk_record(run_id: str, i: int):
    from abstractruntime.core.models import StepRecord

    return StepRecord(
        run_id=run_id,
        step_id=f"s{i}",
        node_id=f"node-{i}",
        status="completed",
        started_at="2026-07-21T00:00:00+00:00",
        ended_at="2026-07-21T00:00:01+00:00",
        result={"i": i, "text": f"record {i}"},
    )


def _jsonl_store(base: Path):
    from abstractruntime import JsonlLedgerStore

    return JsonlLedgerStore(base)


def _sqlite_store(base: Path):
    from abstractruntime import SqliteDatabase, SqliteLedgerStore

    return SqliteLedgerStore(SqliteDatabase(base / "ledger.sqlite3"))


@pytest.mark.basic
@pytest.mark.parametrize("backend", ["jsonl", "sqlite"])
def test_i1_tail_from_zero_equals_list(tmp_path: Path, backend: str) -> None:
    store = _jsonl_store(tmp_path) if backend == "jsonl" else _sqlite_store(tmp_path)
    for i in range(7):
        store.append(_mk_record("run-i1", i))

    tail = resolve_ledger_tail(store, "run-i1", start_index=0)
    got = tail.read_new()
    expect = store.list("run-i1")
    assert [r for _, r in got] == expect
    assert [c for c, _ in got] == list(range(1, 8))


@pytest.mark.basic
@pytest.mark.parametrize("backend", ["jsonl", "sqlite"])
def test_i3_cursor_resume_is_exact(tmp_path: Path, backend: str) -> None:
    """Hold a cursor mid-stream, reconnect from it: the concatenation equals
    one full replay — no misses, no duplicates."""
    store = _jsonl_store(tmp_path) if backend == "jsonl" else _sqlite_store(tmp_path)
    for i in range(5):
        store.append(_mk_record("run-i3", i))

    tail1 = resolve_ledger_tail(store, "run-i3", start_index=0)
    first = tail1.read_new()
    held_cursor = first[2][0]  # cursor after 3 records

    for i in range(5, 9):
        store.append(_mk_record("run-i3", i))

    tail2 = resolve_ledger_tail(store, "run-i3", start_index=held_cursor)
    rest = tail2.read_new()

    combined = [r for _, r in first[:3]] + [r for _, r in rest]
    assert combined == store.list("run-i3")


@pytest.mark.basic
def test_i3_incremental_reads_deliver_appends(tmp_path: Path) -> None:
    """A live tail sees appends made after its last read (JSONL byte-offset)."""
    store = _jsonl_store(tmp_path)
    store.append(_mk_record("run-live", 0))
    tail = resolve_ledger_tail(store, "run-live", start_index=0)
    assert len(tail.read_new()) == 1
    assert tail.read_new() == []  # idle: nothing new

    store.append(_mk_record("run-live", 1))
    store.append(_mk_record("run-live", 2))
    got = tail.read_new()
    assert [c for c, _ in got] == [2, 3]
    assert [r["node_id"] for _, r in got] == ["node-1", "node-2"]


@pytest.mark.basic
def test_i4_concatenated_line_recovery_matches_list(tmp_path: Path) -> None:
    """The #FALLBACK recovery class: two JSON objects on ONE line recover
    identically in list() and in the tail reader."""
    store = _jsonl_store(tmp_path)
    store.append(_mk_record("run-i4", 0))
    p = tmp_path / "ledger_run-i4.jsonl"
    a = json.dumps({"run_id": "run-i4", "step_id": "sx", "node_id": "glued-1", "status": "completed"})
    b = json.dumps({"run_id": "run-i4", "step_id": "sy", "node_id": "glued-2", "status": "completed"})
    with p.open("a", encoding="utf-8") as f:
        f.write(a + b + "\n")  # one line, two objects (crash-interleave class)

    expect = store.list("run-i4")
    assert [r["node_id"] for r in expect] == ["node-0", "glued-1", "glued-2"]

    tail = resolve_ledger_tail(store, "run-i4", start_index=0)
    got = tail.read_new()
    assert [r for _, r in got] == expect


@pytest.mark.basic
def test_torn_append_is_held_not_dropped(tmp_path: Path) -> None:
    """A partial line (no trailing newline yet — a writer mid-append) is
    neither emitted nor skipped: the offset holds and the record arrives
    whole once the newline lands."""
    store = _jsonl_store(tmp_path)
    store.append(_mk_record("run-torn", 0))
    tail = resolve_ledger_tail(store, "run-torn", start_index=0)
    assert len(tail.read_new()) == 1

    p = tmp_path / "ledger_run-torn.jsonl"
    full = json.dumps({"run_id": "run-torn", "node_id": "late", "status": "completed"})
    with p.open("a", encoding="utf-8") as f:
        f.write(full[:10])  # torn write, no newline
    assert tail.read_new() == []

    with p.open("a", encoding="utf-8") as f:
        f.write(full[10:] + "\n")  # completion
    got = tail.read_new()
    assert [r["node_id"] for _, r in got] == ["late"]


@pytest.mark.basic
def test_idle_poll_is_stat_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cost pin: an idle JSONL poll must not OPEN the file (the old body
    line-scanned the whole file per 0.25s poll per client)."""
    store = _jsonl_store(tmp_path)
    for i in range(3):
        store.append(_mk_record("run-idle", i))
    tail = resolve_ledger_tail(store, "run-idle", start_index=0)
    tail.read_new()  # catch up

    opened: List[str] = []
    real_open = Path.open

    def _counting_open(self, *a, **kw):
        opened.append(str(self))
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", _counting_open)
    for _ in range(20):
        assert tail.read_new() == []
    assert opened == [], f"idle polls opened the ledger file: {opened}"


@pytest.mark.basic
def test_shrunk_file_resets_honestly(tmp_path: Path) -> None:
    """External rewrite (prune) under a live tail: held offsets cannot map
    onto the new content, so the reader resets to a FRESH ledger — the
    current file's records (re)emit with cursors 1..N (at-least-once;
    clients dedupe by cursor) and nothing from the new content is skipped."""
    store = _jsonl_store(tmp_path)
    for i in range(4):
        store.append(_mk_record("run-shrink", i))
    tail = resolve_ledger_tail(store, "run-shrink", start_index=0)
    assert len(tail.read_new()) == 4

    p = tmp_path / "ledger_run-shrink.jsonl"
    lines = p.read_text(encoding="utf-8").strip().split("\n")
    p.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")  # rewrite: keep 2
    store.append(_mk_record("run-shrink", 99))

    got = tail.read_new()
    assert [r["node_id"] for _, r in got] == ["node-0", "node-1", "node-99"]
    assert [c for c, _ in got] == [1, 2, 3]  # fresh-ledger cursors


@pytest.mark.basic
def test_sqlite_tail_uses_indexed_read(tmp_path: Path) -> None:
    store = _sqlite_store(tmp_path)
    for i in range(6):
        store.append(_mk_record("run-sq", i))
    tail = resolve_ledger_tail(store, "run-sq", start_index=0)
    assert isinstance(tail, SeqLedgerTail)
    got = tail.read_new()
    assert [c for c, _ in got] == [1, 2, 3, 4, 5, 6]
    store.append(_mk_record("run-sq", 6))
    got2 = tail.read_new()
    assert [c for c, _ in got2] == [7]


@pytest.mark.basic
def test_gateway_store_chain_resolves_to_byte_tail(tmp_path: Path) -> None:
    """The REAL gateway chain (Offloading(Observable(Jsonl))) must resolve to
    the byte-offset tail (not the fallback) and expose the observable."""
    from abstractgateway.stores import build_file_stores

    stores = build_file_stores(base_dir=tmp_path)
    tail = resolve_ledger_tail(stores.ledger_store, "run-x", start_index=0)
    assert isinstance(tail, JsonlLedgerTail)
    assert find_observable(stores.ledger_store) is not None

    # And the tail serves what the CHAIN's list() serves (refs-as-refs parity:
    # the backend file holds the persisted records list() returns).
    stores.ledger_store.append(_mk_record("run-x", 0))
    got = tail.read_new()
    assert [r for _, r in got] == stores.ledger_store.list("run-x")


@pytest.mark.basic
def test_fallback_tail_for_plain_stores() -> None:
    from abstractruntime.storage.in_memory import InMemoryLedgerStore

    store = InMemoryLedgerStore()
    for i in range(3):
        store.append(_mk_record("run-mem", i))
    tail = resolve_ledger_tail(store, "run-mem", start_index=1)
    assert isinstance(tail, ListSliceTail)
    got = tail.read_new()
    assert [c for c, _ in got] == [2, 3]


# ---------------------------------------------------------------------------
# Fable5 adversary folds (P0-1 fat line, P0-2 silent progress, P1-1 loud errors)
# ---------------------------------------------------------------------------


@pytest.mark.basic
def test_p0_1_line_larger_than_window_grows_and_delivers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single line larger than the read window used to stall the tail
    FOREVER (offset pinned, silent truncation, futile capped re-read per
    poll). The window now grows geometrically until the newline fits, then
    resets."""
    store = _jsonl_store(tmp_path)
    store.append(_mk_record("run-fat", 0))
    fat = _mk_record("run-fat", 1)
    fat.result = {"i": 1, "blob": "x" * 4096}
    store.append(fat)
    store.append(_mk_record("run-fat", 2))

    tail = resolve_ledger_tail(store, "run-fat", start_index=0)
    tail._window = 256  # simulate a >window line at test scale
    monkeypatch.setattr(type(tail), "_READ_MAX_BYTES", 256, raising=False)

    collected: List[Dict[str, Any]] = []
    for _ in range(20):  # bounded drain loop (the route's re-drain shape)
        got = tail.read_new()
        collected.extend(r for _, r in got)
        if tail.caught_up:
            break
    assert collected == store.list("run-fat"), "fat line must deliver, never stall or truncate"
    assert tail.caught_up is True


@pytest.mark.basic
def test_p0_2_skip_swallowed_read_reports_progress_not_caught_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A capped catch-up window fully swallowed by the resume skip returns
    zero records while making PROGRESS — the terminal close must not treat
    it as drained (it used to emit `done` below the client's own cursor)."""
    store = _jsonl_store(tmp_path)
    for i in range(40):
        store.append(_mk_record("run-deep", i))

    tail = resolve_ledger_tail(store, "run-deep", start_index=20)
    tail._window = 512  # a few records per read; skip swallows early windows

    emitted: List[Dict[str, Any]] = []
    saw_silent_progress = False
    for _ in range(200):
        got = tail.read_new()
        if not got and tail.progressed:
            saw_silent_progress = True
        emitted.extend(r for _, r in got)
        if tail.caught_up:
            break
    assert saw_silent_progress, "test setup must exercise the skip-swallowed window"
    assert emitted == store.list("run-deep")[20:], "deep resume delivers exactly the suffix"


@pytest.mark.basic
def test_p1_1_read_errors_propagate_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A read error must kill the stream loudly (client reconnects), never
    masquerade as 'no news' — on a terminal run that masked a truncated
    replay under a clean done frame."""
    store = _jsonl_store(tmp_path)
    store.append(_mk_record("run-err", 0))
    tail = resolve_ledger_tail(store, "run-err", start_index=0)
    assert len(tail.read_new()) == 1

    def _boom(self, *a, **kw):
        raise PermissionError("injected read failure")

    monkeypatch.setattr(Path, "stat", _boom)
    with pytest.raises(PermissionError):
        tail.read_new()


@pytest.mark.basic
def test_missing_ledger_file_is_caught_up_empty(tmp_path: Path) -> None:
    """No ledger file = legitimately empty (no records appended yet), not an
    error: caught_up so a terminal run with zero records still closes."""
    store = _jsonl_store(tmp_path)
    tail = resolve_ledger_tail(store, "run-none", start_index=0)
    assert tail.read_new() == []
    assert tail.caught_up is True


# ---------------------------------------------------------------------------
# I2 + I5 through the real route (SSE end-to-end)
# ---------------------------------------------------------------------------


def _sse_events(body: str) -> List[Dict[str, Any]]:
    """Parse SSE text into [{id, event, data}] frames (comments skipped)."""
    events: List[Dict[str, Any]] = []
    cur: Dict[str, Any] = {}
    for line in body.split("\n"):
        if not line.strip():
            if cur:
                events.append(cur)
                cur = {}
            continue
        if line.startswith(":"):
            continue
        if line.startswith("id: "):
            cur["id"] = line[4:]
        elif line.startswith("event: "):
            cur["event"] = line[7:]
        elif line.startswith("data: "):
            cur["data"] = json.loads(line[6:])
    if cur:
        events.append(cur)
    return events


@pytest.mark.basic
@pytest.mark.parametrize("backend", ["file", "sqlite"])
def test_i2_i5_stream_equals_replay_and_closes_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """End-to-end through the route ON BOTH BACKENDS (adversary P1-2: the
    SQLite SeqLedgerTail was never driven through the route): a terminal run
    streams exactly its replay (I2) from cursor 0 AND from a mid-stream
    reconnect cursor (I3), then closes with a `done` frame (I5)."""
    from fastapi.testclient import TestClient

    from test_gateway_runs_list_endpoint import _write_min_bundle

    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-stream-0075", flow_id="root")

    token = "t" * 32
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_STORE_BACKEND", backend)

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": "bundle-stream-0075", "flow_id": "root", "input_data": {}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        rid = r.json()["run_id"]

        deadline = time.time() + 30.0
        while time.time() < deadline:
            st = client.get(f"/api/gateway/runs/{rid}", headers=headers).json()
            if str(st.get("status") or "").lower() in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        else:
            pytest.fail("run never reached terminal state")

        replay = client.get(f"/api/gateway/runs/{rid}/ledger?limit=2000", headers=headers).json()["items"]
        assert replay, "terminal run has an empty ledger"

        # I2: stream from 0 delivers exactly the replay, then done (I5).
        with client.stream("GET", f"/api/gateway/runs/{rid}/ledger/stream", headers=headers) as resp:
            body = "".join(chunk for chunk in resp.iter_text())
        events = _sse_events(body)
        steps = [e for e in events if e.get("event") == "step"]
        dones = [e for e in events if e.get("event") == "done"]
        assert [e["data"]["record"] for e in steps] == replay
        assert len(dones) == 1
        assert dones[0]["data"]["cursor"] == len(replay)

        # I3: reconnect mid-stream via Last-Event-ID resumes exactly.
        mid = max(1, len(replay) // 2)
        h2 = dict(headers)
        h2["Last-Event-ID"] = str(mid)
        with client.stream("GET", f"/api/gateway/runs/{rid}/ledger/stream", headers=h2) as resp:
            body2 = "".join(chunk for chunk in resp.iter_text())
        steps2 = [e for e in _sse_events(body2) if e.get("event") == "step"]
        assert [e["data"]["record"] for e in steps2] == replay[mid:]
        # And the explicit query param still wins over the header.
        with client.stream(
            "GET", f"/api/gateway/runs/{rid}/ledger/stream?after={len(replay)}", headers=h2
        ) as resp:
            body3 = "".join(chunk for chunk in resp.iter_text())
        steps3 = [e for e in _sse_events(body3) if e.get("event") == "step"]
        assert steps3 == []
