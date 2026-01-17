from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GatewayStores:
    """Concrete durability stores used by a gateway host."""

    base_dir: Path
    run_store: Any
    ledger_store: Any
    artifact_store: Any
    command_store: Any
    command_cursor_store: Any


def build_file_stores(*, base_dir: Path) -> GatewayStores:
    """Create file-backed Run/Ledger/Artifact stores under base_dir.

    Contract: base_dir is owned by the gateway host process (durable control plane).
    """

    from abstractruntime import (
        FileArtifactStore,
        JsonFileCommandCursorStore,
        JsonFileRunStore,
        JsonlCommandStore,
        JsonlLedgerStore,
        OffloadingLedgerStore,
        OffloadingRunStore,
        ObservableLedgerStore,
    )

    base = Path(base_dir).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)

    artifact_store = FileArtifactStore(base)
    run_store = OffloadingRunStore(JsonFileRunStore(base), artifact_store=artifact_store)
    ledger_store = OffloadingLedgerStore(ObservableLedgerStore(JsonlLedgerStore(base)), artifact_store=artifact_store)
    command_store = JsonlCommandStore(base)
    command_cursor_store = JsonFileCommandCursorStore(base / "commands_cursor.json")
    return GatewayStores(
        base_dir=base,
        run_store=run_store,
        ledger_store=ledger_store,
        artifact_store=artifact_store,
        command_store=command_store,
        command_cursor_store=command_cursor_store,
    )


def build_sqlite_stores(*, base_dir: Path, db_path: Path | None = None) -> GatewayStores:
    """Create SQLite-backed stores under base_dir.

    Note:
    - Artifacts remain file-backed (blobs on disk); the DB stores structured metadata/state.
    - The DB file defaults to `<base_dir>/gateway.sqlite3` when db_path is not provided.
    """

    from abstractruntime import (
        FileArtifactStore,
        ObservableLedgerStore,
        OffloadingLedgerStore,
        OffloadingRunStore,
        SqliteCommandCursorStore,
        SqliteCommandStore,
        SqliteDatabase,
        SqliteLedgerStore,
        SqliteRunStore,
    )

    base = Path(base_dir).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)

    db_file = Path(db_path).expanduser().resolve() if db_path is not None else (base / "gateway.sqlite3")
    db = SqliteDatabase(db_file)

    artifact_store = FileArtifactStore(base)
    run_store = OffloadingRunStore(SqliteRunStore(db), artifact_store=artifact_store)
    ledger_store = OffloadingLedgerStore(ObservableLedgerStore(SqliteLedgerStore(db)), artifact_store=artifact_store)
    command_store = SqliteCommandStore(db)
    command_cursor_store = SqliteCommandCursorStore(db, consumer_id="gateway_runner")

    return GatewayStores(
        base_dir=base,
        run_store=run_store,
        ledger_store=ledger_store,
        artifact_store=artifact_store,
        command_store=command_store,
        command_cursor_store=command_cursor_store,
    )
