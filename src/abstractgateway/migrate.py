from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


def _safe_json_loads(line: str) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(line)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def migrate_file_to_sqlite(*, base_dir: Path, db_path: Path, overwrite: bool = False) -> None:
    """Best-effort migration from file-backed stores to SQLite-backed stores.

    This is intended for local testing and single-host deployments.

    Migrates:
    - runs:   `run_*.json`          -> `runs` table (+ wait_index)
    - ledger: `ledger_*.jsonl`      -> `ledger` table (+ ledger_heads)
    - inbox:  `commands.jsonl`      -> `commands` table
    - cursor: `commands_cursor.json` -> `command_cursors` row (consumer_id=gateway_runner)
    """

    base = Path(base_dir).expanduser().resolve()
    if not base.exists():
        raise FileNotFoundError(f"base_dir does not exist: {base}")

    db_file = Path(db_path).expanduser().resolve()
    if db_file.exists():
        if not overwrite:
            raise FileExistsError(f"Destination DB already exists: {db_file} (use --overwrite to replace)")
        db_file.unlink()

    from abstractruntime import JsonFileRunStore, SqliteDatabase, SqliteRunStore

    db = SqliteDatabase(db_file)
    src_runs = JsonFileRunStore(base)
    dst_runs = SqliteRunStore(db)

    # --- Runs ---
    for p in sorted(base.glob("run_*.json")):
        run_id = p.stem[len("run_") :] if p.stem.startswith("run_") else ""
        if not run_id:
            continue
        run = src_runs.load(run_id)
        if run is None:
            continue
        dst_runs.save(run)

    conn = db.connection()

    # --- Ledger ---
    for p in sorted(base.glob("ledger_*.jsonl")):
        run_id = p.stem[len("ledger_") :] if p.stem.startswith("ledger_") else ""
        if not run_id:
            continue
        seq = 0
        with conn:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = _safe_json_loads(line)
                    if obj is None:
                        continue
                    seq += 1
                    conn.execute(
                        "INSERT INTO ledger (run_id, seq, record_json) VALUES (?, ?, ?);",
                        (run_id, int(seq), json.dumps(obj, ensure_ascii=False)),
                    )
            if seq > 0:
                conn.execute(
                    """
                    INSERT INTO ledger_heads (run_id, last_seq)
                    VALUES (?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET last_seq=excluded.last_seq;
                    """,
                    (run_id, int(seq)),
                )

    # --- Commands inbox (optional, best-effort) ---
    commands_path = base / "commands.jsonl"
    if commands_path.exists():
        with conn:
            with commands_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = _safe_json_loads(line)
                    if obj is None:
                        continue
                    try:
                        seq = int(obj.get("seq") or 0)
                    except Exception:
                        seq = 0
                    command_id = str(obj.get("command_id") or "").strip()
                    run_id = str(obj.get("run_id") or "").strip()
                    typ = str(obj.get("type") or "").strip()
                    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                    ts = str(obj.get("ts") or "").strip()
                    client_id = str(obj.get("client_id") or "").strip() or None

                    if not command_id or not run_id or not typ or not ts:
                        continue
                    if seq <= 0:
                        continue

                    conn.execute(
                        """
                        INSERT OR IGNORE INTO commands (seq, command_id, run_id, type, payload_json, ts, client_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            int(seq),
                            command_id,
                            run_id,
                            typ,
                            json.dumps(payload, ensure_ascii=False),
                            ts,
                            client_id,
                        ),
                    )

    # --- Cursor ---
    cursor_path = base / "commands_cursor.json"
    if cursor_path.exists():
        try:
            cursor_obj = json.loads(cursor_path.read_text(encoding="utf-8") or "{}")
        except Exception:
            cursor_obj = {}
        cur_val = cursor_obj.get("cursor")
        try:
            cur_i = int(cur_val or 0)
        except Exception:
            cur_i = 0
        with conn:
            conn.execute(
                """
                INSERT INTO command_cursors (consumer_id, cursor, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(consumer_id) DO UPDATE SET
                  cursor=excluded.cursor,
                  updated_at=excluded.updated_at;
                """,
                ("gateway_runner", int(cur_i), str(cursor_obj.get("updated_at") or "")),
            )

