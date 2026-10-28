"""First-run onboarding and password sign-in (0.18.0), through the real app.

A fresh data dir is UNCLAIMED: the console offers "Set up your gateway" to any accepted origin,
proxied or not; the setup code is ALWAYS required (a proxy that adds no forwarding headers looks
exactly like a local browser); choosing the admin password signs in and closes the window for good; the account then
signs in with a password (attempt-limited) next to its token. The console side is pinned in
test_gateway_console_first_run.py (node VM).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abstractgateway import first_run
from abstractgateway.passwords import ONBOARDING_LIMITER, PASSWORD_LIMITER, password_problem, verify_password
from abstractgateway.security.sessions import gateway_csrf_cookie_name, gateway_session_cookie_name

pytestmark = pytest.mark.basic

ORIGIN = {"Origin": "http://localhost:8080"}
REMOTE = ("203.0.113.9", 50000)
GOOD = "lighthouse keeper 42"


@pytest.fixture(autouse=True)
def _limits() -> None:
    ONBOARDING_LIMITER.reset()
    PASSWORD_LIMITER.reset()
    yield
    ONBOARDING_LIMITER.reset()
    PASSWORD_LIMITER.reset()


@pytest.fixture()
def fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh data dir after `serve`'s bootstrap: the admin exists with a generated token."""
    data = tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.delenv("ABSTRACTGATEWAY_BOOTSTRAP_ADMIN_TOKEN", raising=False)
    from abstractgateway.config_cli import ensure_bootstrap_admin_user
    from abstractgateway.service import reset_gateway_boot_state

    reset_gateway_boot_state()
    ensure_bootstrap_admin_user()
    return data


def _client(peer=REMOTE) -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, client=peer)


def _claim(client: TestClient, data: Path, **extra):
    body = {"password": GOOD, "password_confirm": GOOD, "setup_code": first_run.setup_code_for_display(data)}
    body.update(extra)
    return client.post("/api/gateway/session/onboarding", json=body, headers=ORIGIN)


# ---------------------------------------------------------------------------- the unclaimed state


def test_a_fresh_data_dir_is_unclaimed_and_always_asks_for_the_setup_code(fresh: Path) -> None:
    st = _client().get("/api/gateway/session/onboarding").json()
    assert st["unclaimed"] is True and st["account"] == "admin"
    assert st["setup_code_required"] is True and "setup code" in st["setup_code_reason"]
    rec = json.loads(first_run.onboarding_path(fresh).read_text(encoding="utf-8"))
    assert rec["state"] == "unclaimed" and len(rec["setup_code"].split()) == 6
    assert oct(first_run.onboarding_path(fresh).stat().st_mode & 0o777) == "0o600"
    # Even a loopback peer without forwarding headers: a header-less proxy looks the same.
    local = _client(("127.0.0.1", 50001)).get("/api/gateway/session/onboarding").json()
    assert local["unclaimed"] is True and local["setup_code_required"] is True


