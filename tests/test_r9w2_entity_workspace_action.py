"""Round 9 (DESIGN.md R9.2): the Accounts row's Workspace action follows the server rule, so the
console (a thin client) shows the folder icon only where the modal can work.

- An admin: every registered entity's row offers Workspace (`PUT /workspace/policy/default:<slug>`
  works), a legacy home without a recorded creator included.
- A non-admin: their own row offers Workspace (`me`); their own entity's row does NOT (an entity's
  folders are admin-only: the route answers 403 to its creator), with the reason as a sentence.
"""

from __future__ import annotations

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from test_gateway_entity_rbac import world  # noqa: F401 - fixture

pytestmark = pytest.mark.integration


def test_entity_workspace_action_is_admin_only_and_matches_the_route(world) -> None:  # noqa: F811
    c = world["c"]
    mine = {a["id"]: a for a in c.get("/api/gateway/me/accounts", headers=world["alice"]).json()["accounts"]}
    assert mine["alice"]["actions"]["workspace"] == {"available": True, "reason": None}
    assert mine["aster"]["actions"]["workspace"] == {"available": False, "reason": "Only an admin can change an entity's workspace folders."}
    # The route agrees: the creator may not read or change the entity's folders; their own, yes.
    assert c.get("/api/gateway/workspace/policy/default:aster", headers=world["alice"]).status_code == 403
    assert c.put("/api/gateway/workspace/policy/default:aster", headers=world["alice"], json={"enabled_folders": []}).status_code == 403
    assert c.get("/api/gateway/workspace/policy/me", headers=world["alice"]).status_code == 200

    rows = {a["id"]: a for a in c.get("/api/gateway/admin/accounts", headers=world["admin"]).json()["accounts"]}
    for slug in ("aster", "borea"):
        assert rows[slug]["actions"]["workspace"] == {"available": True, "reason": None}, rows[slug]["actions"]["workspace"]
        r = c.get(f"/api/gateway/workspace/policy/default:{slug}", headers=world["admin"])
        assert r.status_code == 200, r.text
        assert c.put(f"/api/gateway/workspace/policy/default:{slug}", headers=world["admin"], json={"enabled_folders": []}).status_code == 200
    # A legacy home (no recorded creator) still has its gateway account: admins set its folders too.
    assert c.get("/api/gateway/workspace/policy/default:olden", headers=world["admin"]).status_code == 200
    assert rows["olden"]["actions"]["workspace"] == {"available": True, "reason": None}
