"""The bundle min_runtime enforcement gate (flow c5652, gateway c5653).

A bundle declaring `metadata.min_runtime` above the serving abstractruntime
version REFUSES to load — loudly, per-bundle, never bricking the boot (the
staged-then-commit lesson). The fail-dangerous class this kills: a
pin-expression bundle on a non-evaluating runtime runs WRONG (a failed run
reads as success), not degraded. Refusal fires only on a PROVEN gap: absent
metadata = no gate; an unparsable declaration warns and loads (a typo must
not brick a working bundle).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic


def _write_bundle(path: Path, *, bundle_id: str, metadata: dict) -> None:
    flow = {
        "id": "root",
        "name": "minimal",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "n1",
                "type": "on_flow_start",
                "position": {"x": 0.0, "y": 0.0},
                "data": {"nodeType": "on_flow_start", "label": "Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "n2",
                "type": "on_flow_end",
                "position": {"x": 200.0, "y": 0.0},
                "data": {"nodeType": "on_flow_end", "label": "End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "e1", "source": "n1", "sourceHandle": "exec-out", "target": "n2", "targetHandle": "exec-in"}],
        "entryNode": "n1",
    }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.1",
        "created_at": "2026-01-21T00:00:00+00:00",
        "entrypoints": [{"flow_id": "root", "name": "root", "description": "", "interfaces": []}],
        "default_entrypoint": "root",
        "flows": {"root": "flows/root.json"},
        "artifacts": {},
        "assets": {},
        "metadata": metadata,
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("flows/root.json", json.dumps(flow))


def _load_host(tmp_path: Path):
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost
    from abstractgateway.stores import build_file_stores

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    stores = build_file_stores(base_dir=data_dir)
    return WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=tmp_path / "bundles",
        data_dir=data_dir,
        run_store=stores.run_store,
        ledger_store=stores.ledger_store,
        artifact_store=stores.artifact_store,
    )


def _served_flow_ids(host) -> set:
    """The SERVING truth: which namespaced workflow specs registered. A
    skipped bundle's flows never compile, so its ids are absent here — the
    same observable the compile-skip (boot resilience) produces."""
    return set(host.specs.keys())


def _has_bundle(host, bundle_id: str) -> bool:
    return any(bundle_id in wfid for wfid in _served_flow_ids(host))


def test_future_min_runtime_refuses_that_bundle_and_serves_the_rest(tmp_path: Path):
    bundles = tmp_path / "bundles"
    bundles.mkdir(parents=True)
    _write_bundle(bundles / "future.flow", bundle_id="future", metadata={"min_runtime": "999.0.0", "requires_pin_expressions": True})
    _write_bundle(bundles / "plain.flow", bundle_id="plain", metadata={})

    host = _load_host(tmp_path)
    assert _has_bundle(host, "plain"), "an ungated bundle must keep serving"
    assert not _has_bundle(host, "future"), "a proven version gap must refuse the bundle"
    # ABSENT MEANS ABSENT (flow's live probe read the LISTING as the gate's
    # verdict): a skipped bundle must not appear in the catalog either —
    # specs-skipped-but-listed made the skip look like a load.
    assert "future" not in set(host.bundles.keys()), "a skipped bundle must leave the listing too"
    assert "plain" in set(host.bundles.keys())


def test_satisfied_min_runtime_loads(tmp_path: Path):
    import importlib.metadata as im

    bundles = tmp_path / "bundles"
    bundles.mkdir(parents=True)
    installed = im.version("abstractruntime")
    _write_bundle(bundles / "ok.flow", bundle_id="ok", metadata={"min_runtime": installed})

    host = _load_host(tmp_path)
    assert _has_bundle(host, "ok"), "a satisfied floor must load"


def test_unparsable_min_runtime_warns_and_loads(tmp_path: Path, caplog):
    bundles = tmp_path / "bundles"
    bundles.mkdir(parents=True)
    _write_bundle(bundles / "typo.flow", bundle_id="typo", metadata={"min_runtime": "not-a-version-!!"})

    import logging

    with caplog.at_level(logging.WARNING):
        host = _load_host(tmp_path)
    assert _has_bundle(host, "typo"), "an unparsable pin must not brick a working bundle"
    assert any("min_runtime" in r.message for r in caplog.records), "the degrade must be labeled"


def test_absent_metadata_is_no_gate(tmp_path: Path):
    bundles = tmp_path / "bundles"
    bundles.mkdir(parents=True)
    _write_bundle(bundles / "legacy.flow", bundle_id="legacy", metadata={})
    host = _load_host(tmp_path)
    assert _has_bundle(host, "legacy")
