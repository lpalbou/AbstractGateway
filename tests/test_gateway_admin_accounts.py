"""Accounts page backend (DESIGN-v2 §2, §6): one resolver for the email address and mailbox
(item 4), users and entities in one list with the Active switch (item 9), account activity
from the audit log.

Real email + gateway routers behind the real security middleware, AbstractCore's hermetic
IMAP/SMTP servers; entities from a real EntityRegistry in the test data dir (no embedder)."""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ADMIN, ADMIN_ADDR, ALICE, connect_body

pytestmark = pytest.mark.integration


def _admin_record_session(gateway):
    """The registry admin (`admin`, default runtime) with its own token: the operator's shape."""
    from abstractgateway.users import GatewayUserRegistry

    _rec, token = GatewayUserRegistry().create_user(user_id="admin", roles=["admin", "user"], runtime_id="default")
    return {"Authorization": f"Bearer {token}"}


def test_admin_row_and_admin_card_read_the_same_resolver(gateway, imap, smtp) -> None:
    """Item 4: the operator's address lives in the gateway knob (set before the admin had a
    registry record), the admin record has no email of its own, the admin connects a mailbox.
    The card, /admin/users and /admin/accounts must all say the same address and "connected"."""
    c = gateway["client"]
    # Account-less operator (static token, no record yet): the address goes to the knob.
    r = c.put("/api/gateway/me/email/address", headers=ADMIN, json={"address": ADMIN_ADDR})
    assert r.status_code == 200, r.text
    admin = _admin_record_session(gateway)
    r = c.put("/api/gateway/me/email", headers=admin, json=connect_body(ADMIN_ADDR, imap, smtp))
    assert r.status_code == 200, r.text

    card = c.get("/api/gateway/me/email", headers=admin).json()
    assert card["email_address"] == ADMIN_ADDR
    assert card["mailbox"] == {"state": "connected", "address": ADMIN_ADDR, "provider": "imap", "reason": None}

    users = c.get("/api/gateway/admin/users", headers=admin).json()["users"]
    row = next(u for u in users if u["user_id"] == "admin")
    assert row["email"] == ""  # the raw record field stays what an edit writes
    assert row["email_address"] == card["email_address"]
    assert row["mailbox"] == card["mailbox"]

    accounts = c.get("/api/gateway/admin/accounts", headers=admin).json()["accounts"]
    arow = next(a for a in accounts if a["id"] == "admin")
    assert arow["email_address"] == card["email_address"]
    assert arow["mailbox"] == card["mailbox"]


def test_user_rows_show_their_own_mailbox_and_paused_state(gateway, imap, smtp) -> None:
    c = gateway["client"]
    assert c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp)).status_code == 200
    assert c.put("/api/gateway/me/email/enabled", headers=gateway["alice"], json={"enabled": False}).status_code == 200
    rows = {a["id"]: a for a in c.get("/api/gateway/admin/accounts", headers=ADMIN).json()["accounts"]}
    assert rows["alice"]["mailbox"]["state"] == "paused" and rows["alice"]["mailbox"]["address"] == ALICE
    assert rows["alice"]["mailbox"]["reason"]
    assert rows["bob"]["mailbox"] == {"state": "not_connected", "address": None, "provider": None, "reason": None}
    assert rows["bob"]["email_address"] == "bob@example.test"


def test_an_empty_address_with_a_connected_mailbox_shows_the_mailbox_address(gateway, imap, smtp) -> None:
    """Sign-in codes and notifications go to the registered address, else the connected mailbox
    (self_address). A mailbox connected before "connecting sets the address" left the address empty:
    the card and the row must show where mail really goes, never "No address" beside "Connected as"."""
    c = gateway["client"]
    assert c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp)).status_code == 200
    r = c.put("/api/gateway/me/email/address", headers=gateway["alice"], json={"address": ""})
    assert r.status_code == 200, r.text
    card = c.get("/api/gateway/me/email", headers=gateway["alice"]).json()
    assert card["email_address"] == ALICE
    rows = {a["id"]: a for a in c.get("/api/gateway/admin/accounts", headers=ADMIN).json()["accounts"]}
    assert rows["alice"]["email_address"] == ALICE


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


