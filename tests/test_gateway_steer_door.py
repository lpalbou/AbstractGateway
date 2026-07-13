"""H4 steer door (hooks plan, 2026-07-12): the HTTP half of steering.

- POST /api/gateway/commands accepts type=inject_guidance (was 400).
- Entity VISIT runs refuse raw steers with a synchronous 403 at the door
  (the H5 rite is not built; runtime's Runtime.steer refuses asynchronously
  as the authoritative backstop).
- The ticking runtimes built by the bundle host carry the per-root steer
  sidecar, so accepted steers actually DRAIN at tick boundaries.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

_TOKEN = "steer-door-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _submit(client: TestClient, run_id: str, typ: str, payload: dict) -> "object":
    return client.post(
        "/api/gateway/commands",
        json={"command_id": str(uuid.uuid4()), "run_id": run_id, "type": typ, "payload": payload},
    )


def _plant_run(run_id: str, *, workflow_id: str = "wf", visit: bool = False) -> None:
    from abstractgateway.service import get_gateway_service
    from abstractruntime.core.models import RunState, RunStatus

    svc = get_gateway_service()
    run = RunState(
        run_id=run_id,
        workflow_id=workflow_id,
        status=RunStatus.RUNNING,
        current_node="n",
        vars={"_runtime": {"inbox": []}, **({"_visit": {"idle_seconds": 600}} if visit else {})},
    )
    svc.runner.run_store.save(run)


def test_commands_door_accepts_inject_guidance_for_a_known_run() -> None:
    with _client() as client:
        _plant_run("agent-run-1")
        r = _submit(client, "agent-run-1", "inject_guidance", {"guidance": "focus"})
        assert r.status_code == 200, r.text
        assert r.json()["accepted"] is True


def test_unknown_run_refuses_404_at_the_door() -> None:
    """Adversary F2: an inject_guidance at a run the runner store cannot see
    (per-home visit runs, typos, other principals) must refuse NOW — the old
    behavior accepted the command and let it die in a log the caller never
    sees."""
    with _client() as client:
        r = _submit(client, "nowhere-run", "inject_guidance", {"guidance": "focus"})
        assert r.status_code == 404, r.text
        assert "visit channel" in r.json()["detail"]
        # Other command types keep their existing semantics (queue-and-apply).
        r2 = _submit(client, "nowhere-run", "pause", {})
        assert r2.status_code == 200, r2.text


def test_visit_host_served_run_gets_the_rite_403_even_when_absent_from_runner_store() -> None:
    """Adversary F2, the honest half: production visit runs live in per-home
    stores, so the runner-store load is None — the door consults the visit
    host's served-run cache and still names the rite."""
    with _client() as client:
        from abstractgateway.service import get_gateway_service

        svc = get_gateway_service()
        visit_host = svc.entity_visit_host
        assert visit_host is not None
        # The spec cache is exactly what open/turn maintain for served visits.
        visit_host._specs["home-visit-run"] = object()
        try:
            r = _submit(client, "home-visit-run", "inject_guidance", {"guidance": "obey"})
            assert r.status_code == 403, r.text
            assert "rite" in r.json()["detail"].lower()
        finally:
            visit_host._specs.pop("home-visit-run", None)


def test_commands_door_still_rejects_unknown_types() -> None:
    with _client() as client:
        r = _submit(client, "some-run", "mind_control", {"guidance": "x"})
        assert r.status_code == 400, r.text
        assert "inject_guidance" in r.json()["detail"], "the allowlist error should name the new type"


def test_capabilities_listing_serves_the_new_command_type() -> None:
    with _client() as client:
        caps = client.get("/api/gateway/discovery/capabilities")
        assert caps.status_code == 200, caps.text
        contracts = caps.json()["capabilities"]["contracts"]
        types = contracts["common"]["runs"]["commands"]["types"]
        assert "inject_guidance" in types


def test_entity_visit_run_refuses_raw_steer_with_403() -> None:
    """H5 interim at the door: a run carrying the visit signals refuses the
    steer SYNCHRONOUSLY — the caller learns now, not from a failed command."""
    pytest.importorskip("abstractmemory")
    with _client() as client:
        svc_run_store = None
        # Plant a visit-shaped run directly in the primary run store (the
        # door checks the dual signal on the loaded run; no LLM needed).
        from abstractgateway.service import get_gateway_service
        from abstractruntime.core.models import RunState, RunStatus

        svc = get_gateway_service()
        svc_run_store = svc.runner.run_store
        run = RunState(
            run_id="visit-run-1",
            workflow_id="entity-visit@1",
            status=RunStatus.RUNNING,
            current_node="PARK",
            vars={"_visit": {"idle_seconds": 600}, "_runtime": {"inbox": []}},
        )
        svc_run_store.save(run)

        r = _submit(client, "visit-run-1", "inject_guidance", {"guidance": "obey"})
        assert r.status_code == 403, r.text
        assert "rite" in r.json()["detail"].lower() or "visit" in r.json()["detail"].lower()

        # Non-steer commands stay open for the same run (pause is legitimate).
        r2 = _submit(client, "visit-run-1", "pause", {})
        assert r2.status_code == 200, r2.text


def test_bundle_host_runtimes_carry_the_steer_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring pin: the runtime that TICKS runs for a data root holds the
    root's sidecar — without it, door-accepted steers would queue forever."""
    import shutil

    from abstractgateway.config import _default_flows_dir

    # conftest points ABSTRACTGATEWAY_FLOWS_DIR at an EMPTY tmp dir; plant
    # the shipped basic-agent so the host has a bundle to build a runtime for.
    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(_default_flows_dir()) / "basic-agent.flow", flows / "basic-agent.flow")
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))

    with _client() as client:
        from abstractgateway.service import get_gateway_service

        svc = get_gateway_service()
        rid = svc.host.start_run(
            flow_id="",
            bundle_id="basic-agent",
            bundle_version=None,
            input_data={"prompt": "hi"},
            actor_id="test",
            session_id="steer-door-wiring",
        )
        runtime, _wf = svc.host.runtime_and_workflow_for_run(rid)
        assert getattr(runtime, "_steer_store", None) is not None, (
            "bundle-host runtime must carry the per-root steer sidecar (attach_steer_store)"
        )


def test_steer_sidecar_is_per_data_root_and_cached(tmp_path: Path) -> None:
    from abstractgateway.steering import gateway_steer_sidecar

    a1 = gateway_steer_sidecar(tmp_path / "rootA")
    a2 = gateway_steer_sidecar(tmp_path / "rootA")
    b = gateway_steer_sidecar(tmp_path / "rootB")
    assert a1 is a2, "same root must reuse one sidecar instance"
    assert a1 is not b, "different roots must get isolated sidecars"
    assert (tmp_path / "rootA" / "steer_sidecar.sqlite3").exists()
