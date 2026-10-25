"""`automation_defaults.context.growing.max_tokens`: a flow's growing history budget, stored and
published with its defaults and carried into the automations created from them; the gateway's
central default budget is served by `GET /api/gateway/automations/defaults`."""

from __future__ import annotations

import copy
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_FLOW_ID, HEADERS, gateway_env, write_echo_bundle
from test_flow_automation_defaults import DEFAULTS, _flow

GROWING = {**copy.deepcopy(DEFAULTS), "context": {"mode": "growing", "growing": {"max_tokens": 20000}}}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c


def test_the_budget_is_stored_echoed_and_published(client: TestClient) -> None:
    created = client.post("/api/gateway/visualflows", headers=HEADERS, json=_flow(automation_defaults=GROWING))
    assert created.status_code == 200, created.text
    assert created.json()["automation_defaults"]["context"] == {"mode": "growing", "growing": {"max_tokens": 20000}}
    flow_id = created.json()["id"]
    assert client.get(f"/api/gateway/visualflows/{flow_id}", headers=HEADERS).json()["automation_defaults"] == GROWING
    pub = client.post(f"/api/gateway/visualflows/{flow_id}/publish", headers=HEADERS,
                      json={"bundle_id": "budget", "bundle_version": "0.0.1", "overwrite": True, "reload_gateway": True})
    assert pub.status_code == 200, pub.text
    with zipfile.ZipFile(Path(pub.json()["bundle_path"])) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["metadata"]["automation_defaults"][flow_id]["context"]["growing"] == {"max_tokens": 20000}


@pytest.mark.parametrize(
    "context, field",
    [
        ({"mode": "independent", "growing": {"max_tokens": 20000}}, "automation_defaults.context.growing"),
        ({"mode": "growing", "growing": {"max_tokens": 0}}, "automation_defaults.context.growing.max_tokens"),
        ({"mode": "growing", "growing": {"max_tokens": True}}, "automation_defaults.context.growing.max_tokens"),
        ({"mode": "growing", "growing": {"max_tokens": "20k"}}, "automation_defaults.context.growing.max_tokens"),
        ({"mode": "growing", "growing": {"summary": True}}, "automation_defaults.context.growing.summary"),
        ({"mode": "growing", "growing": 5}, "automation_defaults.context.growing"),
    ],
)
def test_invalid_budgets_are_refused(client: TestClient, context: dict, field: str) -> None:
    bad = {**copy.deepcopy(DEFAULTS), "context": context}
    r = client.post("/api/gateway/visualflows", headers=HEADERS, json=_flow(automation_defaults=bad))
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["field"] == field


def test_an_automation_created_from_the_defaults_carries_the_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_env(monkeypatch, tmp_path, runner=True)
    defaults = {"schema_version": 1, "title": "Budgeted", "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
                "context": {"mode": "growing", "growing": {"max_tokens": 20000}}, "input_data": {"prompt": "p"}}
    ref = write_echo_bundle(tmp_path / "bundles", automation_defaults=defaults)
    from abstractgateway.app import app

    with TestClient(app) as c:
        central = c.get("/api/gateway/automations/defaults", headers=HEADERS)
        assert central.status_code == 200 and central.json()["growing_max_tokens"] == 50000
        r = c.post("/api/gateway/automations", headers=HEADERS, json={"request_id": "b1", "target": {"bundle_ref": ref, "flow_id": ECHO_FLOW_ID}})
        assert r.status_code == 200, r.text
        aid = r.json()["automation_id"]
        detail = c.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()
        assert detail["definition"]["context"]["growing"] == {"max_tokens": 20000}
        assert r.json()["summary"]["growing_max_tokens"] == 20000
