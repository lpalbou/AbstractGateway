"""Server side of `bench_replay_view.py`: the seeded run tree and the gateway launcher.

Run as a script, this is the gateway process the benchmark measures: it configures the gateway
on a fresh data dir, serves `abstractgateway.app:app` with uvicorn in a thread on 127.0.0.1, seeds
a delegated run tree through the gateway's OWN stores (`svc.host.run_store` /
`svc.host.ledger_store`), and then appends ledger records IN THIS PROCESS on command. Appends go
through the same `ObservableLedgerStore` a runner appends through, so they wake `/view/stream`
and `/ledger/stream` exactly as a live run does (no cross-process poll is involved).

Control protocol (one JSON object per line on stdin; one reply per command on stdout, prefixed
`@@BENCH `; every other stdout line is ignored by the benchmark):

    {"cmd": "append", "n": 300, "interval_s": 0.05}  -> {"appended": [[seq, t_before, t_after], ...]}
    {"cmd": "clear_hub"}                             -> {"ok": true}   (the next GET /view is cold)
    {"cmd": "hub_stats"}                             -> {"folds", "records_folded", "subscribers"}
    {"cmd": "quit"}

Imported (by the benchmark), it provides the same seed for the in-process microbenchmarks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import asdict
from typing import Any, Dict, Iterator, List, Optional, Tuple

ROOT = "a1b2c3d4-root-4000-8000-000000000001"
CHILD = "e5f6a7b8-chld-4000-8000-000000000002"
TOKEN = "bench-" + "t" * 32
RECORDS_PER_BLOCK = 5  # llm STARTED, progress, llm COMPLETED, tool STARTED, tool COMPLETED

_SYSTEM_SHORT = "You are a careful coding agent. Use tools to inspect and change files. " * 4  # < 512 B: never slimmed
_SYSTEM_LONG = ("You are a careful coding agent working in a repository. Follow the project rules, read before "
                "you write, run the tests you touch, and report what changed. ") * 16  # > 512 B: slimmed


def _ts(i: int) -> str:
    """A deterministic, ordered timestamp per record index (one second apart)."""
    h, rem = divmod(int(i), 3600)
    m, s = divmod(rem, 60)
    return f"2026-10-27T{10 + (h % 13):02d}:{m:02d}:{s:02d}.{(i * 7919) % 1000000:06d}+00:00"


def snapshots() -> Tuple[Any, Any]:
    """The run tree's durable snapshots: the root waits on its delegated child; the child runs."""
    from abstractruntime.core.models import RunState, RunStatus, WaitReason, WaitState

    sub_wait = WaitState(reason=WaitReason.SUBWORKFLOW, wait_key=f"subworkflow:{CHILD}", details={"sub_run_id": CHILD})
    root = RunState(run_id=ROOT, workflow_id="bench:default-agent", status=RunStatus.WAITING, current_node="agent",
                    vars={"prompt": "Refactor the storage layer and keep every test green."}, waiting=sub_wait,
                    created_at="2026-10-27T10:00:00+00:00")
    child = RunState(run_id=CHILD, workflow_id="bench:react-agent", status=RunStatus.RUNNING, current_node="think",
                     vars={"task": "Refactor the storage layer."}, parent_run_id=ROOT,
                     created_at="2026-10-27T10:00:00.500000+00:00")
    return root, child


def root_records() -> List[Any]:
    """The root's ledger: it delegates to the child and waits on `subworkflow:<child>`."""
    from abstractruntime.core.models import StepRecord, StepStatus

    eff = {"type": "start_subworkflow", "payload": {"workflow_id": "bench:react-agent", "sub_run_id": CHILD},
           "result_key": "agent_result"}
    a = StepRecord(run_id=ROOT, step_id="s-root-1", node_id="agent", status=StepStatus.STARTED, effect=eff,
                   idempotency_key="root-agent-1", started_at=_ts(0))
    b = StepRecord(run_id=ROOT, step_id="s-root-1", node_id="agent", status=StepStatus.WAITING, effect=eff,
                   idempotency_key="root-agent-1", started_at=_ts(0), ended_at=_ts(0),
                   result={"wait": {"reason": "subworkflow", "wait_key": f"subworkflow:{CHILD}",
                                    "details": {"sub_run_id": CHILD}}})
    return [a, b]


