"""Summoning an entity over HTTP (a2a 0004, deliverable 2).

POST /api/gateway/entities/{name}/summon:
- renders the identity prelude (pure read) and REFUSES the summon when the
  budget cannot fit the core — 409, reasons verbatim, NO run started;
- on success starts a run whose vars carry a SIGNED stamp bound to that
  run's id and session (verified by the routing layer before any home
  opens), with the prelude leading the system prompt.
"""

from __future__ import annotations

import copy
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-summon-shared-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _write_min_bundle(*, bundles_dir: Path, bundle_id: str = "min", flow_id: str = "root") -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": flow_id,
        "name": "minimal",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 0.0, "y": 0.0},
                "data": {
                    "nodeType": "on_flow_start",
                    "label": "On Flow Start",
                    "inputs": [],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}],
                },
            },
            {
                "id": "node-2",
                "type": "on_flow_end",
                "position": {"x": 200.0, "y": 0.0},
                "data": {
                    "nodeType": "on_flow_end",
                    "label": "On Flow End",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                    "outputs": [],
                },
            },
        ],
        "edges": [
            {"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}
        ],
        "entryNode": "node-1",
    }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-21T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "default_entrypoint": flow_id,
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    with zipfile.ZipFile(bundles_dir / f"{bundle_id}.flow", "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow))


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir)
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")

    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}) as c:
        yield c


def test_summon_starts_a_stamped_run(client: TestClient):
    r = client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()})
    assert r.status_code == 201, r.text

    r2 = client.post(
        "/api/gateway/entities/Castor/summon",
        json={"prompt": "hello there", "bundle_id": "min", "flow_id": "root"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    run_id = body["run_id"]
    assert body["entity_id"].startswith("entity:castor@home-")
    assert body["channel"] == "workplace"
    assert "You are Castor." in body["prelude"]["text"]
    assert body["session_id"].startswith("entity-castor-")

    # The persisted run carries a VERIFYING stamp bound to this run, and the
    # actor was flipped to "gateway" (tickable) after the stamp was saved.
    from abstractgateway.entity_gate import read_stamp, verify_summon_stamp
    from abstractgateway.service import get_gateway_service

    svc = get_gateway_service()
    run = svc.host.run_store.load(run_id)
    assert run is not None
    assert run.actor_id == "gateway"
    stamp = read_stamp(run)
    ok, err = verify_summon_stamp(stamp, data_dir=svc.entity_registry.data_dir, run=run)
    assert ok, err
    # WHO is stamped by the door: the verified principal PLUS the entity
    # itself (EXPLICIT co-presence, ruled a2a 0007 — owners are never
    # implied; the entity is present at its own session by construction).
    assert len(stamp["participants"]) == 2
    assert stamp["participants"][0].startswith("person:")
    assert stamp["participants"][1] == body["entity_id"]

    # The prelude leads the system prompt of the summoned run.
    assert str(run.vars.get("system") or "").startswith("<identity_prelude")

    # The run actually progresses (the runner ticks stamped runs normally).
    deadline = time.time() + 10.0
    while time.time() < deadline:
        rr = client.get(f"/api/gateway/runs/{run_id}")
        assert rr.status_code == 200, rr.text
        if rr.json().get("status") == "completed":
            break
        time.sleep(0.1)
    else:
        pytest.fail("summoned run did not complete")


def test_refused_prelude_aborts_the_summon(client: TestClient):
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

    from abstractgateway.service import get_gateway_service

    svc = get_gateway_service()
    list_runs = getattr(svc.host.run_store, "list_runs", None)
    runs_before = len(list_runs(limit=1000) or []) if callable(list_runs) else None

    r = client.post(
        "/api/gateway/entities/Castor/summon",
        json={"prompt": "hello", "bundle_id": "min", "flow_id": "root", "prelude_budget": 16},
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["refused"] is True
    assert any("#REFUSED" in reason for reason in detail["reasons"])
    assert any("truncated core is a different person" in reason for reason in detail["reasons"])

    # No fallback to a truncated header — and NO run was started.
    if runs_before is not None:
        runs_after = len(list_runs(limit=1000) or [])
        assert runs_after == runs_before, "a refused prelude must not start any run"


def test_summon_unknown_entity_404(client: TestClient):
    r = client.post("/api/gateway/entities/nobody/summon", json={"prompt": "hi"})
    assert r.status_code == 404


def test_context_floor_refuses_small_windows(client: TestClient):
    """Maintainer round 8: 'the minimal context should be 20 000 tokens,
    never less.' Declared windows below the floor refuse the summon; an
    undeclared window proceeds with a labeled warning (the gateway checks
    what it can see, never guesses what it cannot)."""
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

    # Explicit declaration below the floor -> refused.
    r = client.post(
        "/api/gateway/entities/Castor/summon",
        json={"prompt": "hi", "bundle_id": "min", "flow_id": "root", "context_window_tokens": 8192},
    )
    assert r.status_code == 409, r.text
    assert "20 000 tokens" in json.dumps(r.json()["detail"]["reasons"])

    # A flow pin declaring a small window -> refused too.
    r2 = client.post(
        "/api/gateway/entities/Castor/summon",
        json={"prompt": "hi", "bundle_id": "min", "flow_id": "root", "input_data": {"max_in_tokens": 4096}},
    )
    assert r2.status_code == 409, r2.text

    # Declared at/above the floor -> proceeds, no context warning.
    r3 = client.post(
        "/api/gateway/entities/Castor/summon",
        json={"prompt": "hi", "bundle_id": "min", "flow_id": "root", "context_window_tokens": 32768},
    )
    assert r3.status_code == 200, r3.text
    assert not any("context window" in w for w in r3.json()["warnings"])

    # Undeclared -> proceeds with the labeled #FALLBACK warning.
    r4 = client.post(
        "/api/gateway/entities/Castor/summon",
        json={"prompt": "hi", "bundle_id": "min", "flow_id": "root"},
    )
    assert r4.status_code == 200, r4.text
    assert any("#FALLBACK context window undeclared" in w for w in r4.json()["warnings"])
