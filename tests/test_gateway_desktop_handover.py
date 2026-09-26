"""The desktop Assistant's sign-in hand-over (CONTRACTS A1, amendment A-3).

A newly started Assistant gets `--gateway-url <url> --gateway-handover-file
<file>`; the file (0600, <data dir>/handover/) holds {schema, code, base_url,
expires_at}; the code is never on argv nor in the environment. The Assistant
trades it at the PUBLIC route POST /api/gateway/apps/desktop-handover, which
answers only a direct loopback caller, once, for two minutes.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abstractgateway import apps_desktop as desk
from abstractgateway import apps_manager as am
from abstractgateway.security.principal import GatewayPrincipal
from test_gateway_apps_install_and_assistant import BUNDLE, amgr, home  # noqa: F401 - fixtures

SCHEMA = "abstractgateway.desktop_handover.v1"


def _principal() -> GatewayPrincipal:
    """The operator as the tray's per-process loopback token presents it."""
    from abstractgateway.security.principal import local_admin_principal
    from abstractgateway.security.sessions import token_fingerprint

    return local_admin_principal(token_fingerprint=token_fingerprint("agtray_ephemeral_token_for_tests"))


def test_argv_carries_url_and_file_never_the_code() -> None:
    assert desk.assistant_argv_with_handover(["/v/bin/abstractassistant"], gateway_url="http://127.0.0.1:8080", handover_file="/d/h/x.json") == [
        "/v/bin/abstractassistant", "--gateway-url", "http://127.0.0.1:8080", "--gateway-handover-file", "/d/h/x.json"]
    assert desk.assistant_argv_with_handover(["open", "-a", BUNDLE], gateway_url="http://h:1", handover_file="/f") == [
        "open", "-a", BUNDLE, "--args", "--gateway-url", "http://h:1", "--gateway-handover-file", "/f"]
    py = ["/venv/bin/python", "-c", "import sys; from abstractassistant.cli import main as _m; sys.exit(_m())"]
    assert desk.assistant_argv_with_handover(py, gateway_url="u", handover_file="f")[-4:] == ["--gateway-url", "u", "--gateway-handover-file", "f"]


def test_launch_writes_a_private_file_and_passes_it(amgr) -> None:  # noqa: F811
    m, state, spawned = amgr
    m.gateway_url = "http://127.0.0.1:18852"
    state["files"] = {state["script"]}
    out = m.launch_desktop("assistant", same_machine=True, principal=_principal())
    argv = spawned[-1]["argv"]
    assert argv[:2] == [state["script"], "--gateway-url"] and argv[2] == "http://127.0.0.1:18852"
    assert argv[3] == "--gateway-handover-file"
    f = Path(argv[4])
    assert f.parent == m.data_dir / "handover"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    body = json.loads(f.read_text())
    assert set(body) == {"schema", "code", "base_url", "expires_at"} and body["schema"] == SCHEMA
    assert body["base_url"] == "http://127.0.0.1:18852" and body["expires_at"].endswith("Z")
    assert body["code"] not in " ".join(argv) and not any(body["code"] in str(v) for v in spawned[-1]["env"].values())
    assert out["signed_in_by_gateway"] is True and "signed in" in out["message"]
    # Redeem: once, and the file goes.
    who, url = m.redeem_desktop_handover(body["code"])
    assert url == "http://127.0.0.1:18852" and getattr(who, "user_id", None) == "admin"
    assert m.redeem_desktop_handover(body["code"]) is None and not f.exists()

    # Bundle: flags after --args.
    state["files"] = {BUNDLE}
    m._desktop_cache.clear()
    m.launch_desktop("assistant", same_machine=True, principal=_principal())
    assert spawned[-1]["argv"][:4] == ["open", "-a", BUNDLE, "--args"]

    # Already running: no code, the message says how to sign it in.
    state["files"] = {state["script"]}
    state["procs"] = [(99, [state["script"]])]
    m._desktop_cache.clear()
    before = set((m.data_dir / "handover").glob("*.json"))
    out = m.launch_desktop("assistant", same_machine=True, principal=_principal())
    assert out["already_running"] and out["signed_in_by_gateway"] is False
    assert "quit the Assistant and open it again from here" in out["message"]
    assert set((m.data_dir / "handover").glob("*.json")) == before