def child_block(i: int) -> List[Any]:
    """One agent iteration of the child (5 records): an LLM call (STARTED, a progress event,
    COMPLETED with usage and a tool call) and its tool execution (STARTED, COMPLETED with the
    result). Block 0 carries a large system prompt whose COMPLETED copy is a real `$slim` marker
    (`slim_terminal_effect` with STARTED-time digests, as the runtime writes it)."""
    from abstractruntime.core.models import StepRecord, StepStatus
    from abstractruntime.storage.ledger_slim import capture_started_payload_digests, slim_terminal_effect

    base = 10 + i * RECORDS_PER_BLOCK
    llm_step, tool_step = f"llm-{i:05d}", f"tool-{i:05d}"
    call_id = f"call-{i:05d}"
    path = f"src/storage/module_{i % 37:02d}.py"
    messages = [
        {"role": "user", "content": "Refactor the storage layer."},
        {"role": "assistant", "content": f"Step {i}: reading {path}."},
        {"role": "tool", "content": f"(contents of {path}, {i * 13 % 400} lines)"},
    ]
    llm_eff = {"type": "llm_call", "result_key": "_temp.llm",
               "payload": {"provider": "lmstudio", "model": "qwen/qwen3-coder-30b",
                           "prompt": f"Continue the task. Iteration {i}.",
                           "system_prompt": _SYSTEM_LONG if i == 0 else _SYSTEM_SHORT,
                           "messages": messages, "tools": ["read_file", "write_file", "run_tests"],
                           "temperature": 0.2}}
    started = StepRecord(run_id=CHILD, step_id=llm_step, node_id="think", status=StepStatus.STARTED, effect=llm_eff,
                         idempotency_key=f"idem-{llm_step}", started_at=_ts(base))
    done_eff = llm_eff
    if i == 0:
        done_eff = slim_terminal_effect(llm_eff, step_id=llm_step, started_digests=capture_started_payload_digests(llm_eff))
        assert done_eff is not llm_eff and "$slim" in json.dumps(done_eff), "the $slim pair was not slimmed"
    tool_call = {"name": "read_file" if i % 3 else "write_file", "call_id": call_id, "runtime_call_id": f"rt-{call_id}",
                 "arguments": {"file_path": path, "start_line": 1, "end_line": 120}}
    progress = StepRecord(run_id=CHILD, step_id=f"prog-{i:05d}", node_id="think", status=StepStatus.COMPLETED,
                          effect={"type": "emit_event", "payload": {"name": "abstract.progress", "scope": "run",
                                                                    "payload": {"stage": "generating", "tokens": 40 + i % 90}}},
                          result={"emitted": True, "name": "abstract.progress",
                                  "payload": {"stage": "generating", "tokens": 40 + i % 90}},
                          idempotency_key=f"system:progress:{llm_step}:{uuid.UUID(int=i).hex[:12]}",
                          started_at=_ts(base + 1), ended_at=_ts(base + 1))
    completed = StepRecord(run_id=CHILD, step_id=llm_step, node_id="think", status=StepStatus.COMPLETED,
                           effect=done_eff, idempotency_key=f"idem-{llm_step}", started_at=_ts(base), ended_at=_ts(base + 2),
                           result={"content": f"I will read {path} next to check the call sites (iteration {i}).",
                                   "model": "qwen/qwen3-coder-30b", "finish_reason": "tool_calls",
                                   "tool_calls": [tool_call],
                                   "usage": {"prompt_tokens": 1800 + i, "completion_tokens": 60 + i % 50,
                                             "total_tokens": 1860 + i + i % 50}})
    tc_eff = {"type": "tool_calls", "payload": {"tool_calls": [tool_call]}, "result_key": "_temp.tool_results"}
    tool_started = StepRecord(run_id=CHILD, step_id=tool_step, node_id="act", status=StepStatus.STARTED, effect=tc_eff,
                              idempotency_key=f"idem-{tool_step}", started_at=_ts(base + 3))
    tool_done = StepRecord(run_id=CHILD, step_id=tool_step, node_id="act", status=StepStatus.COMPLETED, effect=tc_eff,
                           idempotency_key=f"idem-{tool_step}", started_at=_ts(base + 3), ended_at=_ts(base + 4),
                           result={"mode": "executed", "results": [
                               {"call_id": call_id, "runtime_call_id": f"rt-{call_id}", "name": tool_call["name"],
                                "success": True, "output": f"# {path}\n" + "def f():\n    return 1\n" * 12}]})
    return [started, progress, completed, tool_started, tool_done]


def child_records(start_block: int, n_records: int) -> Iterator[Any]:
    """`n_records` child records from block `start_block` on (cut mid-block if need be)."""
    i, left = int(start_block), int(n_records)
    while left > 0:
        for rec in child_block(i)[:left]:
            yield rec
            left -= 1
        i += 1


