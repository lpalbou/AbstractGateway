"""R11.3 (round 11): the Apps card's status badge IS the start/stop control.

Every apps row carries `status_control` {label, tone, busy, action, enabled,
tip}: the web console renders it as the badge button (kit tooltip = tip) and
the terminal console as the selectable badge cell (hint line = tip), so both
say the gateway's words.

- A browser app the gateway started: "Running" → action stop ("Running —
  click to stop"); installed and stopped: "Stopped" → action launch
  ("Stopped — click to start"); crashed: "Stopped unexpectedly — click to
  start"; starting/stopping/not installed: a plain pill (tip None).
- An app started outside the gateway: "Running", disabled, "Started outside
  the gateway — stop it where it was started".
- A non-admin: the same label, disabled, "Only an admin can start or stop apps".
- The desktop Assistant: started by THIS gateway → stop (POST /stop quits it,
  "Running — click to quit"); started elsewhere → disabled with the sentence
  (POST /stop refuses 409, nothing is quit); stopped → launch (its Open).

The request a badge click sends is the one the old Stop/Start buttons sent:
POST /api/gateway/apps/{id}/stop and /launch (exercised through the routes).
Nothing real is started: processes are fakes, the desktop is DesktopProbes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from abstractgateway import apps_manager as am
from test_r10w5_app_updates import CALLER, _install_web, _row, client, home, world  # noqa: F401 - fixtures

RUN_TIP = "Running — click to stop"
START_TIP = "Stopped — click to start"
QUIT_TIP = "Running — click to quit"
EXTERNAL_TIP = "Started outside the gateway — stop it where it was started"
ADMIN_TIP = "Only an admin can start or stop apps"
USER = {"local": True, "same_machine": True, "admin": False}


class FakeAppProcess:
    """Stands in for AppProcess: start() runs, stop() stops, both recorded."""

    def __init__(self, calls: List[str], running: bool = False) -> None:
        self.calls = calls
        self.running = running
        self.port = 3105 if running else None

    def alive(self) -> bool:
        return self.running

    def snapshot(self) -> Dict[str, Any]:
        return {"status": "running" if self.running else "stopped", "running": self.running, "pid": 4242 if self.running else None,
                "port": self.port, "version": "0.10.0", "started_at": None, "restarts_last_minute": 0, "last_exit_code": None, "last_error": None}

    def start(self, **kw: Any) -> None:
        self.calls.append("start")
        self.running, self.port = True, kw.get("port")

    def stop(self, *a: Any, **kw: Any) -> None:
        self.calls.append("stop")
        self.running = False


def _managed(world, app_id: str, *, running: bool) -> List[str]:
    calls: List[str] = []
    _install_web(world.m, app_id, "0.10.0")
    cli = world.m.package_dir(app_id, "0.10.0") / "bin" / "cli.js"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text("// fake app: never executed (FakeAppProcess)\n", encoding="utf-8")
    world.m._procs[app_id] = FakeAppProcess(calls, running=running)  # type: ignore[assignment]
    world.net.npm(am.spec_for(app_id).package, "0.10.0")
    world.net.pypi("0.13.0")
    return calls


def test_status_control_matrix() -> None:
    sc = am.browser_status_control
    assert sc(status="running", actions=["open", "stop"], external=False, admin=True) == {
        "label": "Running", "tone": "ok", "busy": False, "action": "stop", "enabled": True, "tip": RUN_TIP}
    assert sc(status="stopped", actions=["launch"], external=False, admin=True) == {
        "label": "Stopped", "tone": "muted", "busy": False, "action": "launch", "enabled": True, "tip": START_TIP}
    crashed = sc(status="crashed", actions=["launch"], external=False, admin=True)
    assert (crashed["label"], crashed["action"], crashed["enabled"], crashed["tip"]) == ("Stopped unexpectedly", "launch", True, "Stopped unexpectedly — click to start")
    assert sc(status="crash_loop", actions=["launch"], external=False, admin=True)["tip"] == "Keeps crashing — click to start"
    ext = sc(status="running", actions=["open"], external=True, admin=True)
    assert (ext["label"], ext["action"], ext["enabled"], ext["tip"]) == ("Running", None, False, EXTERNAL_TIP)
    for st, actions in (("running", ["open", "stop"]), ("stopped", ["launch"])):
        u = sc(status=st, actions=actions, external=False, admin=False)
        assert u["enabled"] is False and u["tip"] == ADMIN_TIP
    for st, label in (("starting", "Starting…"), ("stopping", "Stopping…"), ("not_installed", "Not installed")):
        p = sc(status=st, actions=[], external=False, admin=True)
        assert p["label"] == label and p["tip"] is None and p["action"] is None and p["enabled"] is False
    assert sc(status="starting", actions=[], external=False, admin=True)["busy"] is True


def test_every_overview_row_carries_its_badge(world) -> None:
    world.net.pypi("0.13.0")
    ov = world.m.overview(caller=CALLER)
    assert ov["apps"], "no rows"
    for row in ov["apps"]:
        sc = row["status_control"]
        assert set(sc) == {"label", "tone", "busy", "action", "enabled", "tip"}, row["id"]


def test_browser_rows_running_stopped_external_and_non_admin(world) -> None:
    _managed(world, "flow", running=True)
    _managed(world, "code", running=False)
    world.m.external_probe = lambda **kw: {"observer": am.ExternalApp("observer", 3001, "http://127.0.0.1:3001/", version="0.1.12", pid=77)}
    ov = world.m.overview(caller=CALLER)
    f, c, o = _row(ov, "flow")["status_control"], _row(ov, "code")["status_control"], _row(ov, "observer")["status_control"]
    assert (f["label"], f["action"], f["enabled"], f["tip"]) == ("Running", "stop", True, RUN_TIP)
    assert (c["label"], c["action"], c["enabled"], c["tip"]) == ("Stopped", "launch", True, START_TIP)
    assert (o["label"], o["action"], o["enabled"], o["tip"]) == ("Running", None, False, EXTERNAL_TIP)
    ov = world.m.overview(caller=USER)
    for app_id in ("flow", "code"):
        sc = _row(ov, app_id)["status_control"]
        assert sc["enabled"] is False and sc["tip"] == ADMIN_TIP, app_id
    assert _row(ov, "observer")["status_control"]["tip"] == EXTERNAL_TIP


def test_route_badge_click_stop_then_start(client, world, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _managed(world, "flow", running=True)
    # No port is probed (the operator's live apps listen on 3001-3005).
    monkeypatch.setattr(am, "allocate_port", lambda **kw: 3105)
    a = _row(client.get("/api/gateway/apps").json(), "flow")["status_control"]
    assert a["action"] == "stop" and a["enabled"]
    r = client.post(f"/api/gateway/apps/flow/{a['action']}", json={})
    assert r.status_code == 200, r.text
    assert calls == ["stop"]
    b = r.json()["app"]["status_control"]
    assert (b["label"], b["action"], b["tip"]) == ("Stopped", "launch", START_TIP)
    r = client.post(f"/api/gateway/apps/flow/{b['action']}", json={})
    assert r.status_code == 200, r.text
    assert calls == ["stop", "start"]
    assert r.json()["app"]["status_control"]["label"] == "Running"


def test_route_external_stop_is_refused(client, world) -> None:
    world.net.pypi("0.13.0")
    world.m.external_probe = lambda **kw: {"observer": am.ExternalApp("observer", 3001, "http://127.0.0.1:3001/", version="0.1.12", pid=77)}
    r = client.post("/api/gateway/apps/observer/stop", json={})
    assert r.status_code == 409 and r.json()["reason"] == "started_outside_gateway"


# ---------------------------------------------------------------------------
# The desktop Assistant
# ---------------------------------------------------------------------------


def test_assistant_badge_stopped_opens(world) -> None:
    world.net.pypi("0.13.0")
    a = _row(world.m.overview(caller=CALLER), "assistant")
    sc = a["status_control"]
    assert (sc["label"], sc["action"], sc["enabled"], sc["tip"]) == ("Stopped", "launch", True, START_TIP)
    assert "stop" not in a["actions"]
    u = _row(world.m.overview(caller=USER), "assistant")["status_control"]
    assert u["enabled"] is False and u["tip"] == ADMIN_TIP
    remote = _row(world.m.overview(caller={"local": False, "same_machine": False, "admin": True}), "assistant")["status_control"]
    assert remote["enabled"] is False and remote["tip"] == "The Assistant runs on the gateway's computer: open it there."


def test_assistant_started_by_the_gateway_quits_from_the_badge(client, world) -> None:
    world.net.pypi("0.13.0")
    assert client.post("/api/gateway/apps/assistant/launch", json={}).status_code == 200
    pid = world.spawned[-1]["pid"]
    world.m._desktop_cache.clear()
    a = _row(client.get("/api/gateway/apps").json(), "assistant")
    sc = a["status_control"]
    assert (sc["label"], sc["action"], sc["enabled"], sc["tip"]) == ("Running", "stop", True, QUIT_TIP)
    assert "stop" in a["actions"]
    r = client.post("/api/gateway/apps/assistant/stop", json={})
    assert r.status_code == 200, r.text
    assert world.quit == [pid]
    after = r.json()["app"]["status_control"]
    assert (after["label"], after["action"]) == ("Stopped", "launch")


def test_assistant_started_elsewhere_is_disabled_and_never_quit(client, world) -> None:
    world.net.pypi("0.13.0")
    world.state["procs"] = [(777, [world.state["script"]])]
    world.m._desktop_cache.clear()
    a = _row(client.get("/api/gateway/apps").json(), "assistant")
    sc = a["status_control"]
    assert a["running"] is True
    assert (sc["label"], sc["action"], sc["enabled"], sc["tip"]) == ("Running", None, False, EXTERNAL_TIP)
    assert "stop" not in a["actions"]
    r = client.post("/api/gateway/apps/assistant/stop", json={})
    assert r.status_code == 409 and r.json()["reason"] == "started_outside_gateway"
    assert world.quit == []
