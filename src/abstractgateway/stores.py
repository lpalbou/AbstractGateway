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


def build_file_stores(*, base_dir: Path) -> GatewayStores:
    """Create file-backed Run/Ledger/Artifact stores under base_dir.

    Contract: base_dir is owned by the gateway host process (durable control plane).
    """

    from abstractruntime import FileArtifactStore, JsonFileRunStore, JsonlLedgerStore, ObservableLedgerStore

    base = Path(base_dir).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)

    run_store = JsonFileRunStore(base)
    ledger_store = ObservableLedgerStore(JsonlLedgerStore(base))
    artifact_store = FileArtifactStore(base)
    return GatewayStores(base_dir=base, run_store=run_store, ledger_store=ledger_store, artifact_store=artifact_store)


