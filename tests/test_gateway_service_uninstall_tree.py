"""`abstractgateway service uninstall` stops the whole gateway process tree (backlog 0934).

A fake serve process (its command line names an abstract* program, its pid is in
`<data>/run/gateway-serve.json`) starts a child in its own session that ignores SIGTERM, as the
entity own-time loop and the download jobs are started. launchd is a stub whose `bootout` sends
the serve process SIGTERM, as launchd does, and returns. The command must not return while
either is alive, must say what it stopped, and must fail naming what it could not stop.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List

import pytest

from abstractgateway import os_service, process_tree

pytestmark = [pytest.mark.basic, pytest.mark.skipif(sys.platform == "win32", reason="POSIX process trees")]

_SERVE = r"""
import signal, subprocess, sys, time
child = subprocess.Popen(
    [sys.executable, "-c", "import signal, sys, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nopen(sys.argv[1], 'w').write('ready')\nwhile True: time.sleep(0.2)", sys.argv[2]],
    start_new_session=True,
)
open(sys.argv[1], "w").write(str(child.pid))
while True:
    time.sleep(0.2)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie has exited (ps says Z).
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


def _wait_for(pred, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(autouse=True)
def _narrow_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    # CI runners set COLUMNS=80 and ps cut every command line there, so the fake serve's
    # "abstractgateway" name (past column 80) went unseen; every test here runs that narrow.
    monkeypatch.setenv("COLUMNS", "80")


@pytest.fixture()
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The fake serve's `abstractgateway` marker sits at the END of a long command line, and the
    # interpreter's path names no abstract* program (CI: /opt/hostedtoolcache/Python/...): the
    # tree must read the whole command line (procfs on Linux, `ps -ww` elsewhere), whatever the
    # terminal width says (CI 2026-10-29: a cut line dropped the marker and the tree came back empty).
    monkeypatch.setenv("COLUMNS", "80")
    data = tmp_path / "data"
    (data / "run").mkdir(parents=True)
    pid_file = tmp_path / "child.pid"
    ready = tmp_path / "child.ready"
    serve = subprocess.Popen([sys.executable, "-c", _SERVE, str(pid_file), str(ready), "abstractgateway-fake-serve"])
    assert _wait_for(lambda: pid_file.exists() and pid_file.read_text().strip().isdigit() and ready.exists())
    child = int(pid_file.read_text())
    (data / "run" / "gateway-serve.json").write_text(json.dumps({"pid": serve.pid}), encoding="utf-8")
    yield data, serve, child
    for pid in (serve.pid, child):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    serve.wait(timeout=5)


class _Launchd:
    """bootout: SIGTERM to the serve process, then return (launchd's own behaviour); print: gone."""

    def __init__(self, serve: subprocess.Popen) -> None:
        self.serve = serve
        self.calls: List[List[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ["launchctl", "bootout"]:
            self.serve.send_signal(signal.SIGTERM)
            return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")
        return subprocess.CompletedProcess(list(argv), 113, stdout="", stderr="not found")


def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os_service, "STOP_TREE_WAIT_S", 1.0)
    monkeypatch.setattr(process_tree, "TERM_WAIT_S", 1.0)
    monkeypatch.setattr(process_tree, "KILL_WAIT_S", 3.0)


def test_launchd_uninstall_waits_for_the_tree_and_kills_a_detached_child(tmp_path: Path, gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    data, serve, child = gateway
    _fast(monkeypatch)
    plan = os_service.build_uninstall_plan(platform="darwin", home=tmp_path / "home", data_dir=data, uid=os.getuid())
    assert [os_service.STOP_TREE_STEP, "1"] in plan.commands
    lines: List[str] = []
    results = os_service.execute_plan(plan, runner=_Launchd(serve), echo=lines.append)
    serve.wait(timeout=5)
    assert not _alive(serve.pid) and not _alive(child)
    out = next(r["stop_tree"] for r in results if "stop_tree" in r)
    running = {r["pid"] for r in out["running"]}
    assert {serve.pid, child} <= running
    assert serve.pid in out["exited"]  # launchd's SIGTERM was enough for the serve process
    # The detached child ignored SIGTERM: killed, and said so.
    assert child in out["killed"] and not out["survivors"]
    text = "\n".join(lines)
    assert "gateway processes running:" in text and f"sending SIGKILL: {child}" in text
    assert "the gateway process tree has exited" in text


def test_xdg_autostart_uninstall_stops_the_gateway_itself(tmp_path: Path, gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux without a systemd user manager: nothing stops an autostarted gateway, so the command does."""
    data, serve, child = gateway
    _fast(monkeypatch)
    home = tmp_path / "home"
    desktop = os_service.xdg_autostart_path(home, {})
    desktop.parent.mkdir(parents=True)
    desktop.write_text("[Desktop Entry]\n", encoding="utf-8")
    plan = os_service.build_uninstall_plan(platform="linux", home=home, data_dir=data, env={})
    assert plan.commands == [[os_service.STOP_TREE_STEP, "0"]]
    assert not any("keeps running" in n for n in plan.notes)
    results = os_service.execute_plan(plan, runner=lambda a: pytest.fail(f"no command expected: {a}"), echo=lambda _l: None)
    serve.wait(timeout=5)
    out = next(r["stop_tree"] for r in results if "stop_tree" in r)
    assert serve.pid in out["terminated"] and child in out["killed"]
    assert not _alive(serve.pid) and not _alive(child) and not desktop.exists()


def test_a_process_that_survives_fails_the_command_naming_it(tmp_path: Path, gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    data, serve, child = gateway
    _fast(monkeypatch)
    monkeypatch.setattr(process_tree, "KILL_WAIT_S", 0.5)
    real_kill = os.kill
    # The child cannot be signalled (another user's process, a stuck one): the command must say so.
    monkeypatch.setattr(process_tree, "_kill", lambda pid, sig: None if pid == child and sig else real_kill(pid, sig))
    plan = os_service.build_uninstall_plan(platform="darwin", home=tmp_path / "home", data_dir=data, uid=os.getuid())
    with pytest.raises(SystemExit) as e:
        os_service.execute_plan(plan, runner=_Launchd(serve), echo=lambda _l: None)
    assert "could not be stopped" in str(e.value) and str(child) in str(e.value)
    assert "abstractgateway service uninstall" in str(e.value)


def test_disable_never_stops_anything(tmp_path: Path) -> None:
    for plat in ("darwin", "linux"):
        plan = os_service.build_uninstall_plan(platform=plat, home=tmp_path / "h", data_dir=tmp_path / "d", uid=501, env={}, stop=False)
        assert not any(c and c[0] == os_service.STOP_TREE_STEP for c in plan.commands)


def test_the_tree_is_the_recorded_gateway_its_descendants_and_the_app_watcher(tmp_path: Path) -> None:
    data = tmp_path / "data"
    P = process_tree.Proc
    watcher = f"node -r {process_tree.app_watcher_marker(data)} server.js"
    procs = [
        P(1, 0, 0, "Ss", "launchd"),
        P(50, 1, 501, "S", "zsh"),
        P(60, 50, 501, "S", "abstractgateway service uninstall"),  # this command
        P(100, 1, 501, "S", "python -m abstractgateway.cli serve"),  # recorded serve pid
        P(101, 100, 501, "Ss", "python -m abstractruntime.identity.life"),  # detached, still its child
        P(102, 101, 501, "S", "ollama runner"),  # grandchild
        P(103, 100, 501, "Z", "python <defunct>"),  # exited
        P(200, 1, 501, "S", watcher),  # the data folder's app watcher (reparented)
        P(201, 200, 501, "S", "node server.js"),
        P(300, 1, 501, "S", "vim notes.txt"),  # unrelated
        P(400, 1, 502, "S", "python -m abstractgateway.cli serve"),  # another user's
        P(500, 1, 501, "S", "sleep 100"),  # a recycled recorded pid: not an abstract* program
    ]
    tree = process_tree.gateway_tree(procs, data_dir=data, seeds=[100, 400, 500], uid=501, self_pid=60)
    assert sorted(tree) == [100, 101, 102, 200, 201]
    # This command and its ancestors are never part of it, even if they descend from the gateway.
    procs.append(P(61, 102, 501, "S", "sh -c abstractgateway service uninstall"))
    tree = process_tree.gateway_tree(procs, data_dir=data, seeds=[100], uid=501, self_pid=61)
    assert 61 not in tree and 102 not in tree and 100 not in tree


def test_recorded_pids_read_the_serve_and_app_records(tmp_path: Path) -> None:
    run = tmp_path / "run"
    (run / "apps").mkdir(parents=True)
    (run / "gateway-serve.json").write_text(json.dumps({"pid": 4242}), encoding="utf-8")
    (run / "apps" / "code.json").write_text(json.dumps({"pid": 4343}), encoding="utf-8")
    (run / "apps" / "broken.json").write_text("{", encoding="utf-8")
    assert process_tree.recorded_pids(tmp_path) == [4242, 4343]
    assert process_tree.recorded_pids(tmp_path / "missing") == []


def test_the_dry_run_says_the_tree_is_stopped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from abstractgateway import firstrun_cli

    plan = os_service.build_uninstall_plan(platform="darwin", home=tmp_path / "h", data_dir=tmp_path / "d", uid=501)
    firstrun_cli._print_plan(plan)
    out = capsys.readouterr().out
    assert "run: launchctl bootout gui/501/ai.abstractframework.gateway" in out
    assert "stop: the gateway's processes and the children it started (wait up to 20 s" in out


def test_procfs_reads_the_whole_command_line_and_the_state(tmp_path: Path) -> None:
    """Linux's procfs, read without `ps`: a `comm` with spaces and parentheses, the state after the
    last ")", the uid from `status`, the NUL-separated command line whole (no terminal width)."""
    long_arg = "x" * 300
    for pid, comm, state, ppid, uid, argv in (
        (100, "python3", "S", 1, 1001, [b"/opt/py/bin/python3", b"-c", long_arg.encode(), b"abstractgateway-fake-serve"]),
        (101, "a (weird) name", "Z", 100, 1001, []),
    ):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} ({comm}) {state} {ppid} 1 1 0 -1\n", encoding="utf-8")
        (d / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n", encoding="utf-8")
        (d / "cmdline").write_bytes(b"\0".join(argv) + (b"\0" if argv else b""))
    (tmp_path / "self").mkdir()  # not a pid: skipped
    procs = {p.pid: p for p in process_tree._procfs_processes(tmp_path)}
    assert sorted(procs) == [100, 101]
    assert procs[100].ppid == 1 and procs[100].uid == 1001 and procs[100].stat == "S"
    assert procs[100].args.endswith("abstractgateway-fake-serve") and long_arg in procs[100].args
    assert procs[101].zombie and procs[101].ppid == 100 and procs[101].args == ""
    assert process_tree._procfs_processes(tmp_path / "missing") is None

