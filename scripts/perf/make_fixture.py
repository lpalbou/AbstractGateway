"""Synthetic benchmark fixture for the gateway load test (backlog 1054): a gateway data folder with
N runs, M artifacts and K automations, written through the runtime's own stores (so every file is
in the format the gateway reads), with no operator data at all.

    python scripts/perf/make_fixture.py --dest DIR [--runs 10000] [--artifacts 100000]
                                        [--automations 50] [--session ID] [--replace]

Shape (deterministic for a given --seed):
  - K automations: a controller run each, `--occurrences` occurrence runs below it, and the
    controller ledger (admitted / dispatched / completed per occurrence; every 10th raises a
    notify) that `GET /automations` reads backwards;
  - the rest are chats: sessions of a root turn plus follow-up children, a small ledger per root,
    1% failed, a few WAITING on a person; the session `--session` is one of them (its root run id
    is the session id, as `bench_gateway.sh` expects) and holds `--session-artifacts` artifacts;
  - M artifacts spread over the chat runs, tagged with their session (the session-artifacts
    route reads them through the catalog's session index). Contents come from a pool of 1,000
    small blobs, so the folder holds M metadata files but ~1,000 blobs. The SQLite catalog is
    filled in one transaction (the store's own row builder), not one commit per artifact;
  - `gateway_paused.json` (no run executes) and `README-scrub.md` (nothing to scrub: synthetic).

Prints one JSON line with the counts, the session id and the seconds taken.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_SESSION = "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"
CONTROLLER_WORKFLOW_ID = "abstractframework.automation-controller@1.0.0:controller"
TARGET_WORKFLOW_ID = "bundle-target@0.0.0:root"
CHAT_WORKFLOW_ID = "basic-agent@1.0.0:root"


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def _run(RunState: Any, RunStatus: Any, *, run_id: str, workflow_id: str, at: datetime, vars: Dict[str, Any],
         session_id: str | None, parent_run_id: str | None = None, status: Any = None, output: Any = None) -> Any:
    return RunState(
        run_id=run_id, workflow_id=workflow_id, status=status or RunStatus.COMPLETED, current_node="end", vars=vars,
        output=output if output is not None else {"success": True, "response": "ok"},
        created_at=_iso(at), updated_at=_iso(at), session_id=session_id, parent_run_id=parent_run_id,
    )


def build(dest: Path, *, runs: int, artifacts: int, automations: int, occurrences: int, session: str,
          session_artifacts: int, seed: int) -> Dict[str, Any]:
    from abstractruntime import FileArtifactStore, JsonFileRunStore, JsonlLedgerStore
    from abstractruntime.automations.ledger import _step_record
    from abstractruntime.core.models import RunState, RunStatus, StepRecord, StepStatus, WaitReason, WaitState

    rnd = random.Random(seed)
    t_start = time.perf_counter()
    dest.mkdir(parents=True, exist_ok=True)
    run_store = JsonFileRunStore(dest)
    ledger = JsonlLedgerStore(dest)
    now = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)
    t0 = now - timedelta(days=365)
    n_auto_runs = automations * (1 + occurrences)
    if n_auto_runs >= runs:
        raise SystemExit(f"--runs {runs} leaves no room for chats after {n_auto_runs} automation runs")
    span = (now - t0).total_seconds()
    saved = 0

    def at(i: int) -> datetime:
        return t0 + timedelta(seconds=span * i / max(1, runs))

    # Automations first (oldest), then chats, so the newest runs are chats like on a real gateway.
    for a in range(automations):
        aid = str(uuid.UUID(int=rnd.getrandbits(128)))
        created = at(saved)
        definition = {
            "schema_version": 1, "revision": 1, "title": f"Automation {a:02d}",
            "controller": {"bundle_ref": "abstractframework.automation-controller@1.0.0", "flow_id": "controller"},
            "target": {"workflow_id": TARGET_WORKFLOW_ID, "bundle_ref": "bundle-target@0.0.0", "flow_id": "root", "input_data": {}},
            "trigger": {"binding_id": str(uuid.UUID(int=rnd.getrandbits(128))), "source_id": "schedule", "source_version": 1,
                        "config": {"start_at": _iso(created), "anchor": _iso(created), "every": "1h"}},  # the normalized shape
            "context": {"mode": "independent", "growing": {}},
            "policy": {"serial": True, "misfire": "coalesce", "failure": "continue",
                       "retry": {"max_attempts": 3, "backoff": {"initial": "30s", "factor": 2, "max": "10m"}}},
            "session_id": f"automation-session:{aid}", "workspace_root": "/tmp/ws", "created_at": _iso(created), "archived_at": None,
        }
        state = {"state_version": 1, "active_revision": 1, "pending_occurrence": None, "anchor": _iso(created), "tick": 0,
                 "next_index": occurrences + 1, "scheduled_count": 0, "paused": a % 7 == 0, "attention_seq": occurrences // 10,
                 "exhausted": False, "manual_pending": None,
                 "last_outcome": {"run_id": "", "index": occurrences, "status": "completed", "attempts": 1, "finished_at": _iso(now)}}
        controller = _run(RunState, RunStatus, run_id=aid, workflow_id=CONTROLLER_WORKFLOW_ID, at=created,
                          vars={"_meta": {"automation": definition}, "_runtime": {"automation": state}},
                          session_id=definition["session_id"], status=RunStatus.WAITING)
        run_store.save(controller)
        saved += 1
        seq = 0
        for i in range(1, occurrences + 1):
            when = at(saved)
            child = _run(RunState, RunStatus, run_id=str(uuid.UUID(int=rnd.getrandbits(128))), workflow_id=TARGET_WORKFLOW_ID, at=when,
                         vars={"_meta": {"occurrence": {"automation_id": aid, "occurrence_index": i, "attempt": 1, "event_id": f"e{i}",
                                                        "revision": 1, "role": "occurrence", "session_kind": "occurrence",
                                                        "fired_at": _iso(when), "trigger_envelope": {}}}},
                         session_id=f"occ-session:{aid}:{i}", parent_run_id=aid,
                         output={"response": f"answer {i} " + "x" * 200, "success": True})
            run_store.save(child)
            saved += 1
            attention = None
            if i % 10 == 0:
                seq += 1
                attention = {"kind": "notify", "seq": seq, "title": f"Automation {a:02d}", "body": f"occurrence {i}"}
            for name, disc, payload in (
                ("automation.admitted", i, {"run_id": child.run_id, "index": i, "revision": 1, "event_id": f"e{i}",
                                            "trigger_envelope": {"fired_at": _iso(when), "source_id": "schedule", "source_version": 1},
                                            "prepared": {"input_data": {"prompt": f"prompt {i} " + "p" * 200}}}),
                ("automation.dispatched", child.run_id, {"run_id": child.run_id, "index": i, "attempt": 1}),
                ("automation.completed", i, {"run_id": child.run_id, "index": i, "status": "completed", "attempts": 1,
                                             "finished_at": _iso(when), "notify": None, "attention": attention}),
            ):
                ledger.append(_step_record(controller, name=name, key=f"automation:{name}:{aid}:{disc}", payload=payload,
                                           node_id="n", at=_iso(when)))

    # Chats: sessions of a root turn and 0-4 follow-ups; the probed session is the last (newest) one.
    chat_runs: List[Any] = []
    left = runs - saved
    sessions: List[str] = []
    while left > 0:
        last = left <= 5
        sid = session if last else str(uuid.UUID(int=rnd.getrandbits(128)))
        n = left if last else min(left - 1, rnd.randint(1, 5))
        root_id = sid  # the session's root run id is the session id (bench_gateway.sh checks run_<session>.json)
        for k in range(n):
            idx = runs - left
            status = RunStatus.COMPLETED
            output = None
            if rnd.random() < 0.01:
                status, output = RunStatus.FAILED, {"success": False, "error": "synthetic failure"}
            run = _run(RunState, RunStatus, run_id=root_id if k == 0 else str(uuid.UUID(int=rnd.getrandbits(128))),
                       workflow_id=CHAT_WORKFLOW_ID, at=at(idx), vars={"prompt": f"question {idx} " + "q" * 120},
                       session_id=sid, parent_run_id=None if k == 0 else root_id, status=status, output=output)
            if k == 0 and len(sessions) % 500 == 1:
                run.status = RunStatus.WAITING
                run.waiting = WaitState(reason=WaitReason.USER, wait_key=f"ask:{run.run_id}", prompt="Proceed?")
            run_store.save(run)
            chat_runs.append(run)
            left -= 1
            if k == 0:
                for s in range(3):
                    ledger.append(StepRecord(run_id=run.run_id, step_id=f"{run.run_id}:{s}", node_id=f"n{s}",
                                             status=StepStatus.COMPLETED, effect=None, result={"text": "step " + "r" * 80},
                                             started_at=_iso(at(idx)), ended_at=_iso(at(idx)), session_id=sid))
        sessions.append(sid)
    t_runs = time.perf_counter() - t_start

    # Artifacts: metadata files through the store, the catalog in one transaction.
    collected: List[Any] = []

    class _BulkArtifactStore(FileArtifactStore):
        def _index_metadata(self, meta: Any, *, conn: Any = None) -> None:  # noqa: D401 - collected, inserted below
            if conn is None:
                collected.append(meta)
                return
            super()._index_metadata(meta, conn=conn)

    store = _BulkArtifactStore(dest)
    pool = [f"synthetic artifact content {i}\n".encode() * 8 for i in range(1000)]
    probe_runs = [r for r in chat_runs if r.session_id == session]
    others = [r for r in chat_runs if r.session_id != session]
    for i in range(artifacts):
        run = probe_runs[i % len(probe_runs)] if i < session_artifacts else others[rnd.randrange(len(others))]
        store.store(pool[i % len(pool)], content_type="text/plain", run_id=run.run_id,
                    tags={"session_id": str(run.session_id), "kind": "note"}, artifact_id=uuid.UUID(int=rnd.getrandbits(128)).hex)
    with store._connect_catalog() as conn:
        for meta in collected:
            FileArtifactStore._index_metadata(store, meta, conn=conn)
    t_artifacts = time.perf_counter() - t_start - t_runs

    (dest / "gateway_paused.json").write_text(json.dumps({"paused": True, "paused_at": _iso(now), "paused_by": "perf-fixture",
                                                          "reason": "benchmark fixture: no run may execute"}), encoding="utf-8")
    (dest / "README-scrub.md").write_text(
        "# Synthetic benchmark fixture\n\nWritten by scripts/perf/make_fixture.py: no operator data, nothing to scrub.\n", encoding="utf-8")
    out = {"dest": str(dest), "runs": saved + len(chat_runs), "automations": automations, "occurrences_each": occurrences,
           "chat_sessions": len(sessions), "artifacts": len(collected), "session": session,
           "session_artifacts": min(session_artifacts, artifacts), "seconds": {"runs": round(t_runs, 1), "artifacts": round(t_artifacts, 1)}}
    (dest / "fixture.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dest", required=True, type=Path)
    ap.add_argument("--runs", type=int, default=10_000)
    ap.add_argument("--artifacts", type=int, default=100_000)
    ap.add_argument("--automations", type=int, default=50)
    ap.add_argument("--occurrences", type=int, default=20, help="occurrence runs per automation")
    ap.add_argument("--session", default=DEFAULT_SESSION)
    ap.add_argument("--session-artifacts", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1054)
    ap.add_argument("--replace", action="store_true", help="delete --dest first when it exists")
    a = ap.parse_args(argv)
    dest = a.dest.expanduser().resolve()
    if dest.exists() and any(dest.iterdir()):
        if not a.replace:
            print(f"{dest} exists and is not empty: pass --replace", file=sys.stderr)
            return 2
        if not (dest / "README-scrub.md").is_file() or "Synthetic benchmark fixture" not in (dest / "README-scrub.md").read_text(encoding="utf-8"):
            print(f"refusing to replace {dest}: it is not a synthetic fixture (no README-scrub.md written by this script)", file=sys.stderr)
            return 2
        shutil.rmtree(dest)
    out = build(dest, runs=a.runs, artifacts=a.artifacts, automations=a.automations, occurrences=a.occurrences,
                session=a.session, session_artifacts=a.session_artifacts, seed=a.seed)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
