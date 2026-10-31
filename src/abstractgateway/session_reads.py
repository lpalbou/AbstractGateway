"""Durable, per-principal conversation read events (separate from execution ledgers).

Each immutable row acknowledges ONE observed run revision. A request receipt never
acknowledges its later answer, and an older turn completing late stays unread.
The user-event stream is a delivery projection; its retention cannot erase receipts.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SessionReadLedger:
    def __init__(self, data_dir: Path, tenant: str, user: str):
        from .events_stream import stream_principal
        self.tenant, self.user = stream_principal(tenant, user)
        self.path = Path(data_dir) / "session_reads.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE TABLE IF NOT EXISTS read_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant TEXT NOT NULL, user TEXT NOT NULL,
            session_id TEXT NOT NULL, run_id TEXT NOT NULL, status TEXT NOT NULL,
            at TEXT NOT NULL, event_id TEXT NOT NULL UNIQUE, published INTEGER NOT NULL DEFAULT 0,
            UNIQUE(tenant,user,session_id,run_id,status))""")
        db.execute("CREATE INDEX IF NOT EXISTS reads_principal_sequence ON read_events(tenant,user,sequence)")
        # Clients ask for receipts of only the visible run ids. Keep that lookup
        # proportional to the sidebar's runs rather than the account's history.
        db.execute("CREATE INDEX IF NOT EXISTS reads_principal_run ON read_events(tenant,user,run_id,sequence)")
        return db

    @staticmethod
    def _wire(row: sqlite3.Row) -> dict[str, Any]:
        return {k: row[k] for k in ("sequence", "session_id", "run_id", "status", "at", "event_id")}

    def append(self, receipts: list[dict[str, str]]) -> list[dict[str, Any]]:
        db = self._connect()
        try:
            with db:
                for r in receipts:
                    identity = json.dumps([self.tenant, self.user, r["session_id"], r["run_id"], r["status"]], separators=(",", ":"))
                    event_id = "session.read:" + hashlib.sha256(identity.encode()).hexdigest()
                    db.execute("INSERT OR IGNORE INTO read_events(tenant,user,session_id,run_id,status,at,event_id) VALUES(?,?,?,?,?,?,?)",
                               (self.tenant, self.user, r["session_id"], r["run_id"], r["status"], datetime.now(timezone.utc).isoformat(), event_id))
                rows = [db.execute("SELECT * FROM read_events WHERE tenant=? AND user=? AND session_id=? AND run_id=? AND status=?",
                                   (self.tenant, self.user, r["session_id"], r["run_id"], r["status"])).fetchone() for r in receipts]
            return [self._wire(r) for r in rows]
        finally:
            db.close()

    def page(self, after: int = 0, limit: int = 200, run_ids: list[str] | None = None) -> dict[str, Any]:
        if not self.path.exists():
            return {"items": [], "next_after": after, "more": False}
        db = self._connect()
        try:
            where = " AND run_id IN (" + ",".join("?" for _ in run_ids) + ")" if run_ids else ""
            rows = db.execute("SELECT * FROM read_events WHERE tenant=? AND user=? AND sequence>?" + where + " ORDER BY sequence LIMIT ?",
                              (self.tenant, self.user, after, *(run_ids or []), limit + 1)).fetchall()
            return {"items": [self._wire(r) for r in rows[:limit]], "next_after": rows[min(len(rows), limit)-1]["sequence"] if rows else after,
                    "more": len(rows) > limit}
        finally:
            db.close()

    def publish_pending(self, stream: Any) -> None:
        if not self.path.exists():
            return
        db = self._connect()
        try:
            rows = db.execute("SELECT * FROM read_events WHERE tenant=? AND user=? AND published=0 ORDER BY sequence LIMIT 200",
                              (self.tenant, self.user)).fetchall()
            for row in rows:
                stream.append("session.read", event_id=row["event_id"], subject=row["session_id"], time=row["at"], data=self._wire(row))
                with db:
                    db.execute("UPDATE read_events SET published=1 WHERE sequence=?", (row["sequence"],))
        finally:
            db.close()