@pytest.fixture
def entity_home(gateway, monkeypatch):
    pytest.importorskip("abstractmemory")
    pytest.importorskip("yaml")
    from abstractgateway.entities import EntityRegistry
    import abstractgateway.routes.entities as entities_routes

    registry = EntityRegistry(data_dir=gateway["data_dir"], embedder_factory=lambda: None)
    registry.create(name="Selka", spark=_spark("Selka"))
    monkeypatch.setattr(entities_routes, "_registry", lambda: registry)
    return registry


def test_accounts_list_users_and_entities_with_reasons(gateway, entity_home) -> None:
    c = gateway["client"]
    admin = _admin_record_session(gateway)
    body = c.get("/api/gateway/admin/accounts", headers=admin).json()
    rows = body["accounts"]
    assert [(r["role"], r["id"]) for r in rows] == [("admin", "admin"), ("user", "alice"), ("user", "bob"), ("entity", "selka")]
    own = rows[0]
    assert own["own"] is True
    assert own["actions"]["suspend"] == {"available": False, "reason": "You can't deactivate your own account."}
    assert own["actions"]["archive"] == {"available": False, "reason": "You can't archive your own account."}
    alice = rows[1]
    assert alice["kind"] == "user" and alice["active"] is True and alice["archived"] is False
    assert all(alice["actions"][k]["available"] for k in ("email", "logs", "workspace", "rotate", "archive", "suspend"))
    assert alice["actions"]["manage"]["available"] is False and alice["actions"]["unarchive"]["available"] is False
    assert set(alice["actions"]) == {"openai_api", "email", "logs", "workspace", "preferences", "rotate", "manage", "archive", "unarchive", "suspend"}
    ent = rows[3]
    # Round 3: an entity is an AI user with its own mailbox (not connected yet).
    assert ent["kind"] == "entity" and ent["mailbox"]["state"] == "not_connected"
    assert ent["actions"]["rotate"]["available"] is False and ent["actions"]["rotate"]["reason"]
    assert ent["actions"]["email"]["available"] is True and ent["actions"]["archive"]["available"] is True
    assert ent["actions"]["manage"]["available"] is True and ent["actions"]["suspend"]["available"] is True
    assert ent["entity_state"] in ("awake", "asleep")


def test_active_switch_users_refuses_own_account(gateway) -> None:
    c = gateway["client"]
    admin = _admin_record_session(gateway)
    r = c.put("/api/gateway/admin/accounts/admin/active", headers=admin, json={"active": False})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["message"] == "You can't deactivate your own account."

    r = c.put("/api/gateway/admin/accounts/alice/active", headers=admin, json={"active": False})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "alice" and r.json()["active"] is False
    # Deactivated = can't use the gateway.
    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).status_code == 401
    r = c.put("/api/gateway/admin/accounts/alice/active", headers=admin, json={"active": True})
    assert r.status_code == 200 and r.json()["active"] is True
    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).status_code == 200
    assert c.put("/api/gateway/admin/accounts/nobody/active", headers=admin, json={"active": False}).status_code == 404


def test_entity_suspend_pauses_and_resume_restores_the_previous_state(gateway, entity_home) -> None:
    from abstractgateway.users import GatewayUserRegistry

    c = gateway["client"]
    admin = _admin_record_session(gateway)
    entity_home.set_state(name="selka", state="asleep", reason="test setup")

    r = c.put("/api/gateway/admin/accounts/selka/active", headers=admin, json={"active": False})
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False and r.json()["entity_state"] == "paused"
    assert entity_home.state_of("selka")["state"] == "paused"
    assert GatewayUserRegistry().get_user("selka").enabled is False
    stored = json.loads((gateway["data_dir"] / "auth" / "entity_suspended.json").read_text())
    assert stored["selka"]["previous_state"] == "asleep"

    r = c.put("/api/gateway/admin/accounts/selka/active", headers=admin, json={"active": True})
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True and r.json()["entity_state"] == "asleep"
    assert entity_home.state_of("selka")["state"] == "asleep"
    assert GatewayUserRegistry().get_user("selka").enabled is True
    assert "selka" not in json.loads((gateway["data_dir"] / "auth" / "entity_suspended.json").read_text())


