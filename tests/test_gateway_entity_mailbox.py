"""Round 3 §3.1: entities are AI users with their own mailbox.

The entity's plane is rooted at its home (`entity_access.entity_home_dir`), configured by an
admin or its creator through the `/accounts/{id}/email...` mirror of `/me/email...`; its
notifications go to its own address through its own account; its agents' email tools run
through its account; its watcher reads its mailbox. Users are refused on the mirror (403).
AbstractCore's hermetic IMAP/SMTP servers; a real EntityRegistry (no embedder, no model)."""

from __future__ import annotations

import copy
import json

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ADMIN, ADMIN_ADDR, ALICE, connect_body, smtp_bodies

pytestmark = pytest.mark.integration

ENTITY_ADDR = ADMIN_ADDR  # a mailbox the fake servers know; owned by the entity in these tests


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


@pytest.fixture
def world(gateway, monkeypatch):
    pytest.importorskip("abstractmemory")
    pytest.importorskip("yaml")
    from fastapi.testclient import TestClient

    from abstractgateway.entities import EntityRegistry
    from abstractgateway.routes import entities_router
    from abstractgateway.users import GatewayUserRegistry
    import abstractgateway.routes.entities as entities_routes

    app = gateway["app"]
    app.include_router(entities_router, prefix="/api")
    registry = EntityRegistry(data_dir=gateway["data_dir"], embedder_factory=lambda: None)
    monkeypatch.setattr(entities_routes, "_registry", lambda: registry)
    c = TestClient(app)
    _rec, admin_token = GatewayUserRegistry().create_user(user_id="admin", roles=["admin", "user"], runtime_id="default")
    for who, name in (("alice", "Aster"), ("bob", "Borea")):
        r = c.post("/api/gateway/entities", headers=gateway[who], json={"name": name, "spark": _spark(name), "skills": []})
        assert r.status_code == 201, r.text
    yield {"c": c, "admin": {"Authorization": f"Bearer {admin_token}"}, "registry": registry, **gateway}
    from abstractgateway.mail.worker import stop_all_entity_workers

    stop_all_entity_workers()
    registry.close_all() if hasattr(registry, "close_all") else None


def _connect(world, imap, smtp, headers=None):
    r = world["c"].put("/api/gateway/accounts/aster/email", headers=headers or world["alice"], json=connect_body(ENTITY_ADDR, imap, smtp))
    assert r.status_code == 200, r.text
    return r.json()


def test_entity_plane_is_rooted_at_its_home(world) -> None:
    from abstractgateway.mail.accounts import entity_plane, plane_for_principal
    from abstractgateway.users import GatewayUserRegistry

    home = world["registry"].entities_dir / "aster"
    plane = plane_for_principal(GatewayUserRegistry().get_user("aster").to_principal())
    assert plane == entity_plane("aster")
    assert plane.root == home and plane.is_entity and plane.key == "default:aster"
    assert plane.account_config_file == home / "email" / "account" / "abstractcore.json"
    assert plane.email_dir == home / "email"


def test_creator_connects_the_entity_mailbox_and_it_is_the_entitys_own(world, imap, smtp) -> None:
    c = world["c"]
    out = _connect(world, imap, smtp)
    assert out["ok"] is True
    home = world["registry"].entities_dir / "aster"
    assert (home / "email" / "account" / "abstractcore.json").is_file()
    # Same answers as /me/email, on the entity's plane.
    card = c.get("/api/gateway/accounts/aster/email", headers=world["alice"]).json()
    assert card["mailbox"] == {"state": "connected", "address": ENTITY_ADDR, "provider": "imap", "reason": None}
    assert card["email_address"] == ENTITY_ADDR  # connecting set the entity's address
    # The creator's own mailbox is untouched.
    mine = c.get("/api/gateway/me/email", headers=world["alice"]).json()
    assert mine["mailbox"]["state"] == "not_connected" and mine["email_address"] == ALICE
    # The Accounts row shows the entity's real state.
    rows = {r["id"]: r for r in c.get("/api/gateway/admin/accounts", headers=world["admin"]).json()["accounts"]}
    assert rows["aster"]["mailbox"]["state"] == "connected" and rows["aster"]["email_address"] == ENTITY_ADDR
    assert rows["borea"]["mailbox"]["state"] == "not_connected"
    # Every /me/email sub-route is mirrored.
    from abstractgateway.routes.email import ENTITY_MAILBOX_ROUTES

    assert "/accounts/{account_id}/email/agent-tools" in ENTITY_MAILBOX_ROUTES
    assert "/accounts/{account_id}/notifications/test" in ENTITY_MAILBOX_ROUTES
    assert len(ENTITY_MAILBOX_ROUTES) == 21


def test_mirror_refuses_users_and_entities_you_cannot_manage(world) -> None:
    c = world["c"]
    r = c.get("/api/gateway/accounts/alice/email", headers=world["admin"])
    assert r.status_code == 403 and "is a user" in r.json()["detail"]["message"]
    for who, target in (("bob", "aster"), ("alice", "borea"), ("alice", "bob"), ("alice", "nobody")):
        r = c.get(f"/api/gateway/accounts/{target}/email", headers=world[who])
        assert r.status_code == 403, (who, target, r.text)
        assert r.json()["detail"]["message"] == f"There is no entity named {target!r} whose mailbox you can manage."
    # The admin manages any entity's mailbox.
    assert c.get("/api/gateway/accounts/borea/email", headers=world["admin"]).status_code == 200
    # Nobody reads an entity's mail: no message routes, and its email/ dir is outside its workspace.
    paths = {getattr(r, "path", "") for r in world["app"].routes}
    assert not any(p.startswith("/api/gateway/accounts/{account_id}/email/messages") for p in paths)
    r = c.get("/api/gateway/entities/aster/workspace/file?path=../email/account/abstractcore.json", headers=world["admin"])
    assert r.status_code in (400, 403), r.text


