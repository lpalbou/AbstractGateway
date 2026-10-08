"""Round 5 access model of the OpenAI API at /v1: the per-account "OpenAI API"
switch (Accounts row, migration default-on, standard 403), who Open mode runs
as (the built-in Guest, models only, or an account the admin chose — never an
admin), the role-shaped page data, the log's recorded request/response with
credentials removed, and no route ever answering a stored token.

Hermetic: the `gw` fixture of test_openai_api (scratch data dir, Core stubbed)."""
from __future__ import annotations

import json

import pytest

from abstractgateway import core_endpoint as ce
from test_openai_api import ADMIN, CHAT, USER_TOKEN, _audit, _peer, _set, gw  # noqa: F401 - gw is a fixture

pytestmark = pytest.mark.basic

ALICE = {"Authorization": f"Bearer {USER_TOKEN}"}
BOB_TOKEN = "openai-user-token-0002-bob"
BOB = {"Authorization": f"Bearer {BOB_TOKEN}"}
ADMIN2_TOKEN = "openai-admin-token-0003-ann"


def _make(gw, user_id, token, roles=("user",)):
    r = gw.admin.post("/api/gateway/admin/users", headers=ADMIN,
                      json={"user_id": user_id, "tenant_id": "default", "roles": list(roles), "token": token})
    assert r.status_code == 200, r.text


def _switch(gw, user_id, enabled, headers=ADMIN):
    return gw.admin.put(f"/api/gateway/admin/accounts/{user_id}/openai-api", headers=headers, json={"enabled": enabled})


def _row(gw, user_id):
    rows = gw.admin.get("/api/gateway/admin/accounts", headers=ADMIN).json()["accounts"]
    return next(r for r in rows if r["id"] == user_id)


def _registry_record(user_id):
    from abstractgateway.users import GatewayUserRegistry

    return GatewayUserRegistry().get_user(user_id)


# ---- the per-account switch ---------------------------------------------------

def test_switch_off_answers_the_standard_403_and_on_serves_again(gw):
    _set(gw, enabled=True)
    assert _row(gw, "alice")["openai_api"] is True
    assert gw.admin.post("/v1/chat/completions", headers=ALICE, json=CHAT).status_code == 200
    r = _switch(gw, "alice", False)
    assert r.status_code == 200 and r.json()["openai_api"] is False
    refused = gw.admin.post("/v1/chat/completions", headers=ALICE, json=CHAT)
    assert refused.status_code == 403, refused.text
    assert refused.json() == {"error": {"message": "The OpenAI API is off for your account. An admin can turn it on "
                                                   "in Accounts.", "type": "permission_error", "param": None,
                                        "code": "openai_api_off"}}
    assert gw.admin.get("/v1/models", headers=ALICE).status_code == 403
    calls = len(gw.stub.calls)
    gw.admin.post("/v1/chat/completions", headers=ALICE, json=CHAT)
    assert len(gw.stub.calls) == calls  # nothing reached Core
    # The console sign-in is a different thing: the account is still active.
    assert _row(gw, "alice")["active"] is True
    # The page tells the person.
    assert gw.admin.get("/api/gateway/openai-api", headers=ALICE).json()["key"]["allowed"] is False
    assert _switch(gw, "alice", True).json()["openai_api"] is True
    assert gw.admin.post("/v1/chat/completions", headers=ALICE, json=CHAT).status_code == 200
    # The operator's own token is not a registry account: always allowed.
    assert gw.admin.post("/v1/chat/completions", headers=ADMIN, json=CHAT).status_code == 200


def test_only_an_admin_flips_the_switch_and_it_is_audited(gw):
    _make(gw, "bob", BOB_TOKEN)
    assert _switch(gw, "alice", False, headers=BOB).status_code == 403
    assert _switch(gw, "nobody", False).status_code == 404
    assert _switch(gw, "alice", False).status_code == 200
    lines = [json.loads(x) for x in (gw.data / "audit_log.jsonl").read_text().splitlines()]
    assert any("openai_api" in json.dumps(x.get("detail") or x.get("changes") or x) and x.get("path", "").endswith(
        "/admin/accounts/alice/openai-api") for x in lines)
    # A plain user's own row shows the switch, unavailable, with the reason.
    mine = gw.admin.get("/api/gateway/me/accounts", headers=BOB).json()["accounts"][0]
    assert mine["openai_api"] is True and mine["actions"]["openai_api"]["available"] is False
    assert mine["actions"]["openai_api"]["reason"] == "Only an admin can change who may use the OpenAI API."


