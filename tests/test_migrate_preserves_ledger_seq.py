"""File -> SQLite migration keeps every ledger record, its order and its `seq` (backlog 0082 I3/I4).

A client holding a cursor (the last seq it consumed) before the migration must get exactly the
same records after it: `list_after(cursor)` on the migrated SQLite store equals the file
store's. Covers a pre-seq ledger (lines without `seq`, numbered by position) and a line with two
concatenated records (the JSONL recovery path), which the old migration skipped and renumbered.
"""
from __future__ import annotations

import json
from pathlib import Path

from abstractruntime import JsonlLedgerStore, SqliteDatabase, SqliteLedgerStore
from abstractruntime.core.models import StepRecord, StepStatus

from abstractgateway.migrate import migrate_file_to_sqlite


def _rec(run_id: str, i: int, status: StepStatus = StepStatus.COMPLETED) -> StepRecord:
    return StepRecord(run_id=run_id, step_id=f"s{i}", node_id=f"n{i}", status=status,
                      idempotency_key=f"k{i}", result={"i": i})


def test_migration_preserves_seq_order_count_and_cursor(tmp_path: Path) -> None:
    base = tmp_path / "files"
    base.mkdir()
    # A ledger written before seq existed, with one concatenated line (recovery path).
    legacy = [{"run_id": "old", "step_id": f"s{i}", "node_id": "n", "status": "completed"} for i in range(1, 5)]
    (base / "ledger_old.jsonl").write_text(
        json.dumps(legacy[0]) + "\n" + json.dumps(legacy[1]) + json.dumps(legacy[2]) + "\n" + json.dumps(legacy[3]) + "\n",
        encoding="utf-8")
    files = JsonlLedgerStore(base)
    # ...then appended to by a seq-aware writer: numbering continues after the legacy lines.
    assert files.append(_rec("old", 5)) == 5
    for i in range(1, 8):
        assert files.append(_rec("new", i)) == i

    before = {rid: files.list(rid) for rid in ("old", "new")}
    assert [r["seq"] for r in before["old"]] == [1, 2, 3, 4, 5]
    held = {rid: files.list_after(run_id=rid, after=3) for rid in ("old", "new")}

    db_path = tmp_path / "gw.sqlite3"
    migrate_file_to_sqlite(base_dir=base, db_path=db_path)
    sql = SqliteLedgerStore(SqliteDatabase(db_path))

    for rid in ("old", "new"):
        after = sql.list(rid)
        assert [r["seq"] for r in after] == [r["seq"] for r in before[rid]]
        assert after == before[rid]  # every record, in order, same content
        assert sql.list_after(run_id=rid, after=3) == held[rid]  # a held cursor means the same position
    # The SQLite head continues the numbering.
    assert sql.append(_rec("old", 6)) == 6
    assert sql.find_completed_result("new", "k2") == {"i": 2}  # the idempotency index was filled
