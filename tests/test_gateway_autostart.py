"""Start at login (`abstractgateway.autostart`), the switch the tray, the CLI and
the installers share (2026-09-24).

Every OS runs on every OS: launchctl / systemctl are a recording runner, the
Windows registry is a dict, a detached start is a recorder, HOME is tmp_path.
Nothing here can register a real login item.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from abstractgateway import autostart, os_service

pytestmark = pytest.mark.basic


class Runner:
    """Records argv; answers per argv prefix with (rc, stdout); a prefix mapped
    to None raises FileNotFoundError (the tool is not installed)."""

    def __init__(self, answers: Optional[Dict[Tuple[str, ...], Any]] = None) -> None:
        self.calls: List[List[str]] = []
        self.answers = answers or {}

    def __call__(self, argv):
        self.calls.append(list(argv))
        best: Any = (0, "")
        best_len = -1
        for prefix, ans in self.answers.items():
            if tuple(argv[: len(prefix)]) == prefix and len(prefix) > best_len:
                best, best_len = ans, len(prefix)
        if best is None:
            raise FileNotFoundError(argv[0])
        rc, out = best
        return subprocess.CompletedProcess(list(argv), rc, stdout=out, stderr="" if rc == 0 else "error")

    def mutating(self) -> List[List[str]]:
        """Calls that CHANGE the service manager (probes filtered out)."""
        probes = {("launchctl", "print"), ("launchctl", "print-disabled"), ("systemctl", "--user", "is-enabled"), ("systemctl", "--user", "is-active"), ("systemctl", "--user", "show-environment")}
        return [c for c in self.calls if not any(tuple(c[: len(p)]) == p for p in probes)]


class Registry:
    def __init__(self) -> None:
        self.values: Dict[Tuple[str, str], Any] = {}
        self.log: List[Tuple[str, str, str]] = []

    def get(self, key, name):
        return self.values.get((key, name))

    def set_string(self, key, name, value):
        self.log.append(("set", key, name))
        self.values[(key, name)] = value

    def delete(self, key, name):
        self.log.append(("delete", key, name))
        return self.values.pop((key, name), None) is not None


def _exe(tmp_path: Path, name: str = "abstractgateway") -> Path:
    p = tmp_path / "venv" / "bin" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\n", encoding="utf-8")
    p.chmod(0o755)
    return p


# --------------------------------------------------------------------- macOS


def test_macos_enable_writes_the_exact_agent_registers_for_next_login_only_and_disable_removes_it(tmp_path: Path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    exe = _exe(tmp_path)
    run = Runner({("launchctl", "print"): (113, "")})  # not loaded (we never bootstrap)
    assert autostart.autostart_status(data_dir=data, platform="darwin", home=home, runner=run, uid=501)["state"] == "off"

    out = autostart.enable_autostart(data_dir=data, host="127.0.0.1", port=18840, platform="darwin", home=home, exe_argv=[str(exe)], runner=run, uid=501, env={})
    assert out["ok"] is True, out["error"]
    plist = home / "Library" / "LaunchAgents" / "ai.abstractframework.gateway.plist"
    pl = plistlib.loads(plist.read_bytes())
    assert pl["ProgramArguments"] == [str(exe), "serve"]  # the Network setting binds it
    assert pl["EnvironmentVariables"]["ABSTRACTGATEWAY_DATA_DIR"] == str(data) and pl["RunAtLoad"] is True
    # The tray's own gateway is running: enabling must NOT start a second one.
    assert run.mutating() == [], run.calls
    after = out["after"]
    assert after["state"] == "on" and after["registered_port"] is None and after["mechanism"] == "launchd-agent"
    assert after["bind_source"] == "network_setting" and after["network_setting"]["port"] == 18840
    assert os_service.read_service_record(data)["port"] == 18840

    off = autostart.disable_autostart(data_dir=data, platform="darwin", home=home, runner=run, uid=501, env={})
    assert off["ok"] is True and not plist.exists()
    # Disabling must not bootout: that would kill a launchd-started gateway (the tray's parent).
    assert run.mutating() == []
    assert os_service.read_service_record(data) is None and data.is_dir()


def test_macos_cli_install_path_still_starts_now(tmp_path: Path) -> None:
    run = Runner()
    out = autostart.enable_autostart(data_dir=tmp_path / "d", host="127.0.0.1", port=18840, platform="darwin", home=tmp_path / "h", exe_argv=[str(_exe(tmp_path))], runner=run, uid=501, env={}, start_now=True)
    assert out["ok"] and [c[:2] for c in run.mutating()] == [["launchctl", "bootout"], ["launchctl", "bootstrap"]]


def test_a_stale_agent_pointing_at_a_removed_binary_is_broken_not_on_and_enable_repairs_it(tmp_path: Path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    gone = tmp_path / "old-venv" / "bin" / "abstractgateway"
    plan = os_service.build_install_plan(platform="darwin", home=home, host="127.0.0.1", port=8080, data_dir=data, exe_argv=[str(gone)], uid=501, env={})
    Path(plan.files[0]["path"]).parent.mkdir(parents=True)
    Path(plan.files[0]["path"]).write_text(plan.files[0]["content"], encoding="utf-8")
    st = autostart.autostart_status(data_dir=data, platform="darwin", home=home, runner=Runner(), uid=501)
    assert st["state"] == "broken"
    assert "the program it starts is gone" in st["summary"] and str(gone) in st["summary"]

    fixed = autostart.enable_autostart(data_dir=data, host="127.0.0.1", port=8080, platform="darwin", home=home, exe_argv=[str(_exe(tmp_path))], runner=Runner(), uid=501, env={})
    assert fixed["before"]["state"] == "broken" and fixed["after"]["state"] == "on"


def test_macos_unreadable_plist_other_data_dir_and_launchctl_disable_are_named(tmp_path: Path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    exe = _exe(tmp_path)
    plist = home / "Library" / "LaunchAgents" / "ai.abstractframework.gateway.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("not a plist", encoding="utf-8")
    st = autostart.autostart_status(data_dir=data, platform="darwin", home=home, runner=Runner(), uid=501)
    assert st["state"] == "broken" and "unreadable" in st["summary"]

    plan = os_service.build_install_plan(platform="darwin", home=home, host="127.0.0.1", port=8080, data_dir=tmp_path / "another", exe_argv=[str(exe)], uid=501, env={})
    plist.write_text(plan.files[0]["content"], encoding="utf-8")
    st = autostart.autostart_status(data_dir=data, platform="darwin", home=home, runner=Runner(), uid=501)
    assert st["state"] == "other" and str(tmp_path / "another") in st["summary"]

    disabled = Runner({("launchctl", "print-disabled"): (0, 'disabled services = {\n\t"ai.abstractframework.gateway" => disabled\n}')})
    st = autostart.autostart_status(data_dir=tmp_path / "another", platform="darwin", home=home, runner=disabled, uid=501)
    assert st["state"] == "broken" and "launchctl disable" in st["summary"]


# --------------------------------------------------------------------- Linux


def test_linux_systemd_enable_is_enable_without_now_and_status_reads_is_enabled(tmp_path: Path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    exe = _exe(tmp_path)
    run = Runner({("systemctl", "--user", "is-enabled"): (0, "enabled\n")})
    out = autostart.enable_autostart(data_dir=data, host="127.0.0.1", port=18840, platform="linux", home=home, exe_argv=[str(exe)], runner=run, env={})
    assert out["ok"], out["error"]
    unit = home / ".config" / "systemd" / "user" / "abstractgateway.service"
    assert f"ExecStart={exe} serve\n" in unit.read_text()
    # `enable` WITHOUT --now: the gateway serving the tray is already running.
    assert run.mutating() == [["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "abstractgateway.service"]]
    assert out["after"]["state"] == "on" and out["after"]["registered_data_dir"] == str(data)
    # %h is expanded when read back (a unit under $HOME is written %h-relative).
    inside = autostart.enable_autostart(data_dir=home / "d", host="127.0.0.1", port=1, platform="linux", home=home, exe_argv=[str(exe)], runner=run, env={})
    assert "%h/d" in unit.read_text() and inside["after"]["registered_data_dir"] == str(home / "d") and inside["after"]["state"] == "on"
    autostart.enable_autostart(data_dir=data, host="127.0.0.1", port=18840, platform="linux", home=home, exe_argv=[str(exe)], runner=run, env={})

    not_enabled = Runner({("systemctl", "--user", "is-enabled"): (1, "disabled\n")})
    st = autostart.autostart_status(data_dir=data, platform="linux", home=home, runner=not_enabled)
    assert st["state"] == "broken" and "disabled" in st["summary"]

    run2 = Runner()
    off = autostart.disable_autostart(data_dir=data, platform="linux", home=home, runner=run2, env={})
    assert off["ok"] and not unit.exists()
    assert run2.mutating() == [["systemctl", "--user", "disable", "abstractgateway.service"], ["systemctl", "--user", "daemon-reload"]]


def test_linux_without_systemd_falls_back_to_an_xdg_autostart_entry(tmp_path: Path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    py = _exe(tmp_path, "python3")
    no_systemd = Runner({("systemctl",): None})
    assert autostart.detect_linux_mechanism(no_systemd) == "xdg"
    out = autostart.enable_autostart(data_dir=data, host="127.0.0.1", port=18840, platform="linux", home=home, exe_argv=[str(py), "-m", "abstractgateway"], runner=no_systemd, env={})
    assert out["ok"], out["error"]
    desktop = home / ".config" / "autostart" / "abstractgateway.desktop"
    exec_line = next(line for line in desktop.read_text().splitlines() if line.startswith("Exec="))
    argv = os_service.desktop_exec_split(exec_line[5:])
    assert argv[0] == "/usr/bin/env" and f"ABSTRACTGATEWAY_DATA_DIR={data}" in argv
    assert argv[argv.index(str(py)) + 1 : argv.index(str(py)) + 4] == ["-m", "abstractgateway.os_service", "launch"]
    assert argv[-2:] == ["--", "serve"]
    assert out["after"]["state"] == "on" and out["after"]["mechanism"] == "xdg-autostart"
    assert [c for c in no_systemd.calls if c[0] != "systemctl"] == []

    desktop.write_text(desktop.read_text().replace("X-GNOME-Autostart-enabled=true", "X-GNOME-Autostart-enabled=false"))
    st = autostart.autostart_status(data_dir=data, platform="linux", home=home, runner=no_systemd)
    assert st["state"] == "broken" and "Startup Applications" in st["summary"]

    off = autostart.disable_autostart(data_dir=data, platform="linux", home=home, runner=no_systemd, env={})
    assert off["ok"] and not desktop.exists()


def test_two_linux_registrations_are_broken_because_two_gateways_would_start(tmp_path: Path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    py = _exe(tmp_path, "python3")
    autostart.enable_autostart(data_dir=data, host="127.0.0.1", port=1, platform="linux", home=home, exe_argv=[str(py), "-m", "abstractgateway"], runner=Runner({("systemctl",): None}), env={})
    unit_plan = os_service.build_install_plan(platform="linux", home=home, host="127.0.0.1", port=1, data_dir=data, exe_argv=[str(py), "-m", "abstractgateway"], env={})
    Path(unit_plan.files[0]["path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(unit_plan.files[0]["path"]).write_text(unit_plan.files[0]["content"])
    st = autostart.autostart_status(data_dir=data, platform="linux", home=home, runner=Runner({("systemctl", "--user", "is-enabled"): (0, "enabled")}))
    assert st["state"] == "broken" and "two would start" in st["summary"] + " ".join(st["problems"])
    # Enabling with systemd removes the XDG leftover.
    out = autostart.enable_autostart(data_dir=data, host="127.0.0.1", port=1, platform="linux", home=home, exe_argv=[str(py), "-m", "abstractgateway"], runner=Runner({("systemctl", "--user", "is-enabled"): (0, "enabled")}), env={})
    assert out["ok"] and not (home / ".config" / "autostart" / "abstractgateway.desktop").exists()


# ------------------------------------------------------------------- Windows


def _win_python(tmp_path: Path) -> Path:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for n in ("python.exe", "pythonw.exe"):
        (scripts / n).write_text("", encoding="utf-8")
    return scripts / "python.exe"


def test_windows_enable_writes_the_run_value_clears_task_manager_veto_and_the_old_shortcut(tmp_path: Path) -> None:
    home, data = tmp_path / "home", tmp_path / "Local" / "AbstractGateway"
    reg = Registry()
    reg.values[(os_service.WINDOWS_STARTUP_APPROVED_KEY, "AbstractGateway")] = bytes([3]) + bytes(11)
    appdata = tmp_path / "Roaming"
    legacy = os_service.windows_startup_shortcut_path(home, {"APPDATA": str(appdata)})
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"L\x00\x00\x00")
    env = {"APPDATA": str(appdata)}
    assert autostart.autostart_status(data_dir=data, platform="win32", home=home, registry=reg, env=env)["mechanism"] == "startup-shortcut"

    spawned: List[List[str]] = []
    out = autostart.enable_autostart(data_dir=data, host="127.0.0.1", port=18840, platform="win32", home=home, exe_argv=[str(_win_python(tmp_path)), "-m", "abstractgateway"], registry=reg, env=env, start_detached=lambda a: spawned.append(list(a)))
    assert out["ok"], out["error"]
    value = reg.values[(os_service.WINDOWS_RUN_KEY, "AbstractGateway")]
    argv = os_service.windows_split(value)
    assert argv[0] == str(tmp_path / "venv" / "Scripts" / "pythonw.exe")
    assert argv[argv.index("--data-dir") + 1] == str(data)
    assert (os_service.WINDOWS_STARTUP_APPROVED_KEY, "AbstractGateway") not in reg.values
    assert not legacy.exists() and spawned == []
    assert out["after"]["state"] == "on" and out["after"]["mechanism"] == "registry-run"

    reg.values[(os_service.WINDOWS_STARTUP_APPROVED_KEY, "AbstractGateway")] = bytes([3]) + bytes(11)
    st = autostart.autostart_status(data_dir=data, platform="win32", home=home, registry=reg, env=env)
    assert st["state"] == "broken" and "Task Manager" in st["summary"]

    off = autostart.disable_autostart(data_dir=data, platform="win32", home=home, registry=reg, env=env)
    assert off["ok"] and (os_service.WINDOWS_RUN_KEY, "AbstractGateway") not in reg.values
    assert off["after"]["state"] == "off"


def test_windows_run_value_pointing_at_a_removed_python_is_broken(tmp_path: Path) -> None:
    reg = Registry()
    reg.values[(os_service.WINDOWS_RUN_KEY, "AbstractGateway")] = os_service.windows_join(["C:\\gone\\pythonw.exe", "-m", "abstractgateway.os_service", "launch", "--data-dir", str(tmp_path)])
    st = autostart.autostart_status(data_dir=tmp_path, platform="win32", home=tmp_path, registry=reg, env={})
    assert st["state"] == "broken" and "is gone" in st["summary"]


def test_windows_without_a_registry_backend_fails_loudly_not_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os_service, "default_registry", lambda platform=None: None)
    out = autostart.enable_autostart(data_dir=tmp_path / "d", host="127.0.0.1", port=1, platform="win32", home=tmp_path, exe_argv=[str(_win_python(tmp_path)), "-m", "abstractgateway"], env={})
    assert out["ok"] is False and "registry" in out["error"]


# ----------------------------------------------------------------------- CLI


def test_cli_enable_status_disable_share_the_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import argparse
    import json

    from abstractgateway import firstrun_cli

    data, home = tmp_path / "data", tmp_path / "home"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    exe = _exe(tmp_path)
    monkeypatch.setattr(os_service, "current_gateway_argv", lambda platform=None: [str(exe)])
    run = Runner({("launchctl", "print"): (113, "")})
    monkeypatch.setattr(os_service, "_default_runner", run)

    ns = argparse.Namespace(service_cmd="enable", data_dir=None, json=True, host="127.0.0.1", port=18840, start_now=False)
    assert firstrun_cli.run_service(ns, platform="darwin", home=home) == 0
    assert json.loads(capsys.readouterr().out)["after"]["state"] == "on"
    ns = argparse.Namespace(service_cmd="status", data_dir=None, json=True)
    assert firstrun_cli.run_service(ns, platform="darwin", home=home) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["state"] == "on" and st["installed"] is True
    ns = argparse.Namespace(service_cmd="disable", data_dir=None, json=True, stop=False)
    assert firstrun_cli.run_service(ns, platform="darwin", home=home) == 0
    assert json.loads(capsys.readouterr().out)["after"]["state"] == "off"
    assert run.mutating() == []


def test_an_agent_without_run_at_load_does_not_start_at_login(tmp_path: Path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    plan = os_service.build_install_plan(platform="darwin", home=home, host="127.0.0.1", port=1, data_dir=data, exe_argv=[str(_exe(tmp_path))], uid=501, env={})
    pl = plistlib.loads(plan.files[0]["content"].encode())
    pl["RunAtLoad"] = False
    path = Path(plan.files[0]["path"])
    path.parent.mkdir(parents=True)
    path.write_bytes(plistlib.dumps(pl))
    st = autostart.autostart_status(data_dir=data, platform="darwin", home=home, runner=Runner(), uid=501)
    assert st["state"] == "broken" and "RunAtLoad" in st["summary"]


def test_enable_reports_the_read_back_not_the_write(tmp_path: Path) -> None:
    """The file is written, but launchd has the label disabled: the switch did
    NOT make the gateway start at login, and `ok` must say so with the reason."""
    disabled = Runner({("launchctl", "print-disabled"): (0, '"ai.abstractframework.gateway" => disabled')})
    out = autostart.enable_autostart(data_dir=tmp_path / "d", host="127.0.0.1", port=1, platform="darwin", home=tmp_path / "h", exe_argv=[str(_exe(tmp_path))], runner=disabled, uid=501, env={})
    assert out["ok"] is False and out["after"]["state"] == "broken" and "launchctl disable" in out["error"]


# ------------------------------------------- mission T: pinned command lines


_QUIET = {"discover": lambda: ([], "test"), "hostname_fn": lambda: None}


def _write_legacy(plan: "os_service.ServicePlan", reg: Optional[Registry] = None) -> None:
    """What a 2026-09-23 install left behind: `serve --host --port` and a
    record without the `pinned` key."""
    if plan.registry:
        assert reg is not None
        reg.values[(os_service.WINDOWS_RUN_KEY, "AbstractGateway")] = plan.registry[0]["value"]
    else:
        path = Path(plan.files[0]["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.files[0]["content"], encoding="utf-8")
    os_service.write_service_record(plan)
    rec_path = os_service.service_record_path(plan.data_dir)
    import json

    rec = json.loads(rec_path.read_text())
    rec.pop("pinned", None)
    rec.pop("bind_source", None)
    rec_path.write_text(json.dumps(rec))


def _set_network(data: Path, mode: str, port: int) -> None:
    from abstractgateway.network_exposure import apply_network_change

    code, body = apply_network_change(data, mode=mode, port=port, actor="test", env={}, in_process=False, status_kwargs=_QUIET)
    assert code == 200, body


@pytest.mark.parametrize("platform", ["darwin", "linux", "linux-xdg", "win32"])
def test_a_legacy_pinned_registration_needs_repair_on_every_os_and_enable_rewrites_it_keeping_lan(tmp_path: Path, platform: str) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    plat, lm = ("linux", "xdg") if platform == "linux-xdg" else (platform, "systemd" if platform == "linux" else None)
    env = {"APPDATA": str(tmp_path / "Roaming")}
    exe = [str(_win_python(tmp_path)), "-m", "abstractgateway"] if plat == "win32" else ([str(_exe(tmp_path, "python3")), "-m", "abstractgateway"] if lm == "xdg" else [str(_exe(tmp_path))])
    run = Runner({("systemctl", "--user", "is-enabled"): (0, "enabled\n")} if lm != "xdg" else {("systemctl",): None})
    reg = Registry()
    kw = dict(platform=plat, home=home, env=env, runner=run, registry=reg, uid=501)
    legacy = os_service.build_install_plan(platform=plat, home=home, host="127.0.0.1", port=18877, data_dir=data, exe_argv=exe, uid=501, env=env, linux_mechanism=lm, pinned=True)
    _write_legacy(legacy, reg)
    # The operator chose "Local network" in the tray; the restart could never apply it.
    _set_network(data, "lan", 18877)

    st = autostart.autostart_status(data_dir=data, **kw)
    want = "pinned to 127.0.0.1:18877 by the login item — run `abstractgateway service enable` again to let the Network setting apply"
    assert st["state"] == "broken" and st["needs_repair"] is True and st["repairs"] == [want]
    assert st["problems"][0] == want and "needs repair" in st["summary"]
    assert st["pinned_command_line"] == {"host": "127.0.0.1", "port": "18877", "by_choice": False}

    # The tray's click = enable_autostart with no host/port: repaired in place, `lan` kept.
    out = autostart.enable_autostart(data_dir=data, exe_argv=exe, linux_mechanism=lm, status_kwargs=_QUIET, **kw)
    assert out["ok"], out["error"]
    assert out["after"]["state"] == "on" and out["after"]["bind_source"] == "network_setting"
    assert out["after"]["registered_host"] is None and out["after"]["registered_port"] is None
    assert out["network"]["action"] == "unchanged"
    assert out["after"]["network_setting"]["mode"] == "lan" and out["after"]["network_setting"]["port"] == 18877
    assert any("OLD registration" in n for n in out["plan"]["notes"]), "the running pinned gateway is named, not hidden"
    assert os_service.read_service_record(data)["pinned"] is False


def test_the_tray_shows_needs_repair_with_the_pinned_wording(tmp_path: Path) -> None:
    from abstractgateway.tray import menu_model

    home, data = tmp_path / "home", tmp_path / "data"
    legacy = os_service.build_install_plan(platform="darwin", home=home, host="127.0.0.1", port=8080, data_dir=data, exe_argv=[str(_exe(tmp_path))], uid=501, env={}, pinned=True)
    _write_legacy(legacy)
    st = autostart.autostart_status(data_dir=data, platform="darwin", home=home, runner=Runner(), uid=501, env={})
    view = menu_model.AutostartView(st["state"], st["summary"], tuple(st["problems"]))
    nodes = menu_model.autostart_nodes(view)
    assert "needs repair" in nodes[0].label and nodes[0].checked is False
    assert "pinned to 127.0.0.1:8080 by the login item" in nodes[1].label


def test_a_deliberate_pin_stays_on_and_names_the_override(tmp_path: Path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    out = autostart.enable_autostart(data_dir=data, host="127.0.0.1", port=18878, pin_command_line=True, platform="darwin", home=home, exe_argv=[str(_exe(tmp_path))], runner=Runner(), uid=501, env={})
    assert out["ok"], out["error"]
    after = out["after"]
    assert after["state"] == "on" and after["needs_repair"] is False
    assert after["pinned_command_line"] == {"host": "127.0.0.1", "port": "18878", "by_choice": True}
    assert "pinned to 127.0.0.1:18878" in after["summary"] and "does not apply" in after["summary"]
    assert out["network"]["action"] == "pinned"


def test_a_plain_registration_without_a_stored_setting_needs_repair(tmp_path: Path) -> None:
    home, data = tmp_path / "home", tmp_path / "data"
    plan = os_service.build_install_plan(platform="darwin", home=home, host="127.0.0.1", port=8080, data_dir=data, exe_argv=[str(_exe(tmp_path))], uid=501, env={})
    Path(plan.files[0]["path"]).parent.mkdir(parents=True)
    Path(plan.files[0]["path"]).write_text(plan.files[0]["content"], encoding="utf-8")
    st = autostart.autostart_status(data_dir=data, platform="darwin", home=home, runner=Runner(), uid=501, env={})
    assert st["state"] == "broken" and "the Network setting is not stored" in st["repairs"][0]
    fixed = autostart.enable_autostart(data_dir=data, platform="darwin", home=home, exe_argv=[str(_exe(tmp_path))], runner=Runner(), uid=501, env={}, status_kwargs=_QUIET)
    assert fixed["ok"] and fixed["network"]["action"] == "seeded" and fixed["network"]["mode"] == "localhost"


def test_stored_lan_plus_a_fresh_registration_makes_serve_bind_all_interfaces(tmp_path: Path) -> None:
    """The end-to-end promise, without a bind: what the login item passes to
    `serve` (read back from the registration) + the stored setting, through
    network_exposure's own resolution, gives 0.0.0.0 from the SETTING."""
    from abstractgateway.network_exposure import prepare_serve_bind

    home, data = tmp_path / "home", tmp_path / "data"
    _set_network(data, "lan", 18879)
    out = autostart.enable_autostart(data_dir=data, platform="darwin", home=home, exe_argv=[str(_exe(tmp_path))], runner=Runner(), uid=501, env={}, status_kwargs=_QUIET)
    assert out["ok"], out["error"]
    argv = out["after"]["registered_argv"]
    serve = argv[argv.index("serve"):]
    cli_host = autostart._arg_after(serve, "--host")
    cli_port = autostart._arg_after(serve, "--port")
    bind = prepare_serve_bind(cli_host=cli_host, cli_port=int(cli_port) if cli_port else None, data_dir=data, env={}, **_QUIET)
    assert (bind.host, bind.port, bind.host_source, bind.port_source) == ("0.0.0.0", 18879, "setting", "setting")

    # The pre-fix registration, same setting: the command line wins (the bug).
    legacy = os_service.build_install_plan(platform="darwin", home=home, host="127.0.0.1", port=18879, data_dir=data, exe_argv=[str(_exe(tmp_path))], uid=501, env={}, pinned=True)
    pl = plistlib.loads(legacy.files[0]["content"].encode())["ProgramArguments"]
    old = prepare_serve_bind(cli_host=autostart._arg_after(pl, "--host"), cli_port=int(autostart._arg_after(pl, "--port")), data_dir=data, env={}, **_QUIET)
    assert (old.host, old.host_source) == ("127.0.0.1", "cli")


