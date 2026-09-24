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
    # Plain `serve`: the Network setting binds it (2026-09-24, mission T).
    assert pl["ProgramArguments"] == [*_exe(home), "serve"]
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
    assert "ExecStart=%h/.local/bin/abstractgateway serve\n" in unit
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


def test_windows_run_value_is_experimental_hidden_and_replaces_the_old_shortcut(tmp_path: Path) -> None:
    home = tmp_path / "Users" / "u"
    appdata = str(tmp_path / "Roaming")
    data = tmp_path / "Local" / "My Data" / "AbstractGateway"
    exe = [str(tmp_path / "venv" / "Scripts" / "python.exe"), "-m", "abstractgateway"]
    plan = os_service.build_install_plan(platform="win32", home=home, host="127.0.0.1", port=8080, data_dir=data, exe_argv=exe, env={"APPDATA": appdata})
    assert plan.experimental is True and plan.mechanism == "registry-run"
    assert plan.files == []
    set_op, approved_op = plan.registry
    assert set_op["op"] == "set" and set_op["key"] == r"Software\Microsoft\Windows\CurrentVersion\Run" and set_op["name"] == "AbstractGateway"
    argv = os_service.windows_split(set_op["value"])
    assert argv[0].endswith("pythonw.exe") and argv[1:4] == ["-m", "abstractgateway.os_service", "launch"]
    assert argv[argv.index("--data-dir") + 1] == str(data)  # a path with a space survives the round trip
    assert argv[-2:] == ["--", "serve"]
    assert approved_op == {"op": "delete", "key": r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run", "name": "AbstractGateway", "missing_ok": True}
    shortcut = str(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "AbstractGateway.lnk")
    assert shortcut in plan.remove, "an older Startup shortcut would start a second gateway"
    start = plan.commands[0]
    assert start[0] == "@start-detached" and start[1].endswith("pythonw.exe")
    assert any("EXPERIMENTAL" in n for n in plan.notes)


@pytest.mark.parametrize(
    "argv",
    [
        ["C:\\Program Files\\Py\\pythonw.exe", "-m", "x", "--log-file", "C:\\Users\\A B\\log.txt"],
        ["C:\\p.exe", 'say "hi"', "trailing\\", "a\\\\b", ""],
        ["C:\\dir with space\\", "x"],
    ],
)
def test_windows_command_line_round_trips(argv: List[str]) -> None:
    assert os_service.windows_split(os_service.windows_join(argv)) == argv


def test_xdg_exec_round_trips_quotes_dollars_and_percent(tmp_path: Path) -> None:
    argv = ["/usr/bin/env", "PATH=/a b:/c", "/opt/py $x/bin/python", "-m", "m", "--data-dir", '/srv/"q"/100%']
    content = os_service.render_xdg_desktop(launch_argv=argv)
    exec_line = next(line for line in content.splitlines() if line.startswith("Exec="))
    assert "100%%" in exec_line
    assert os_service.desktop_exec_split(exec_line[len("Exec="):]) == argv


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
    assert os_service.without_start(wplan) == [] and wplan.registry, "the Run value is the registration; only the start is skipped"
    xplan = os_service.build_install_plan(platform="linux", home=home, host="127.0.0.1", port=8080, data_dir=tmp_path / "d", exe_argv=_exe(home), env={}, linux_mechanism="xdg")
    assert os_service.without_start(xplan) == [] and xplan.files


def test_port_choice_prefers_flag_then_persisted_then_first_free() -> None:
    busy = {8080, 8081}
    free = lambda _h, p: p not in busy  # noqa: E731
    assert os_service.choose_port(host="127.0.0.1", requested=9000, persisted=8090, free=free)["port"] == 9000
    assert os_service.choose_port(host="127.0.0.1", requested=None, persisted=8090, free=free)["port"] == 8090
    chosen = os_service.choose_port(host="127.0.0.1", requested=None, persisted=None, free=free)
    assert chosen == {"port": 8082, "source": "probe", "busy_skipped": [8080, 8081]}
    with pytest.raises(SystemExit):
        os_service.choose_port(host="127.0.0.1", requested=None, persisted=None, free=lambda _h, _p: False)


def test_non_loopback_pinned_install_warns_about_auth(tmp_path: Path) -> None:
    plan = os_service.build_install_plan(platform="linux", home=tmp_path, host="0.0.0.0", port=8080, data_dir=tmp_path / "d", exe_argv=_exe(tmp_path), env={}, pinned=True)
    assert any("refuses to start without explicit auth" in n for n in plan.notes)
    assert any("--pin-command-line" in n and "does NOT apply" in n for n in plan.notes)


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


# ---------------------------------------------------------------------------
# Mission T (2026-09-24): the login item runs plain `serve`; the bind is the
# Network setting, seeded at install/enable; --pin-command-line is the old shape.
# ---------------------------------------------------------------------------


def _setting(data: Path) -> dict:
    from abstractgateway.runtime_config import resolve_network_setting

    return resolve_network_setting(data, env={})


_QUIET = {"discover": lambda: ([], "test"), "hostname_fn": lambda: None}


@pytest.mark.parametrize("platform", ["darwin", "linux", "linux-xdg", "win32"])
def test_every_os_registers_plain_serve_unless_pinned(tmp_path: Path, platform: str) -> None:
    home = tmp_path / "home"
    plat, lm = ("linux", "xdg") if platform == "linux-xdg" else (platform, None)
    env = {"APPDATA": str(tmp_path / "Roaming")}

    def served(plan) -> List[str]:
        if plan.registry:
            argv = os_service.windows_split(plan.registry[0]["value"])
        elif plan.files[0]["path"].endswith(".plist"):
            argv = plistlib.loads(plan.files[0]["content"].encode())["ProgramArguments"]
        elif plan.files[0]["path"].endswith(".desktop"):
            line = next(x for x in plan.files[0]["content"].splitlines() if x.startswith("Exec="))
            argv = os_service.desktop_exec_split(line[5:])
        else:
            line = next(x for x in plan.files[0]["content"].splitlines() if x.startswith("ExecStart="))
            argv = line[len("ExecStart="):].split(" ")
        return argv[argv.index("serve"):]

    kw = dict(platform=plat, home=home, host="127.0.0.1", port=18871, data_dir=tmp_path / "d", exe_argv=_exe(home), uid=501, env=env, linux_mechanism=lm)
    plain = os_service.build_install_plan(**kw)
    assert served(plain) == ["serve"]
    assert plain.public_dict()["bind_source"] == "network_setting" and plain.public_dict()["serve_args"] == ["serve"]
    pinned = os_service.build_install_plan(**kw, pinned=True)
    assert served(pinned) == ["serve", "--host", "127.0.0.1", "--port", "18871"]
    assert pinned.public_dict()["pinned_command_line"] is True


def test_resolve_seeds_localhost_on_the_first_free_port_like_the_old_default(tmp_path: Path) -> None:
    free = lambda _h, p: p != 8080  # noqa: E731
    bind = os_service.resolve_service_bind(tmp_path, env={}, free=free)
    assert bind["pinned"] is False and bind["host"] == "127.0.0.1" and bind["port"] == 8081
    assert bind["mode"] == "localhost" and bind["mode_source"] == "default" and bind["busy_skipped"] == [8080]
    assert bind["seed"] == {"mode": "localhost", "port": 8081}
    out = os_service.seed_network_setting(tmp_path, bind, actor="test", env={}, status_kwargs=_QUIET)
    assert out["action"] == "seeded"
    st = _setting(tmp_path)
    assert (st["mode"], st["source"], st["port"], st["port_source"]) == ("localhost", "stored", 8081, "stored")
    # Stored now: a second resolve has nothing to write.
    again = os_service.resolve_service_bind(tmp_path, env={}, free=free)
    assert again["seed"] is None and again["port_source"] == "stored"
    assert os_service.seed_network_setting(tmp_path, again, actor="test", env={})["action"] == "unchanged"


def test_resolve_keeps_a_stored_lan_and_its_port(tmp_path: Path) -> None:
    from abstractgateway.network_exposure import apply_network_change

    assert apply_network_change(tmp_path, mode="lan", port=18872, actor="t", env={}, in_process=False, status_kwargs=_QUIET)[0] == 200
    bind = os_service.resolve_service_bind(tmp_path, running_port=9999, env={}, free=lambda *_: pytest.fail("no probe"))
    assert (bind["mode"], bind["host"], bind["port"], bind["seed"]) == ("lan", "0.0.0.0", 18872, None)


def test_host_and_port_flags_go_into_the_setting(tmp_path: Path) -> None:
    from abstractgateway.network_exposure import apply_network_change

    apply_network_change(tmp_path, mode="lan", port=18872, actor="t", env={}, in_process=False, status_kwargs=_QUIET)
    bind = os_service.resolve_service_bind(tmp_path, host="127.0.0.1", port=18873, env={})
    assert bind["seed"] == {"mode": "localhost", "port": 18873} and bind["mode_source"] == "flag"
    os_service.seed_network_setting(tmp_path, bind, actor="t", env={}, status_kwargs=_QUIET)
    assert (_setting(tmp_path)["mode"], _setting(tmp_path)["port"]) == ("localhost", 18873)
    wild = os_service.resolve_service_bind(tmp_path, host="0.0.0.0", env={})
    assert wild["mode"] == "lan" and wild["port"] == 18873
    with pytest.raises(SystemExit) as e:
        os_service.resolve_service_bind(tmp_path, host="192.168.1.20", env={})
    assert "--pin-command-line" in str(e.value)


def test_pin_command_line_never_touches_the_setting(tmp_path: Path) -> None:
    bind = os_service.resolve_service_bind(tmp_path, host="192.168.1.20", port=18874, pin_command_line=True, env={})
    assert bind["pinned"] is True and bind["seed"] is None and (bind["host"], bind["port"]) == ("192.168.1.20", 18874)
    assert os_service.seed_network_setting(tmp_path, bind, actor="t", env={})["action"] == "pinned"
    assert _setting(tmp_path)["source"] == "default"


def test_a_refused_mode_is_loud_and_writes_nothing(tmp_path: Path) -> None:
    # A token-only posture stated by the operator: `lan` needs user auth.
    env = {"ABSTRACTGATEWAY_AUTH_TOKEN": "x" * 40, "ABSTRACTGATEWAY_USER_AUTH": "0"}
    bind = os_service.resolve_service_bind(tmp_path, host="0.0.0.0", env=env)
    with pytest.raises(SystemExit) as e:
        os_service.seed_network_setting(tmp_path, bind, actor="t", env=env, status_kwargs=_QUIET)
    assert "refused 'lan'" in str(e.value) and "Nothing was registered" in str(e.value)
    assert _setting(tmp_path)["source"] == "default"


def _install_args(**over):
    import argparse

    base = dict(service_cmd="install", data_dir=None, json=True, host=None, port=18875, pin_command_line=False, dry_run=False, no_start=False, no_wait=True, wait_s=1.0, no_claim=True)
    base.update(over)
    return argparse.Namespace(**base)


def test_cli_install_seeds_the_setting_and_records_the_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from abstractgateway import network_exposure
    from abstractgateway.firstrun_cli import run_service

    home, data = tmp_path / "home", tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setattr(network_exposure, "discover_interfaces", _QUIET["discover"])
    monkeypatch.setattr(network_exposure, "bonjour_hostname", _QUIET["hostname_fn"])
    rec = _Recorder({("launchctl", "bootout"): 36})
    monkeypatch.setattr(os_service, "_default_runner", rec)
    monkeypatch.setattr(os_service, "current_gateway_argv", lambda platform=None: _exe(home))

    assert run_service(_install_args(), platform="darwin", home=home) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["serve_args"] == ["serve"] and out["network_seed"]["action"] == "seeded"
    pl = plistlib.loads(Path(out["files"][0]["path"]).read_bytes())
    assert pl["ProgramArguments"][1:] == ["serve"]
    assert (_setting(data)["mode"], _setting(data)["port"]) == ("localhost", 18875)
    assert os_service.read_service_record(data)["pinned"] is False

    assert run_service(_install_args(port=18876, host="127.0.0.1", pin_command_line=True), platform="darwin", home=home) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["serve_args"] == ["serve", "--host", "127.0.0.1", "--port", "18876"]
    assert out["network_seed"]["action"] == "pinned" and _setting(data)["port"] == 18875
    assert os_service.read_service_record(data)["pinned"] is True


def test_cli_dry_run_reports_the_seed_and_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from abstractgateway.firstrun_cli import run_service

    home, data = tmp_path / "home", tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setattr(os_service, "_default_runner", lambda argv: pytest.fail(f"dry-run ran {argv}"))
    assert run_service(_install_args(dry_run=True, json=False), platform="linux", home=home) == 0
    text = capsys.readouterr().out
    assert "Network setting: stores 'localhost'" in text and "on port 18875" in text
    assert " serve --host" not in text and not data.exists()


def test_service_parser_accepts_pin_command_line_and_defaults_host_to_none() -> None:
    import argparse

    from abstractgateway.firstrun_cli import add_service_subparser

    parser = argparse.ArgumentParser()
    add_service_subparser(parser.add_subparsers(dest="cmd"))
    for verb in ("install", "enable"):
        ns = parser.parse_args(["service", verb])
        assert ns.host is None and ns.port is None and ns.pin_command_line is False
        ns = parser.parse_args(["service", verb, "--host", "0.0.0.0", "--port", "9", "--pin-command-line"])
        assert (ns.host, ns.port, ns.pin_command_line) == ("0.0.0.0", 9, True)
