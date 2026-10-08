"""Named API keys for the OpenAI API at /v1 (round 16, backlog 1000; operator ruling 1001 Q3
"Yes, build them"): made with a name and answered once, listed without the key, revoked one at a
time with immediate effect, valid at /v1 ONLY (401 with a sentence anywhere under /api/gateway,
sign-in included), running as the owning account under its OpenAI API switch and Active state,
named in the request log, admin view and revoke, and users.json stays loadable both ways.

Hermetic: the `gw` fixture of test_openai_api (scratch data dir, Core stubbed, no model)."""
from __future__ import annotations

import json

import pytest

from abstractgateway import core_endpoint as ce
from abstractgateway.openai_keys import ENDPOINT_ONLY
from test_openai_api import ADMIN, CHAT, USER_TOKEN, _audit, _peer, _set, gw  # noqa: F401 - gw is a fixture

pytestmark = pytest.mark.basic

ALICE = {"Authorization": f"Bearer {USER_TOKEN}"}
BOB_TOKEN = "openai-user-token-0002-bob"
BOB = {"Authorization": f"Bearer {BOB_TOKEN}"}


def _make(gw, user_id, token, roles=("user",)):
    r = gw.admin.post("/api/gateway/admin/users", headers=ADMIN,
                      json={"user_id": user_id, "tenant_id": "default", "roles": list(roles), "token": token})
    assert r.status_code == 200, r.text


