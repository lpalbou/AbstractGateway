"""An automation's target inputs carry the registered user's email (framework backlog 0992 WP0).

The runtime's automation grant never pre-approves `send_email`; the per-call
refiner auto-approves a send only when every recipient is the registered
address (`_runtime.operator_email`). Occurrences are started by the runtime,
not through `host.start_run` where chat runs receive that value, so the gateway
freezes it into the definition's protected inputs. It is server-owned: a value
the client sends is dropped, never trusted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_FLOW_ID, HEADERS, gateway_env, wait_until, write_echo_bundle

OWNER = "owner@example.invalid"


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = gateway_env(monkeypatch, tmp_path, runner=True)  # the runner applies revisions
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        c.data_dir = data_dir  # type: ignore[attr-defined]
        yield c


def _register_email(c: TestClient, email: str) -> None:
    from abstractgateway.runtime_config import write_runtime_config

    write_runtime_config(Path(c.data_dir), {"operator_email": email}, actor="test")  # type: ignore[attr-defined]


def _create(c: TestClient, request_id: str, runtime_ns: dict | None = None) -> str:
    input_data: dict = {"prompt": "mail me the digest"}
    if runtime_ns is not None:
        input_data["_runtime"] = runtime_ns
    r = c.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": request_id, "title": "digest",
        "target": {"bundle_ref": c.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": input_data},  # type: ignore[attr-defined]
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}}})
    assert r.status_code == 200, r.text
    return r.json()["automation_id"]


def _runtime_inputs(c: TestClient, aid: str) -> dict:
    definition = c.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]
    return definition["target"]["input_data"].get("_runtime") or {}


def test_the_registered_email_is_frozen_into_the_target_inputs(live: TestClient) -> None:
    _register_email(live, OWNER)
    aid = _create(live, "with-email")
    assert _runtime_inputs(live, aid)["operator_email"] == OWNER


def test_a_client_supplied_operator_email_is_never_trusted(live: TestClient) -> None:
    # With a registered email the gateway's value replaces the client's...
    _register_email(live, OWNER)
    aid = _create(live, "spoof", {"operator_email": "attacker@example.invalid", "model": "m"})
    assert _runtime_inputs(live, aid) == {"model": "m", "operator_email": OWNER}


def test_without_a_registered_email_the_key_is_absent(live: TestClient) -> None:
    # ...and without one the client's value is dropped, so every send asks.
    aid = _create(live, "spoof-no-email", {"operator_email": "attacker@example.invalid"})
    assert "operator_email" not in _runtime_inputs(live, aid)


def test_a_target_revision_refreshes_the_registered_email(live: TestClient) -> None:
    _register_email(live, OWNER)
    aid = _create(live, "revise")
    _register_email(live, "new-owner@example.invalid")
    r = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS, json={"command_id": "t", "changes": {"target": {
        "bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "again"}}}})  # type: ignore[attr-defined]
    assert r.status_code == 200, r.text
    wait_until(lambda: live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]["revision"] == 2)
    assert _runtime_inputs(live, aid)["operator_email"] == "new-owner@example.invalid"
