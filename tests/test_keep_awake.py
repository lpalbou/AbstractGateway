"""Keep this Mac awake while remote access is on (keep_awake.py, `gateway_keep_awake_v1`).

Operator ruling 2026-11-02: "yes, unless a user specifically put the mac on
sleep mode." Pins:
- remote access = the `internet` exposure this process started with, or
  Tailscale Serve/Funnel forwarding to its port on this machine;
- the setting: `serve --keep-awake on|off` > saved > on, through the one
  settings door (runtime-config POST, `config set`), applied live;
- the assertion is held exactly while setting AND remote access are on,
  released on stop and never re-taken after it; a refusal is reported and
  retried; a system without a backend never probes and says so;
- the IOKit signatures work on a real Mac (acquire + release, darwin only).
No test here holds a real assertion except the darwin ctypes smoke test,
which releases it at once.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

import pytest
from fastapi.testclient import TestClient

from abstractgateway import keep_awake as ka

pytestmark = pytest.mark.basic

_TOKEN = "keep-awake-admin-secret"


class FakeAssertion:
    def __init__(self, fail: Optional[str] = None) -> None:
        self.fail = fail
        self.held: List[int] = []
        self.calls: List[str] = []
        self._next = 100

    def acquire(self, name: str) -> int:
        self.calls.append(f"acquire:{name}")
        if self.fail:
            raise ka.PowerAssertionError(self.fail)
        self._next += 1
        self.held.append(self._next)
        return self._next

    def release(self, aid: int) -> None:
        self.calls.append(f"release:{aid}")
        self.held.remove(aid)


def _controller(backend: Optional[FakeAssertion], tailscale=lambda port: None, platform: str = "darwin") -> ka.KeepAwake:
    return ka.KeepAwake(backend_factory=lambda: backend, tailscale_fn=tailscale, platform=platform, interval_s=3600)


def _started(c: ka.KeepAwake, data: Path, *, exposure: str, port: int = 8080) -> ka.KeepAwake:
    c.configure(data_dir=data, port=port, exposure=exposure)
    c.start()
    return c


# ----------------------------------------------------------- Tailscale Serve / Funnel


_FUNNEL = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {"forge.tail.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}},
    "AllowFunnel": {"forge.tail.ts.net:443": True},
}


def test_tailscale_funnel_serve_and_foreground_are_recognised() -> None:
    assert ka.parse_tailscale_forwarding(json.dumps(_FUNNEL), 8080) == "tailscale_funnel"
    serve = {"Web": {"m.ts.net:443": {"Handlers": {"/": {"Proxy": "http://localhost:8080"}}}}}
    assert ka.parse_tailscale_forwarding(json.dumps(serve), 8080) == "tailscale_serve"
    fg = {"Foreground": {"sess1": {"Web": {"m.ts.net:443": {"Handlers": {"/": {"Proxy": "127.0.0.1:8080"}}}}}}}
    assert ka.parse_tailscale_forwarding(json.dumps(fg), 8080) == "tailscale_serve"
    tcp = {"TCP": {"10000": {"TCPForward": "127.0.0.1:8080"}}, "AllowFunnel": {"m.ts.net:10000": True}}
    assert ka.parse_tailscale_forwarding(json.dumps(tcp), 8080) == "tailscale_funnel"


def test_tailscale_forwarding_elsewhere_is_not_remote_access_to_this_gateway() -> None:
    assert ka.parse_tailscale_forwarding(json.dumps(_FUNNEL), 18080) is None  # another port
    other = {"Web": {"m.ts.net:443": {"Handlers": {"/": {"Proxy": "http://192.168.1.9:8080"}}}}}
    assert ka.parse_tailscale_forwarding(json.dumps(other), 8080) is None  # another machine
    for garbage in ("", "not json", "[]", "{}", json.dumps({"Web": {"x": None}})):
        assert ka.parse_tailscale_forwarding(garbage, 8080) is None


# ----------------------------------------------------------- the setting


def test_setting_flag_beats_saved_beats_on(tmp_path: Path) -> None:
    from abstractgateway.runtime_config import record_launch_settings, write_runtime_config

    assert ka.resolve_setting(tmp_path)["value"] is True
    assert ka.resolve_setting(tmp_path)["source"] == "default"
    write_runtime_config(tmp_path, {"keep_awake": "off"}, actor="test")
    assert {k: ka.resolve_setting(tmp_path)[k] for k in ("value", "source")} == {"value": False, "source": "stored"}
    record_launch_settings(tmp_path, {"keep_awake": True})
    row = ka.resolve_setting(tmp_path)
    assert (row["value"], row["source"], row["stored_value"]) == (True, "flag", False)
    assert row["label"] == ka.LABEL and row["flag"] == ka.FLAG and row["cli"] == ka.CLI


def test_settings_door_validates_and_clears(tmp_path: Path) -> None:
    from abstractgateway.runtime_config import RuntimeConfigError, read_runtime_config, write_runtime_config

    with pytest.raises(RuntimeConfigError):
        write_runtime_config(tmp_path, {"keep_awake": "of"}, actor="test")
    out = write_runtime_config(tmp_path, {"keep_awake": False}, actor="test")
    assert out["applied"] == {"keep_awake": False} and out["keep_awake"]["value"] is False
    write_runtime_config(tmp_path, {"keep_awake": None}, actor="test")
    assert read_runtime_config(tmp_path)["keep_awake"]["source"] == "default"


def test_serve_flag_is_recorded_as_a_launch_setting(tmp_path: Path) -> None:
    from abstractgateway.cli import _apply_backlog_launch_flags

    args = argparse.Namespace(backlog_root=None, exec_runner=None, keep_awake="off")
    _apply_backlog_launch_flags(args, tmp_path)
    row = ka.resolve_setting(tmp_path)
    assert (row["value"], row["source"]) == (False, "flag")


def test_serve_parser_offers_keep_awake_on_off() -> None:
    import abstractgateway.cli as cli

    src = Path(cli.__file__).read_text(encoding="utf-8")
    assert '"--keep-awake"' in src and 'choices=["on", "off"]' in src
    assert "keep_awake.configure(" in src  # serve configures the controller the lifespan starts


# ----------------------------------------------------------- the controller


def test_internet_exposure_holds_and_setting_off_releases(tmp_path: Path) -> None:
    from abstractgateway.runtime_config import write_runtime_config

    backend = FakeAssertion()
    probes: List[int] = []
    c = _controller(backend, tailscale=lambda port: probes.append(port))
    _started(c, tmp_path, exposure="internet")
    st = c.refresh()
    assert st["state"] == "awake" and st["holding"] and st["line"] == "Staying awake for remote access"
    assert st["remote_access"] == {"on": True, "via": "internet", "label": "internet exposure"}
    assert backend.calls == [f"acquire:{ka.ASSERTION_NAME}"] and len(backend.held) == 1
    assert probes == []  # the exposure answers: no Tailscale call
    c.refresh()
    assert len(backend.held) == 1  # held once, not stacked
    write_runtime_config(tmp_path, {"keep_awake": False}, actor="test")
    st = c.refresh()
    assert st["state"] == "off" and not st["holding"] and st["line"] == "Not keeping awake"
    assert backend.held == []
    c.stop()


def test_localhost_exposure_follows_tailscale_funnel(tmp_path: Path) -> None:
    backend = FakeAssertion()
    forwarding = {"via": None}
    c = _controller(backend, tailscale=lambda port: forwarding["via"] if port == 18080 else "tailscale_funnel")
    _started(c, tmp_path, exposure="localhost", port=18080)
    assert c.refresh()["state"] == "no_remote_access" and backend.held == []
    forwarding["via"] = "tailscale_funnel"
    st = c.refresh()
    assert st["state"] == "awake" and st["remote_access"]["label"] == "Tailscale Funnel"
    forwarding["via"] = None
    assert c.refresh()["state"] == "no_remote_access" and backend.held == []
    c.stop()


def test_lan_exposure_alone_is_not_remote_access(tmp_path: Path) -> None:
    backend = FakeAssertion()
    c = _started(_controller(backend), tmp_path, exposure="lan")
    assert c.refresh()["state"] == "no_remote_access" and backend.held == []
    c.stop()


def test_stop_releases_and_nothing_holds_after_it(tmp_path: Path) -> None:
    backend = FakeAssertion()
    c = _started(_controller(backend), tmp_path, exposure="internet")
    assert c.refresh()["holding"]
    c.stop()
    assert backend.held == []
    assert c.refresh()["holding"] is False and backend.held == []


def test_a_refusal_is_reported_and_retried(tmp_path: Path) -> None:
    backend = FakeAssertion(fail="macOS refused the assertion (IOReturn 0xe00002c2)")
    c = _started(_controller(backend), tmp_path, exposure="internet")
    st = c.refresh()
    assert st["state"] == "error" and "0xe00002c2" in st["detail"] and not st["holding"]
    backend.fail = None
    assert c.refresh()["state"] == "awake"
    c.stop()


def test_no_backend_never_probes_and_says_so(tmp_path: Path) -> None:
    probes: List[int] = []
    c = _started(_controller(None, tailscale=lambda port: probes.append(port), platform="linux"), tmp_path, exposure="internet")
    st = c.status()
    assert st["state"] == "unsupported" and st["supported"] is False and st["line"] == "Not keeping awake"
    assert probes == []


def test_an_unconfigured_process_is_inactive(tmp_path: Path) -> None:
    backend = FakeAssertion()
    c = _controller(backend)
    c.start()
    assert c.refresh()["state"] == "inactive" and backend.calls == []


def test_the_lifespan_starts_and_stops_the_controller() -> None:
    import abstractgateway.app as app_mod

    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    assert "keep_awake.start_configured()" in src and "keep_awake.stop_active()" in src


# ----------------------------------------------------------- routes


def _serve_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    data = tmp_path / "runtime"
    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    return data


def test_routes_read_the_state_and_the_settings_door_applies_it_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _serve_env(monkeypatch, tmp_path)
    backend = FakeAssertion()
    c = _started(_controller(backend), data, exposure="internet")
    c.refresh()
    monkeypatch.setattr(ka, "_controller", c)
    from abstractgateway.app import app

    client = TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})
    st = client.get("/api/gateway/host/keep-awake").json()
    assert st["schema"] == "gateway_keep_awake_v1" and st["state"] == "awake"
    out = client.post("/api/gateway/admin/runtime-config", json={"keep_awake": False})
    assert out.status_code == 200, out.text
    assert out.json()["keep_awake_status"]["state"] == "off" and backend.held == []
    assert client.get("/api/gateway/admin/runtime-config").json()["keep_awake"]["value"] is False
    assert client.post("/api/gateway/admin/runtime-config", json={"keep_awake": "maybe"}).status_code == 400
    c.stop()


# ----------------------------------------------------------- the real IOKit seam


@pytest.mark.skipif(sys.platform != "darwin", reason="IOKit power assertions are macOS only")
def test_iokit_assertion_acquires_and_releases_on_a_real_mac() -> None:
    real = ka.MacIdleSleepAssertion()
    aid = real.acquire("AbstractGateway: test (released at once)")
    try:
        assert aid > 0
    finally:
        real.release(aid)
    with pytest.raises(ka.PowerAssertionError):
        real.release(aid)  # already gone: the IOReturn is surfaced, never swallowed


# ----------------------------------------------------------- the tray


def test_tray_network_menus_show_the_line(tmp_path: Path) -> None:
    from abstractgateway.tray import menu_model as mm
    from abstractgateway.tray import menu_v2

    c = _started(_controller(FakeAssertion()), tmp_path, exposure="internet")
    payload = {"configured": {"mode": "internet", "port": 8080}, "effective": {"mode": "internet", "port": 8080},
               "addresses": [], "keep_awake": c.refresh()}
    nv = mm.parse_network(payload)
    assert nv.keep_awake_line == "Staying awake for remote access"

    def texts(node) -> List[str]:
        return [ch.label for ch in node.children]

    assert "Staying awake for remote access" in texts(mm.network_section(nv, reachable=True))
    from tests.test_gateway_tray_menu_v2 import _snap

    inputs = mm.MenuInputs(snap=_snap(), network=nv)
    assert "Staying awake for remote access" in texts(menu_v2.connect_section(inputs, reachable=True))
    c.stop()
    # An older gateway (no block) or a system without it: no line at all.
    assert mm.parse_network({**payload, "keep_awake": None}).keep_awake_line is None
    assert mm.parse_network({**payload, "keep_awake": {"state": "unsupported", "line": "Not keeping awake"}}).keep_awake_line is None


# ----------------------------------------------------------- the web console


def test_console_says_it_on_the_card_and_offers_the_switch_to_admins() -> None:
    """The Resources page's Gateway card is a summary (its rows move into Manage gateway):
    the keep-awake line must reach the summary, the switch lives in the modal."""
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    assert 'id="gateway-host-awake-toggle"' in html and "Keep this Mac awake while remote access is on" in html
    assert 'api("/api/gateway/host/keep-awake")' in html
    assert "JSON.stringify({ keep_awake: on })" in html  # the one settings door
    assert "const awake=state.hostKeepAwake;" in html  # resourcesRunnerSummary shows the line on the card
    render = html[html.index("function renderKeepAwake("):html.index("async function toggleKeepAwake(")]
    assert "resourcesRunnerSummary();" in render  # a new state refreshes the card at once