def test_test_notification_goes_to_the_entity_address_through_its_account(world, imap, smtp) -> None:
    _connect(world, imap, smtp)
    r = world["c"].post("/api/gateway/accounts/aster/notifications/test", headers=world["alice"])
    assert r.status_code == 200, r.text
    assert r.json()["message"] == f"Sent to {ENTITY_ADDR}."
    sent = smtp_bodies(smtp)
    assert sent and sent[-1]["to"] == [ENTITY_ADDR] and sent[-1]["from"] == ENTITY_ADDR


def test_entity_agent_email_tool_sends_through_the_entitys_account(world, imap, smtp) -> None:
    from abstractruntime.core.models import Effect, EffectType

    from abstractgateway.mail.runtime_wiring import entity_email_tools_offered, run_entity_email_tool

    c = world["c"]
    _connect(world, imap, smtp)
    assert entity_email_tools_offered("aster") == ()  # switch off by default: nothing offered
    r = c.put("/api/gateway/accounts/aster/email/agent-tools", headers=world["alice"], json={"enabled": True})
    assert r.status_code == 200 and r.json()["agent_tools"]["active"] is True, r.text
    assert "send_email" in entity_email_tools_offered("aster")

    # The entity runtime is bound to the entity's account (never its creator's).
    er = world["registry"].get_entity_runtime("aster")
    assert er.runtime.email_binding.account_ref == "default:aster:default"
    assert er.runtime.email_binding.address == ENTITY_ADDR

    # The door's TOOL_CALLS handler runs send_email through the entity's account.
    handler = world["registry"]._entity_tool_handler("aster")

    class _Run:
        run_id = "r1"
        vars: dict = {"_runtime": {"turn_id": "t1"}}

    effect = Effect(type=EffectType.TOOL_CALLS, payload={"tool_calls": [
        {"call_id": "c1", "name": "send_email", "arguments": {"to": ENTITY_ADDR, "subject": "From Aster", "body_text": "hello"}},
    ]})
    outcome = handler(_Run(), effect)
    res = outcome.result["results"][0]
    assert res["name"] == "send_email" and json.loads(res["output"])["success"] is True, res
    sent = smtp_bodies(smtp)
    assert sent[-1]["subject"] == "From Aster" and sent[-1]["from"] == ENTITY_ADDR

    # The recipient policy is the entity's: a stranger is refused, nothing sent.
    before = len(smtp.messages)
    out = run_entity_email_tool("aster", "send_email", {"to": "stranger@example.test", "subject": "x", "body_text": "y"})
    assert out["success"] is False and len(smtp.messages) == before

    # Switch off: the tool is refused at the door, no send.
    assert c.put("/api/gateway/accounts/aster/email/agent-tools", headers=world["alice"], json={"enabled": False}).status_code == 200
    outcome = handler(_Run(), effect)
    assert outcome.result["results"][0]["success"] is False and len(smtp.messages) == before


def test_entity_watcher_reads_its_mailbox_and_stops_when_archived(world, imap, smtp) -> None:
    from abstractgateway.mail.accounts import entity_plane
    from abstractgateway.mail.worker import EntityEmailWorker, entity_worker, sync_entity_workers

    _connect(world, imap, smtp)
    plane = entity_plane("aster")
    out = EntityEmailWorker(plane)._watcher().poll_once(force=True)
    assert out["state"] == "watching", out
    assert (plane.root / "event_inbox").is_dir()  # the entity's own durable inbox, in its home
    # The mirror started the entity's worker; archiving stops it, suspending too.
    sync_entity_workers()
    assert entity_worker("aster") is not None
    r = world["c"].post("/api/gateway/admin/accounts/aster/archive", headers=world["admin"])
    assert r.status_code == 200, r.text
    assert entity_worker("aster") is None
    # An archived entity's mailbox is not configurable (403 with the sentence).
    r = world["c"].get("/api/gateway/accounts/aster/email", headers=world["admin"])
    assert r.status_code == 403 and "archived" in r.json()["detail"]["message"]
    r = world["c"].put("/api/gateway/admin/accounts/borea/active", headers=world["admin"], json={"active": False})
    assert r.status_code == 200 and entity_worker("borea") is None


def test_entity_recipient_rules_are_its_own(world, imap, smtp) -> None:
    """The recipients lane's policy body on the entity mirror: Always denied on the ENTITY's plane
    refuses there and leaves the creator's own policy alone."""
    c = world["c"]
    _connect(world, imap, smtp)
    r = c.put("/api/gateway/accounts/aster/email/policy", headers=world["alice"], json={"mode": "denylist", "always_deny": ["xxx.gov"]})
    assert r.status_code == 200, r.text
    r = c.post("/api/gateway/accounts/aster/email/policy/check", headers=world["alice"], json={"to": ["a@sub.xxx.gov"]})
    assert r.status_code == 200, r.text
    assert r.json()["allowed"] is False, r.json()
    mine = c.post("/api/gateway/me/email/policy/check", headers=world["alice"], json={"to": ["a@sub.xxx.gov"]})
    assert mine.status_code == 200 and "xxx.gov" not in json.dumps(c.get("/api/gateway/me/email", headers=world["alice"]).json().get("policy"))