def as_dicts(records: List[Any]) -> List[Dict[str, Any]]:
    """StepRecords as the ledger serves them (`asdict` + a dense per-run `seq`)."""
    out: List[Dict[str, Any]] = []
    seq: Dict[str, int] = {}
    for r in records:
        d = asdict(r)
        d["status"] = getattr(r.status, "value", r.status)
        seq[r.run_id] = seq.get(r.run_id, 0) + 1
        d["seq"] = seq[r.run_id]
        out.append(d)
    return out


# --------------------------------------------------------------------------- launcher


def _reply(obj: Dict[str, Any]) -> None:
    sys.stdout.write("@@BENCH " + json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--backend", choices=("file", "sqlite"), required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--seed-records", type=int, default=3000, help="child records seeded before serving")
    args = ap.parse_args()

    data = os.path.abspath(args.data_dir)
    os.makedirs(os.path.join(data, "flows"), exist_ok=True)
    os.environ.update({
        "ABSTRACTGATEWAY_DATA_DIR": os.path.join(data, "runtime"),
        "ABSTRACTGATEWAY_FLOWS_DIR": os.path.join(data, "flows"),
        "ABSTRACTGATEWAY_WORKFLOW_SOURCE": "bundle",
        "ABSTRACTGATEWAY_AUTH_TOKEN": TOKEN,
        "ABSTRACTGATEWAY_ALLOWED_ORIGINS": "*",
        "ABSTRACTGATEWAY_STORE_BACKEND": args.backend,
        "ABSTRACTGATEWAY_RUNNER": "0",
        # 50 BEFORE clients follow two ledger streams each; the default SSE pool (32) refuses most.
        "ABSTRACTGATEWAY_MAX_SSE": os.environ.get("ABSTRACTGATEWAY_MAX_SSE", "256"),
    })

    import uvicorn

    from abstractgateway.app import app
    from abstractgateway.replay_view import get_view_hub
    from abstractgateway.service import get_gateway_service

    config = uvicorn.Config(app, host="127.0.0.1", port=int(args.port), log_level="warning", access_log=False,
                            lifespan="on", timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, name="bench-uvicorn", daemon=True)
    th.start()
    deadline = time.time() + 60
    while not server.started:
        if not th.is_alive() or time.time() > deadline:
            _reply({"error": "uvicorn did not start"})
            return 2
        time.sleep(0.05)

    svc = get_gateway_service()  # off the loop: builds (or joins) the service the routes use
    rs, ls = svc.host.run_store, svc.host.ledger_store
    root, child = snapshots()
    rs.save(root)
    rs.save(child)
    t0 = time.perf_counter()
    for rec in root_records():
        ls.append(rec)
    n_blocks_seeded = -(-int(args.seed_records) // RECORDS_PER_BLOCK)
    for rec in child_records(0, int(args.seed_records)):
        ls.append(rec)
    seed_s = time.perf_counter() - t0
    next_block = n_blocks_seeded
    ledger_bytes = 0
    for dirpath, _dirs, files in os.walk(data):
        for f in files:
            if f.endswith((".jsonl", ".sqlite", ".db", ".sqlite3")) or "ledger" in f:
                try:
                    ledger_bytes += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    _reply({"ready": True, "pid": os.getpid(), "root": ROOT, "child": CHILD, "token": TOKEN, "seed_s": seed_s,
            "ledger_store": type(ls).__name__, "store_bytes": ledger_bytes, "child_records": int(args.seed_records)})

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            cmd = json.loads(line)
            op = cmd.get("cmd")
            if op == "append":
                n, interval = int(cmd["n"]), float(cmd.get("interval_s", 0.05))
                out: List[List[float]] = []
                recs = list(child_records(next_block, n))
                next_block += -(-n // RECORDS_PER_BLOCK)
                t_next = time.perf_counter()
                for rec in recs:
                    delay = t_next - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                    t_before = time.time()
                    seq = ls.append(rec)
                    out.append([seq, t_before, time.time()])
                    t_next += interval
                _reply({"appended": out})
            elif op == "clear_hub":
                get_view_hub().clear()
                _reply({"ok": True})
            elif op == "hub_stats":
                hub = get_view_hub()
                entry = None
                for (_ls_id, rid), e in list(hub._entries.items()):  # noqa: SLF001 - a benchmark probe
                    if rid == ROOT:
                        entry = e
                _reply({"folds": getattr(entry, "folds", None), "records_folded": getattr(entry, "records_folded", None),
                        "subscribers": len(entry.subscribers) if entry is not None else None})
            elif op == "quit":
                _reply({"bye": True})
                break
            else:
                _reply({"error": f"unknown command {op!r}"})
    finally:
        server.should_exit = True
        th.join(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
