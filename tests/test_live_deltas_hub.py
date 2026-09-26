"""LiveDeltaHub, FileDeltaSink, the file tailer and the startup sweep (live_deltas.py).

Pins the gateway half of token streaming (CONTRACTS S-1/S-2):

* the hub is keyed by (data folder, ROOT run); a subscription to a run gets
  that run's calls and its subtree's, never a sibling's or another folder's;
* subscribe = one snapshot frame per open call and channel, then every later
  event; a slow subscriber holds ONE pending entry per call and loses nothing;
* a run's terminal status closes its open calls with a synthetic delta_end
  (failed | cancelled) and frees a root's state; late events never reopen;
* no byte cap exists: a 1 MB call arrives whole, `truncated` never appears;
* split mode: 0600 files, one line per event, deleted at the root's end; the
  tailer buffers partial lines and follows delete/recreate; the sweep only
  deletes finished (or vanished) runs' files.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Dict, List, Optional

import pytest

from abstractgateway import live_deltas as ld


def _delta(run_id: str, call_id: str, seq: int, text: str, *, channel: str = "content", parent: Optional[str] = None) -> Dict[str, Any]:
    return {
        "kind": "llm.delta", "run_id": run_id, "parent_run_id": parent, "node_id": "n1",
        "call_id": call_id, "seq": seq, "text": text, "channel": channel,
    }


def _end(run_id: str, call_id: str, seq: int, reason: str = "completed", *, parent: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    out = {"kind": "llm.delta_end", "run_id": run_id, "parent_run_id": parent, "node_id": "n1",
           "call_id": call_id, "seq": seq, "reason": reason}
    out.update(extra)
    return out


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


S = "/data/a"
ROOT = ("root",)
CHILD = ("child", "root")
SIBLING = ("sib", "root")


def test_snapshot_then_live_frames_with_root_run_id(loop) -> None:
    hub = ld.LiveDeltaHub()
    hub.publish(S, _delta("root", "c1", 0, "Hel", channel="reasoning"), ROOT)
    hub.publish(S, _delta("root", "c1", 1, "lo"), ROOT)
    hub.publish(S, _delta("root", "c1", 2, " world"), ROOT)

    sub = hub.subscribe(S, ROOT, loop=loop)
    assert [(f["channel"], f["text"], f["seq"], f["snapshot"]) for f in sub.snapshot] == [
        ("reasoning", "Hel", 2, True),
        ("content", "lo world", 2, True),
    ]
    assert all(f["root_run_id"] == "root" and f["call_id"] == "c1" for f in sub.snapshot)
    assert sub.drain() == [], "the snapshot already covers what was published before subscribing"

    hub.publish(S, _delta("root", "c1", 3, "!"), ROOT)
    hub.publish(S, _end("root", "c1", 4), ROOT)
    frames = sub.drain()
    assert [(f["kind"], f.get("text"), f["seq"], f["snapshot"]) for f in frames] == [
        ("llm.delta", "!", 3, False),
        ("llm.delta_end", None, 4, False),
    ]
    assert hub.open_calls(S, "root") == []
    sub.close()
    assert hub.tracked_roots() == [], "state is freed once the call ended and nobody watches"


def test_root_subscription_sees_the_tree_child_subscription_only_its_subtree(loop) -> None:
    hub = ld.LiveDeltaHub()
    root_sub = hub.subscribe(S, ROOT, loop=loop)
    child_sub = hub.subscribe(S, CHILD, loop=loop)
    hub.publish(S, _delta("root", "r1", 0, "root text"), ROOT)
    hub.publish(S, _delta("child", "k1", 0, "child text", parent="root"), CHILD)
    hub.publish(S, _delta("sib", "s1", 0, "sibling text", parent="root"), SIBLING)
    hub.publish(S, _delta("grand", "g1", 0, "grandchild", parent="child"), ("grand", "child", "root"))

    assert sorted(f["text"] for f in root_sub.drain()) == ["child text", "grandchild", "root text", "sibling text"]
    got = child_sub.drain()
    assert sorted(f["text"] for f in got) == ["child text", "grandchild"]
    assert {f["root_run_id"] for f in got} == {"root"}
    assert {f["run_id"] for f in got} == {"child", "grand"}

    # A late child subscriber's snapshot is its subtree only.
    late = hub.subscribe(S, CHILD, loop=loop)
    assert sorted(f["text"] for f in late.snapshot) == ["child text", "grandchild"]


def test_another_data_folder_never_sees_the_frames(loop) -> None:
    hub = ld.LiveDeltaHub()
    other = hub.subscribe("/data/b", ROOT, loop=loop)
    hub.publish(S, _delta("root", "c1", 0, "secret"), ROOT)
    assert other.snapshot == [] and other.drain() == []
    again = hub.subscribe("/data/b", ROOT, loop=loop)
    assert again.snapshot == []


def test_a_slow_subscriber_holds_one_pending_entry_per_call_and_loses_nothing(loop) -> None:
    hub = ld.LiveDeltaHub()
    sub = hub.subscribe(S, ROOT, loop=loop)
    for i in range(5000):
        hub.publish(S, _delta("root", "c1", i, f"{i},"), ROOT)
    hub.publish(S, _delta("root", "c2", 0, "other"), ROOT)
    assert sub.pending_calls == 2, "pending work is per call, never a frame queue"
    frames = sub.drain()
    c1 = [f for f in frames if f["call_id"] == "c1"]
    assert [f["seq"] for f in c1] == list(range(5000))
    assert "".join(f["text"] for f in c1) == "".join(f"{i}," for i in range(5000))


def test_no_byte_cap_a_megabyte_call_arrives_whole(loop) -> None:
    hub = ld.LiveDeltaHub()
    piece = "x" * 4096
    for i in range(256):
        hub.publish(S, _delta("root", "big", i, piece), ROOT)
    sub = hub.subscribe(S, ROOT, loop=loop)
    assert len(sub.snapshot) == 1
    assert len(sub.snapshot[0]["text"]) == 256 * 4096
    assert "truncated" not in sub.snapshot[0]


def test_terminal_closes_open_calls_with_a_synthetic_end_and_frees_the_root(loop) -> None:
    hub = ld.LiveDeltaHub()
    sub = hub.subscribe(S, ROOT, loop=loop)
    hub.publish(S, _delta("root", "c1", 0, "partial"), ROOT)
    hub.publish(S, _delta("child", "k1", 0, "child partial", parent="root"), CHILD)
    sub.drain()

    assert hub.run_terminal(S, "root", "failed", ROOT) == 2
    ends = sub.drain()
    assert sorted((f["call_id"], f["reason"], f["synthetic"], f["seq"]) for f in ends) == [
        ("c1", "failed", True, 1),
        ("k1", "failed", True, 1),
    ]
    sub.close()
    assert hub.tracked_roots() == [], "a finished root's state is freed"

    # The runtime's own (late) events for those calls never reopen a bubble.
    watcher = hub.subscribe(S, ROOT, loop=loop)
    hub.publish(S, _end("root", "c1", 1, "cancelled"), ROOT)
    assert watcher.snapshot == []


def test_cancelled_is_the_synthetic_reason_for_any_non_failed_status(loop) -> None:
    hub = ld.LiveDeltaHub()
    sub = hub.subscribe(S, ROOT, loop=loop)
    hub.publish(S, _delta("root", "c1", 0, "x"), ROOT)
    hub.run_terminal(S, "root", NS(value="cancelled"), ROOT)
    assert [f["reason"] for f in sub.drain() if f["kind"] == "llm.delta_end"] == ["cancelled"]


def test_child_terminal_closes_only_the_child_subtree(loop) -> None:
    hub = ld.LiveDeltaHub()
    sub = hub.subscribe(S, ROOT, loop=loop)
    hub.publish(S, _delta("root", "r1", 0, "root"), ROOT)
    hub.publish(S, _delta("child", "k1", 0, "child", parent="root"), CHILD)
    sub.drain()
    assert hub.run_terminal(S, "child", "cancelled", CHILD) == 1
    assert [(f["call_id"], f["reason"]) for f in sub.drain()] == [("k1", "cancelled")]
    assert hub.open_calls(S, "root") == ["r1"]
    # A late delta of the closed child call is dropped.
    hub.publish(S, _delta("child", "k1", 1, "late", parent="root"), CHILD)
    assert sub.drain() == []
    assert hub.open_calls(S, "root") == ["r1"]


def test_a_delta_end_for_a_call_that_never_streamed_is_forwarded_not_kept(loop) -> None:
    hub = ld.LiveDeltaHub()
    sub = hub.subscribe(S, ROOT, loop=loop)
    hub.publish(S, _end("root", "c9", 0, "unavailable", detail="structured_output"), ROOT)
    frames = sub.drain()
    assert [(f["reason"], f["detail"]) for f in frames] == [("unavailable", "structured_output")]
    assert hub.open_calls(S, "root") == []


def test_duplicate_seq_is_ignored(loop) -> None:
    hub = ld.LiveDeltaHub()
    sub = hub.subscribe(S, ROOT, loop=loop)
    hub.publish(S, _delta("root", "c1", 0, "a"), ROOT)
    hub.publish(S, _delta("root", "c1", 0, "a"), ROOT)
    assert len(sub.drain()) == 1


def test_contract_violations_fail_loudly() -> None:
    hub = ld.LiveDeltaHub()
    with pytest.raises(ld.LiveDeltaError):
        hub.publish(S, {"kind": "llm.chunk", "run_id": "r", "call_id": "c", "seq": 0}, ROOT)
    with pytest.raises(ld.LiveDeltaError):
        hub.publish(S, _delta("root", "c1", 0, "x"), CHILD)  # chain does not start at the run


def test_sse_frame_has_no_id_line() -> None:
    raw = ld.sse_frame({**_delta("root", "c1", 0, "hi"), "root_run_id": "root", "snapshot": False}).decode()
    assert raw.startswith("event: llm.delta\ndata: ")
    assert "\nid:" not in raw and not raw.startswith("id:")
    raw_end = ld.sse_frame(_end("root", "c1", 1)).decode()
    assert raw_end.startswith("event: llm.delta_end\n")


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self, runs: Dict[str, Any]) -> None:
        self.runs = runs
        self.loads: List[str] = []

    def load(self, rid: str) -> Any:
        self.loads.append(rid)
        return self.runs.get(rid)


def _run(rid: str, parent: Optional[str] = None, status: str = "running") -> Any:
    return NS(run_id=rid, parent_run_id=parent, status=NS(value=status))


def test_root_is_resolved_once_per_run() -> None:
    store = _Store({"root": _run("root"), "mid": _run("mid", "root"), "leaf": _run("leaf", "mid")})
    r = ld.RootResolver(store)
    assert r.chain("leaf", "mid", known_parent=True) == ("leaf", "mid", "root")
    n = len(store.loads)
    assert r.chain("leaf", "mid", known_parent=True) == ("leaf", "mid", "root")
    assert r.chain("mid", "root", known_parent=True) == ("mid", "root")
    assert len(store.loads) == n, "cached: no store read for a known run"
    r.forget_root("root")
    r.chain("leaf", "mid", known_parent=True)
    assert len(store.loads) > n


def test_chain_in_the_callers_store() -> None:
    store = _Store({"root": _run("root"), "child": _run("child", "root")})
    assert ld.resolve_chain_in_store(store, store.runs["child"]) == ("child", "root")
    assert ld.resolve_chain_in_store(store, store.runs["root"]) == ("root",)


# ---------------------------------------------------------------------------
# Split mode: file sink, tailer, sweep
# ---------------------------------------------------------------------------


def _lines(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def test_file_sink_writes_0600_lines_and_deletes_at_the_root_end(tmp_path: Path) -> None:
    sink = ld.FileDeltaSink(tmp_path)
    scope = ld.scope_key(tmp_path)
    sink.publish(scope, _delta("root", "c1", 0, "hi"), ROOT)
    sink.publish(scope, _delta("child", "k1", 0, "kid", parent="root"), CHILD)
    path = ld.live_file_path(tmp_path, "root")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    got = _lines(path)
    assert [(g["event"]["call_id"], g["chain"]) for g in got] == [("c1", ["root"]), ("k1", ["child", "root"])]

    # A child's end writes a terminal line; the file stays.
    sink.run_terminal(scope, "child", "completed", CHILD)
    assert _lines(path)[-1] == {"terminal": {"run_id": "child", "status": "completed"}, "chain": ["child", "root"]}

    sink.run_terminal(scope, "root", "cancelled", ROOT)
    assert not path.exists(), "the root's end deletes its live file"
    # Late events of calls that were open at the end never recreate it.
    sink.publish(scope, _delta("root", "c1", 1, "late"), ROOT)
    sink.publish(scope, _end("root", "c1", 2, "cancelled"), ROOT)
    assert not path.exists()


def test_tailer_buffers_partial_lines_and_follows_delete_and_recreate(tmp_path: Path) -> None:
    path = tmp_path / "x.deltas.jsonl"
    tail = ld._FileTail(path)
    assert tail.read_lines() == []  # absent: nothing, no error

    one = json.dumps({"event": _delta("root", "c1", 0, "a"), "chain": ["root"]})
    two = json.dumps({"event": _delta("root", "c1", 1, "b"), "chain": ["root"]})
    with open(path, "w") as fh:
        fh.write(one + "\n" + two[:10])
    assert [o["event"]["seq"] for o in tail.read_lines()] == [0]
    with open(path, "a") as fh:
        fh.write(two[10:] + "\n")
    assert [o["event"]["seq"] for o in tail.read_lines()] == [1], "the partial line was held, then completed"

    # Deleted with a final line appended just before: the open descriptor still reads it.
    three = json.dumps({"terminal": {"run_id": "root", "status": "completed"}, "chain": ["root"]})
    with open(path, "a") as fh:
        fh.write(three + "\n")
    path.unlink()
    assert [list(o) for o in tail.read_lines()] == [["terminal", "chain"]]
    assert tail._fd is None, "a deleted file is closed"

    # Recreated: read from its start.
    with open(path, "w") as fh:
        fh.write(one + "\n")
    assert [o["event"]["seq"] for o in tail.read_lines()] == [0]

    # Replaced under the reader (new inode): the old file is finished, the new one read from 0.
    os.rename(path, tmp_path / "old")
    with open(path, "w") as fh:
        fh.write(two + "\n")
    assert [o["event"]["seq"] for o in tail.read_lines()] == [1]


def test_api_role_subscription_tails_the_runner_file(tmp_path: Path, loop, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ld, "_process_role", ld.ROLE_API)
    scope = ld.scope_key(tmp_path)
    runner = ld.FileDeltaSink(tmp_path)  # what the runner process writes
    runner.publish(scope, _delta("root", "c1", 0, "Hello"), ROOT)
    runner.publish(scope, _delta("root", "c1", 1, " there"), ROOT)

    hub = ld.LiveDeltaHub()
    sub = hub.subscribe(scope, ROOT, loop=loop, data_dir=tmp_path)
    assert [(f["text"], f["snapshot"]) for f in sub.snapshot] == [("Hello there", True)], "caught up before the snapshot"

    runner.publish(scope, _delta("root", "c1", 2, "!"), ROOT)
    runner.run_terminal(scope, "root", "failed", ROOT)
    hub._tails.pump(sub.key)
    frames = sub.drain()
    assert [(f["kind"], f.get("text"), f.get("reason"), f.get("synthetic")) for f in frames] == [
        ("llm.delta", "!", None, None),
        ("llm.delta_end", None, "failed", True),
    ]
    assert not ld.live_file_path(tmp_path, "root").exists()
    sub.close()
    assert hub.tracked_roots() == []
    assert hub._tails._tails == {}


def test_the_tail_thread_delivers_without_a_manual_pump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ld, "_process_role", ld.ROLE_API)
    scope = ld.scope_key(tmp_path)
    runner = ld.FileDeltaSink(tmp_path)
    hub = ld.LiveDeltaHub()

    async def main() -> List[Dict[str, Any]]:
        sub = await asyncio.to_thread(hub.subscribe, scope, ROOT, loop=asyncio.get_running_loop(), data_dir=tmp_path)
        runner.publish(scope, _delta("root", "c1", 0, "tick"), ROOT)
        await asyncio.wait_for(sub.ready.wait(), timeout=5)
        out = sub.drain()
        sub.close()
        return out

    frames = asyncio.run(main())
    assert [f["text"] for f in frames] == ["tick"]


def test_sweep_deletes_only_finished_or_vanished_runs_files(tmp_path: Path) -> None:
    d = ld.live_dir(tmp_path)
    d.mkdir(parents=True)
    for rid in ("done", "failed", "live", "gone"):
        (d / f"{rid}{ld.LIVE_FILE_SUFFIX}").write_text("{}\n")
    (d / "notes.txt").write_text("keep")
    store = _Store({
        "done": _run("done", status="completed"),
        "failed": _run("failed", status="failed"),
        "live": _run("live", status="running"),
    })
    deleted = sorted(Path(p).name for p in ld.sweep_finished_live_files(tmp_path, store))
    assert deleted == ["done.deltas.jsonl", "failed.deltas.jsonl", "gone.deltas.jsonl"]
    assert sorted(p.name for p in d.iterdir()) == ["live.deltas.jsonl", "notes.txt"]


def test_process_role_is_validated() -> None:
    with pytest.raises(ValueError):
        ld.set_process_role("both")


def test_live_files_are_never_written_under_a_relative_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scope that is not an absolute folder (a broken resolver, a bare
    name) must fail loudly instead of writing next to the process's cwd."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ld, "scope_key", lambda data_dir: "one")
    with pytest.raises(ld.LiveDeltaError, match="absolute data folder"):
        ld.FileDeltaSink("one")
    with pytest.raises(ld.LiveDeltaError, match="absolute data folder"):
        ld.live_file_path("one", "root")
    assert not (tmp_path / "one").exists()
