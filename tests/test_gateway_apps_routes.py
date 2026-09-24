"""`/api/gateway/apps/*` routes, the `/apps/handover/{code}` sign-in handover
and the `abstractgateway apps` CLI (no network, no Node.js)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from abstractgateway import apps_manager as am

_TOKEN = "apps-routes-test-token-0123456789"


class _Offline:
    def __call__(self, req, timeout=None):
        import urllib.error

        raise urllib.error.URLError("offline")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    allowed = {"value": True}
    m = am.AppsManager(tmp_path / "runtime", urlopen=_Offline(), install_allowed=lambda: allowed["value"])
    import abstractgateway.routes.apps as routes

    monkeypatch.setattr(routes, "get_apps_manager", lambda: m)
    from abstractgateway.app import app

    client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
    return client, m, allowed


def test_overview_requires_auth_and_lists_five_apps(env) -> None:
    client, _m, _ = env
    from abstractgateway.app import app

    anon = TestClient(app)
    assert anon.get("/api/gateway/apps").status_code == 401
    r = client.get("/api/gateway/apps?latest=false")
    assert r.status_code == 200, r.text
    data = r.json()
    # The stack order (scripts/start-local.sh port map: 3001..3005).
    assert [a["id"] for a in data["apps"]] == ["observer", "continuum", "code", "entity", "flow"]
    node = data["runtime"]["node"]
    assert set(node) >= {"available", "version", "source", "install_available", "message"}
    row = data["apps"][1]
    assert set(row) >= {"id", "name", "description", "installed", "version", "latest_version", "running", "url", "port", "install_available", "actions"}
    assert row["installed"] is False and row["actions"] == ["install"]


def test_overview_reports_registry_unreachable(env) -> None:
    client, _m, _ = env
    data = client.get("/api/gateway/apps").json()
    assert data["registry"]["reachable"] is False
    assert all(a["install_available"] is False for a in data["apps"])
    assert "not reachable" in data["apps"][0]["install_blocked_reason"]


def test_install_refused_when_host_installs_are_off(env) -> None:
    client, _m, allowed = env
    allowed["value"] = False
    r = client.post("/api/gateway/apps/code/install", json={})
    assert r.status_code == 403
    assert r.json()["reason"] == "installs_not_allowed"
    r = client.post("/api/gateway/apps/runtime/install", json={})
    assert r.status_code in (200, 403)  # 200 only when a Node.js is already available


def test_install_offline_is_a_failed_job_with_details(env) -> None:
    client, _m, _ = env
    r = client.post("/api/gateway/apps/code/install", json={"launch": True})
    assert r.status_code == 200, r.text
    job_id = r.json()["job"]["id"]
    import time

    for _ in range(100):
        job = client.get(f"/api/gateway/apps/jobs/{job_id}").json()["job"]
        if job["state"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert job["state"] == "failed"
    assert job["error"]["reason"] == "network_unavailable"
    assert job["details"] and "ERROR" in job["details"]
    assert client.get("/api/gateway/apps/jobs/nope").status_code == 404


def test_unknown_app_is_404(env) -> None:
    client, _m, _ = env
    r = client.post("/api/gateway/apps/nope/launch", json={})
    assert r.status_code == 404 and r.json()["reason"] == "unknown_app"
    r = client.post("/api/gateway/apps/flow/launch", json={})
    assert r.status_code == 409 and r.json()["reason"] == "not_installed"


def _running(m: am.AppsManager, monkeypatch: pytest.MonkeyPatch, port: int = 18999) -> None:
    real = m.app_row

    def row(spec, **kw):
        out = real(spec, **kw)
        out.update({"running": True, "port": port, "url": f"http://127.0.0.1:{port}/", "status": "running"})
        return out

    monkeypatch.setattr(m, "app_row", row)
    m._update_app_state("observer", gateway_url="http://127.0.0.1:18823", port=port)


def test_open_and_handover_sets_the_app_session_cookies(env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, m, _ = env
    assert client.post("/api/gateway/apps/observer/open", json={}, headers={"host": "127.0.0.1:18823"}).status_code == 409  # not running
    _running(m, monkeypatch)
    # a browser on another machine cannot reach a loopback-only app
    r = client.post("/api/gateway/apps/observer/open", json={}, headers={"host": "192.168.1.20:18823"})
    assert r.status_code == 409 and r.json()["reason"] == "app_loopback_only"
    r = client.post("/api/gateway/apps/observer/open", json={}, headers={"host": "127.0.0.1:18823"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["open_url"].startswith("/apps/handover/") and body["app_url"] == "http://127.0.0.1:18999/"
    assert _TOKEN not in r.text

    browser = TestClient(client.app)  # a fresh browser: no bearer header, no cookies
    h = browser.get(body["open_url"], headers={"host": "127.0.0.1:18823"}, follow_redirects=False)
    assert h.status_code == 303
    assert h.headers["location"] == "http://127.0.0.1:18999/"
    assert _TOKEN not in h.headers["location"]
    cookies = {c.split("=", 1)[0]: c for c in h.headers.get_list("set-cookie")}
    assert set(cookies) == {"abstractobserver_gateway_url", "abstractobserver_gateway_session", "abstractobserver_gateway_csrf"}
    assert "HttpOnly" in cookies["abstractobserver_gateway_session"]
    assert "HttpOnly" not in cookies["abstractobserver_gateway_csrf"]
    assert unquote(cookies["abstractobserver_gateway_url"].split("=", 1)[1].split(";")[0]) == "http://127.0.0.1:18823"
    session_value = cookies["abstractobserver_gateway_session"].split("=", 1)[1].split(";")[0]
    # the minted session is a real gateway session for the caller
    me = browser.get("/api/gateway/me", headers={"x-abstractgateway-session": session_value})
    assert me.status_code == 200, me.text
    # one use only
    again = browser.get(body["open_url"], headers={"host": "127.0.0.1:18823"}, follow_redirects=False)
    assert again.status_code == 410


def test_handover_refuses_another_host(env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, m, _ = env
    _running(m, monkeypatch)
    body = client.post("/api/gateway/apps/observer/open", json={}, headers={"host": "127.0.0.1:18823"}).json()
    r = TestClient(client.app).get(body["open_url"], headers={"host": "localhost:18823"}, follow_redirects=False)
    assert r.status_code == 400
    assert not r.headers.get_list("set-cookie")


def test_handover_unknown_code(env) -> None:
    client, _m, _ = env
    r = TestClient(client.app).get("/apps/handover/not-a-code", follow_redirects=False)
    assert r.status_code == 410 and "expired" in r.text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _FakeTransport:
    url = "http://127.0.0.1:18823"

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def call(self, method, path, body=None, *, timeout=None):
        from abstractgateway.models_engines_cli import _Answer

        self.calls.append((method, path, body))
        key = (method, path.split("?")[0])
        value = self.answers[key]
        if callable(value):
            value = value()
        return _Answer(value[0], value[1])


def _run_cli(argv, transport, monkeypatch, capsys):
    import abstractgateway.apps_cli as cli

    monkeypatch.setattr(cli, "_transport", lambda args: transport)
    monkeypatch.setattr(cli, "_POLL_S", 0.0)
    from abstractgateway.cli import main

    with pytest.raises(SystemExit) as ei:
        main(argv)
    out = capsys.readouterr()
    return ei.value.code, out.out, out.err


def test_cli_install_follows_the_job(monkeypatch, capsys) -> None:
    states = iter(
        [
            {"id": "appjob_1", "state": "running", "percent": 40.0, "message": "Downloading Code 0.4.2: 0.1 MB of 0.2 MB (50%)", "bytes_done": 1, "bytes_total": 2},
            {"id": "appjob_1", "state": "succeeded", "percent": 100.0, "message": "done", "result": {"message": "Code 0.4.2 is running at http://127.0.0.1:18830/"}},
        ]
    )
    t = _FakeTransport(
        {
            ("POST", "/apps/code/install"): (200, {"ok": True, "job": {"id": "appjob_1", "state": "queued", "percent": 0, "message": "Waiting"}}),
            ("GET", "/apps/jobs/appjob_1"): lambda: (200, {"ok": True, "job": next(states)}),
        }
    )
    code, out, err = _run_cli(["apps", "install", "code", "--launch"], t, monkeypatch, capsys)
    assert code == 0
    assert t.calls[0] == ("POST", "/apps/code/install", {"launch": True})
    assert "Downloading Code 0.4.2" in err and "[1/2 B]" in err
    assert "done: Code 0.4.2 is running at http://127.0.0.1:18830/" in out


def test_cli_list_and_refusal(monkeypatch, capsys) -> None:
    overview = {
        "runtime": {"node": {"message": "Node.js 24.19.0 (installed by the gateway).", "path": "/x/node"}},
        "registry": {"reachable": True},
        "apps": [{"id": "code", "version": "0.4.2", "latest_version": "0.4.3", "update_available": True, "status": "running", "url": "http://127.0.0.1:18830/", "running": True}],
    }
    t = _FakeTransport({("GET", "/apps"): (200, overview), ("POST", "/apps/flow/launch"): (409, {"ok": False, "message": "Flow Editor is not installed yet.", "hint": "Install it first"})})
    code, out, _ = _run_cli(["apps", "list"], t, monkeypatch, capsys)
    assert code == 0 and "0.4.3 *" in out and "http://127.0.0.1:18830/" in out
    code, _, err = _run_cli(["apps", "launch", "flow"], t, monkeypatch, capsys)
    assert code == 2 and "not installed" in err and "Install it first" in err
