"""Round 8 (R8.1): a workflow's description is editable by its owner.

`PATCH /api/gateway/bundles/{id}` {description}: the owner edits (a user their own,
an admin the gateway's); another user / a user on a shared workflow is refused
(403); a shipped workflow refuses (409); the .flow file is never rewritten
(Export keeps the original bytes); every attempt is audited (lengths only); an
empty text goes back to the file's own description. `GET /bundles` carries the
effective text and `actions.can_edit_description` so the console decides
nothing itself.

Drives the REAL app over HTTP on the multi-user fixture of
test_gateway_workflow_governance.py (shared flows = the admin's registry;
alice has her own registry).
"""

from __future__ import annotations

import json

import pytest

from automations_fixtures import ECHO_BUNDLE_ID
from test_gateway_workflow_governance import ADMIN, _items, gw  # noqa: F401 - the fixture


def _patch(c, bundle_id, text, headers):
    return c.patch(f"/api/gateway/bundles/{bundle_id}", headers=headers, json={"description": text})


def _audit(gw):
    path = gw["data"] / "audit_log.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == "workflow.description"]


def test_the_list_says_who_may_edit(gw):
    c = gw["c"]
    admin = _items(c, ADMIN)
    assert admin[ECHO_BUNDLE_ID]["actions"]["can_edit_description"] is True
    assert admin["basic-agent"]["actions"]["can_edit_description"] is False, "shipped: never editable"
    alice = _items(c, gw["alice"])
    assert alice["alice-wf"]["actions"]["can_edit_description"] is True
    assert alice[ECHO_BUNDLE_ID]["actions"]["can_edit_description"] is False, "a user cannot edit a shared workflow"
    assert alice["alice-wf"]["description_edited"] is False


def test_the_owner_edits_and_every_version_shows_it(gw):
    c = gw["c"]
    flow = gw["alice_flows"] / "alice-wf@1.0.0.flow"
    before = flow.read_bytes()
    res = _patch(c, "alice-wf", "  Summarises my inbox every morning.  ", gw["alice"])
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["description"] == "Summarises my inbox every morning."
    assert body["updated_by"] == "alice"
    row = _items(c, gw["alice"])["alice-wf"]
    assert row["description"] == "Summarises my inbox every morning."
    assert row["description_edited"] is True
    assert flow.read_bytes() == before, "the .flow file must never be rewritten"
    # Stored next to the owner's archive file, not in the gateway's.
    store = gw["alice_flows"].parent / "config" / "workflow_descriptions.json"
    assert json.loads(store.read_text())["bundles"]["alice-wf"]["description"] == "Summarises my inbox every morning."
    assert not (gw["data"] / "config" / "workflow_descriptions.json").exists()
    rows = _audit(gw)
    assert rows[-1]["outcome"] == "ok" and rows[-1]["actor"] == "alice" and rows[-1]["bundle_id"] == "alice-wf"
    assert rows[-1]["chars"] == len("Summarises my inbox every morning.")
    assert "description" not in rows[-1], "the audit carries lengths, never the text"

    # Empty = back to the file's own description.
    res = _patch(c, "alice-wf", "", gw["alice"])
    assert res.status_code == 200, res.text
    row = _items(c, gw["alice"])["alice-wf"]
    assert row["description_edited"] is False
    assert row["description"] != "Summarises my inbox every morning."


def test_rbac_owner_or_admin_only(gw):
    c = gw["c"]
    # bob cannot reach alice's workflow at all (it is not on his gateway view).
    res = _patch(c, "alice-wf", "pwned", gw["bob"])
    assert res.status_code == 404, res.text
    # A user cannot edit a workflow the gateway shares.
    res = _patch(c, ECHO_BUNDLE_ID, "pwned", gw["alice"])
    assert res.status_code == 403, res.text
    assert res.json()["detail"]["reason_code"] == "admin_required"
    assert _items(c, ADMIN)[ECHO_BUNDLE_ID]["description"] != "pwned"
    # The admin can.
    res = _patch(c, ECHO_BUNDLE_ID, "Echoes its prompt (test fixture).", ADMIN)
    assert res.status_code == 200, res.text
    assert _items(c, gw["alice"])[ECHO_BUNDLE_ID]["description"] == "Echoes its prompt (test fixture)."
    outcomes = [(r["actor"], r["outcome"], r.get("reason")) for r in _audit(gw)]
    assert ("alice", "refused", "admin_required") in outcomes
    assert ("admin", "ok", None) in outcomes


def test_shipped_refuses(gw):
    c = gw["c"]
    res = _patch(c, "basic-agent", "mine now", ADMIN)
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["reason_code"] == "workflow_shipped"
    assert _items(c, ADMIN)["basic-agent"]["description"] != "mine now"
    assert ("admin", "refused", "shipped") in [(r["actor"], r["outcome"], r.get("reason")) for r in _audit(gw)]


@pytest.mark.parametrize("payload", [{}, {"description": "x" * 2001}])
def test_bad_bodies_are_refused(gw, payload):
    res = gw["c"].patch("/api/gateway/bundles/alice-wf", headers=gw["alice"], json=payload)
    assert res.status_code == 422, res.text


def test_the_list_names_interfaces_for_everyone(gw):
    """Round 8 (adversary F2): GET /bundles carries the plain names of the interfaces its
    workflows declare, so a non-admin's "Used by" never needs the admin settings read."""
    c = gw["c"]
    res = c.get("/api/gateway/bundles?all_versions=true", headers=gw["alice"])
    assert res.status_code == 200, res.text
    ifaces = res.json()["interfaces"]
    assert ifaces["abstractcode.agent.v1"]["label"] == "AbstractCode \u2014 chat agent"
    assert ifaces["abstractcode.agent.v1"]["known"] is True and ifaces["abstractcode.agent.v1"]["help"]