def test_onboarding_from_a_remote_browser_sets_the_password_signs_in_and_closes_the_window(
    fresh: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = _client()
    first_run.mint_claim(data_dir=fresh)  # a pending first-run link
    no_code = client.post("/api/gateway/session/onboarding", json={"password": GOOD, "password_confirm": GOOD}, headers=ORIGIN)
    assert no_code.status_code == 403 and no_code.json()["detail"]["reason_code"] == "setup_code_required"
    wrong = client.post(
        "/api/gateway/session/onboarding",
        json={"password": GOOD, "password_confirm": GOOD, "setup_code": "acorn acorn acorn acorn acorn acorn"},
        headers=ORIGIN,
    )
    assert wrong.status_code == 403 and wrong.json()["detail"]["reason_code"] == "setup_code_refused"
    code = first_run.setup_code_for_display(fresh)
    with caplog.at_level(logging.WARNING, logger="abstractgateway.onboarding"):
        # Any case and separators: the person types it.
        r = client.post(
            "/api/gateway/session/onboarding",
            json={"password": GOOD, "password_confirm": GOOD, "setup_code": code.upper().replace(" ", "-"),
                  "display_name": "Laurent", "remember": True},
            headers=ORIGIN,
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claimed"] is True and body["principal"]["admin"] is True and body["first_run"]["completed"] is False
    assert r.cookies.get(gateway_session_cookie_name()) and r.cookies.get(gateway_csrf_cookie_name())
    # Logged with the client address, on the record and in the log.
    rec = json.loads(first_run.onboarding_path(fresh).read_text(encoding="utf-8"))
    assert rec["state"] == "claimed" and rec["claimed_via"] == "onboarding" and rec["claimed_from"] == REMOTE[0]
    assert "setup_code" not in rec
    assert any("gateway claimed" in m and REMOTE[0] in m for m in caplog.messages)
    # Pending first-run links are gone; the code is spent; the window is closed for everyone.
    assert first_run.pending_claims(fresh)["pending"] == 0
    assert first_run.setup_code_for_display(fresh) is None
    assert _client().get("/api/gateway/session/onboarding").json()["unclaimed"] is False
    again = _claim(_client(("198.51.100.20", 1)), fresh, setup_code=code)
    assert again.status_code == 409 and again.json()["detail"]["reason_code"] == "already_claimed"
    # The signed-in browser is the admin, with the display name and a password.
    me = client.get("/api/gateway/me").json()
    assert me["principal"]["user_id"] == "admin" and me["account"] == {"display_name": "Laurent", "password_set": True}


@pytest.mark.parametrize("headers", [
    {},  # a header-less proxy (minimal nginx proxy_pass, `tailscale funnel --tcp`, ssh -R) or a local browser
    {"Host": "gateway.example.com"},
    {"X-Forwarded-For": "198.51.100.7", "X-Forwarded-Proto": "https"},
])
def test_a_loopback_peer_still_needs_the_code_whatever_its_headers(fresh: Path, headers: dict) -> None:
    # Adversarial review (0.18.0): skipping the code for a loopback peer let a stranger behind a
    # header-less proxy take the gateway.
    client = _client(("127.0.0.1", 50001))
    r = client.post("/api/gateway/session/onboarding", json={"password": GOOD, "password_confirm": GOOD}, headers={**ORIGIN, **headers})
    assert r.status_code == 403 and r.json()["detail"]["reason_code"] == "setup_code_required"
    ok = _claim(client, fresh)
    assert ok.status_code == 200, ok.text
    assert first_run.read_onboarding(fresh)["claimed_from"] == "127.0.0.1"


def test_onboarding_refuses_non_browser_mismatched_and_weak_passwords(fresh: Path) -> None:
    client = _client(("127.0.0.1", 50001))
    no_origin = client.post("/api/gateway/session/onboarding", json={"password": GOOD, "password_confirm": GOOD})
    assert no_origin.status_code == 403 and no_origin.json()["detail"]["reason_code"] == "onboarding_browser_only"
    foreign = client.post("/api/gateway/session/onboarding", json={"password": GOOD, "password_confirm": GOOD}, headers={"Origin": "https://evil.example"})
    assert foreign.status_code == 403  # the security middleware's origin check
    mismatch = client.post("/api/gateway/session/onboarding", json={"password": GOOD, "password_confirm": GOOD + "!"}, headers=ORIGIN)
    assert mismatch.status_code == 400 and mismatch.json()["detail"]["reason_code"] == "password_mismatch"
    for weak in ("short", "aaaaaaaaaaaa", "password123", "admin12345"):
        r = client.post("/api/gateway/session/onboarding", json={"password": weak, "password_confirm": weak}, headers=ORIGIN)
        assert r.status_code == 400 and r.json()["detail"]["reason_code"] == "password_too_weak", weak
    assert first_run.onboarding_unclaimed(fresh) is not None


def test_wrong_setup_codes_are_rate_limited_per_address_even_for_the_right_code(fresh: Path) -> None:
    client = _client()
    for _ in range(5):
        r = _claim(client, fresh, setup_code="acorn acorn acorn acorn acorn acorn")
        assert r.status_code == 403
    locked = _claim(client, fresh)
    assert locked.status_code == 429 and locked.json()["detail"]["reason_code"] == "too_many_attempts"
    assert int(locked.headers["retry-after"]) > 0
    # Another address is not locked by this one.
    assert _claim(_client(("198.51.100.30", 1)), fresh).status_code == 200


def test_many_addresses_together_lock_the_form_for_everyone(fresh: Path) -> None:
    for i in range(20):
        _claim(_client((f"198.51.100.{i + 1}", 1)), fresh, setup_code="acorn acorn acorn acorn acorn acorn")
    r = _claim(_client(("198.51.100.200", 1)), fresh)
    assert r.status_code == 429 and "on this gateway" in r.json()["detail"]["message"]


def test_any_admin_sign_in_claims_the_gateway(fresh: Path) -> None:
    # The local claim link (loopback) is a sign-in too: the onboarding closes.
    code = first_run.mint_claim(data_dir=fresh)["code"]
    r = _client(("127.0.0.1", 50001)).post("/api/gateway/session/claim", json={"code": code})
    assert r.status_code == 200, r.text
    assert first_run.read_onboarding(fresh)["claimed_via"] == "claim_link"
    assert _client().get("/api/gateway/session/onboarding").json()["unclaimed"] is False


def test_a_token_sign_in_as_admin_claims_it_too(fresh: Path) -> None:
    token = (fresh / "auth" / "bootstrap-admin-token").read_text(encoding="utf-8").strip()
    r = _client().post("/api/gateway/session/login", json={"user_id": "admin", "token": token})
    assert r.status_code == 200, r.text
    assert first_run.read_onboarding(fresh)["claimed_via"] == "token"


@pytest.mark.parametrize("how", ["existing_registry", "explicit_token"])
def test_a_data_dir_that_was_not_fresh_is_never_unclaimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    from abstractgateway.config_cli import ensure_bootstrap_admin_user
    from abstractgateway.service import reset_gateway_boot_state
    from abstractgateway.users import GatewayUserRegistry

    reset_gateway_boot_state()
    if how == "existing_registry":
        # An install from before 0.18.0: accounts exist, no onboarding record.
        GatewayUserRegistry().create_user(user_id="bob", roles=["user"])
    else:
        monkeypatch.setenv("ABSTRACTGATEWAY_BOOTSTRAP_ADMIN_TOKEN", "operator-chosen-admin-token-xyz")
    ensure_bootstrap_admin_user()
    assert first_run.read_onboarding(data) is None
    assert _client().get("/api/gateway/session/onboarding").json()["unclaimed"] is False
    r = _client(("127.0.0.1", 1)).post("/api/gateway/session/onboarding", json={"password": GOOD, "password_confirm": GOOD}, headers=ORIGIN)
    assert r.status_code == 409


def test_without_user_accounts_there_is_no_onboarding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "operator-token-for-onboarding-tests")
    monkeypatch.delenv("ABSTRACTGATEWAY_USER_AUTH", raising=False)
    from abstractgateway.service import reset_gateway_boot_state

    reset_gateway_boot_state()
    first_run.open_onboarding(tmp_path / "data")
    assert _client().get("/api/gateway/session/onboarding").json()["unclaimed"] is False
    r = _client(("127.0.0.1", 1)).post("/api/gateway/session/onboarding", json={"password": GOOD, "password_confirm": GOOD}, headers=ORIGIN)
    assert r.status_code == 409


# ---------------------------------------------------------------------------- password sign-in


def _claimed(fresh: Path) -> TestClient:
    client = _client(("127.0.0.1", 50001))
    assert _claim(client, fresh, display_name="Laurent").status_code == 200
    return client


def test_password_sign_in_by_account_or_display_name_and_one_refusal_for_everything_else(fresh: Path) -> None:
    _claimed(fresh)
    second = _client(("198.51.100.40", 1))
    ok = second.post("/api/gateway/session/login", json={"user_id": "admin", "password": GOOD, "remember": True})
    assert ok.status_code == 200 and ok.json()["principal"]["user_id"] == "admin" and ok.json()["sign_in"] == {"method": "password"}
    by_name = _client(("198.51.100.41", 1)).post("/api/gateway/session/login", json={"user_id": "laurent", "password": GOOD})
    assert by_name.status_code == 200 and by_name.json()["principal"]["user_id"] == "admin"
    refusals = [
        {"user_id": "admin", "password": "not the password"},
        {"user_id": "nobody-here", "password": GOOD},
    ]
    for body in refusals:
        r = _client(("198.51.100.42", 1)).post("/api/gateway/session/login", json=body)
        assert r.status_code == 401 and r.json()["detail"] == {"reason_code": "password_refused", "message": "That account and password don't match."}
    both = _client().post("/api/gateway/session/login", json={"user_id": "admin", "password": GOOD, "token": "x"})
    assert both.status_code == 400
    # The hash at rest is scrypt, never the password.
    raw = (fresh / "auth" / "users.json").read_text(encoding="utf-8")
    assert GOOD not in raw and '"password_hash": "scrypt$' in raw


def test_wrong_passwords_lock_the_guessing_address_for_that_account_not_the_owner(fresh: Path) -> None:
    _claimed(fresh)
    guesser = _client(("198.51.100.50", 1))
    for i in range(5):
        assert guesser.post("/api/gateway/session/login", json={"user_id": "admin", "password": f"wrong guess {i}"}).status_code == 401
    locked = guesser.post("/api/gateway/session/login", json={"user_id": "admin", "password": GOOD})
    assert locked.status_code == 429 and locked.json()["detail"]["reason_code"] == "too_many_attempts"
    assert "this account" in locked.json()["detail"]["message"]
    # The owner, elsewhere, is not locked out by the guesser (review finding: a renewable lock).
    assert _client(("198.51.100.99", 1)).post("/api/gateway/session/login", json={"user_id": "admin", "password": GOOD}).status_code == 200
    # The token still signs in from the guessing address too.
    token = (fresh / "auth" / "bootstrap-admin-token").read_text(encoding="utf-8").strip()
    assert guesser.post("/api/gateway/session/login", json={"user_id": "admin", "token": token}).status_code == 200


def test_many_addresses_guessing_one_account_lock_it_and_locks_decay(fresh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.passwords as pw

    _claimed(fresh)
    for i in range(50):
        _client((f"10.9.{i // 200}.{i % 200 + 1}", 1)).post("/api/gateway/session/login", json={"user_id": "admin", "password": "wrong guess here"})
    r = _client(("198.51.100.99", 1)).post("/api/gateway/session/login", json={"user_id": "admin", "password": GOOD})
    assert r.status_code == 429
    # Every lock ends: 16 minutes later the right password works again.
    real = pw.time.time
    monkeypatch.setattr(pw.time, "time", lambda: real() + 960)
    assert _client(("198.51.100.99", 1)).post("/api/gateway/session/login", json={"user_id": "admin", "password": GOOD}).status_code == 200


def test_a_lone_guess_per_window_never_keeps_a_pair_locked(fresh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.passwords as pw

    _claimed(fresh)
    real = pw.time.time
    clock = {"t": real()}
    monkeypatch.setattr(pw.time, "time", lambda: clock["t"])
    client = _client(("198.51.100.51", 1))
    for _ in range(8):  # one wrong guess every 16 minutes: the ratchet resets each time
        assert client.post("/api/gateway/session/login", json={"user_id": "admin", "password": "wrong guess here"}).status_code == 401
        clock["t"] += 960
    assert client.post("/api/gateway/session/login", json={"user_id": "admin", "password": GOOD}).status_code == 200


def test_one_address_guessing_across_accounts_is_locked(fresh: Path) -> None:
    _claimed(fresh)
    client = _client(("198.51.100.77", 1))
    for i in range(10):
        client.post("/api/gateway/session/login", json={"user_id": f"user{i}", "password": "wrong guess here"})
    r = client.post("/api/gateway/session/login", json={"user_id": "admin", "password": GOOD})
    assert r.status_code == 429 and "this address" in r.json()["detail"]["message"]


def test_changing_my_password_needs_the_current_one_and_ends_my_other_sessions(fresh: Path) -> None:
    mine = _claimed(fresh)
    other = _client(("198.51.100.60", 1))
    assert other.post("/api/gateway/session/login", json={"user_id": "admin", "password": GOOD}).status_code == 200
    assert other.get("/api/gateway/me").status_code == 200
    csrf = {"X-AbstractGateway-CSRF": mine.cookies.get(gateway_csrf_cookie_name())}
    no_current = mine.put("/api/gateway/me/password", json={"new_password": "harbor lights at dawn"}, headers=csrf)
    assert no_current.status_code == 400 and no_current.json()["detail"]["reason_code"] == "current_password_required"
    wrong = mine.put("/api/gateway/me/password", json={"current_password": "nope nope nope", "new_password": "harbor lights at dawn"}, headers=csrf)
    assert wrong.status_code == 401
    ok = mine.put("/api/gateway/me/password", json={"current_password": GOOD, "new_password": "harbor lights at dawn",
                                                     "new_password_confirm": "harbor lights at dawn"}, headers=csrf)
    assert ok.status_code == 200, ok.text
    assert "session" in ok.json()
    # This browser stays signed in (a fresh session); the other one is signed out.
    assert mine.get("/api/gateway/me").status_code == 200
    assert other.get("/api/gateway/me").status_code == 401
    assert _client(("198.51.100.61", 1)).post("/api/gateway/session/login", json={"user_id": "admin", "password": GOOD}).status_code == 401
    assert _client(("198.51.100.62", 1)).post("/api/gateway/session/login", json={"user_id": "admin", "password": "harbor lights at dawn"}).status_code == 200


def test_admin_creates_a_member_with_a_password_and_resets_it(fresh: Path) -> None:
    admin = _claimed(fresh)
    csrf = {"X-AbstractGateway-CSRF": admin.cookies.get(gateway_csrf_cookie_name())}
    weak = admin.post("/api/gateway/admin/users", json={"user_id": "bob", "roles": ["user"], "password": "bob"}, headers=csrf)
    assert weak.status_code == 400 and weak.json()["detail"]["reason_code"] == "password_too_weak"
    created = admin.post("/api/gateway/admin/users", json={"user_id": "bob", "roles": ["user"], "password": "bob's garden gate 7"}, headers=csrf)
    assert created.status_code == 200, created.text
    assert created.json()["user"]["password_set"] is True and created.json()["token"].startswith("agw_")
    bob = _client(("198.51.100.70", 1))
    signed = bob.post("/api/gateway/session/login", json={"user_id": "bob", "password": "bob's garden gate 7"})
    assert signed.status_code == 200 and signed.json()["principal"]["admin"] is False
    # The member path never sees the onboarding, and a member is not an admin.
    assert bob.get("/api/gateway/admin/users").status_code == 403
    rows = {r["id"]: r for r in admin.get("/api/gateway/admin/accounts").json()["accounts"]}
    assert rows["bob"]["password_set"] is True and rows["bob"]["actions"]["password"]["available"] is True
    reset = admin.put("/api/gateway/admin/accounts/bob/password", json={"password": "a brand new gate 8"}, headers=csrf)
    assert reset.status_code == 200, reset.text
    assert bob.get("/api/gateway/me").status_code == 401  # signed out by the reset
    assert _client(("198.51.100.71", 1)).post("/api/gateway/session/login", json={"user_id": "bob", "password": "a brand new gate 8"}).status_code == 200
    # A session from before the reset must not come back when the password is removed (review finding).
    pre = _client(("198.51.100.74", 1))
    assert pre.post("/api/gateway/session/login", json={"user_id": "bob", "password": "a brand new gate 8"}).status_code == 200
    removed = admin.delete("/api/gateway/admin/accounts/bob/password", headers=csrf)
    assert pre.get("/api/gateway/me").status_code == 401
    assert removed.status_code == 200 and removed.json()["user"]["password_set"] is False
    assert _client(("198.51.100.72", 1)).post("/api/gateway/session/login", json={"user_id": "bob", "password": "a brand new gate 8"}).status_code == 401
    # Members set their own.
    bob2 = _client(("198.51.100.73", 1))
    token = created.json()["token"]
    assert bob2.post("/api/gateway/session/login", json={"user_id": "bob", "token": token}).status_code == 200
    own = bob2.put("/api/gateway/me/password", json={"new_password": "my own secret words"},
                   headers={"X-AbstractGateway-CSRF": bob2.cookies.get(gateway_csrf_cookie_name())})
    assert own.status_code == 200, own.text
    # Non-admins cannot set another account's password.
    assert bob2.put("/api/gateway/admin/accounts/admin/password", json={"password": "taking over now 1"},
                    headers={"X-AbstractGateway-CSRF": bob2.cookies.get(gateway_csrf_cookie_name())}).status_code == 403


def test_a_deactivated_or_archived_account_cannot_sign_in_with_its_password(fresh: Path) -> None:
    from abstractgateway.users import GatewayUserRegistry
    from abstractgateway.passwords import hash_password

    _claimed(fresh)
    reg = GatewayUserRegistry()
    reg.create_user(user_id="carol", roles=["user"])
    reg.set_password(user_id="carol", password_hash=hash_password("carol's quiet room"))
    reg.update_user(user_id="carol", enabled=False)
    r = _client(("198.51.100.80", 1)).post("/api/gateway/session/login", json={"user_id": "carol", "password": "carol's quiet room"})
    assert r.status_code == 401


def test_the_strength_rule_and_the_hash() -> None:
    assert password_problem("lighthouse keeper 42", account="admin") is None
    assert password_problem("admin-and-more-words-here", account="admin") is None
    assert password_problem("admin12345", account="admin")
    assert password_problem("1111111111")
    from abstractgateway.passwords import hash_password

    h = hash_password(GOOD)
    assert h.startswith("scrypt$") and verify_password(GOOD, h) and not verify_password(GOOD + "x", h)
    assert not verify_password(GOOD, "pbkdf2_sha256$1$00$00")


def test_setup_code_words_are_distinct_and_codes_are_six_words() -> None:
    assert len(set(first_run.SETUP_WORDLIST)) == len(first_run.SETUP_WORDLIST) >= 256
    code = first_run.generate_setup_code()
    assert len(code.split()) == 6 and all(w in first_run.SETUP_WORDLIST for w in code.split())


# ---------------------------------------------------------------------------- the local CLI


def test_claim_code_prints_the_setup_code_until_claimed(fresh: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from abstractgateway.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["claim", "--code", "--data-dir", str(fresh)])
    assert exc.value.code == 0
    out = capsys.readouterr()
    assert out.out.strip() == first_run.setup_code_for_display(fresh)
    _claimed(fresh)
    with pytest.raises(SystemExit) as exc:
        main(["claim", "--code", "--data-dir", str(fresh), "--json"])
    assert exc.value.code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["reason_code"] == "no_setup_code" and "already set up" in payload["message"]


def test_a_stranger_cannot_lock_this_machines_browser_out_of_onboarding(fresh: Path) -> None:
    for i in range(20):
        _claim(_client((f"198.51.100.{i + 1}", 1)), fresh, setup_code="acorn acorn acorn acorn acorn acorn")
    assert _claim(_client(("198.51.100.200", 1)), fresh).status_code == 429
    assert _claim(_client(("127.0.0.1", 50001)), fresh).status_code == 200


def test_creating_an_account_closes_the_window(fresh: Path) -> None:
    token = (fresh / "auth" / "bootstrap-admin-token").read_text(encoding="utf-8").strip()
    r = _client().post("/api/gateway/admin/users", json={"user_id": "dave", "roles": ["user"]}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert first_run.read_onboarding(fresh)["claimed_via"] == "account_created"
    assert _client().get("/api/gateway/session/onboarding").json()["unclaimed"] is False


def test_a_shared_token_deployment_switching_to_accounts_is_not_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "the-old-shared-operator-token")
    from abstractgateway.config_cli import ensure_bootstrap_admin_user
    from abstractgateway.service import reset_gateway_boot_state

    reset_gateway_boot_state()
    ensure_bootstrap_admin_user()
    assert first_run.read_onboarding(data) is None
