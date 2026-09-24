"""Mission HH (2026-09-24): apps started OUTSIDE the gateway, the one port
table, the same-machine install rule and the tray's loopback base URL.

The operator's report: the dev stack (`scripts/start-local.sh --build`) ran
all five apps on 127.0.0.1:3001-3005, and the tray said "can't install here"
for each of them. No test here touches the operator's live apps: every probe
names its own scratch ports (the conftest also refuses 3000-3007).
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from abstractgateway import apps_manager as am
from abstractgateway.security import same_machine as sm

_TOKEN = "apps-external-test-token-0123456789"

# The real `<title>`s the five app servers send for `/` (read from the live
# dev apps on 127.0.0.1:3001-3005 on 2026-09-24, untracked/missionHH/body_*.html).
PAGES: Dict[str, str] = {
    "observer": "<!DOCTYPE html><html><head><title>AbstractObserver</title>"
    '<script>window.__ABSTRACT_UI_CONFIG__=Object.assign(window.__ABSTRACT_UI_CONFIG__||{}, {"gateway_url":"http://127.0.0.1:8080","entity_app_url":"http://127.0.0.1:3004"});</script></head></html>',
    "continuum": "<html><head><title>AbstractContinuum — continuous development console</title></head></html>",
    "code": '<html><head><meta name="apple-mobile-web-app-title" content="AbstractCode" />\n    <title>AbstractCode</title></head></html>',
    "entity": "<html><head><title>AbstractEntity — memory &amp; visits</title></head></html>",
    "flow": "<html><head><title>AbstractFlow Visual Editor</title></head></html>",
}


# ---------------------------------------------------------------------------
# Identity + the port table
# ---------------------------------------------------------------------------


def test_each_app_page_is_recognised_by_its_title_and_nothing_else_is() -> None:
    for app_id, page in PAGES.items():
        assert am.identify_app_page(page) == app_id, app_id
    for other in ("<title>Vite App</title>", "<title>AbstractCodeX</title>", "<title>My AbstractObserver fork</title>", "no title", ""):
        assert am.identify_app_page(other) is None, other


def test_one_port_table_is_the_stack_map_for_launch_probe_and_tray() -> None:
    expected = {"observer": 3001, "continuum": 3002, "code": 3003, "entity": 3004, "flow": 3005}
    assert dict(am.STACK_PORTS) == expected
    assert {a.id: a.default_port for a in am.APPS} == expected
    assert [a.id for a in am.APPS] == ["observer", "continuum", "code", "entity", "flow"]  # stack order
    from abstractgateway.tray import apps as tray_apps

    assert {w[0]: w[4] for w in tray_apps.WEB_APPS} == expected
    assert [w[0] for w in tray_apps.WEB_APPS] == [a.id for a in am.APPS]


def test_the_port_table_matches_the_start_local_header_when_the_framework_checkout_is_here() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "start-local.sh"
    if not script.is_file():
        pytest.skip("not inside the AbstractFramework checkout (scripts/start-local.sh absent)")
    import re

    header = script.read_text(encoding="utf-8").split("set -euo pipefail", 1)[0]
    found = {m.group(1): int(m.group(2)) for m in re.finditer(r"^#\s+(observer|continuum|code/web|entity|flow)\s+(\d+)", header, re.M)}
    found["code"] = found.pop("code/web")
    assert found == dict(am.STACK_PORTS)


# ---------------------------------------------------------------------------
# Detection against real HTTP servers on scratch ports
# ---------------------------------------------------------------------------


class _Page(http.server.BaseHTTPRequestHandler):
    page = ""

    def do_GET(self) -> None:  # noqa: N802
        body = self.page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a) -> None:  # noqa: D401
        return


@pytest.fixture()
def app_servers():
    started: List[http.server.ThreadingHTTPServer] = []

    def serve(page: str) -> int:
        handler = type("H", (_Page,), {"page": page})
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return int(srv.server_address[1])

    yield serve
    for srv in started:
        srv.shutdown()
        srv.server_close()


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_detect_external_apps_finds_each_app_by_its_page(app_servers) -> None:
    ports = {app_id: app_servers(page) for app_id, page in PAGES.items()}
    other = app_servers("<title>Vite App</title>")
    dead = _free_port()
    order = [dead, other] + list(ports.values())
    got = am.detect_external_apps(ports=order, pids=lambda ps: {p: 4242 for p in ps}, version_of=lambda pid, spec: {"observer": "0.1.12"}.get(spec.id))
    assert set(got) == set(PAGES)
    for app_id, port in ports.items():
        assert got[app_id].port == port and got[app_id].url == f"http://127.0.0.1:{port}/" and got[app_id].pid == 4242
    assert got["observer"].version == "0.1.12" and got["code"].version is None
    assert got["observer"].gateway_url == "http://127.0.0.1:8080" and got["flow"].gateway_url is None


def test_detection_prefers_the_first_port_and_never_probes_excluded_ports(app_servers) -> None:
    first = app_servers(PAGES["observer"])
    second = app_servers(PAGES["observer"])
    asked: List[int] = []

    def probe(port, **kw):
        asked.append(port)
        return am.probe_app_port(port, **kw)

    got = am.detect_external_apps(ports=[first, second], probe=probe, pids=lambda ps: {}, version_of=lambda pid, spec: None)
    assert got["observer"].port == first
    asked.clear()
    got = am.detect_external_apps(ports=[first, second], exclude_ports=[first], probe=probe, pids=lambda ps: {}, version_of=lambda pid, spec: None)
    assert asked == [second] and got["observer"].port == second


def test_version_comes_from_the_package_json_above_the_script(tmp_path: Path) -> None:
    pkg = tmp_path / "node_modules" / "@abstractframework" / "observer"
    (pkg / "bin").mkdir(parents=True)
    (pkg / "bin" / "cli.js").write_text("// cli\n", encoding="utf-8")
    (pkg / "package.json").write_text(json.dumps({"name": "@abstractframework/observer", "version": "0.1.12"}), encoding="utf-8")
    spec = am.APP_BY_ID["observer"]
    assert am.app_version_from_process(1, spec, argv=["node", str(pkg / "bin" / "cli.js")]) == "0.1.12"
    # Another package's script is not this app's version.
    assert am.app_version_from_process(1, am.APP_BY_ID["flow"], argv=["node", str(pkg / "bin" / "cli.js")]) is None
    assert am.app_version_from_process(1, spec, argv=["node", "relative/cli.js"]) is None


# ---------------------------------------------------------------------------
# The manager's rows
# ---------------------------------------------------------------------------


def _manager(tmp_path: Path, found: Dict[str, am.ExternalApp], *, allowed: bool = False) -> am.AppsManager:
    m = am.AppsManager(tmp_path / "data", urlopen=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")), install_allowed=lambda: allowed)
    m.external_probe = lambda **kw: dict(found)
    return m


def test_an_external_app_is_installed_running_and_open_only(tmp_path: Path) -> None:
    m = _manager(tmp_path, {"observer": am.ExternalApp("observer", 3001, "http://127.0.0.1:3001/", version="0.1.12", pid=77)})
    row = m.app_row(am.APP_BY_ID["observer"])
    assert row["installed"] is True and row["running"] is True and row["status"] == "running"
    assert row["managed"] is False and row["source"] == "external"
    assert row["url"] == "http://127.0.0.1:3001/" and row["port"] == 3001 and row["version"] == "0.1.12"
    assert row["actions"] == ["open"]
    assert row["external"]["detail"] == "Started outside the gateway on port 3001"
    assert row["install_available"] is False and row["install_blocked_reason"] is None
    web = row["interfaces"][0]
    assert web["launch_available"] is True and web["running"] is True
    # Nothing found for the others: the usual not-installed rows.
    other = m.app_row(am.APP_BY_ID["flow"])
    assert other["installed"] is False and other["source"] is None and other["managed"] is False


def test_the_gateway_neither_stops_nor_starts_a_second_copy_of_an_external_app(tmp_path: Path) -> None:
    m = _manager(tmp_path, {"entity": am.ExternalApp("entity", 3004, "http://127.0.0.1:3004/")})
    with pytest.raises(am.StartedOutsideGateway) as exc:
        m.stop("entity")
    assert exc.value.status_code == 409 and "port 3004" in exc.value.message
    row = m.launch("entity")  # not installed by the gateway, yet running: nothing to start
    assert row["running"] is True and row["source"] == "external"


def test_the_probe_is_cached_and_a_failing_probe_is_nothing_found(tmp_path: Path) -> None:
    calls: List[dict] = []
    m = _manager(tmp_path, {})

    def probe(**kw):
        calls.append(kw)
        return {"code": am.ExternalApp("code", 3003, "http://127.0.0.1:3003/")}

    m.external_probe = probe
    m.overview(check_latest=False)
    m.overview(check_latest=False)
    assert len(calls) == 1 and calls[0]["exclude_ports"] == []
    m.external_probe = lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    assert m.external_apps(refresh=True) == {}


# ---------------------------------------------------------------------------
# Routes: open / handover / stop for an external app
# ---------------------------------------------------------------------------


@pytest.fixture()
def routes_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    m = _manager(tmp_path, {"observer": am.ExternalApp("observer", 18957, "http://127.0.0.1:18957/", version="0.1.12")})
    m.gateway_url = "http://127.0.0.1:18951"
    import abstractgateway.routes.apps as routes

    monkeypatch.setattr(routes, "get_apps_manager", lambda: m)
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}), m


def test_open_hands_the_sign_in_to_an_app_the_gateway_did_not_start(routes_env) -> None:
    client, m = routes_env
    listing = client.get("/api/gateway/apps?latest=false").json()
    row = next(a for a in listing["apps"] if a["id"] == "observer")
    assert row["source"] == "external" and row["actions"] == ["open"]
    r = client.post("/api/gateway/apps/observer/open", json={}, headers={"host": "127.0.0.1:18951"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["app_url"] == "http://127.0.0.1:18957/"
    h = TestClient(client.app).get(body["open_url"], headers={"host": "127.0.0.1:18951"}, follow_redirects=False)
    assert h.status_code == 303 and h.headers["location"] == "http://127.0.0.1:18957/"
    cookies = {c.split("=", 1)[0]: c for c in h.headers.get_list("set-cookie")}
    assert set(cookies) == {"abstractobserver_gateway_url", "abstractobserver_gateway_session", "abstractobserver_gateway_csrf"}
    # The app talks to THIS gateway (its own URL), never a stale launch URL.
    m._update_app_state("observer", gateway_url="http://127.0.0.1:9999")
    body = client.post("/api/gateway/apps/observer/open", json={}, headers={"host": "127.0.0.1:18951"}).json()
    h = TestClient(client.app).get(body["open_url"], headers={"host": "127.0.0.1:18951"}, follow_redirects=False)
    url_cookie = next(c for c in h.headers.get_list("set-cookie") if c.startswith("abstractobserver_gateway_url="))
    assert unquote(url_cookie.split("=", 1)[1].split(";")[0]) == "http://127.0.0.1:18951"


def test_open_from_another_address_needs_the_app_to_answer_there(routes_env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _m = routes_env
    import abstractgateway.routes.apps as routes

    monkeypatch.setattr(routes, "_port_answers", lambda host, port, timeout=0.5: False)
    r = client.post("/api/gateway/apps/observer/open", json={}, headers={"host": "192.168.1.175:18951"})
    assert r.status_code == 409 and r.json()["reason"] == "app_loopback_only"
    assert "started outside the gateway" in r.json()["message"]
    monkeypatch.setattr(routes, "_port_answers", lambda host, port, timeout=0.5: True)
    r = client.post("/api/gateway/apps/observer/open", json={}, headers={"host": "192.168.1.175:18951"})
    assert r.status_code == 200 and r.json()["app_url"] == "http://192.168.1.175:18957/"


def test_port_answers_never_connects_outside_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.apps as routes

    monkeypatch.setattr(sm, "own_addresses", lambda **kw: frozenset({"127.0.0.1"}))
    # 203.0.113.9 (TEST-NET-3) is not this machine: refused before any connect
    # (the conftest network guard would fail the test on a connect attempt).
    assert routes._port_answers("203.0.113.9", 18957) is False


def test_stop_is_refused_for_an_external_app(routes_env) -> None:
    client, _m = routes_env
    r = client.post("/api/gateway/apps/observer/stop", json={})
    assert r.status_code == 409 and r.json()["reason"] == "started_outside_gateway"


# ---------------------------------------------------------------------------
# Same machine: loopback OR this host's own address, and no proxy headers
# ---------------------------------------------------------------------------

OWN = ["127.0.0.1", "::1", "192.168.1.175", "fe80::1"]


def test_peer_rules() -> None:
    assert sm.peer_is_this_machine("127.0.0.1", addresses=OWN)
    assert sm.peer_is_this_machine("::ffff:127.0.0.1", addresses=OWN)
    assert sm.peer_is_this_machine("192.168.1.175", addresses=OWN)
    assert sm.peer_is_this_machine("::ffff:192.168.1.175", addresses=OWN)
    assert not sm.peer_is_this_machine("192.168.1.50", addresses=OWN)
    assert not sm.peer_is_this_machine("testclient", addresses=OWN)
    assert not sm.peer_is_this_machine("0.0.0.0", addresses=OWN + ["0.0.0.0"])
    assert not sm.peer_is_this_machine("", addresses=OWN)


class _Req:
    def __init__(self, peer: str, headers: Optional[Dict[str, str]] = None) -> None:
        self.client = type("C", (), {"host": peer})()
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


def test_request_rule_refuses_proxied_requests() -> None:
    assert sm.request_is_from_this_machine(_Req("192.168.1.175"), addresses=OWN)
    for h in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP"):
        assert not sm.request_is_from_this_machine(_Req("127.0.0.1", {h: "203.0.113.9"}), addresses=OWN), h
    assert not sm.request_is_from_this_machine(_Req("192.168.1.50"), addresses=OWN)


def _policy_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: str, bind: str = "0.0.0.0"):
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_BIND_HOST", bind)
    monkeypatch.setattr(sm, "own_addresses", lambda **kw: frozenset(OWN))
    data = tmp_path / "runtime"
    m = am.AppsManager(data, urlopen=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")), install_allowed=am._default_install_allowed(data))
    m.external_probe = lambda **kw: {}
    started: List[str] = []
    monkeypatch.setattr(m.jobs, "start", lambda **kw: (started.append(kw["target"]) or (type("J", (), {"to_dict": lambda self: {"id": "j"}})(), True)))
    import abstractgateway.routes.apps as routes

    monkeypatch.setattr(routes, "get_apps_manager", lambda: m)
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}, client=(peer, 50123)), started


def test_a_person_at_a_lan_bound_gateway_may_install_apps_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, started = _policy_client(tmp_path, monkeypatch, "192.168.1.175")
    listing = client.get("/api/gateway/apps?latest=false", headers={"host": "192.168.1.175:8080"}).json()
    assert listing["install_allowed"] is True
    assert all(a["install_available"] is True and a["install_blocked_reason"] is None for a in listing["apps"])
    r = client.post("/api/gateway/apps/observer/install", json={}, headers={"host": "192.168.1.175:8080"})
    assert r.status_code == 200, r.text
    assert started == ["app:observer"]


def test_a_remote_caller_still_needs_the_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, started = _policy_client(tmp_path, monkeypatch, "192.168.1.50")
    listing = client.get("/api/gateway/apps?latest=false").json()
    assert listing["install_allowed"] is False
    assert all(a["install_available"] is False and a["install_blocked_reason"] == am.INSTALLS_OFF_MESSAGE for a in listing["apps"])
    r = client.post("/api/gateway/apps/observer/install", json={})
    assert r.status_code == 403 and r.json()["reason"] == "installs_not_allowed"
    assert started == []


def test_a_proxied_request_from_this_machine_is_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, started = _policy_client(tmp_path, monkeypatch, "127.0.0.1")
    r = client.post("/api/gateway/apps/observer/install", json={}, headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 403 and started == []


def test_a_stored_off_is_off_for_the_person_at_the_machine_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, started = _policy_client(tmp_path, monkeypatch, "192.168.1.175")
    assert client.post("/api/gateway/admin/runtime-config", json={"allow_engine_install": False}).status_code == 200
    r = client.post("/api/gateway/apps/observer/install", json={})
    assert r.status_code == 403 and started == []


# ---------------------------------------------------------------------------
# The tray talks to its gateway over loopback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bind", ["0.0.0.0", "127.0.0.1", "localhost", ""])
def test_the_tray_base_url_is_loopback_for_every_mode_the_network_setting_picks(bind: str) -> None:
    from abstractgateway.cli import _tray_base_url

    assert _tray_base_url(bind, 8080) == "http://127.0.0.1:8080"


def test_the_tray_client_uses_the_handshake_url_not_the_network_address(tmp_path: Path) -> None:
    from abstractgateway.tray import app as tray_app

    t = tray_app.TrayApp({"base_url": "http://127.0.0.1:18951", "token": "t" * 20, "data_dir": str(tmp_path)})
    assert t.client.base_url == "http://127.0.0.1:18951"


def test_console_card_says_started_outside_only_under_technical_details() -> None:
    """Mission GG's card, kept: an external app shows the Running pill and
    Open (its row's only action); Technical details adds "Started outside the
    gateway on port N" where Stop would be. Rendered for real by
    untracked/missionHH/capture_apps.mjs (Playwright, dark 1440)."""
    from abstractgateway.console_ui import CONSOLE_UI_JS

    card = CONSOLE_UI_JS[CONSOLE_UI_JS.index("function appCardMarkup(app) {"):CONSOLE_UI_JS.index("function appViewMarkup()")]
    plain, tech = card.split("if (techOn) {", 1)
    line = "Started outside the gateway on port ${esc(ext.port)}"
    assert line in tech and line not in plain
    assert 'app.source === "external" && app.external' in tech
    # Stop is offered only when the row lists it; an external row never does.
    assert 'if (app.running && actions.includes("stop") && admin) items.push(b("stop"' in tech


def test_cli_list_says_an_app_was_started_outside(monkeypatch, capsys) -> None:
    from tests.test_gateway_apps_tui import _cli, _FakeTransport

    data = {
        "runtime": {"node": {"message": "Node.js 24", "path": None}}, "registry": {"reachable": True},
        "apps": [{"id": "observer", "version": "0.1.12", "latest_version": None, "status": "running", "url": "http://127.0.0.1:3001/", "running": True,
                  "source": "external", "external": {"port": 3001, "detail": "Started outside the gateway on port 3001"}, "interfaces": []}],
    }
    code, out, _ = _cli(["apps", "list", "--no-latest"], _FakeTransport({("GET", "/apps"): (200, data)}), monkeypatch, capsys)
    assert code == 0, out
    assert "observer   0.1.12" in out and "http://127.0.0.1:3001/" in out
    assert "Started outside the gateway on port 3001 (open only: stop it where it was started)" in out
