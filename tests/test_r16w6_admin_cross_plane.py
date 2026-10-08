"""R16.5 "admins always" across runtime planes (adversary F1, orchestrator ruling 2026-10-09).

On a user-accounts gateway each account works in its own runtime plane; an entity alice creates
lives in HER plane (`<data>/users/default/alice/runtime/entities/nova`). An admin must configure it
like any other: every `/entities/{name}/…` route resolves an admin's request to the plane that holds
the entity (`entity_access.entity_plane_resolver`), and Accounts' Active / archive act there too.
Real per-principal services (no registry monkeypatch), runner off, no embedder needed.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ADMIN

pytestmark = pytest.mark.integration


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


@pytest.fixture
def planes(gateway, monkeypatch):
    pytest.importorskip("abstractmemory")
    pytest.importorskip("yaml")
    from fastapi.testclient import TestClient

    import abstractgateway.entity_settings_access as esa
    from abstractgateway.entities import EntityRegistry
    from abstractgateway.routes import entities_router, entity_replay_router
    from abstractgateway.users import GatewayUserRegistry

    app = gateway["app"]
    app.include_router(entities_router, prefix="/api")
    app.include_router(entity_replay_router, prefix="/api")
    data = Path(gateway["data_dir"])
    alice_plane = data / "users" / "default" / "alice" / "runtime"
    alice_plane.mkdir(parents=True, exist_ok=True)
    EntityRegistry(data_dir=alice_plane, embedder_factory=lambda: None, users_registry_path=data / "auth" / "users.json").create(
        name="Nova", spark=_spark("Nova"), created_by={"tenant_id": "default", "user_id": "alice"}
    )
    monkeypatch.setattr(esa, "offered_text_models", lambda p: (["qwen3-4b"], None))
    from test_r16w6_entity_settings_authz import _knock_as_entity

    entity = {"Authorization": f"Bearer {_knock_as_entity(GatewayUserRegistry(), 'nova')}"}
    return {"c": TestClient(app), "alice": gateway["alice"], "bob": gateway["bob"], "admin": ADMIN,
            "entity": entity, "home": alice_plane / "entities" / "nova", "data": data}


def test_the_entity_lives_in_alices_plane_only(planes) -> None:
    assert (planes["home"] / "manifest.json").is_file()
    assert not (planes["data"] / "entities" / "nova").exists()


def test_an_admin_configures_an_entity_in_a_members_plane(planes) -> None:
    c = planes["c"]
    a = c.get("/api/gateway/entities/nova/access", headers=planes["admin"])
    assert a.status_code == 200, a.text
    assert a.json()["can_configure"] is True and a.json()["as"] == "admin"
    # An admin is not bounded by the offered list; the write lands in ALICE's plane.
    r = c.put("/api/gateway/entities/nova/substrate", headers=planes["admin"], json={"provider": "lmstudio", "model": "not-offered"})
    assert r.status_code == 200, r.text
    assert "not-offered" in (planes["home"] / "substrate.yaml").read_text()
    for path, body in (("prompt", {"overlay": {"operator": "Be brief."}}), ("tool-policy", {"policy": {"visit": ["read_memory"]}})):
        r = c.put(f"/api/gateway/entities/nova/{path}", headers=planes["admin"], json=body)
        assert r.status_code == 200, (path, r.text)
    # Reads resolve there too (the console's Manage dialog).
    assert c.get("/api/gateway/entities/nova/substrate", headers=planes["admin"]).json()["model"] == "not-offered"
    assert c.get("/api/gateway/entities/nova/card", headers=planes["admin"]).status_code == 200


def test_an_admin_runs_lifecycle_acts_there_too(planes) -> None:
    r = planes["c"].post("/api/gateway/entities/nova/state", headers=planes["admin"], json={"state": "asleep"})
    assert r.status_code == 200, r.text
    st = json.loads((planes["home"] / "state.json").read_text()) if (planes["home"] / "state.json").exists() else None
    got = planes["c"].get("/api/gateway/entities/nova/state", headers=planes["admin"]).json()
    assert (got.get("state") or (st or {}).get("state")) == "asleep", got


def test_the_matrix_holds_beside_the_admin(planes) -> None:
    c = planes["c"]
    body = {"provider": "lmstudio", "model": "qwen3-4b"}
    assert c.put("/api/gateway/entities/nova/substrate", headers=planes["alice"], json=body).status_code == 200
    assert c.put("/api/gateway/entities/nova/substrate", headers=planes["bob"], json=body).status_code == 404
    r = c.put("/api/gateway/entities/nova/substrate", headers=planes["entity"], json=body)
    assert r.status_code in (403, 404), r.text  # its own (empty) plane answers 404 before the guard's 403


def test_accounts_row_and_active_switch_work_for_an_admin(planes) -> None:
    c = planes["c"]
    rows = {a["id"]: a for a in c.get("/api/gateway/admin/accounts", headers=planes["admin"]).json()["accounts"]}
    assert rows["nova"]["actions"]["manage"] == {"available": True, "reason": None}, rows["nova"]["actions"]["manage"]
    assert rows["nova"]["actions"]["configure"]["available"] is True
    r = c.put("/api/gateway/admin/accounts/nova/active", headers=planes["admin"], json={"active": False})
    assert r.status_code == 200 and r.json()["active"] is False, r.text
    assert c.get("/api/gateway/entities/nova/state", headers=planes["alice"]).json().get("state") == "paused"
    r = c.put("/api/gateway/admin/accounts/nova/active", headers=planes["admin"], json={"active": True})
    assert r.status_code == 200 and r.json()["active"] is True, r.text
