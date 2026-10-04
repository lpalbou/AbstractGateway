"""The Accounts row's Workspace action follows the server rule, so the console (a thin client)
shows the folder icon only where the modal can work.

Round 11 (DESIGN.md R11.1 FINAL + R11.2): an entity's workspaces are set by admins AND the entity's
creator; every entity has a gateway account (legacy homes get theirs at boot), so every row that is
not archived offers the action, and "This entity has no gateway account yet" can no longer occur.
"""

from __future__ import annotations

import json

import pytest

import abstractgateway.admin_accounts as admin_accounts
from email_fixtures import *  # noqa: F401,F403 - fixtures
from test_gateway_entity_rbac import world  # noqa: F401 - fixture

pytestmark = pytest.mark.integration


def test_entity_workspace_action_is_admins_and_the_creator_and_matches_the_route(world) -> None:  # noqa: F811
    c = world["c"]
    mine = {a["id"]: a for a in c.get("/api/gateway/me/accounts", headers=world["alice"]).json()["accounts"]}
    assert mine["alice"]["actions"]["workspace"] == {"available": True, "reason": None}
    assert mine["aster"]["actions"]["workspace"] == {"available": True, "reason": None}
    # The route agrees: the creator reads and changes the entity's workspaces; another user may not.
    r = c.get("/api/gateway/workspace/policy/default:aster", headers=world["alice"])
    assert r.status_code == 200 and r.json()["can_edit"] is True, r.text
    assert c.put("/api/gateway/workspace/policy/default:aster", headers=world["alice"], json={"configured": False}).status_code == 200
    r = c.put("/api/gateway/workspace/policy/default:aster", headers=world["bob"], json={"configured": False})
    assert r.status_code == 403 and r.json()["detail"] == "Only an admin or aster's creator can change its workspaces."
    assert c.get("/api/gateway/workspace/policy/me", headers=world["alice"]).status_code == 200

    rows = {a["id"]: a for a in c.get("/api/gateway/admin/accounts", headers=world["admin"]).json()["accounts"]}
    for slug in ("aster", "borea", "olden"):
        assert rows[slug]["actions"]["workspace"] == {"available": True, "reason": None}, rows[slug]["actions"]["workspace"]
        assert c.get(f"/api/gateway/workspace/policy/default:{slug}", headers=world["admin"]).status_code == 200
        assert c.put(f"/api/gateway/workspace/policy/default:{slug}", headers=world["admin"], json={"configured": False}).status_code == 200
    # A legacy home without a recorded creator: admins only.
    assert c.put("/api/gateway/workspace/policy/default:olden", headers=world["alice"], json={"configured": False}).status_code == 403


def test_a_home_without_an_account_gets_one_and_the_old_reason_is_gone(world) -> None:  # noqa: F811
    from abstractgateway.entity_accounts import ensure_entity_accounts
    from abstractgateway.users import GatewayUserRegistry

    assert not hasattr(admin_accounts, "REASON_ENTITY_NO_ACCOUNT")
    c, reg = world["c"], world["registry"]
    # A home that predates entity accounts (the operator's castor): a manifest, no user record.
    home = reg.entities_dir / "castor"
    home.mkdir(parents=True)
    (home / "manifest.json").write_text(json.dumps({"slug": "castor", "name": "castor", "entity_id": "entity:castor@home-1", "home_id": "home-1"}))
    assert GatewayUserRegistry().get_user("castor") is None
    assert ensure_entity_accounts() == ["entity 'castor' now has a gateway account"]
    rec = GatewayUserRegistry().get_user("castor")
    assert rec is not None and rec.principal_kind == "entity" and list(rec.roles) == ["entity"] and rec.runtime_id == "castor"
    assert ensure_entity_accounts() == []  # idempotent
    row = next(a for a in c.get("/api/gateway/admin/accounts", headers=world["admin"]).json()["accounts"] if a["id"] == "castor")
    assert row["actions"]["workspace"] == {"available": True, "reason": None}
    assert c.get("/api/gateway/workspace/policy/default:castor", headers=world["admin"]).status_code == 200
