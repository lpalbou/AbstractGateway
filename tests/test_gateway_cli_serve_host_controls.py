"""`abstractgateway serve` with the host-control seam (2026-09-05).

Pins, against a uvicorn test double that exposes Config/Server:
- the Server is registered restartable with the parsed argv;
- the tray helper is launched only AFTER `server.started` (never against a
  port another gateway owns) and never at all under `--reload`;
- a clean `server.run()` return honours a requested restart, a Ctrl-C
  during the drain never does (and prints no traceback);
- a startup failure (port in use) exits with uvicorn's STARTUP_FAILURE code.
"""

from __future__ import annotations

import logging
import sys
import threading
import types
from typing import Any, Dict, List

import pytest

pytestmark = pytest.mark.basic


class _FakeServer:
    instances: List["_FakeServer"] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self.should_exit = False
        self.started = False
        self.run_behaviour: Any = None
        _FakeServer.instances.append(self)

    def run(self) -> None:
        self.started = True
        # Give the tray-launch thread a moment to observe `started`.
        threading.Event().wait(0.3)
        if self.run_behaviour == "keyboard-interrupt":
            raise KeyboardInterrupt()
        if self.run_behaviour == "never-started":
            self.started = False
        return None


def _fake_uvicorn(monkeypatch: pytest.MonkeyPatch, *, behaviour: Any = None) -> types.ModuleType:
    uvicorn = types.ModuleType("uvicorn")
    calls: Dict[str, Any] = {}

    class _Config:
        def __init__(self, app: str, **kwargs: Any) -> None:
            calls["app"] = app
            calls.update(kwargs)
            self.timeout_graceful_shutdown = kwargs.get("timeout_graceful_shutdown")

    def _server(config: Any) -> _FakeServer:
        srv = _FakeServer(config)
        srv.run_behaviour = behaviour
        return srv

    uvicorn.Config = _Config  # type: ignore[attr-defined]
    uvicorn.Server = _server  # type: ignore[attr-defined]
    uvicorn.run = lambda *a, **k: calls.__setitem__("run_called", True)  # type: ignore[attr-defined]
    main_mod = types.ModuleType("uvicorn.main")
    main_mod.STARTUP_FAILURE = 3  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    monkeypatch.setitem(sys.modules, "uvicorn.main", main_mod)
    uvicorn.calls = calls  # type: ignore[attr-defined]
    return uvicorn


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import host_control, tray_supervisor

    host_control._reset_for_tests()
    _FakeServer.instances.clear()
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "operator-token-for-tests")
    monkeypatch.delenv("ABSTRACTGATEWAY_RUNNER", raising=False)
    yield
    host_control._reset_for_tests()
    tray_supervisor._serve_context.clear()


def _serve(monkeypatch: pytest.MonkeyPatch, *, behaviour: Any = None, extra_args: List[str] | None = None, tray_calls: List[Dict[str, Any]] | None = None, decision_start: bool = True):
    from abstractgateway import cli as gateway_cli
    from abstractgateway import tray_supervisor

    uvicorn = _fake_uvicorn(monkeypatch, behaviour=behaviour)
    monkeypatch.setattr(gateway_cli, "_resolve_default_console_level", lambda: logging.ERROR)
    monkeypatch.setattr(gateway_cli, "_reserve_gguf_metal", lambda: None)
    monkeypatch.setattr(gateway_cli, "_migrate_legacy_core_config_store", lambda: None)
    # The decision is injected so the test never depends on the runner's desktop.
    monkeypatch.setattr(gateway_cli, "tray_decision", lambda **kw: tray_supervisor.TrayDecision(decision_start, "ok" if decision_start else "headless"), raising=False)
    monkeypatch.setattr(tray_supervisor, "tray_decision", lambda **kw: tray_supervisor.TrayDecision(decision_start, "ok" if decision_start else "headless"))

    recorded = tray_calls if tray_calls is not None else []

    class _Sup:
        def start(self, **kw: Any) -> Dict[str, Any]:
            recorded.append({"start": kw, "server_started": _FakeServer.instances[-1].started if _FakeServer.instances else None})
            return {"running": True, "pid": 4242, "ready": True}

        def stop(self, **kw: Any) -> Dict[str, Any]:
            recorded.append({"stop": True})
            return {"running": False}

    monkeypatch.setattr(tray_supervisor, "get_tray_supervisor", lambda: _Sup())
    args = ["serve", "--host", "127.0.0.1", "--port", "9999", *(extra_args or [])]
    relaunched: List[List[str]] = []
    from abstractgateway import host_control

    monkeypatch.setattr(host_control, "relaunch_process", lambda command=None: relaunched.append(list(command or host_control.build_relaunch_command())))
    gateway_cli.main(args)
    # The tray-launch thread is short-lived; let it finish.
    for t in threading.enumerate():
        if t.name == "gateway-tray-launch":
            t.join(timeout=3.0)
    return uvicorn, recorded, relaunched


def test_serve_registers_server_launches_tray_after_bind_and_stops_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import tray_supervisor

    uvicorn, tray_calls, relaunched = _serve(monkeypatch)
    assert uvicorn.calls["app"] == "abstractgateway.app:app" and uvicorn.calls["port"] == 9999
    assert "run_called" not in uvicorn.calls  # the Server API path, not uvicorn.run
    starts = [c for c in tray_calls if "start" in c]
    assert starts and starts[0]["server_started"] is True  # never before the listener is bound
    assert starts[0]["start"]["base_url"] == "http://127.0.0.1:9999"
    assert tray_calls[-1] == {"stop": True}
    assert relaunched == []
    ctx = tray_supervisor.serve_context()
    assert ctx["base_url"] == "http://127.0.0.1:9999" and ctx["reload"] is False


def test_serve_reload_uses_uvicorn_run_and_no_tray(monkeypatch: pytest.MonkeyPatch) -> None:
    uvicorn, tray_calls, _ = _serve(monkeypatch, extra_args=["--reload"])
    assert uvicorn.calls.get("run_called") is True
    assert not [c for c in tray_calls if "start" in c]


def test_requested_restart_relaunches_only_after_a_clean_return(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import host_control

    # Arrange a restart request the moment the server starts (as the tray would).
    orig_run = _FakeServer.run

    def _run_then_restart(self: _FakeServer) -> None:
        host_control.request_restart(by="test")
        return orig_run(self)

    monkeypatch.setattr(_FakeServer, "run", _run_then_restart)
    _, _, relaunched = _serve(monkeypatch)
    assert relaunched and relaunched[0][1:3] == ["-m", "abstractgateway"] and relaunched[0][3:] == ["serve", "--host", "127.0.0.1", "--port", "9999"]
    assert _FakeServer.instances[-1].config.timeout_graceful_shutdown == host_control.REQUESTED_EXIT_GRACEFUL_S


def test_ctrl_c_during_a_restart_drain_never_relaunches(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import host_control

    orig_run = _FakeServer.run

    def _run_then_restart(self: _FakeServer) -> None:
        host_control.request_restart(by="test")
        self.run_behaviour = "keyboard-interrupt"
        return orig_run(self)

    monkeypatch.setattr(_FakeServer, "run", _run_then_restart)
    _, tray_calls, relaunched = _serve(monkeypatch)  # no traceback escapes main()
    assert relaunched == []
    assert tray_calls[-1] == {"stop": True}
    assert host_control.restart_requested() is False


def test_startup_failure_exits_with_uvicorn_code(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as exc:
        _serve(monkeypatch, behaviour="never-started")
    assert exc.value.code == 3
