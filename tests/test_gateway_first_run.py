"""No-terminal first run (2026-09-23): per-OS data dir, loopback auth default,
one-time claim links, first-run state, doctor fields in `config status --json`.

Service registration is pinned in test_gateway_os_service.py; the console
wizard in test_gateway_console_first_run.py.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abstractgateway import first_run, host_paths
from abstractgateway.security.sessions import gateway_csrf_cookie_name, gateway_session_cookie_name

pytestmark = pytest.mark.basic


# ---------------------------------------------------------------------------
# [NEW-1] data dir resolution
# ---------------------------------------------------------------------------


def test_env_wins_and_names_its_variable(tmp_path: Path) -> None:
    res = host_paths.resolve_data_dir(env={"ABSTRACTGATEWAY_DATA_DIR": str(tmp_path / "d")}, cwd=tmp_path)
    assert res.path == (tmp_path / "d").resolve()
    assert res.source == "env"
    assert res.env_name == "ABSTRACTGATEWAY_DATA_DIR"


def test_legacy_env_names_still_count(tmp_path: Path) -> None:
    res = host_paths.resolve_data_dir(env={"ABSTRACTFLOW_RUNTIME_DIR": str(tmp_path / "legacy")}, cwd=tmp_path)
    assert res.source == "env" and res.env_name == "ABSTRACTFLOW_RUNTIME_DIR"


def test_existing_cwd_runtime_is_kept_for_checkouts(tmp_path: Path) -> None:
    (tmp_path / "runtime").mkdir()
    res = host_paths.resolve_data_dir(env={}, cwd=tmp_path, system="darwin", home=tmp_path / "home")
    assert res.path == (tmp_path / "runtime").resolve()
    assert res.source == "legacy_cwd_runtime"
    assert "backward compatibility" in res.reason


@pytest.mark.parametrize(
    "system, env, expected",
    [
        ("darwin", {}, ("Library", "Application Support", "AbstractGateway")),
        ("linux", {}, (".local", "share", "abstractgateway")),
        ("linux", {"XDG_DATA_HOME": "/xdg/data"}, None),
        ("linux", {"XDG_DATA_HOME": "relative/ignored"}, (".local", "share", "abstractgateway")),
        ("win32", {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}, None),
        ("win32", {}, ("AppData", "Local", "AbstractGateway")),
    ],
)
def test_per_os_default_without_cwd_runtime(tmp_path: Path, system: str, env: dict, expected) -> None:
    home = tmp_path / "home"
    res = host_paths.resolve_data_dir(env=env, cwd=tmp_path, system=system, home=home)
    assert res.source == "os_default"
    if system == "linux" and env.get("XDG_DATA_HOME") == "/xdg/data":
        assert res.path == Path("/xdg/data") / "abstractgateway"
    elif system == "win32" and env.get("LOCALAPPDATA"):
        assert res.path == Path(env["LOCALAPPDATA"]) / "AbstractGateway"
    else:
        assert res.path == home.joinpath(*expected)


def test_apply_exports_with_provenance_and_reads_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    env: dict = {}
    res = host_paths.apply_data_dir_default(env)
    assert res.source == "os_default"
    assert env["ABSTRACTGATEWAY_DATA_DIR"] == str(res.path)
    # A later reader (or a child process) still sees WHY, not "env".
    again = host_paths.resolve_data_dir(env=env, cwd=tmp_path)
    assert again.source == "os_default" and again.path == res.path
    # A different value exported by the operator is theirs: the stale marker
    # must not claim it.
    env["ABSTRACTGATEWAY_DATA_DIR"] = str(tmp_path / "mine")
    assert host_paths.resolve_data_dir(env=env, cwd=tmp_path).source == "env"


def test_host_config_uses_the_resolver_when_env_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.config import GatewayHostConfig
    from abstractgateway.users import gateway_data_dir_from_env

    monkeypatch.delenv("ABSTRACTGATEWAY_DATA_DIR", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_FLOWS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(host_paths.sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    expected = (tmp_path / "home" / ".local" / "share" / "abstractgateway")
    assert GatewayHostConfig.from_env().data_dir == expected.resolve()
    assert gateway_data_dir_from_env() == expected
    # And a checkout's ./runtime is kept.
    (tmp_path / "runtime").mkdir()
    assert GatewayHostConfig.from_env().data_dir == (tmp_path / "runtime").resolve()


# ---------------------------------------------------------------------------
# [NEW-2] loopback auth default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]"])
def test_loopback_unconfigured_applies_user_auth(host: str) -> None:
    env: dict = {}
    assert first_run.apply_loopback_auth_default(host, env) is True
    assert env["ABSTRACTGATEWAY_USER_AUTH"] == "1"
    assert first_run.auth_mode_summary(env)["mode"] == "users"
    assert first_run.auth_mode_summary(env)["source"] == "loopback_default"


@pytest.mark.parametrize(
    "host, env",
    [
        ("0.0.0.0", {}),
        ("192.168.1.10", {}),
        ("127.0.0.1", {"ABSTRACTGATEWAY_AUTH_TOKEN": "x" * 20}),
        ("127.0.0.1", {"ABSTRACTGATEWAY_USER_AUTH": "0"}),
        ("127.0.0.1", {"ABSTRACTGATEWAY_AUTH_MODE": "legacy"}),
        ("127.0.0.1", {"ABSTRACTGATEWAY_SECURITY": "0"}),
    ],
)
def test_explicit_config_or_non_loopback_is_never_overridden(host: str, env: dict) -> None:
    before = dict(env)
    assert first_run.apply_loopback_auth_default(host, env) is False
    assert env == before


def test_default_bind_host_keeps_configured_deployments_on_all_interfaces() -> None:
    assert first_run.default_bind_host({}) == "127.0.0.1"
    assert first_run.default_bind_host({"ABSTRACTGATEWAY_AUTH_TOKEN": "t" * 20}) == "0.0.0.0"
    assert first_run.default_bind_host({"ABSTRACTGATEWAY_USER_AUTH": "1"}) == "0.0.0.0"
    # Our own export (a relaunched child) is not an operator statement.
    assert first_run.default_bind_host({"ABSTRACTGATEWAY_USER_AUTH": "1", "ABSTRACTGATEWAY_AUTH_MODE_SOURCE": "loopback_default"}) == "127.0.0.1"


def test_auth_mode_summary_modes() -> None:
    assert first_run.auth_mode_summary({})["mode"] == "loopback_auto"
    assert first_run.auth_mode_summary({"ABSTRACTGATEWAY_AUTH_TOKEN": "t"})["mode"] == "token"
    assert first_run.auth_mode_summary({"ABSTRACTGATEWAY_AUTH_TOKEN": "t", "ABSTRACTGATEWAY_USER_AUTH": "1"})["mode"] == "users+token"
    assert first_run.auth_mode_summary({"ABSTRACTGATEWAY_SECURITY": "0"})["mode"] == "open"


def _fake_uvicorn(monkeypatch: pytest.MonkeyPatch) -> dict:
    called: dict = {}
    uvicorn = types.ModuleType("uvicorn")

    def _run(app: str, **kwargs: object) -> None:
        called["app"] = app
        called.update(kwargs)

    uvicorn.run = _run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    return called


def test_bare_serve_starts_on_loopback_with_user_auth_and_a_claim_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from abstractgateway import cli as gateway_cli
    from abstractgateway.users import GatewayUserRegistry

    called = _fake_uvicorn(monkeypatch)
    monkeypatch.setattr(gateway_cli, "_resolve_default_console_level", lambda: logging.ERROR)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    seen: dict = {}
    monkeypatch.setattr(gateway_cli, "_record_serve_stop", lambda: seen.setdefault("stopped", True))

    gateway_cli.main(["serve", "--port", "18999", "--no-runner"])

    assert called["host"] == "127.0.0.1"
    assert os.environ.get("ABSTRACTGATEWAY_USER_AUTH") == "1"
    err = capsys.readouterr().err
    assert "user auth enabled automatically" in err
    assert "Gateway data dir:" in err
    line = next(l for l in err.splitlines() if l.startswith("First run: open "))
    url = line.split("First run: open ", 1)[1].strip()
    assert url.startswith("http://127.0.0.1:18999/console#claim=agclaim_")
    token = (tmp_path / "data" / "auth" / "bootstrap-admin-token").read_text(encoding="utf-8").strip()
    assert token and f"Gateway admin token: {token}" in err  # printed on a loopback bind
    assert GatewayUserRegistry().authenticate(token) is not None
    rec = json.loads((tmp_path / "data" / "run" / "gateway-serve.json").read_text(encoding="utf-8"))
    assert rec["port"] == 18999 and rec["url"] == "http://127.0.0.1:18999"
    assert rec["auth"]["mode"] == "users" and rec["auth"]["source"] == "loopback_default"
    assert seen.get("stopped") is True
    # The printed code is redeemable exactly once.
    code = url.split("#claim=", 1)[1]
    first_run.redeem_claim(code, data_dir=tmp_path / "data")
    with pytest.raises(first_run.ClaimError):
        first_run.redeem_claim(code, data_dir=tmp_path / "data")


def test_configured_serve_keeps_the_historical_all_interfaces_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from abstractgateway import cli as gateway_cli

    called = _fake_uvicorn(monkeypatch)
    monkeypatch.setattr(gateway_cli, "_resolve_default_console_level", lambda: logging.ERROR)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "a-strong-operator-token-123")
    gateway_cli.main(["serve", "--port", "18999", "--no-runner"])
    assert called["host"] == "0.0.0.0"
    assert os.environ.get("ABSTRACTGATEWAY_USER_AUTH") is None


def test_serve_data_dir_flag_is_exported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import cli as gateway_cli

    _fake_uvicorn(monkeypatch)
    monkeypatch.setattr(gateway_cli, "_resolve_default_console_level", lambda: logging.ERROR)
    gateway_cli.main(["serve", "--port", "18999", "--no-runner", "--data-dir", str(tmp_path / "flagged")])
    assert os.environ["ABSTRACTGATEWAY_DATA_DIR"] == str((tmp_path / "flagged").resolve())
    assert (tmp_path / "flagged" / "auth" / "users.json").exists()


# ---------------------------------------------------------------------------
# [NEW-3] claim codes
# ---------------------------------------------------------------------------


def test_claim_is_hashed_at_rest_private_single_use(tmp_path: Path) -> None:
    minted = first_run.mint_claim(data_dir=tmp_path)
    code = minted["code"]
    files = list((tmp_path / "auth" / "claims").glob("*.json"))
    assert len(files) == 1
    assert code not in files[0].read_text(encoding="utf-8")
    if os.name == "posix":
        assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert first_run.pending_claims(tmp_path)["pending"] == 1
    rec = first_run.redeem_claim(code, data_dir=tmp_path)
    assert rec["user_id"] == "admin" and rec["tenant_id"] == "default"
    assert first_run.pending_claims(tmp_path)["pending"] == 0
    with pytest.raises(first_run.ClaimError) as e:
        first_run.redeem_claim(code, data_dir=tmp_path)
    assert e.value.reason_code == "claim_unknown"


def test_claim_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import datetime

    minted = first_run.mint_claim(data_dir=tmp_path, ttl_s=60)
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=11)
    monkeypatch.setattr(first_run, "_now", lambda: later)
    with pytest.raises(first_run.ClaimError) as e:
        first_run.redeem_claim(minted["code"], data_dir=tmp_path)
    assert e.value.reason_code == "claim_expired"
    assert not list((tmp_path / "auth" / "claims").glob("*.json"))


@pytest.mark.parametrize("bad", ["", "nope", "agclaim_short", "agclaim_" + "x" * 30 + "/../x", "../../etc/passwd"])
def test_malformed_codes_are_refused_before_touching_disk(tmp_path: Path, bad: str) -> None:
    with pytest.raises(first_run.ClaimError) as e:
        first_run.redeem_claim(bad, data_dir=tmp_path)
    assert e.value.reason_code in {"claim_invalid", "claim_unknown"}


# ---------------------------------------------------------------------------
# /api/gateway/session/claim + /host/first-run
# ---------------------------------------------------------------------------


@pytest.fixture()
def user_auth_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    from abstractgateway.config_cli import ensure_bootstrap_admin_user
    from abstractgateway.service import reset_gateway_boot_state

    reset_gateway_boot_state()
    ensure_bootstrap_admin_user()
    return data


def _client(**kw) -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, **kw)


def test_claim_route_issues_an_admin_browser_session_once(user_auth_app: Path) -> None:
    code = first_run.mint_claim(data_dir=user_auth_app)["code"]
    client = _client()
    r = client.post("/api/gateway/session/claim", json={"code": code})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["claimed"] is True and body["principal"]["admin"] is True
    assert body["first_run"]["completed"] is False
    session = r.cookies.get(gateway_session_cookie_name())
    csrf = r.cookies.get(gateway_csrf_cookie_name())
    assert session and csrf
    cookies = {gateway_session_cookie_name(): session}
    assert client.get("/api/gateway/admin/users", cookies=cookies).status_code == 200
    # Replay: the second redemption is refused.
    again = _client().post("/api/gateway/session/claim", json={"code": code})
    assert again.status_code == 401
    assert again.json()["detail"]["reason_code"] == "claim_unknown"
    # First-run state: readable, and completing it is an admin write (CSRF).
    assert client.get("/api/gateway/host/first-run", cookies=cookies).json()["completed"] is False
    no_csrf = client.post("/api/gateway/host/first-run", cookies=cookies, json={"outcome": "finished"})
    assert no_csrf.status_code == 403
    done = client.post(
        "/api/gateway/host/first-run",
        cookies=cookies,
        headers={"X-AbstractGateway-CSRF": csrf},
        json={"outcome": "finished"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["completed"] is True
    assert first_run.first_run_state(user_auth_app)["completed"] is True


@pytest.mark.parametrize("minted_by", ["tray", "serve", "cli"])
def test_claim_response_names_who_minted_the_link(user_auth_app: Path, minted_by: str) -> None:
    # The console tells a tray sign-in from a first run by this one field.
    code = first_run.mint_claim(data_dir=user_auth_app, created_by=minted_by)["code"]
    r = _client().post("/api/gateway/session/claim", json={"code": code})
    assert r.status_code == 200, r.text
    assert r.json()["claim"] == {"created_by": minted_by}


def test_claim_response_created_by_is_none_for_a_record_without_it(user_auth_app: Path) -> None:
    # A record minted before the field existed: say "unknown", never guess.
    import json as _json

    code = first_run.mint_claim(data_dir=user_auth_app)["code"]
    (path,) = list(first_run.claims_dir(user_auth_app).glob("*.json"))
    rec = _json.loads(path.read_text(encoding="utf-8"))
    rec.pop("created_by")
    path.write_text(_json.dumps(rec), encoding="utf-8")
    r = _client().post("/api/gateway/session/claim", json={"code": code})
    assert r.status_code == 200, r.text
    assert r.json()["claim"] == {"created_by": None}


def test_claim_route_refuses_non_loopback_and_proxied_peers(user_auth_app: Path) -> None:
    code = first_run.mint_claim(data_dir=user_auth_app)["code"]
    remote = _client(client=("203.0.113.9", 50000)).post("/api/gateway/session/claim", json={"code": code})
    assert remote.status_code == 403
    assert remote.json()["detail"]["reason_code"] == "claim_loopback_only"
    proxied = _client().post("/api/gateway/session/claim", json={"code": code}, headers={"X-Forwarded-For": "127.0.0.1"})
    assert proxied.status_code == 403
    # Neither refusal consumed the code.
    assert _client().post("/api/gateway/session/claim", json={"code": code}).status_code == 200


def test_claim_route_needs_user_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "operator-token-for-claim-tests")
    from abstractgateway.service import reset_gateway_boot_state

    reset_gateway_boot_state()
    code = first_run.mint_claim(data_dir=tmp_path / "data")["code"]
    r = _client().post("/api/gateway/session/claim", json={"code": code})
    assert r.status_code == 409
    assert r.json()["detail"]["reason_code"] == "claim_requires_user_auth"


def test_non_admin_cannot_complete_first_run(user_auth_app: Path) -> None:
    from abstractgateway.users import GatewayUserRegistry

    _rec, token = GatewayUserRegistry().create_user(user_id="bob", roles=["user"])
    r = _client().post("/api/gateway/host/first-run", headers={"Authorization": f"Bearer {token}"}, json={})
    assert r.status_code == 403
    assert _client().get("/api/gateway/host/first-run", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_host_state_install_block(user_auth_app: Path) -> None:
    from abstractgateway.routes.gateway import _gateway_install_block

    block = _gateway_install_block()
    assert block["data_dir"] == str(user_auth_app.resolve())
    assert block["data_dir_source"] == "env"
    assert block["auth_mode"] == "users"
    assert block["first_run"]["completed"] is False
    assert "installed" in block["service"] and "claims" in block


# ---------------------------------------------------------------------------
# CLI: claim-url / claim, and status --json doctor fields
# ---------------------------------------------------------------------------


def test_claim_url_uses_the_running_gateways_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from abstractgateway.config_cli import main as config_main

    data = tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    first_run.write_serve_record(
        data_dir=data, host="127.0.0.1", port=18123, auth={"mode": "users", "user_auth_enabled": True}, data_dir_source="env"
    )
    with pytest.raises(SystemExit) as e:
        config_main(["claim-url", "--json"])
    assert e.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["url"].startswith("http://127.0.0.1:18123/console#claim=agclaim_")
    assert out["gateway_running"] is True and out["base_url_source"] == "serve_record"
    code = out["url"].split("#claim=", 1)[1]
    assert first_run.redeem_claim(code, data_dir=data)["user_id"] == "admin"


def test_claim_refuses_a_token_mode_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from abstractgateway import cli as gateway_cli

    data = tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    first_run.write_serve_record(
        data_dir=data, host="127.0.0.1", port=18123, auth={"mode": "token", "user_auth_enabled": False}, data_dir_source="env"
    )
    with pytest.raises(SystemExit) as e:
        gateway_cli.main(["claim"])
    assert e.value.code == 2
    assert "does not use user auth" in capsys.readouterr().err
    assert first_run.pending_claims(data)["pending"] == 0


def test_claim_open_calls_the_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import webbrowser

    from abstractgateway import cli as gateway_cli

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    opened: list = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    with pytest.raises(SystemExit) as e:
        gateway_cli.main(["claim", "--port", "18555", "--open"])
    assert e.value.code == 0
    printed = capsys.readouterr().out.strip()
    assert opened == [printed] and printed.startswith("http://127.0.0.1:18555/console#claim=")


def test_status_json_carries_the_doctor_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from abstractgateway.config_cli import main as config_main

    data = tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    first_run.mint_claim(data_dir=data)
    config_main(["status", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "gateway_config_status_v1"
    assert payload["data_dir_source"] == "env"
    assert payload["auth_mode"] == "loopback_auto"
    assert payload["claim_pending"] is True
    assert set(payload["service"]) >= {"installed", "unit_path", "mechanism", "platform"}
    assert payload["first_run"]["completed"] is False
    assert payload["serve"] is None
    assert payload["gateway"]["data_dir"] == str(data.resolve())