def test_migration_writes_the_switch_on_for_active_accounts_when_the_endpoint_starts(gw):
    _make(gw, "bob", BOB_TOKEN)
    assert gw.admin.put("/api/gateway/admin/accounts/bob/active", headers=ADMIN, json={"active": False}).status_code == 200
    assert _registry_record("alice").openai_api is None and _registry_record("bob").openai_api is None
    _set(gw, enabled=True)
    assert _registry_record("alice").openai_api is True
    assert _registry_record("bob").openai_api is False
    # A later account defaults on while active.
    _make(gw, "carol", "openai-user-token-0004-carol")
    assert _registry_record("carol").openai_api is None and _registry_record("carol").openai_api_allowed() is True


# ---- Open mode runs as an account --------------------------------------------

def _open(gw, **extra):
    _set(gw, enabled=True, reach="network")
    return _set(gw, access="open", **extra)


def test_open_mode_runs_as_the_builtin_guest_by_default_models_only(gw):
    status = _open(gw)
    assert status["open_account"] == "guest"
    assert status["open_account_options"][0] == {"id": "guest", "label": "Guest (models only)", "available": True,
                                                 "selected": True}
    lan = _peer(gw, "192.168.1.20")
    assert lan.post("/v1/chat/completions", json=CHAT).status_code == 200
    assert lan.get("/v1/models").status_code == 200
    line = _audit(gw)[-1]
    assert line["openai_api"]["client"] == "guest" and "principal_user_id" not in line
    assert b"authorization" not in gw.stub.calls[-1]["headers"]  # anonymous towards Core: local models only
    tool = {"type": "function", "function": {"name": "f", "parameters": {"type": "object", "properties": {}}}}
    for body, param in (({**CHAT, "tools": [tool]}, "tools"),
                        ({"model": CHAT["model"], "messages": [{"role": "user", "content": [
                            {"type": "text", "text": "what is this"},
                            {"type": "image_url", "image_url": {"url": "http://192.168.1.9/x.png"}}]}]}, "messages")):
        r = lan.post("/v1/chat/completions", json=body)
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "guest_not_allowed" and r.json()["error"]["param"] == param
        assert r.json()["error"]["type"] == "permission_error"
    # Text in, media out is using a model; anything that carries a file or attachment is not.
    assert lan.post("/v1/audio/speech", json={"model": "x/y", "input": "hi", "voice": "a"}).status_code == 200
    assert lan.post("/v1/embeddings", json={"model": "x/y", "input": "hi"}).status_code == 200
    r = lan.post("/v1/audio/transcriptions", files={"file": ("a.wav", b"RIFF", "audio/wav")}, data={"model": "x/y"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "guest_not_allowed"
    r = lan.post("/v1/images/edits", files={"image": ("a.png", b"PNG", "image/png")}, data={"model": "x/y", "prompt": "p"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "guest_not_allowed"
    # The guest never reaches the console API: every /api/gateway route needs a signed-in account.
    for path in ("/api/gateway/openai-api", "/api/gateway/openai-api/logs", "/api/gateway/runs", "/api/gateway/me/accounts"):
        assert lan.get(path).status_code == 401, path
        assert lan.get(path, headers={"Authorization": "Bearer not-needed"}).status_code == 401, path


def test_open_mode_runs_as_a_chosen_account_never_an_admin(gw):
    _make(gw, "ann", ADMIN2_TOKEN, roles=("admin",))
    _open(gw)
    r = gw.admin.post("/api/gateway/admin/core-endpoint", headers=ADMIN, json={"open_account": "ann"})
    assert r.status_code == 409 and "never run as an admin" in r.json()["detail"]
    status = _set(gw, open_account="alice")
    assert status["open_account"] == "alice"
    options = {o["id"]: o for o in status["open_account_options"]}
    assert options["ann"]["available"] is False and options["alice"]["selected"] is True
    lan = _peer(gw, "192.168.1.20")
    r = lan.post("/v1/chat/completions", json={**CHAT, "tools": [{"type": "function", "function": {"name": "f"}}]})
    assert r.status_code == 200, r.text  # alice is not the guest: her requests may carry tools
    line = _audit(gw)[-1]
    assert line["principal_user_id"] == "alice" and line["openai_api"]["run_as"] == "alice"
    settings = ce.read_settings(gw.data)
    assert gw.stub.calls[-1]["headers"][b"authorization"].decode() == f"Bearer {settings.token}"
    # Those requests are alice's: they are in her own log.
    own = gw.admin.get("/api/gateway/openai-api/logs", headers=ALICE).json()["rows"]
    assert own and own[0]["client"] == "alice"
    # Alice's switch off: Open mode can't run as her any more.
    _switch(gw, "alice", False)
    r = lan.post("/v1/chat/completions", json=CHAT)
    assert r.status_code == 403 and r.json()["error"]["code"] == "open_account_unavailable"
    r = gw.admin.post("/api/gateway/admin/core-endpoint", headers=ADMIN, json={"open_account": "alice"})
    assert r.status_code == 409 and "turn it on in Accounts" in r.json()["detail"]


# ---- the page per role -----------------------------------------------------------

def test_open_account_is_admin_only(gw):
    _make(gw, "bob", BOB_TOKEN)
    assert gw.admin.post("/api/gateway/admin/core-endpoint", headers=BOB, json={"open_account": "guest"}).status_code == 403
    assert "open_account_options" not in gw.admin.get("/api/gateway/openai-api", headers=BOB).json()


# ---- the log: recorded request and response, credentials removed ------------------

def test_log_rows_expand_to_the_recorded_request_and_response_redacted(gw):
    _make(gw, "bob", BOB_TOKEN)
    _set(gw, enabled=True)
    image = "data:image/png;base64," + "A" * 4000
    body = {"model": CHAT["model"], "metadata": {"password": "hunter2-hunter2", "note": "keep"},
            "messages": [{"role": "user", "content": [{"type": "text", "text": f"my key is {USER_TOKEN}"},
                                                      {"type": "image_url", "image_url": {"url": image}}]}]}
    assert gw.admin.post("/v1/chat/completions", headers={**ALICE, "X-AbstractCore-Run-Id": "run-7"}, json=body).status_code == 200
    raw = (gw.data / "audit_log.jsonl").read_text()
    assert USER_TOKEN not in raw and "hunter2" not in raw and "A" * 4000 not in raw
    rows = gw.admin.get("/api/gateway/openai-api/logs", headers=ALICE).json()["rows"]
    rid = rows[0]["request_id"]
    assert rows[0]["recorded"] is True and "request" not in rows[0]  # the list stays light
    detail = gw.admin.get(f"/api/gateway/openai-api/logs/{rid}", headers=ALICE)
    assert detail.status_code == 200 and detail.headers["cache-control"] == "no-store"
    row = detail.json()["row"]
    req = row["request"]["body"]
    assert req["metadata"] == {"password": "[redacted]", "note": "keep"}
    assert req["messages"][0]["content"][0]["text"] == "my key is [redacted]"
    assert req["messages"][0]["content"][1]["image_url"]["url"] == f"[inline image/png, {len(image)} bytes]"
    assert row["response"]["body"]["choices"][0]["message"]["content"] == "ok"
    assert row["observer_path"].endswith("#run/run-7")
    # Someone else's request is not found; an admin reads every request.
    assert gw.admin.get(f"/api/gateway/openai-api/logs/{rid}", headers=BOB).status_code == 404
    assert gw.admin.get(f"/api/gateway/openai-api/logs/{rid}", headers=ADMIN).status_code == 200
    assert gw.admin.get("/api/gateway/openai-api/logs/nope", headers=ADMIN).status_code == 404


def test_a_streamed_answer_is_recorded_assembled(gw):
    _set(gw, enabled=True)
    gw.stub.stream = True
    assert gw.admin.post("/v1/chat/completions", headers=ALICE, json={**CHAT, "stream": True}).status_code == 200
    rid = gw.admin.get("/api/gateway/openai-api/logs", headers=ALICE).json()["rows"][0]["request_id"]
    resp = gw.admin.get(f"/api/gateway/openai-api/logs/{rid}", headers=ALICE).json()["row"]["response"]["body"]
    assert resp["choices"][0]["message"]["content"] == "he" and resp["assembled_from_stream"] >= 2
    assert resp["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}


def test_redact_removes_named_fields_and_known_secrets():
    doc = {"Authorization": "Bearer x", "api_key": "k", "nested": [{"token": "t", "text": "abc SECRET-123456 def"}],
           "empty_token": {"token": ""}, "model": "m"}
    out = ce.redact(doc, ("SECRET-123456",))
    assert out == {"Authorization": "[redacted]", "api_key": "[redacted]",
                   "nested": [{"token": "[redacted]", "text": "abc [redacted] def"}], "empty_token": {"token": ""},
                   "model": "m"}
    assert doc["api_key"] == "k"  # not mutated


# ---- no route answers a stored token --------------------------------------------

def _paths(app):
    """Every (method, full path) the app serves, however FastAPI nests them.

    FastAPI up to 0.13x copies included routers' routes into app.routes as
    APIRoutes; newer releases keep one _IncludedRouter per include whose
    effective_candidates() are route contexts (full path, methods,
    original_route) or nested included routers. Starlette Mounts nest
    routes under a path prefix. All three are walked, so the token check below
    covers every route on every supported FastAPI."""
    from fastapi.routing import APIRoute

    seen = set()

    def walk(routes, prefix=""):
        for route in routes:
            if isinstance(route, APIRoute):
                items = [(m, prefix + route.path) for m in sorted(route.methods or ())]
            elif callable(getattr(route, "effective_candidates", None)):  # FastAPI >= 0.14x included router
                yield from walk(route.effective_candidates(), prefix)
                continue
            elif hasattr(route, "original_route") and hasattr(route, "methods"):  # its route context
                if not isinstance(route.original_route, APIRoute):
                    continue
                items = [(m, prefix + route.path) for m in sorted(route.methods or ())]
            elif isinstance(getattr(route, "routes", None), list):  # Mount / nested router
                yield from walk(route.routes, prefix + getattr(route, "path", ""))
                continue
            else:
                continue
            for item in items:
                if item not in seen:
                    seen.add(item)
                    yield item

    yield from walk(app.routes)


def test_the_route_walk_sees_every_route(gw):
    """The walk itself sees the console API and the OpenAI API page's routes, whatever FastAPI
    nests them in. (`/v1/*` is served by the gateway's ASGI door, not a FastAPI route.)"""
    walked = list(_paths(gw.app))
    paths = {p for _m, p in walked}
    assert len(walked) > 250, len(walked)
    for must in ("/api/gateway/openai-api", "/api/gateway/admin/core-endpoint", "/api/gateway/runs/start"):
        assert any(p == must or p.startswith(must + "/") for p in paths), must


def test_no_route_answers_a_stored_token_in_clear(gw, monkeypatch):
    """Every GET of the console API, and every door of the OpenAI API page (POST/PUT
    included, except the one that MAKES a new key), answered with each signed-in
    role: no stored secret — a user's token, the operator token, the internal
    endpoint key — appears in any answer. The page fills the key client-side."""
    _make(gw, "bob", BOB_TOKEN)
    _set(gw, enabled=True)
    assert gw.admin.post("/v1/chat/completions", headers=ALICE, json=CHAT).status_code == 200
    internal = ce.read_settings(gw.data).token
    # A named API key (round 16) is a stored secret too: answered once by its POST, never again.
    named = gw.admin.post("/api/gateway/me/openai-keys", headers=ALICE, json={"label": "app"}).json()["key"]
    assert gw.admin.get("/v1/models", headers={"Authorization": f"Bearer {named}"}).status_code == 200
    secrets = {USER_TOKEN, BOB_TOKEN, ADMIN["Authorization"].split(" ", 1)[1], internal, named}
    page_doors = ("/api/gateway/openai-api", "/api/gateway/admin/core-endpoint")
    makes_a_key = {"/api/gateway/admin/core-endpoint/token/rotate"}
    checked = 0
    for method, path in _paths(gw.app):
        if path in makes_a_key:
            continue
        on_page = path.startswith(page_doors)
        if method != "GET" and not on_page:
            continue
        url = (path.replace("{request_id}", "nope").replace("{account_id}", "alice").replace("{user_id}", "alice")
               .replace("{fingerprint}", "nope"))
        if "{" in url:
            url = url.split("{", 1)[0].rstrip("/") or "/"
        for headers in (ADMIN, ALICE):
            try:
                r = gw.admin.request(method, url, headers=headers, json={} if method != "GET" else None)
            except Exception:  # noqa: BLE001 - a route that crashes leaks nothing
                continue
            for secret in secrets:
                assert secret not in r.text, f"{method} {path} answered a stored token"
            checked += 1
    assert checked > 50


def test_a_deactivated_or_archived_accounts_key_answers_403_not_401(gw):
    _make(gw, "bob", BOB_TOKEN)
    _set(gw, enabled=True)
    assert gw.admin.get("/v1/models", headers=BOB).status_code == 200
    assert gw.admin.put("/api/gateway/admin/accounts/bob/active", headers=ADMIN, json={"active": False}).status_code == 200
    r = gw.admin.get("/v1/models", headers=BOB)
    assert r.status_code == 403 and r.json()["error"]["code"] == "account_inactive"
    assert r.json()["error"]["type"] == "permission_error"
    assert gw.admin.post("/api/gateway/admin/accounts/bob/archive", headers=ADMIN).status_code == 200
    assert gw.admin.get("/v1/models", headers=BOB).json()["error"]["code"] == "account_inactive"
    # A key that belongs to no account stays a 401.
    assert gw.admin.get("/v1/models", headers={"Authorization": "Bearer not-a-token-at-all"}).status_code == 401
