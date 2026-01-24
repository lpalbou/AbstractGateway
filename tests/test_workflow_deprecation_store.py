from __future__ import annotations

import json
from pathlib import Path

import pytest

from abstractgateway.workflow_deprecations import WorkflowDeprecationStore


@pytest.mark.basic
def test_workflow_deprecation_store_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "workflow_deprecations.json"
    store = WorkflowDeprecationStore(path=path)

    state, err = store.snapshot()
    assert err is None
    assert state.get("bundles") == {}

    rec = store.set_deprecated(bundle_id="demo", flow_id="root", reason="retired")
    assert rec["bundle_id"] == "demo"
    assert rec["flow_id"] == "root"
    assert rec["reason"] == "retired"
    assert path.exists()

    assert store.is_deprecated(bundle_id="demo", flow_id="root") is True
    assert store.is_deprecated(bundle_id="demo", flow_id="other") is False

    removed = store.clear_deprecated(bundle_id="demo", flow_id="root")
    assert removed is True
    assert store.is_deprecated(bundle_id="demo", flow_id="root") is False


@pytest.mark.basic
def test_workflow_deprecation_store_wildcard(tmp_path: Path) -> None:
    store = WorkflowDeprecationStore(path=tmp_path / "workflow_deprecations.json")
    store.set_deprecated(bundle_id="demo", flow_id=None, reason="sunset")

    assert store.is_deprecated(bundle_id="demo", flow_id="root") is True
    assert store.is_deprecated(bundle_id="demo", flow_id="anything") is True
    assert store.is_deprecated(bundle_id="other", flow_id="root") is False


@pytest.mark.basic
def test_workflow_deprecation_store_recovers_from_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "workflow_deprecations.json"
    path.write_text("{not json", encoding="utf-8")

    store = WorkflowDeprecationStore(path=path)
    state, err = store.snapshot()
    assert state.get("bundles") == {}
    assert isinstance(err, str) and err

    # Corrupt file is moved aside; a new write should recreate the main file.
    assert not path.exists()
    backups = list(tmp_path.glob("workflow_deprecations.json.corrupt.*"))
    assert backups

    store.set_deprecated(bundle_id="demo", flow_id="root")
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")).get("bundles", {}).get("demo")