def _new_key(gw, headers=ALICE, label="laptop Cursor"):
    r = gw.admin.post("/api/gateway/me/openai-keys", headers=headers, json={"label": label})
    assert r.status_code == 200, r.text
    return r.json()


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def _keys(gw, headers=ALICE):
    r = gw.admin.get("/api/gateway/me/openai-keys", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["keys"]


# ---- make, list, answered once ----------------------------------------------------

def test_a_new_key_is_answered_once_and_listed_without_the_key(gw):
    _set(gw, enabled=True)
    assert _keys(gw) == []
    made = _new_key(gw)
    key = made["key"]
    assert key.startswith("sk-agw-") and len(key) > 40
    item = made["item"]
    assert item["label"] == "laptop Cursor" and len(item["fingerprint"]) == 12
    assert item["created_at"] and item["last_used_at"] is None and item["last_client"] is None
    listed = gw.admin.get("/api/gateway/me/openai-keys", headers=ALICE)
    assert listed.headers["cache-control"] == "no-store"
    assert key not in listed.text and "key_hash" not in listed.text and "pbkdf2" not in listed.text
    rows = listed.json()["keys"]
    assert [r["label"] for r in rows] == ["laptop Cursor"]
    assert set(rows[0]) == {"label", "fingerprint", "created_at", "created_by", "last_used_at", "last_client"}
    # The registry keeps a PBKDF2 hash and the fingerprint, never the key.
    raw = (gw.data / "auth" / "users.json").read_text() if (gw.data / "auth" / "users.json").exists() else ""
    from abstractgateway.users import gateway_user_registry_path_from_env

    raw = gateway_user_registry_path_from_env().read_text()
    assert key not in raw and "pbkdf2_sha256$" in raw and item["fingerprint"] in raw


def test_labels_are_required_and_unique_per_account(gw):
    for label, code in (("", "label_required"), ("   ", "label_required"), ("x" * 81, "label_too_long")):
        r = gw.admin.post("/api/gateway/me/openai-keys", headers=ALICE, json={"label": label})
        assert r.status_code == 400 and r.json()["detail"]["reason_code"] == code, r.text
    _new_key(gw, label="phone")
    r = gw.admin.post("/api/gateway/me/openai-keys", headers=ALICE, json={"label": "Phone"})
    assert r.status_code == 409 and r.json()["detail"]["reason_code"] == "label_taken"
    # Another account may use the same name.
    _make(gw, "bob", BOB_TOKEN)
    _new_key(gw, headers=BOB, label="phone")


def test_the_operator_token_has_no_account_so_no_named_keys(gw):
    r = gw.admin.post("/api/gateway/me/openai-keys", headers=ADMIN, json={"label": "x"})
    assert r.status_code == 409 and r.json()["detail"]["reason_code"] == "no_account"
    assert gw.admin.get("/api/gateway/me/openai-keys", headers=ADMIN).status_code == 409


# ---- valid at /v1, and only there ---------------------------------------------------

def test_a_named_key_serves_v1_as_its_account_and_never_reaches_core(gw):
    _set(gw, enabled=True)
    key = _new_key(gw)["key"]
    assert gw.admin.get("/v1/models", headers=_bearer(key)).status_code == 200
    r = gw.admin.post("/v1/chat/completions", headers=_bearer(key), json=CHAT)
    assert r.status_code == 200, r.text
    forwarded = gw.stub.calls[-1]["headers"][b"authorization"].decode()
    assert key not in forwarded and forwarded == f"Bearer {ce.read_settings(gw.data).token}"
    line = _audit(gw)[-1]
    assert line["principal_user_id"] == "alice"
    assert line["openai_api"]["key_label"] == "laptop Cursor"
    assert key not in (gw.data / "audit_log.jsonl").read_text()


ENDPOINT_PATHS = ("/api/gateway/openai-api", "/api/gateway/me/accounts", "/api/gateway/runs",
                  "/api/gateway/me/openai-keys", "/api/gateway/session/me")


def test_a_named_key_is_refused_everywhere_under_api_gateway_with_the_sentence(gw):
    _set(gw, enabled=True)
    key = _new_key(gw)["key"]
    for path in ENDPOINT_PATHS:
        r = gw.admin.get(path, headers=_bearer(key))
        assert r.status_code == 401, (path, r.status_code, r.text)
        assert r.json() == {"detail": ENDPOINT_ONLY, "reason_code": "openai_api_key"}, path
    # Writes too: it can't make keys, start runs or change anything.
    for method, path, body in (("POST", "/api/gateway/me/openai-keys", {"label": "more"}),
                               ("POST", "/api/gateway/runs/start", {}),
                               ("DELETE", "/api/gateway/me/openai-keys/abc", None),
                               ("POST", "/api/gateway/me/token/rotate", {})):
        r = gw.admin.request(method, path, headers=_bearer(key), json=body)
        assert r.status_code == 401 and r.json()["detail"] == ENDPOINT_ONLY, (path, r.text)
    # The event-stream form (?access_token=) is refused the same way.
    r = gw.admin.get(f"/api/gateway/openai-api?access_token={key}")
    assert r.status_code == 401 and r.json()["detail"] == ENDPOINT_ONLY
    # It never signs in.
    r = gw.admin.post("/api/gateway/session/login", json={"user_id": "alice", "token": key})
    assert r.status_code == 401 and r.json()["detail"] == {"reason_code": "openai_api_key", "message": ENDPOINT_ONLY}
    assert "set-cookie" not in {k.lower() for k in r.headers}
    # A refused named key is not a guess: no lockout builds up from it.
    for _ in range(15):
        gw.admin.get("/api/gateway/runs", headers=_bearer(key))
    assert gw.admin.get("/api/gateway/openai-api", headers=ALICE).status_code == 200
    # The account's own token still drives the console API.
    assert gw.admin.get("/api/gateway/me/openai-keys", headers=ALICE).status_code == 200


def test_the_gateway_token_keeps_working_at_v1_for_compatibility(gw):
    _set(gw, enabled=True)
    _new_key(gw)
    r = gw.admin.post("/v1/chat/completions", headers=ALICE, json=CHAT)
    assert r.status_code == 200
    assert "key_label" not in _audit(gw)[-1]["openai_api"]


# ---- revoke ------------------------------------------------------------------------

def test_revoke_is_immediate_and_says_why(gw):
    _set(gw, enabled=True)
    key = _new_key(gw)["key"]
    other = _new_key(gw, label="home assistant")["key"]
    assert gw.admin.get("/v1/models", headers=_bearer(key)).status_code == 200  # cached as valid
    fp = _keys(gw)[0]["fingerprint"]
    r = gw.admin.delete(f"/api/gateway/me/openai-keys/{fp}", headers=ALICE)
    assert r.status_code == 200 and r.json()["revoked"]["label"] == "laptop Cursor"
    refused = gw.admin.get("/v1/models", headers=_bearer(key))
    assert refused.status_code == 401
    err = refused.json()["error"]
    assert err["code"] == "invalid_api_key" and "revoked" in err["message"]
    assert [k["label"] for k in _keys(gw)] == ["home assistant"]
    assert gw.admin.get("/v1/models", headers=_bearer(other)).status_code == 200
    assert gw.admin.delete(f"/api/gateway/me/openai-keys/{fp}", headers=ALICE).status_code == 404
    # The revoked key is no longer recognized anywhere: a plain 401 at /api/gateway.
    r = gw.admin.get("/api/gateway/runs", headers=_bearer(key))
    assert r.status_code == 401 and r.json().get("detail") != ENDPOINT_ONLY


# ---- the account decides ----------------------------------------------------------

def test_the_accounts_openai_api_switch_governs_every_key(gw):
    _set(gw, enabled=True)
    a = _new_key(gw)["key"]
    b = _new_key(gw, label="b")["key"]
    assert gw.admin.put("/api/gateway/admin/accounts/alice/openai-api", headers=ADMIN, json={"enabled": False}).status_code == 200
    calls = len(gw.stub.calls)
    for key in (a, b):
        r = gw.admin.post("/v1/chat/completions", headers=_bearer(key), json=CHAT)
        assert r.status_code == 403 and r.json()["error"]["code"] == "openai_api_off", r.text
    assert len(gw.stub.calls) == calls
    assert gw.admin.put("/api/gateway/admin/accounts/alice/openai-api", headers=ADMIN, json={"enabled": True}).status_code == 200
    assert gw.admin.post("/v1/chat/completions", headers=_bearer(a), json=CHAT).status_code == 200


def test_an_inactive_accounts_keys_answer_403_account_inactive(gw):
    _make(gw, "bob", BOB_TOKEN)
    _set(gw, enabled=True)
    key = _new_key(gw, headers=BOB)["key"]
    assert gw.admin.get("/v1/models", headers=_bearer(key)).status_code == 200
    assert gw.admin.put("/api/gateway/admin/accounts/bob/active", headers=ADMIN, json={"active": False}).status_code == 200
    r = gw.admin.get("/v1/models", headers=_bearer(key))
    assert r.status_code == 403 and r.json()["error"]["code"] == "account_inactive"


# ---- who may see and revoke whose keys --------------------------------------------

def test_authz_matrix_self_admin_other(gw):
    _make(gw, "bob", BOB_TOKEN)
    _set(gw, enabled=True)
    key = _new_key(gw)["key"]
    fp = _keys(gw)[0]["fingerprint"]
    # Bob sees only his own (none) and can't touch Alice's.
    assert _keys(gw, headers=BOB) == []
    assert gw.admin.delete(f"/api/gateway/me/openai-keys/{fp}", headers=BOB).status_code == 404
    assert gw.admin.get("/api/gateway/admin/accounts/alice/openai-keys", headers=BOB).status_code == 403
    assert gw.admin.delete(f"/api/gateway/admin/accounts/alice/openai-keys/{fp}", headers=BOB).status_code == 403
    assert gw.admin.get("/v1/models", headers=_bearer(key)).status_code == 200
    # An admin sees every account's keys and revokes any.
    r = gw.admin.get("/api/gateway/admin/accounts/alice/openai-keys", headers=ADMIN)
    assert r.status_code == 200 and [k["label"] for k in r.json()["keys"]] == ["laptop Cursor"] and key not in r.text
    assert gw.admin.get("/api/gateway/admin/accounts/nobody/openai-keys", headers=ADMIN).status_code == 404
    r = gw.admin.delete(f"/api/gateway/admin/accounts/alice/openai-keys/{fp}", headers=ADMIN)
    assert r.status_code == 200
    assert gw.admin.get("/v1/models", headers=_bearer(key)).status_code == 401
    # The admin's revoke is on the record, naming the key, never the key.
    lines = [json.loads(x) for x in (gw.data / "audit_log.jsonl").read_text().splitlines()]
    hit = [x for x in lines if x.get("openai_key_change", {}).get("action") == "revoke"]
    assert hit and hit[-1]["openai_key_change"]["label"] == "laptop Cursor" and hit[-1]["openai_key_change"]["user_id"] == "alice"
    assert hit[-1]["account_change"]["user_id"] == "alice"


def test_create_is_audited_with_the_label_never_the_key(gw):
    key = _new_key(gw, label="phone app")["key"]
    raw = (gw.data / "audit_log.jsonl").read_text()
    assert key not in raw
    made = [json.loads(x) for x in raw.splitlines() if '"openai_key_change"' in x]
    assert made[-1]["openai_key_change"]["action"] == "create" and made[-1]["openai_key_change"]["label"] == "phone app"


# ---- last used and the request log ------------------------------------------------

def test_last_used_and_client_address_and_the_log_names_the_key(gw):
    _set(gw, enabled=True, reach="network")
    key = _new_key(gw)["key"]
    lan = _peer(gw, "192.168.1.20")
    assert lan.post("/v1/chat/completions", headers=_bearer(key), json=CHAT).status_code == 200
    row = _keys(gw)[0]
    assert row["last_used_at"] and row["last_client"] == "192.168.1.20"
    logs = gw.admin.get("/api/gateway/openai-api/logs", headers=ALICE).json()["rows"]
    assert logs[0]["key_label"] == "laptop Cursor" and logs[0]["key_fingerprint"] == row["fingerprint"]
    assert logs[0]["user_id"] == "alice"
    # The admin's log shows the full recorded request (secrets redacted), with the key's name.
    rid = logs[0]["request_id"]
    det = gw.admin.get(f"/api/gateway/openai-api/logs/{rid}", headers=ADMIN).json()["row"]
    assert det["key_label"] == "laptop Cursor" and det["request"]["body"]["messages"][0]["content"] == "hi"
    assert key not in json.dumps(det)


def test_a_key_pasted_into_a_prompt_is_redacted_from_the_log(gw):
    _set(gw, enabled=True)
    key = _new_key(gw)["key"]
    body = {"model": CHAT["model"], "messages": [{"role": "user", "content": f"my key is {key}"}]}
    assert gw.admin.post("/v1/chat/completions", headers=_bearer(key), json=body).status_code == 200
    assert key not in (gw.data / "audit_log.jsonl").read_text()


# ---- the registry: additive, existing stores load unchanged -----------------------

def test_registry_without_keys_loads_and_saves_byte_identically(gw, tmp_path):
    from abstractgateway.users import GatewayUserRegistry, gateway_user_registry_path_from_env

    path = gateway_user_registry_path_from_env()
    before = json.loads(path.read_text())
    assert all("openai_keys" not in u for u in before["users"])  # an account without keys stores no field
    reg = GatewayUserRegistry()
    rec = reg.get_user("alice")
    assert rec.openai_keys == ()
    # An old (0.13.1) record round-trips unchanged through a write that touches another field.
    reg.update_user(user_id="alice", email="alice@example.com")
    after = json.loads(path.read_text())
    alice = next(u for u in after["users"] if u["user_id"] == "alice")
    assert "openai_keys" not in alice
    # With keys: the field is written, and every other writer keeps it.
    _new_key(gw)
    reg.update_user(user_id="alice", email="a2@example.com")
    reg.set_archived(user_id="alice", archived=False)
    alice = next(u for u in json.loads(path.read_text())["users"] if u["user_id"] == "alice")
    assert [k["label"] for k in alice["openai_keys"]] == ["laptop Cursor"]
    assert set(alice["openai_keys"][0]) == {"label", "key_hash", "fingerprint", "created_at", "created_by"}
    # A malformed entry is skipped, not fatal.
    doc = json.loads(path.read_text())
    for u in doc["users"]:
        if u["user_id"] == "alice":
            u["openai_keys"].append({"label": "broken"})
    path.write_text(json.dumps(doc))
    assert [k.label for k in GatewayUserRegistry().get_user("alice").openai_keys] == ["laptop Cursor"]


def test_status_says_named_keys_and_offers_no_token_as_the_key(gw):
    _set(gw, enabled=True)
    key = gw.admin.get("/api/gateway/openai-api", headers=ALICE).json()["key"]
    assert key["named_keys"] is True
    assert gw.admin.get("/api/gateway/openai-api", headers=ADMIN).json()["key"]["named_keys"] is False
