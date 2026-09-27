"""`automation_defaults` on VisualFlow documents, publish and catalog (contract C6)."""

from __future__ import annotations

import copy
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, gateway_env

DEFAULTS = {
    "schema_version": 1,
    "title": "Memory every 2 minutes",
    "trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": "2m"}},
    "context": {"mode": "growing"},
    "input_data": {"prompt": "Report memory usage."},
}


def _flow(name: str = "Monitor", **extra) -> dict:
    return {
        "name": name,
        "nodes": [
            {"id": "start", "type": "on_flow_start", "position": {"x": 0, "y": 0},
             "data": {"nodeType": "on_flow_start", "label": "Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]}},
            {"id": "end", "type": "on_flow_end", "position": {"x": 200, "y": 0},
             "data": {"nodeType": "on_flow_end", "label": "End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []}},
        ],
        "edges": [{"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"}],
        "entryNode": "start",
        **extra,
    }


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c


def test_create_get_list_echo_and_put_semantics(client: TestClient) -> None:
    created = client.post("/api/gateway/visualflows", headers=HEADERS, json=_flow(automation_defaults=DEFAULTS))
    assert created.status_code == 200, created.text
    flow_id = created.json()["id"]
    assert created.json()["automation_defaults"] == DEFAULTS

    got = client.get(f"/api/gateway/visualflows/{flow_id}", headers=HEADERS).json()
    assert got["automation_defaults"] == DEFAULTS
    listed = client.get("/api/gateway/visualflows", headers=HEADERS).json()
    assert next(f for f in listed if f["id"] == flow_id)["automation_defaults"] == DEFAULTS

    # Absent on PUT = untouched.
    put = client.put(f"/api/gateway/visualflows/{flow_id}", headers=HEADERS, json={"description": "x"})
    assert put.status_code == 200 and put.json()["automation_defaults"] == DEFAULTS

    # A new value is stored and echoed (defaults filled: context/input_data).
    revised = {"schema_version": 1, "trigger": {"source_id": "manual", "source_version": 1, "config": {}}}
    put = client.put(f"/api/gateway/visualflows/{flow_id}", headers=HEADERS, json={"automation_defaults": revised})
    assert put.status_code == 200, put.text
    assert put.json()["automation_defaults"] == {**revised, "context": {"mode": "independent"}, "input_data": {}}

    # Explicit null = removed (and stays removed on read).
    put = client.put(f"/api/gateway/visualflows/{flow_id}", headers=HEADERS, json={"automation_defaults": None})
    assert put.status_code == 200, put.text
    assert "automation_defaults" not in put.json()
    assert "automation_defaults" not in client.get(f"/api/gateway/visualflows/{flow_id}", headers=HEADERS).json()


@pytest.mark.parametrize(
    "mutate, reason, field",
    [
        (lambda d: d["trigger"].__setitem__("binding_id", "b1"), "invalid_definition", "automation_defaults.trigger.binding_id"),
        (lambda d: d.__setitem__("schema_version", 2), "invalid_definition", "automation_defaults.schema_version"),
        (lambda d: d.__setitem__("revision", 3), "invalid_definition", "automation_defaults.revision"),
        (lambda d: d["context"].__setitem__("mode", "shared"), "invalid_definition", "automation_defaults.context.mode"),
        (lambda d: d["trigger"].__setitem__("source_version", True), "invalid_definition", "automation_defaults.trigger.source_version"),
        (lambda d: d["trigger"].__setitem__("source_id", "webhook"), "unknown_trigger_source", "automation_defaults.trigger.source_id"),
        (lambda d: d["trigger"]["config"].__setitem__("every", "2 minutes"), "invalid_definition", "automation_defaults.trigger.config.every"),
        (lambda d: d["trigger"]["config"].__setitem__("cron", "* * * * *"), "invalid_definition", "automation_defaults.trigger.config.cron"),
    ],
)
def test_invalid_defaults_are_refused_on_save(client: TestClient, mutate, reason: str, field: str) -> None:
    bad = copy.deepcopy(DEFAULTS)
    mutate(bad)
    r = client.post("/api/gateway/visualflows", headers=HEADERS, json=_flow(automation_defaults=bad))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["reason_code"] == reason
    assert r.json()["detail"]["field"] == field

    ok = client.post("/api/gateway/visualflows", headers=HEADERS, json=_flow()).json()
    r = client.put(f"/api/gateway/visualflows/{ok['id']}", headers=HEADERS, json={"automation_defaults": bad})
    assert r.status_code == 422, r.text
    assert "automation_defaults" not in client.get(f"/api/gateway/visualflows/{ok['id']}", headers=HEADERS).json()


def test_publish_exports_defaults_to_manifest_bundles_and_catalog(client: TestClient, tmp_path: Path) -> None:
    flow_id = client.post("/api/gateway/visualflows", headers=HEADERS, json=_flow(automation_defaults=DEFAULTS)).json()["id"]
    pub = client.post(f"/api/gateway/visualflows/{flow_id}/publish", headers=HEADERS,
                      json={"bundle_id": "monitor", "bundle_version": "0.0.1", "overwrite": True, "reload_gateway": True})
    assert pub.status_code == 200, pub.text
    bundle_path = Path(pub.json()["bundle_path"])
    with zipfile.ZipFile(bundle_path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["metadata"]["automation_defaults"] == {flow_id: DEFAULTS}

    items = client.get("/api/gateway/bundles", headers=HEADERS).json()["items"]
    row = next(i for i in items if i["bundle_id"] == "monitor")
    assert row["automation_defaults"] == {flow_id: DEFAULTS}
    one = client.get("/api/gateway/bundles/monitor", headers=HEADERS).json()
    assert one["automation_defaults"] == {flow_id: DEFAULTS}

    # Catalog record (tenant catalog install of the same bytes).
    from abstractgateway.workflow_catalog import WorkflowCatalogStore, catalog_record_public_dict

    store = WorkflowCatalogStore(root_data_dir=tmp_path / "catalog-root")
    record = store.install_bundle_bytes(bundle_path.read_bytes(), tenant_id="default")
    assert record["automation_defaults"] == {flow_id: DEFAULTS}
    assert catalog_record_public_dict(record)["automation_defaults"] == {flow_id: DEFAULTS}


def test_publish_without_defaults_exports_nothing(client: TestClient) -> None:
    flow_id = client.post("/api/gateway/visualflows", headers=HEADERS, json=_flow(name="Plain")).json()["id"]
    pub = client.post(f"/api/gateway/visualflows/{flow_id}/publish", headers=HEADERS,
                      json={"bundle_id": "plain", "bundle_version": "0.0.1", "overwrite": True, "reload_gateway": True})
    assert pub.status_code == 200, pub.text
    with zipfile.ZipFile(Path(pub.json()["bundle_path"])) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert "automation_defaults" not in manifest["metadata"]
    one = client.get("/api/gateway/bundles/plain", headers=HEADERS).json()
    assert one["automation_defaults"] == {}