# ---------------------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------------------


def test_activity_records_sign_in_token_rotation_and_email(gateway, imap, smtp) -> None:
    c = gateway["client"]
    r = c.post("/api/gateway/session/login", json={"user_id": "alice", "token": gateway["alice_token"]})
    assert r.status_code == 200, r.text
    c.cookies.clear()
    assert c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp)).status_code == 200
    r = c.patch("/api/gateway/admin/users/alice", headers=ADMIN, json={"rotate_token": True})
    assert r.status_code == 200, r.text

    body = c.get("/api/gateway/admin/accounts/alice/activity", headers=ADMIN).json()
    assert body["source"] == "audit_log" and body["truncated"] is False and body["note"]
    kinds = [(e["kind"], e["title"]) for e in body["events"]]
    assert kinds[0] == ("token", "Token rotated")  # newest first
    assert ("email", "Mailbox connected") in kinds
    assert ("sign_in", "Signed in") in kinds
    sign_in = next(e for e in body["events"] if e["kind"] == "sign_in")
    assert sign_in["detail"] == "With a token." and sign_in["ok"] is True
    # Bob's log holds none of it.
    assert c.get("/api/gateway/admin/accounts/bob/activity", headers=ADMIN).json()["events"] == []
    # The kind filter, and /me/activity for the signed-in account.
    only = c.get("/api/gateway/admin/accounts/alice/activity?kind=email", headers=ADMIN).json()["events"]
    assert only and all(e["kind"] == "email" for e in only)
    assert c.get("/api/gateway/admin/accounts/alice/activity?kind=bogus", headers=ADMIN).status_code == 400
    mine = c.get("/api/gateway/me/activity", headers={"Authorization": f"Bearer {r.json()['token']}"}).json()["events"]
    assert ("email", "Mailbox connected") in [(e["kind"], e["title"]) for e in mine]


def _write_synthetic_log(path: Path, *, target_bytes: int, user: str, runs_every: int) -> int:
    """A 13 MB audit log shaped like the operator's: mostly other principals' writes."""
    written = 0
    i = 0
    with open(path, "w", encoding="utf-8") as fh:
        while written < target_bytes:
            i += 1
            if i % runs_every == 0:
                entry = {"ts": f"2026-09-30T10:{i % 60:02d}:00+00:00", "request_id": f"r{i}", "ip": "127.0.0.1", "method": "POST",
                         "path": "/api/gateway/runs/start", "query": "", "status": 200, "duration_ms": 12, "auth_required": True,
                         "principal_user_id": user, "principal_tenant_id": "default",
                         "run": {"run_id": f"run-{i}", "workflow": "basic-agent", "scheduled": False}}
            else:
                entry = {"ts": "2026-09-30T10:00:00+00:00", "request_id": f"r{i}", "ip": "127.0.0.1", "method": "POST",
                         "path": f"/api/gateway/runs/x{i}/chat", "query": "", "status": 200, "duration_ms": 30,
                         "auth_required": True, "principal_user_id": "someone-else", "principal_tenant_id": "default",
                         "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"}
            line = json.dumps(entry, separators=(",", ":")) + "\n"
            fh.write(line)
            written += len(line)
    return written


