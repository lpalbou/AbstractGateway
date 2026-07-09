from __future__ import annotations

import json
import zipfile
from pathlib import Path

from abstractruntime.storage.artifacts import InMemoryArtifactStore
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore


def _write_min_bundle(*, bundles_dir: Path, bundle_id: str = "framework-demo", flow_id: str = "root") -> Path:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": flow_id,
        "name": flow_id,
        "entryNode": "start",
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "data": {
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}],
                },
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "data": {
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                },
            },
        ],
        "edges": [
            {
                "id": "e-start-end",
                "source": "start",
                "sourceHandle": "exec-out",
                "target": "end",
                "targetHandle": "exec-in",
            }
        ],
    }
    manifest = {
        "bundle_format_version": 1,
        "bundle_id": bundle_id,
        "bundle_version": "0.0.1",
        "created_at": "2026-06-28T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": flow_id, "description": "", "interfaces": []}],
        "default_entrypoint": flow_id,
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow))
    return path


def test_per_principal_host_loads_framework_bundles_from_empty_private_dir(tmp_path: Path) -> None:
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

    private_dir = tmp_path / "user" / "flows"
    framework_dir = tmp_path / "framework" / "flows"
    private_dir.mkdir(parents=True)
    _write_min_bundle(bundles_dir=framework_dir)

    host = WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=private_dir,
        framework_bundles_dir=framework_dir,
        data_dir=tmp_path / "runtime",
        run_store=InMemoryRunStore(),
        ledger_store=InMemoryLedgerStore(),
        artifact_store=InMemoryArtifactStore(),
    )

    assert "framework-demo" in host.bundles
    assert host.latest_bundle_versions["framework-demo"] == "0.0.1"
    assert "framework-demo@0.0.1:root" in host.specs