def test_cli_status_prints_the_repair_and_enable_fixes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import argparse
    import json

    from abstractgateway import firstrun_cli, network_exposure

    data, home = tmp_path / "data", tmp_path / "home"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    for k in ("ABSTRACTGATEWAY_AUTH_TOKEN", "ABSTRACTGATEWAY_USER_AUTH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(network_exposure, "discover_interfaces", _QUIET["discover"])
    monkeypatch.setattr(network_exposure, "bonjour_hostname", _QUIET["hostname_fn"])
    exe = _exe(tmp_path)
    monkeypatch.setattr(os_service, "current_gateway_argv", lambda platform=None: [str(exe)])
    monkeypatch.setattr(os_service, "_default_runner", Runner({("launchctl", "print"): (113, "")}))
    _write_legacy(os_service.build_install_plan(platform="darwin", home=home, host="127.0.0.1", port=18880, data_dir=data, exe_argv=[str(exe)], uid=501, env={}, pinned=True))

    assert firstrun_cli.run_service(argparse.Namespace(service_cmd="status", data_dir=None, json=False), platform="darwin", home=home) == 0
    text = capsys.readouterr().out
    assert "start at login [broken]" in text
    assert "- needs repair: pinned to 127.0.0.1:18880 by the login item — run `abstractgateway service enable` again" in text

    ns = argparse.Namespace(service_cmd="enable", data_dir=None, json=True, host=None, port=None, pin_command_line=False, start_now=False)
    assert firstrun_cli.run_service(ns, platform="darwin", home=home) == 0
    out = json.loads(capsys.readouterr().out)
    # Nothing stored yet: the old record's port is seeded as `localhost`, the old bind exactly.
    assert out["network"]["action"] == "seeded" and (out["network"]["mode"], out["network"]["port"]) == ("localhost", 18880)
    assert firstrun_cli.run_service(argparse.Namespace(service_cmd="status", data_dir=None, json=True), platform="darwin", home=home) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["state"] == "on" and st["needs_repair"] is False and st["bind_source"] == "network_setting"
