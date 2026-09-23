"""`abstractgateway service install|uninstall|status` (first-run, 2026-09-23).

Every OS's artifact is rendered on every OS through the pure renderers and a
monkeypatched platform/home: no launchctl, systemctl or PowerShell ever runs
here (a recording runner stands in), and nothing is written outside tmp_path.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
from pathlib import Path
from typing import List

import pytest

from abstractgateway import os_service

pytestmark = pytest.mark.basic


class _Recorder:
    def __init__(self, rc_by_prefix=None) -> None:
        self.calls: List[List[str]] = []
        self.rc_by_prefix = rc_by_prefix or {}

    def __call__(self, argv):
        self.calls.append(list(argv))
        rc = 0
        for prefix, code in self.rc_by_prefix.items():
            if tuple(argv[: len(prefix)]) == prefix:
                rc = code
        return subprocess.CompletedProcess(list(argv), rc, stdout="", stderr="" if rc == 0 else "not loaded")


def _exe(home: Path) -> List[str]:
    return [str(home / ".local" / "bin" / "abstractgateway")]


def test_launchd_plist(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "u"
    data = home / "Library" / "Application Support" / "AbstractGateway"
    plan = os_service.build_install_plan(platform="darwin", home=home, host="127.0.0.1", port=8080, data_dir=data, exe_argv=_exe(home), uid=501)
    assert plan.files[0]["path"] == str(home / "Library" / "LaunchAgents" / "ai.abstractframework.gateway.plist")
    pl = plistlib.loads(plan.files[0]["content"].encode("utf-8"))
    assert pl["Label"] == "ai.abstractframework.gateway"
    assert pl["ProgramArguments"] == [*_exe(home), "serve", "--host", "127.0.0.1", "--port", "8080"]
    assert Path(pl["ProgramArguments"][0]).is_absolute()
    assert pl["RunAtLoad"] is True and pl["KeepAlive"] == {"SuccessfulExit": False}
    env = pl["EnvironmentVariables"]
    path = env["PATH"].split(":")
    for needed in (str(home / ".local" / "bin"), str(home / ".lmstudio" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"):
        assert needed in path
    assert env["ABSTRACTGATEWAY_DATA_DIR"] == str(data)
    assert pl["WorkingDirectory"] == str(data)
    assert pl["StandardErrorPath"] == str(home / "Library" / "Logs" / "AbstractGateway" / "gateway.err.log")
    assert plan.commands == [
        ["launchctl", "bootout", "gui/501/ai.abstractframework.gateway"],
        ["launchctl", "bootstrap", "gui/501", plan.files[0]["path"]],
    ]
    assert str(home / "Library" / "Logs" / "AbstractGateway") in plan.dirs and str(data) in plan.dirs


def test_systemd_user_unit(tmp_path: Path) -> None:
    home = tmp_path / "home" / "u"
    data = home / ".local" / "share" / "abstractgateway"
    plan = os_service.build_install_plan(platform="linux", home=home, host="127.0.0.1", port=8081, data_dir=data, exe_argv=_exe(home), env={})
    assert plan.files[0]["path"] == str(home / ".config" / "systemd" / "user" / "abstractgateway.service")
    unit = plan.files[0]["content"]
    assert "ExecStart=%h/.local/bin/abstractgateway serve --host 127.0.0.1 --port 8081" in unit
    assert "Environment=ABSTRACTGATEWAY_DATA_DIR=%h/.local/share/abstractgateway" in unit
    assert "Environment=PATH=%h/.local/bin:%h/.lmstudio/bin:/usr/local/bin" in unit
    assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit
    assert "/opt/homebrew" not in unit
    assert plan.commands == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "abstractgateway.service"],
    ]
    assert any("loginctl enable-linger" in n for n in plan.notes)


def test_systemd_quotes_paths_with_spaces(tmp_path: Path) -> None:
    home = tmp_path / "h"
    plan = os_service.build_install_plan(
        platform="linux", home=home, host="127.0.0.1", port=8080, data_dir=Path("/srv/My Data/gw"), exe_argv=["/opt/py 3/bin/python", "-m", "abstractgateway"], env={}
    )
    unit = plan.files[0]["content"]
    assert 'ExecStart="/opt/py 3/bin/python" -m abstractgateway serve' in unit
    assert 'WorkingDirectory="/srv/My Data/gw"' in unit


def test_windows_startup_shortcut_is_experimental_and_hidden(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "u"
    appdata = str(tmp_path / "Roaming")
    data = tmp_path / "Local" / "AbstractGateway"
    exe = [str(tmp_path / "venv" / "Scripts" / "python.exe"), "-m", "abstractgateway"]
    plan = os_service.build_install_plan(platform="win32", home=home, host="127.0.0.1", port=8080, data_dir=data, exe_argv=exe, env={"APPDATA": appdata})
    assert plan.experimental is True
    assert plan.files == []
    ps = plan.commands[0]
    assert ps[:2] == ["powershell", "-NoProfile"] and "Bypass" in ps
    script = ps[-1]
    shortcut = str(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "AbstractGateway.lnk")
    assert shortcut in script
    assert "pythonw.exe" in script and "abstractgateway.os_service launch" in script
    assert "--port 8080" in script
    start = plan.commands[1]
    assert start[0] == "@start-detached" and start[1].endswith("pythonw.exe")
    assert any("EXPERIMENTAL" in n for n in plan.notes)


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_dry_run_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], platform: str) -> None:
    from abstractgateway.firstrun_cli import run_service
    import argparse

    home = tmp_path / "home"
    data = tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setattr(os_service, "_default_runner", lambda argv: pytest.fail(f"dry-run ran {argv}"))
    args = argparse.Namespace(service_cmd="install", data_dir=None, json=True, host="127.0.0.1", port=18777, dry_run=True, no_start=False, no_wait=True, wait_s=1.0, no_claim=True)
    assert run_service(args, platform=platform, home=home) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True and out["port"] == 18777
    assert out["console_url"] == "http://127.0.0.1:18777/console"
    assert not home.exists() and not data.exists()


def test_install_then_status_then_uninstall_with_a_recording_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    plan = os_service.build_install_plan(platform="darwin", home=home, host="127.0.0.1", port=18080, data_dir=data, exe_argv=_exe(home), uid=501)
    # `bootout` of a label that is not loaded fails: that is tolerated.
    rec = _Recorder({("launchctl", "bootout"): 36})
    os_service.execute_plan(plan, runner=rec, echo=lambda _l: None)
    os_service.write_service_record(plan)
    unit = Path(plan.files[0]["path"])
    assert unit.exists() and (home / "Library" / "Logs" / "AbstractGateway").is_dir() and data.is_dir()
    assert [c[:2] for c in rec.calls] == [["launchctl", "bootout"], ["launchctl", "bootstrap"]]

    st = os_service.service_status(platform="darwin", home=home, data_dir=data, probe=True, runner=_Recorder(), uid=501)
    assert st["installed"] is True and st["loaded"] is True and st["port"] == 18080
    assert st["url"] == "http://127.0.0.1:18080"

    un = os_service.build_uninstall_plan(platform="darwin", home=home, data_dir=data, uid=501)
    rec2 = _Recorder()
    os_service.execute_plan(un, runner=rec2, echo=lambda _l: None)
    assert rec2.calls == [["launchctl", "bootout", "gui/501/ai.abstractframework.gateway"]]
    assert not unit.exists() and not os_service.service_record_path(data).exists()
    assert data.is_dir()  # data is kept


def test_a_failing_start_command_is_loud(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = os_service.build_install_plan(platform="linux", home=home, host="127.0.0.1", port=18080, data_dir=tmp_path / "d", exe_argv=_exe(home), env={})
    with pytest.raises(SystemExit) as e:
        os_service.execute_plan(plan, runner=_Recorder({("systemctl", "--user", "enable"): 1}), echo=lambda _l: None)
    assert "service install failed" in str(e.value)


def test_no_start_keeps_registration_but_skips_starting(tmp_path: Path) -> None:
    home = tmp_path / "h"
    for platform, expected in (
        ("darwin", []),
        ("linux", [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "abstractgateway.service"]]),
    ):
        plan = os_service.build_install_plan(platform=platform, home=home, host="127.0.0.1", port=8080, data_dir=tmp_path / "d", exe_argv=_exe(home), uid=501, env={})
        assert os_service.without_start(plan) == expected
    wplan = os_service.build_install_plan(platform="win32", home=home, host="127.0.0.1", port=8080, data_dir=tmp_path / "d", exe_argv=_exe(home), env={"APPDATA": str(tmp_path)})
    kept = os_service.without_start(wplan)
    assert len(kept) == 1 and kept[0][0] == "powershell"


def test_port_choice_prefers_flag_then_persisted_then_first_free() -> None:
    busy = {8080, 8081}
    free = lambda _h, p: p not in busy  # noqa: E731
    assert os_service.choose_port(host="127.0.0.1", requested=9000, persisted=8090, free=free)["port"] == 9000
    assert os_service.choose_port(host="127.0.0.1", requested=None, persisted=8090, free=free)["port"] == 8090
    chosen = os_service.choose_port(host="127.0.0.1", requested=None, persisted=None, free=free)
    assert chosen == {"port": 8082, "source": "probe", "busy_skipped": [8080, 8081]}
    with pytest.raises(SystemExit):
        os_service.choose_port(host="127.0.0.1", requested=None, persisted=None, free=lambda _h, _p: False)


def test_non_loopback_install_warns_about_auth(tmp_path: Path) -> None:
    plan = os_service.build_install_plan(platform="linux", home=tmp_path, host="0.0.0.0", port=8080, data_dir=tmp_path / "d", exe_argv=_exe(tmp_path), env={})
    assert any("refuses to start without explicit auth" in n for n in plan.notes)


def test_windows_launcher_redirects_output_and_runs_the_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from abstractgateway import cli as gateway_cli

    seen: dict = {}

    def _fake_main(argv):
        seen["argv"] = list(argv)
        print("hello from the gateway")
        print("and its stderr", file=sys.stderr)

    monkeypatch.setattr(gateway_cli, "main", _fake_main)
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    log = tmp_path / "logs" / "gateway.log"
    os_service.launch_main(["launch", "--log-file", str(log), "--data-dir", str(tmp_path / "d"), "--", "serve", "--host", "127.0.0.1", "--port", "8080"])
    sys.stdout.flush()
    assert seen["argv"] == ["serve", "--host", "127.0.0.1", "--port", "8080"]
    text = log.read_text(encoding="utf-8")
    assert "hello from the gateway" in text and "and its stderr" in text


def test_current_gateway_argv_is_absolute() -> None:
    argv = os_service.current_gateway_argv()
    assert Path(argv[0]).is_absolute()
