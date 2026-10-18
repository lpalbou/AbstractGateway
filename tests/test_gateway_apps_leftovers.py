"""Leftovers of a previous gateway run: an app of THIS data dir that is still
running when the gateway starts again (or when an app is launched, stopped or
updated) is the gateway's own, whatever stopped the previous gateway. It is
stopped and the installed version takes its port — it is never filed as
"started outside the gateway". A copy whose command line is something else
(the dev stack, `npx`, a service unit) stays external, and a port whose
process this machine does not show is said to be unidentified.

No Node.js and no network: the "node" is the manager tests' shell shim, the
leftover is a Python process whose script lives under `<data dir>/apps/`
(what a real leftover's command line names), and every probe names a
scratch port (the conftest refuses the operator's live 3000-3007).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

import pytest

from abstractgateway import apps_manager as am
from test_gateway_apps_manager import _free_port, _publish, fake_node, manager  # noqa: F401 - fixtures

FLOW_PAGE = "<html><head><title>AbstractFlow Visual Editor</title></head></html>"
_REAL_KILL = os.kill  # the fixtures' cleanup never goes through a test's patched os.kill

# A leftover: listens on its port, answers the Flow start page, and (when
# asked) ignores SIGTERM like an app stuck in a graceful shutdown.
_LEFTOVER_PY = r'''
import http.server, signal, sys
if "--stubborn" in sys.argv:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
port = int(sys.argv[sys.argv.index("--port") + 1])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><head><title>AbstractFlow Visual Editor</title></head></html>"
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
print("listening", flush=True)
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


class _Leftover:
    """A process that runs `<data dir>/apps/<id>/<version>/.../bin/cli.js`
    (Python code in a file of that name: the command line is what matters)."""

    def __init__(self, data_dir: Path, app_id: str, version: str, port: int, *, stubborn: bool = False, under: Optional[Path] = None) -> None:
        spec = am.APP_BY_ID[app_id]
        root = under if under is not None else data_dir / "apps" / app_id / version / "node_modules" / Path(*spec.package.split("/"))
        root.mkdir(parents=True, exist_ok=True)
        (root / "package.json").write_text(json.dumps({"name": spec.package, "version": version}), encoding="utf-8")
        self.script = root / "bin" / "cli.js"
        self.script.parent.mkdir(exist_ok=True)
        self.script.write_text(_LEFTOVER_PY, encoding="utf-8")
        argv = [sys.executable, str(self.script), "--port", str(port)] + (["--stubborn"] if stubborn else [])
        self.port = port
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert self.proc.stdout and self.proc.stdout.readline().startswith("listening")
        # Reaped as soon as it exits: a zombie must not look alive to anyone.
        threading.Thread(target=self.proc.wait, daemon=True).start()
        self.argv = tuple(argv)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def external(self, app_id: str = "flow", *, argv: Optional[tuple] = None) -> am.ExternalApp:
        return am.ExternalApp(app_id, self.port, f"http://127.0.0.1:{self.port}/", version="0.8.0", pid=self.pid, argv=self.argv if argv is None else argv)

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                _REAL_KILL(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for _ in range(50):
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)


@pytest.fixture()
def leftovers():
    made: List[_Leftover] = []

    def make(*a, **kw) -> _Leftover:
        lo = _Leftover(*a, **kw)
        made.append(lo)
        return lo

    yield make
    for lo in made:
        lo.close()


def _wait_gone(lo: _Leftover, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not lo.alive():
            return True
        time.sleep(0.05)
    return not lo.alive()


# ---------------------------------------------------------------------------
# Telling a leftover from a copy started outside the gateway
# ---------------------------------------------------------------------------


def test_a_command_line_under_the_data_dirs_apps_folder_is_this_gateways(manager: am.AppsManager) -> None:
    apps = manager.apps_root
    assert manager.runs_this_gateways_install(["node", str(apps / "flow" / "0.8.0" / "node_modules" / "@abstractframework" / "flow" / "bin" / "cli.js"), "--port", "3005"])
    # The parent-watch preload the gateway adds names the folder too.
    assert manager.runs_this_gateways_install(["/usr/bin/node", "-r", str(apps / "_support" / "parent_watch.cjs"), "/somewhere/else/cli.js"])
    assert not manager.runs_this_gateways_install(["node", "/opt/stack/abstractflow/bin/cli.js", "--port", "3005"])
    assert not manager.runs_this_gateways_install(["node", str(apps.parent.parent / "other-data" / "apps" / "flow" / "0.8.0" / "cli.js")])
    assert not manager.runs_this_gateways_install([])
    flow = am.APP_BY_ID["flow"]
    own = am.ExternalApp("flow", 3005, "http://127.0.0.1:3005/", pid=4242, argv=("node", str(apps / "flow" / "0.8.0" / "bin" / "cli.js")))
    foreign = am.ExternalApp("flow", 3005, "http://127.0.0.1:3005/", pid=4242, argv=("node", "/opt/stack/abstractflow/bin/cli.js"))
    unseen = am.ExternalApp("flow", 3005, "http://127.0.0.1:3005/", pid=4242)
    nopid = am.ExternalApp("flow", 3005, "http://127.0.0.1:3005/")
    assert [manager.external_kind(e) for e in (own, foreign, unseen, nopid)] == ["leftover", "foreign", "unknown", "unknown"]
    manager.external_probe = lambda **kw: {"flow": own}
    assert manager.leftover(flow) == own
    manager.external_probe = lambda **kw: {"flow": foreign}
    assert manager.leftover(flow, refresh=True) is None


def test_detection_carries_the_listeners_command_line(leftovers, manager: am.AppsManager) -> None:
    lo = leftovers(manager.data_dir, "flow", "0.8.0", _free_port())
    got = am.detect_external_apps(ports=[lo.port], pids=lambda ps: {lo.port: lo.pid})
    assert got["flow"].pid == lo.pid and got["flow"].version == "0.8.0"
    assert str(lo.script) in got["flow"].argv, got["flow"].argv
    assert manager.external_kind(got["flow"]) == "leftover"


# ---------------------------------------------------------------------------
# Boot: pid-file leftovers (reap_orphans) and port leftovers (reap_leftovers)
# ---------------------------------------------------------------------------


def test_a_leftover_with_a_pid_file_is_stopped_even_when_it_ignores_sigterm(leftovers, manager: am.AppsManager) -> None:
    lo = leftovers(manager.data_dir, "flow", "0.8.0", _free_port(), stubborn=True)
    manager.write_pid_file("flow", pid=lo.pid, port=lo.port, cmd=list(lo.argv), version="0.8.0", bin_js=str(lo.script))
    t0 = time.time()
    reaped = manager.reap_orphans()
    assert reaped == [{"app_id": "flow", "pid": lo.pid, "version": "0.8.0"}]
    assert _wait_gone(lo), "SIGTERM was ignored: the reaper must SIGKILL it"
    assert time.time() - t0 < am.STOP_TIMEOUT_S + 10
    assert not manager.pid_path("flow").exists()


def test_a_pid_file_naming_a_process_that_could_not_be_stopped_is_kept(leftovers, manager: am.AppsManager, monkeypatch: pytest.MonkeyPatch) -> None:
    lo = leftovers(manager.data_dir, "flow", "0.8.0", _free_port(), stubborn=True)
    manager.write_pid_file("flow", pid=lo.pid, port=lo.port, cmd=list(lo.argv), version="0.8.0", bin_js=str(lo.script))
    # A machine where the hard signal does not land (refused): the record stays
    # for the next start, nothing is reported as reaped.
    real_kill = os.kill

    def kill(pid, sig):
        if pid == lo.pid and sig == signal.SIGKILL:
            raise PermissionError("not allowed")
        real_kill(pid, sig)

    monkeypatch.setattr(am.os, "kill", kill)
    monkeypatch.setattr(am, "STOP_TIMEOUT_S", 0.3)
    assert manager.reap_orphans() == []
    assert lo.alive() and manager.pid_path("flow").exists()


def test_a_leftover_without_a_pid_file_is_found_on_its_port_and_stopped(leftovers, manager: am.AppsManager) -> None:
    lo = leftovers(manager.data_dir, "flow", "0.8.0", _free_port(), stubborn=True)
    assert not manager.pid_path("flow").exists()
    manager.external_probe = lambda **kw: {"flow": lo.external()} if lo.alive() else {}
    reaped = manager.reap_leftovers()
    assert reaped == [{"app_id": "flow", "pid": lo.pid, "version": "0.8.0", "port": lo.port, "stopped": True}]
    assert _wait_gone(lo)
    assert manager.external_apps().get("flow") is None, "the port probe forgot it"


def test_a_copy_started_outside_the_gateway_is_left_alone_at_boot(leftovers, manager: am.AppsManager, tmp_path: Path) -> None:
    # Same program, same page, same port habit — but it runs from somewhere
    # that is not this data dir's apps folder (a checkout, npx's cache).
    lo = leftovers(manager.data_dir, "flow", "0.8.0", _free_port(), under=tmp_path / "elsewhere" / "abstractflow")
    manager.external_probe = lambda **kw: {"flow": lo.external()}
    assert manager.reap_leftovers() == []
    assert lo.alive()
    row = manager.app_row(am.APP_BY_ID["flow"])
    assert row["source"] == "external" and row["external"]["kind"] == "foreign" and row["actions"] == ["open"]
    assert row["external"]["detail"] == f"Started outside the gateway on port {lo.port} (process {lo.pid})"
    assert row["status_control"]["enabled"] is False and row["status_control"]["tip"] == am.BADGE_EXTERNAL_TIP
    assert "Left running" not in json.dumps(row)


def test_autostart_replaces_a_leftover_with_the_installed_version(leftovers, manager: am.AppsManager, monkeypatch: pytest.MonkeyPatch) -> None:
    _publish(manager, am.APP_BY_ID["flow"], "1.0.0")
    job, _ = manager.start_install("flow", run_inline=True)
    assert job.state == "succeeded", job.details
    manager._update_app_state("flow", enabled=True)
    port = _free_port()
    monkeypatch.setenv(am.ENV_PORTS, f"{port}-{port}")
    # The leftover holds the very port the installed app is allowed to use,
    # and no pid file names it.
    lo = leftovers(manager.data_dir, "flow", "0.8.0", port)
    manager.external_probe = lambda **kw: {"flow": lo.external()} if lo.alive() else {}
    outcomes = manager.autostart(gateway_url="http://127.0.0.1:18823")
    assert [o["ok"] for o in outcomes] == [True], outcomes
    assert not lo.alive()
    row = manager.app_row(am.APP_BY_ID["flow"])
    assert row["source"] == "gateway" and row["running"] and row["version"] == "1.0.0" and row["port"] == port


# ---------------------------------------------------------------------------
# launch / stop / update with a leftover on the port
# ---------------------------------------------------------------------------


def _install_flow(manager: am.AppsManager, version: str) -> None:
    manager.external_probe = lambda **kw: {}  # nothing on the usual ports (never the operator's live ones)
    _publish(manager, am.APP_BY_ID["flow"], version)
    job, _ = manager.start_install("flow", version=version, run_inline=True, update=True)
    assert job.state == "succeeded", job.details


def test_launch_stops_the_leftover_and_starts_the_install(leftovers, manager: am.AppsManager, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_flow(manager, "1.0.0")
    port = _free_port()
    monkeypatch.setenv(am.ENV_PORTS, f"{port}-{port}")
    lo = leftovers(manager.data_dir, "flow", "0.8.0", port, stubborn=True)
    manager.external_probe = lambda **kw: {"flow": lo.external()} if lo.alive() else {}
    row = manager.launch("flow", gateway_url="http://127.0.0.1:18823")
    assert not lo.alive()
    assert row["source"] == "gateway" and row["running"] and row["version"] == "1.0.0" and row["port"] == port
    assert row["pid"] != lo.pid


def test_launch_leaves_a_copy_started_outside_the_gateway_alone(leftovers, manager: am.AppsManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_flow(manager, "1.0.0")
    lo = leftovers(manager.data_dir, "flow", "0.8.0", _free_port(), under=tmp_path / "elsewhere" / "abstractflow")
    manager.external_probe = lambda **kw: {"flow": lo.external()}
    signalled: List[tuple] = []
    real_kill = os.kill
    monkeypatch.setattr(am.os, "kill", lambda pid, sig: (signalled.append((pid, sig)), real_kill(pid, sig)))
    row = manager.launch("flow")
    assert row["source"] == "external" and row["external"]["kind"] == "foreign" and row["pid"] == lo.pid
    assert lo.alive() and signalled == []
    with pytest.raises(am.StartedOutsideGateway) as exc:
        manager.stop("flow")
    assert f"process {lo.pid}" in exc.value.message and lo.alive()


def test_stop_stops_a_leftover_and_disables_the_app(leftovers, manager: am.AppsManager) -> None:
    _install_flow(manager, "1.0.0")
    manager._update_app_state("flow", enabled=True)
    lo = leftovers(manager.data_dir, "flow", "0.8.0", _free_port())
    manager.external_probe = lambda **kw: {"flow": lo.external()} if lo.alive() else {}
    row = manager.stop("flow")
    assert _wait_gone(lo)
    assert row["source"] == "gateway" and not row["running"] and not manager.is_enabled("flow")


def test_update_replaces_a_leftover_with_the_new_version(leftovers, manager: am.AppsManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """`abstractgateway apps update flow --version 1.0.0` while 0.8.0 of a
    previous gateway run holds the port: the job installs 1.0.0, stops the
    leftover and starts 1.0.0 on that port."""
    _install_flow(manager, "0.9.0")
    port = _free_port()
    monkeypatch.setenv(am.ENV_PORTS, f"{port}-{port}")
    lo = leftovers(manager.data_dir, "flow", "0.8.0", port)
    manager.external_probe = lambda **kw: {"flow": lo.external()} if lo.alive() else {}
    _publish(manager, am.APP_BY_ID["flow"], "1.0.0")
    job, _ = manager.start_install("flow", version="1.0.0", update=True, run_inline=True, gateway_url="http://127.0.0.1:18823")
    assert job.state == "succeeded", job.details
    assert not lo.alive()
    row = manager.app_row(am.APP_BY_ID["flow"])
    assert row["source"] == "gateway" and row["running"] and row["version"] == "1.0.0" and row["port"] == port
    assert "is running at" in job.message


def test_a_leftover_row_says_what_it_is_and_its_badge_stops_it(leftovers, manager: am.AppsManager) -> None:
    lo = leftovers(manager.data_dir, "flow", "0.8.0", _free_port())
    manager.external_probe = lambda **kw: {"flow": lo.external()}
    row = manager.app_row(am.APP_BY_ID["flow"])
    assert row["source"] == "external" and row["external"]["kind"] == "leftover" and row["external"]["leftover"] is True
    assert row["external"]["detail"] == (
        f"Left running by a previous gateway run on port {lo.port} (version 0.8.0, process {lo.pid}): stopping or starting the app replaces it with the installed version"
    )
    assert "outside" not in row["external"]["detail"]
    assert row["actions"] == ["open", "stop"] and row["update_available"] is False
    sc = row["status_control"]
    assert (sc["label"], sc["action"], sc["enabled"], sc["tip"]) == ("Running", "stop", True, am.BADGE_RUNNING_TIP)
    assert str(lo.script) in row["external"]["command"]


# ---------------------------------------------------------------------------
# Shutdown: no crash restart once the gateway is on its way out
# ---------------------------------------------------------------------------


def test_an_app_that_exits_during_shutdown_is_not_respawned(manager: am.AppsManager, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_flow(manager, "1.0.0")
    port = _free_port()
    monkeypatch.setenv(am.ENV_PORTS, f"{port}-{port}")
    row = manager.launch("flow", gateway_url="http://127.0.0.1:18823")
    proc = manager._procs["flow"]
    first = proc.proc
    assert first is not None and row["running"]
    # The service manager signals the app at the same time as the gateway
    # (systemd's control-group kill): the app is gone before stop_all runs.
    manager.closing = True
    os.kill(first.pid, signal.SIGTERM)
    first.wait(timeout=10)
    time.sleep(0.5)
    assert proc.proc is first and proc.status == "stopped"
    assert not manager.pid_path("flow").exists()
    manager.stop_all()
    assert proc.proc is first


def test_the_systemd_unit_lets_the_gateway_stop_its_children_then_kills_the_rest() -> None:
    from abstractgateway.os_service import render_systemd_unit

    home = Path("/home/someone")
    unit = render_systemd_unit(exe_argv=["/x/abstractgateway"], host="127.0.0.1", port=8080, data_dir=home / "d", home=home)
    assert "KillMode=mixed\n" in unit and "TimeoutStopSec=150\n" in unit