def test_a_failed_start_voids_the_code(amgr) -> None:  # noqa: F811
    m, state, _ = amgr
    state["files"] = {state["script"]}
    state["exit"] = 3
    with pytest.raises(am.LaunchFailed):
        m.launch_desktop("assistant", same_machine=True, principal=_principal(), gateway_url="http://127.0.0.1:1")
    assert list((m.data_dir / "handover").glob("*.json")) == [] and m._desktop_handover == {}


def test_expired_code_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m = am.AppsManager(tmp_path / "data")
    code, f = m.mint_desktop_handover(_principal(), base_url="http://127.0.0.1:1")
    monkeypatch.setattr(am, "_now", lambda: 10**12)
    assert m.redeem_desktop_handover(code) is None and not f.exists()


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: str = "127.0.0.1"):
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    m = am.AppsManager(tmp_path / "runtime")
    import abstractgateway.routes.apps as routes

    monkeypatch.setattr(routes, "get_apps_manager", lambda: m)
    from abstractgateway.app import app

    return TestClient(app, client=(peer, 50123)), m


def test_route_public_loopback_single_use(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, m = _client(tmp_path, monkeypatch)
    url = "/api/gateway/apps/desktop-handover"
    with client:
        code, f = m.mint_desktop_handover(_principal(), base_url="http://127.0.0.1:8080")
        # No Authorization header at all: the route is public.
        r = client.post(url, json={"code": code})
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"base_url", "session_id", "csrf_token", "user_id", "expires_at"}
        assert body["base_url"] == "http://127.0.0.1:8080" and body["user_id"] == "admin" and body["session_id"]
        # A real gateway session: an authenticated read with it succeeds.
        who = client.get("/api/gateway/runs?limit=1", headers={"x-abstractgateway-session": body["session_id"]})
        assert who.status_code == 200, who.text
        from abstractgateway.security.sessions import GatewaySessionStore

        assert GatewaySessionStore().verify_csrf_token(body["session_id"], body["csrf_token"]) is True
        assert client.post(url, json={"code": code}).status_code == 410, "single use"
        assert client.post(url, json={"code": "nope"}).status_code == 410

        for extra in ({"X-Forwarded-For": "127.0.0.1"}, {"Forwarded": "for=127.0.0.1"}, {"X-Real-IP": "127.0.0.1"},
                      {"x-abstractgateway-session": "s"}):
            code2, _ = m.mint_desktop_handover(_principal(), base_url="u")
            r = client.post(url, json={"code": code2}, headers=extra)
            assert r.status_code == 403 and r.json()["reason"] == "loopback_only", extra
            assert m.redeem_desktop_handover(code2) is not None, "a refused caller does not burn the code"


def test_route_refuses_a_non_loopback_peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, m = _client(tmp_path, monkeypatch, peer="192.168.1.50")
    with client:
        code, _ = m.mint_desktop_handover(_principal(), base_url="u")
        r = client.post("/api/gateway/apps/desktop-handover", json={"code": code})
        assert r.status_code == 403


def test_tray_launch_goes_through_the_gateway() -> None:
    """The tray's Launch Assistant calls POST /apps/assistant/launch (one argv,
    one hand-over), never spawning its own copy."""
    import inspect

    from abstractgateway.tray.app import TrayApp  # type: ignore[attr-defined]

    src = inspect.getsource(TrayApp.assistant_launch)
    assert 'self.client.app_launch("assistant")' in src and "spawn_detached" not in src
