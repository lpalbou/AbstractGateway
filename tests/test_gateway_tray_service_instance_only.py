"""Operator ruling 2026-10-29 (two tray icons on one Mac: a scratch e2e gateway's beside the
service's): only the machine's gateway service shows the icon, or a `serve --tray`."""
from __future__ import annotations

import pytest

from abstractgateway import tray_supervisor as ts
from abstractgateway.os_service import LAUNCHD_LABEL, SYSTEMD_UNIT

DEPS = (True, None)


@pytest.fixture(autouse=True)
def _no_marker(monkeypatch):
    monkeypatch.setattr(ts, "_service_launch_marker", None)


def test_launchd_job_with_our_label_is_the_service() -> None:
    assert ts.service_launch(platform="darwin", env={"XPC_SERVICE_NAME": LAUNCHD_LABEL}) == f"launchd ({LAUNCHD_LABEL})"
    # A Terminal, an IDE, a test harness: other XPC names, or none.
    assert ts.service_launch(platform="darwin", env={"XPC_SERVICE_NAME": "application.com.apple.Terminal.1234"}) is None
    assert ts.service_launch(platform="darwin", env={"XPC_SERVICE_NAME": "0"}) is None
    assert ts.service_launch(platform="darwin", env={}) is None


def test_systemd_unit_cgroup_is_the_service() -> None:
    unit = f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{SYSTEMD_UNIT}\n"
    shell = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/vte-spawn-1.scope\n"
    assert ts.service_launch(platform="linux", env={}, cgroup_text=unit) == f"systemd ({SYSTEMD_UNIT})"
    assert ts.service_launch(platform="linux", env={}, cgroup_text=shell) is None
    assert ts.service_launch(platform="linux", env={}, cgroup_text="") is None


def test_the_service_launcher_marks_windows_and_xdg(monkeypatch) -> None:
    assert ts.service_launch(platform="win32", env={}) is None
    ts.mark_service_launch("Windows Run entry")
    assert ts.service_launch(platform="win32", env={}) == "Windows Run entry"


@pytest.mark.parametrize("tray,service,start", [(False, None, False), (True, None, True),
                                                (False, "launchd (x)", True)])
def test_decision(tray, service, start) -> None:
    d = ts.tray_decision(platform="darwin", env={}, dependencies=DEPS, probes={"mac_gui": True},
                         tray=tray, service=service)
    assert d.start is start, d
    if not start:
        assert d.reason == "not_service" and "--tray" in d.hint
    # --no-tray still wins over everything.
    assert ts.tray_decision(platform="darwin", env={}, dependencies=DEPS, probes={"mac_gui": True}, tray=tray,
                            service=service, no_tray=True).start is False


def test_serve_from_a_terminal_starts_no_tray_and_tray_flag_does(monkeypatch) -> None:
    """Through `cli.main(["serve"])` with uvicorn faked: the decision the CLI really takes."""
    import logging
    import threading

    from abstractgateway import cli as gateway_cli
    from abstractgateway import host_control
    from tests.test_gateway_cli_serve_host_controls import _fake_uvicorn

    decisions = []
    real = ts.tray_decision
    started = []

    def _spy(**kw):
        kw.update(platform="darwin", env={}, dependencies=DEPS, probes={"mac_gui": True})
        d = real(**kw)
        decisions.append(d)
        return d

    class _Sup:
        def start(self, **kw):
            started.append(kw)
            return {"running": True, "pid": 4242, "ready": True}

        def stop(self, **kw):
            return {"running": False}

    def _run(extra, *, service=None):
        _fake_uvicorn(monkeypatch)
        monkeypatch.setattr(gateway_cli, "_resolve_default_console_level", lambda: logging.ERROR)
        monkeypatch.setattr(gateway_cli, "_reserve_gguf_metal", lambda: None)
        monkeypatch.setattr(gateway_cli, "_migrate_legacy_core_config_store", lambda: None)
        monkeypatch.setattr(ts, "tray_decision", _spy)
        monkeypatch.setattr(ts, "service_launch", lambda **kw: service)
        monkeypatch.setattr(ts, "get_tray_supervisor", lambda: _Sup())
        monkeypatch.setattr(host_control, "relaunch_process", lambda command=None: None)
        gateway_cli.main(["serve", "--host", "127.0.0.1", "--port", "9999", *extra])
        for t in threading.enumerate():
            if t.name == "gateway-tray-launch":
                t.join(timeout=3.0)

    _run([])
    assert decisions[-1].reason == "not_service" and not started, decisions
    _run(["--tray"])
    assert decisions[-1].start is True, decisions
    _run([], service=f"launchd ({LAUNCHD_LABEL})")
    assert decisions[-1].start is True, decisions
    with pytest.raises(SystemExit):
        gateway_cli.main(["serve", "--tray", "--no-tray"])
