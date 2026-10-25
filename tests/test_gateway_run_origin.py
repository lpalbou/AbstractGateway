"""Run origin (backlog 1038, gateway part): `/runs/start` records WHO started a run.

`_meta.origin = {kind: "person", actor: <principal user id>, client: <interface>, ref: {}}`
is stamped by the host on every person-started run, overriding anything a client sends; the
legacy scheduled wrapper (`/runs/schedule`) is stamped `kind: "system"`. Proven with two real
principals through the real app + auth middleware, and read back from the run store and from
`GET /runs/{id}` (`origin`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_gateway_principal_isolation_matrix import _client, _create_user, _write_min_bundle


def _start(client: TestClient, headers: dict, **extra) -> str:
    body = {"bundle_id": "iso-bundle", "flow_id": "root", "input_data": {"prompt": "hi"}}
    body.update(extra)
    r = client.post("/api/gateway/runs/start", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def test_each_principal_is_the_actor_of_the_runs_they_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        alice = _create_user(client, "alice")
        bob = _create_user(client, "bob")
        for user in ("alice", "bob"):
            _write_min_bundle(bundles_dir=tmp_path / "runtime" / "users" / "default" / user / "flows")

        # A forged origin in the client's input is overwritten, never trusted.
        forged = {"_meta": {"origin": {"kind": "trigger", "actor": "bob", "client": "x", "ref": {"automation_id": "a"}}}, "prompt": "hi"}
        alice_run = _start(client, alice, interface="abstractassistant.chat.v1", input_data=forged)
        bob_run = _start(client, bob)

        a = client.get(f"/api/gateway/runs/{alice_run}", headers=alice)
        b = client.get(f"/api/gateway/runs/{bob_run}", headers=bob)
        assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
        assert a.json()["origin"] == {"kind": "person", "actor": "alice", "client": "abstractassistant.chat.v1", "ref": {}}
        assert b.json()["origin"] == {"kind": "person", "actor": "bob", "client": None, "ref": {}}

        # The stored run carries it inline (`vars._meta.origin`): readable from stored data alone.
        from abstractgateway.service import get_gateway_service_for_principal

        for user_id, run_id in (("alice", alice_run), ("bob", bob_run)):
            run = get_gateway_service_for_principal(_principal(user_id)).host.run_store.load(run_id)
            assert run is not None and run.vars["_meta"]["origin"]["actor"] == user_id
        # `actor_id` stays the runner's ownership marker ("gateway"): the runner ticks
        # only those runs; the principal lives in origin.actor (deviation recorded in L2).
        assert a.json()["actor_id"] == "gateway"


def _principal(user_id: str):
    from abstractgateway.security.principal import GatewayPrincipal

    return GatewayPrincipal(user_id=user_id, tenant_id="default", roles=("user",), runtime_id=user_id)


def test_the_legacy_scheduled_wrapper_is_a_system_run_set_up_by_the_principal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        alice = _create_user(client, "alice")
        _write_min_bundle(bundles_dir=tmp_path / "runtime" / "users" / "default" / "alice" / "flows")
        r = client.post(
            "/api/gateway/runs/schedule",
            headers=alice,
            json={"bundle_id": "iso-bundle", "flow_id": "root", "start_at": "now", "input_data": {"prompt": "hi"}},
        )
        assert r.status_code == 200, r.text
        detail = client.get(f"/api/gateway/runs/{r.json()['run_id']}", headers=alice)
        assert detail.status_code == 200, detail.text
        assert detail.json()["origin"] == {"kind": "system", "actor": "alice", "client": None, "ref": {}}


def test_a_run_started_before_origin_existed_reads_none() -> None:
    from types import SimpleNamespace

    from abstractgateway.service import run_summary

    legacy = SimpleNamespace(run_id="r1", workflow_id="w", status="completed", vars={"_meta": {}}, waiting=None, output=None)
    assert run_summary(legacy)["origin"] is None