def test_activity_scan_answers_under_a_second_on_a_13_mb_log(tmp_path) -> None:
    from abstractgateway.account_activity import account_activity

    size = _write_synthetic_log(tmp_path / "audit_log.jsonl", target_bytes=13 * 1024 * 1024, user="alice", runs_every=5000)
    assert size >= 13 * 1024 * 1024
    t0 = time.perf_counter()
    out = account_activity("alice", limit=100, data_dir=tmp_path)
    elapsed = time.perf_counter() - t0
    assert out["events"] and all(e["kind"] == "run" and e["run_id"] for e in out["events"])
    assert out["truncated"] is False  # the whole file fits the budget: nothing older exists
    # A user with no events forces a full scan: still well under a second.
    t1 = time.perf_counter()
    empty = account_activity("nobody", limit=100, data_dir=tmp_path)
    full_scan = time.perf_counter() - t1
    assert empty["events"] == [] and empty["oldest_ts"]
    # Worst case: the account is on every line (the operator's file is mostly the admin's),
    # so every line is parsed and none is shown.
    t2 = time.perf_counter()
    busy = account_activity("someone-else", limit=100, data_dir=tmp_path)
    parse_all = time.perf_counter() - t2
    assert busy["events"] == []
    assert elapsed < 1.0 and full_scan < 1.0 and parse_all < 1.0, (elapsed, full_scan, parse_all)


def test_activity_reads_rotated_files_and_says_when_truncated(tmp_path) -> None:
    from abstractgateway.account_activity import account_activity

    old = {"ts": "2026-09-01T09:00:00+00:00", "method": "POST", "path": "/api/gateway/automations", "status": 200,
           "principal_user_id": "alice", "principal_tenant_id": "default",
           "automation": {"automation_id": "auto-1", "command": "automation.create"}}
    new = {"ts": "2026-09-30T09:00:00+00:00", "method": "POST", "path": "/api/gateway/automations/auto-1/commands", "status": 200,
           "principal_user_id": "alice", "principal_tenant_id": "default",
           "automation": {"automation_id": "auto-1", "command": "automation.pause"}}
    (tmp_path / "audit_log.20260901T090000+0000.jsonl").write_text(json.dumps(old) + "\n")
    (tmp_path / "audit_log.jsonl").write_text(json.dumps(new) + "\n")
    out = account_activity("alice", data_dir=tmp_path)
    assert [e["title"] for e in out["events"]] == ["Automation paused", "Automation created"]
    assert out["events"][0]["detail"] == "auto-1"
    # The Observer's Automations page (its `#automations` hash route), under the gateway's app mount.
    assert out["events"][0]["observer_path"] == "/apps/observer/#automations"
    assert out["events"][0]["ts_local"].startswith("2026-09-30T") and out["events"][0]["ts_local"][-6] in "+-"
    assert out["oldest_ts"] == "2026-09-01T09:00:00+00:00" and out["truncated"] is False
    small = account_activity("alice", data_dir=tmp_path, byte_budget=10)
    assert small["truncated"] is True


def test_run_started_event_carries_the_run_id(tmp_path) -> None:
    from abstractgateway.account_activity import account_activity

    line = {"ts": "2026-09-30T09:00:00+00:00", "method": "POST", "path": "/api/gateway/runs/start", "status": 200,
            "principal_user_id": "alice", "principal_tenant_id": "default", "run": {"run_id": "r-42", "workflow": "basic-agent"}}
    (tmp_path / "audit_log.jsonl").write_text(json.dumps(line) + "\n")
    ev = account_activity("alice", data_dir=tmp_path)["events"][0]
    # "Open in Observer": the Observer's `#run/<run_id>` route under the gateway's app mount
    # (exact path: a change of either side's format must fail here).
    ts_local = ev.pop("ts_local")
    assert ts_local and ts_local[-6] in "+-"
    assert ev == {"ts": line["ts"], "kind": "run", "title": "Run started", "detail": "basic-agent", "run_id": "r-42",
                  "observer_path": "/apps/observer/#run/r-42", "ok": True}


def test_observer_run_link_quotes_the_run_id() -> None:
    from abstractgateway.account_activity import observer_path_for

    assert observer_path_for("a b/c") == "/apps/observer/#run/a%20b%2Fc"
    assert observer_path_for(None) is None
