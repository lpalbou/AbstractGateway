"""Start the gateway at login — ONE truthful switch shared by the tray
("Start AbstractGateway at login"), the CLI (`abstractgateway service
enable|disable|status`) and the installers (2026-09-24).

The registrations themselves are rendered and executed by `os_service`
(LaunchAgent / systemd user unit / XDG autostart entry / HKCU Run value); this
module answers the question a checkbox has to answer honestly: WOULD this
gateway start at the next login? A file being present is not enough:

- `on`     — registered for THIS data dir, and the program it starts exists;
- `off`    — nothing registered;
- `broken` — registered, but it would not start: the program it points at is
             gone (a moved/removed install), the file is unreadable, the unit
             is not enabled, launchd/Task Manager/the desktop switched it off;
- `other`  — a valid registration for ANOTHER data dir (another gateway).

A registration runs plain `serve` (2026-09-24): the Network setting
(`abstractgateway network`) decides host and port at each start. One that
still carries `--host/--port` (every registration written before, or a
deliberate `--pin-command-line`) overrides the setting forever; unless the
service record says it was pinned on purpose, status reports it as `broken`
with the repair ("pinned to 127.0.0.1:N by the login item — run
`abstractgateway service enable` again …"), which is what the tray shows as
"needs repair" and what its click fixes. A deliberate pin stays `on`, with
the override named in the summary and in `pinned_command_line`.

`enable` registers for the next login WITHOUT starting a second gateway (the
tray's own gateway is already running; `start_now=True` is the CLI/installer
path). `disable` unregisters WITHOUT stopping the running gateway (it may be
the tray's parent); `stop_now=True` is `service uninstall`.

Every probe goes through an injectable `runner` (launchctl / systemctl) and
`registry` (Windows HKCU), so tests drive all three OSes on any OS with
recording doubles and a scratch HOME.
"""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import os_service
from .host_paths import normalize_platform

STATE_ON = "on"
STATE_OFF = "off"
STATE_BROKEN = "broken"
STATE_OTHER = "other"
AUTOSTART_SCHEMA = "gateway_autostart_v1"

REPAIR_COMMAND = "abstractgateway service enable"

MECHANISM_WORDS = {
    "launchd-agent": "a LaunchAgent",
    "systemd-user": "a systemd user unit",
    "xdg-autostart": "a desktop autostart entry",
    "registry-run": "a Windows Run entry",
    "startup-shortcut": "a Startup-folder shortcut",
}

Runner = os_service.Runner


def _run(runner: Optional[Runner], argv: Sequence[str]) -> Optional["subprocess.CompletedProcess[str]"]:
    """A probe that never raises: None when the tool is missing or hangs."""
    run = runner or os_service._default_runner
    try:
        return run(list(argv))
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired, OSError):
        return None


def _same_path(a: Any, b: Any) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))
    except Exception:
        return str(a) == str(b)


def _arg_after(argv: Sequence[str], flag: str) -> Optional[str]:
    for i, a in enumerate(argv[:-1]):
        if a == flag:
            return argv[i + 1]
    return None


def _uid(uid: Optional[int]) -> int:
    return os.getuid() if uid is None and hasattr(os, "getuid") else int(uid or 0)


def _program_problems(program: Optional[str], *, windows: bool) -> List[str]:
    """The one check that makes a checkbox truthful: does the program exist?"""
    if not program:
        return ["the registration names no program to start"]
    p = Path(program)
    if not (p.is_absolute() or (windows and re.match(r"^[A-Za-z]:[\\/]", program))):
        return [f"it starts '{program}' by name, which a login session cannot resolve (no shell PATH)"]
    if not p.exists():
        return [f"the program it starts is gone: {program} (the gateway was moved, reinstalled elsewhere or removed)"]
    if not windows and not os.access(str(p), os.X_OK):
        return [f"the program it starts is not executable: {program}"]
    return []


def detect_linux_mechanism(runner: Optional[Runner] = None) -> str:
    """`systemd` when a systemd USER manager answers, else `xdg` (desktop autostart)."""
    cp = _run(runner, ["systemctl", "--user", "show-environment"])
    return "systemd" if (cp is not None and int(cp.returncode) == 0) else "xdg"


# ---------------------------------------------------------------------------
# Readers: what is registered, per mechanism (pure over files + probes)
# ---------------------------------------------------------------------------


def _read_launchd(path: Path, *, runner: Optional[Runner], uid: Optional[int], probe: bool) -> Dict[str, Any]:
    info: Dict[str, Any] = {"mechanism": "launchd-agent", "location": str(path), "problems": []}
    try:
        pl = plistlib.loads(path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        info["problems"].append(f"the LaunchAgent file is unreadable ({type(exc).__name__}: {exc})")
        return info
    argv = [str(a) for a in (pl.get("ProgramArguments") or ([pl["Program"]] if pl.get("Program") else []))]
    env = pl.get("EnvironmentVariables") if isinstance(pl.get("EnvironmentVariables"), dict) else {}
    info.update(
        argv=argv,
        program=argv[0] if argv else None,
        data_dir=env.get("ABSTRACTGATEWAY_DATA_DIR") or pl.get("WorkingDirectory"),
        port=_arg_after(argv, "--port"),
        host=_arg_after(argv, "--host"),
    )
    if str(pl.get("Label") or "") != os_service.LAUNCHD_LABEL:
        info["problems"].append(f"its Label is {pl.get('Label')!r}, not {os_service.LAUNCHD_LABEL!r}")
    if pl.get("RunAtLoad") is not True:
        info["problems"].append("RunAtLoad is not set, so launchd does not start it at login")
    info["problems"] += _program_problems(info["program"], windows=False)
    if probe:
        domain = f"gui/{_uid(uid)}"
        cp = _run(runner, ["launchctl", "print-disabled", domain])
        if cp is not None and int(cp.returncode) == 0:
            m = re.search(r'"' + re.escape(os_service.LAUNCHD_LABEL) + r'"\s*=>\s*(\w+)', str(cp.stdout or ""))
            if m and m.group(1).lower() in {"disabled", "true"}:
                info["problems"].append("launchd has it disabled (`launchctl disable`), so it will not start at login")
        cp = _run(runner, ["launchctl", "print", f"{domain}/{os_service.LAUNCHD_LABEL}"])
        info["loaded"] = None if cp is None else int(cp.returncode) == 0
    return info


def _systemd_token(tok: str, home: Path) -> str:
    return tok.replace("%%", "\0").replace("%h", str(home)).replace("\0", "%")


def _read_systemd(path: Path, *, home: Path, runner: Optional[Runner], probe: bool) -> Dict[str, Any]:
    info: Dict[str, Any] = {"mechanism": "systemd-user", "location": str(path), "problems": []}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        info["problems"].append(f"the unit file is unreadable ({type(exc).__name__}: {exc})")
        return info
    argv: List[str] = []
    env: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        try:
            if line.startswith("ExecStart="):
                argv = [_systemd_token(t, home) for t in shlex.split(line[len("ExecStart="):])]
            elif line.startswith("Environment="):
                for tok in shlex.split(line[len("Environment="):]):
                    k, _, v = tok.partition("=")
                    env[k] = _systemd_token(v, home)
        except ValueError as exc:
            info["problems"].append(f"unparseable line {line!r} ({exc})")
    info.update(argv=argv, program=argv[0] if argv else None, data_dir=env.get("ABSTRACTGATEWAY_DATA_DIR"), port=_arg_after(argv, "--port"), host=_arg_after(argv, "--host"))
    info["problems"] += _program_problems(info["program"], windows=False)
    if probe:
        cp = _run(runner, ["systemctl", "--user", "is-enabled", os_service.SYSTEMD_UNIT])
        if cp is None:
            info["problems"].append("systemctl --user is not available here, so nothing starts this unit")
        else:
            word = str(cp.stdout or "").strip().splitlines()[0] if str(cp.stdout or "").strip() else ""
            info["enabled"] = word
            if word not in {"enabled", "enabled-runtime", "linked", "alias"}:
                info["problems"].append(f"the unit is {word or 'not enabled'} (systemctl --user is-enabled), so it will not start at login")
        cp = _run(runner, ["systemctl", "--user", "is-active", os_service.SYSTEMD_UNIT])
        info["loaded"] = None if cp is None else int(cp.returncode) == 0
    return info


def _read_xdg(path: Path) -> Dict[str, Any]:
    info: Dict[str, Any] = {"mechanism": "xdg-autostart", "location": str(path), "problems": []}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        info["problems"].append(f"the autostart entry is unreadable ({type(exc).__name__}: {exc})")
        return info
    keys: Dict[str, str] = {}
    for raw in text.splitlines():
        k, sep, v = raw.strip().partition("=")
        if sep and k and not k.startswith("#") and k not in keys:
            keys[k.strip()] = v.strip()
    argv = os_service.desktop_exec_split(keys.get("Exec", ""))
    env: Dict[str, str] = {}
    rest = list(argv)
    if rest and Path(rest[0]).name == "env":
        rest = rest[1:]
        while rest and "=" in rest[0] and not rest[0].startswith("-"):
            k, _, v = rest.pop(0).partition("=")
            env[k] = v
    info.update(argv=argv, program=rest[0] if rest else None, data_dir=env.get("ABSTRACTGATEWAY_DATA_DIR") or _arg_after(argv, "--data-dir"), port=_arg_after(argv, "--port"), host=_arg_after(argv, "--host"))
    if keys.get("Hidden", "").lower() == "true":
        info["problems"].append("the entry is marked Hidden=true (deleted in the desktop's startup settings)")
    if keys.get("X-GNOME-Autostart-enabled", "true").lower() == "false":
        info["problems"].append("it is switched off in the desktop's Startup Applications")
    info["problems"] += _program_problems(info["program"], windows=False)
    return info


def _read_windows(registry: Any, *, home: Path, env: Optional[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    value = None
    if registry is not None:
        try:
            value = registry.get(os_service.WINDOWS_RUN_KEY, os_service.WINDOWS_RUN_VALUE)
        except Exception:  # noqa: BLE001
            value = None
    legacy = os_service.windows_startup_shortcut_path(home, env)
    if not value:
        if legacy.exists():
            # A .lnk cannot be read back without COM: say so instead of guessing.
            return {
                "mechanism": "startup-shortcut",
                "location": str(legacy),
                "argv": [],
                "program": None,
                "data_dir": None,
                "port": None,
                "host": None,
                "problems": [],
                "unverified": "an older Startup-folder shortcut; its target cannot be checked (turn Start at login off and on to replace it)",
            }
        return None
    argv = os_service.windows_split(str(value))
    info: Dict[str, Any] = {
        "mechanism": "registry-run",
        "location": os_service.windows_run_display(),
        "command": str(value),
        "argv": argv,
        "program": argv[0] if argv else None,
        "data_dir": _arg_after(argv, "--data-dir"),
        "port": _arg_after(argv, "--port"),
        "host": _arg_after(argv, "--host"),
        "problems": [],
    }
    info["problems"] += _program_problems(info["program"], windows=True)
    try:
        approved = registry.get(os_service.WINDOWS_STARTUP_APPROVED_KEY, os_service.WINDOWS_RUN_VALUE)
    except Exception:  # noqa: BLE001
        approved = None
    if isinstance(approved, (bytes, bytearray)) and len(approved) >= 1 and (approved[0] & 1):
        info["problems"].append("it is disabled in Task Manager > Startup apps")
    if legacy.exists():
        info["problems"].append(f"an older Startup-folder shortcut is ALSO registered ({legacy}); it would start a second copy")
    return info


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _bind_repairs(primary: Dict[str, Any], *, data_dir: Path, env: Dict[str, str], out: Dict[str, Any]) -> List[str]:
    """Does the registration let the Network setting decide the bind?

    Fills `out` with `bind_source`, `pinned_command_line` and
    `network_setting`; returns the repair lines (empty = fine). A
    registration for ANOTHER data dir is that gateway's business: nothing."""
    host, port = primary.get("host"), primary.get("port")
    if not primary.get("argv"):
        return []  # unreadable / unverifiable: `problems` already speaks
    if primary.get("data_dir") and not _same_path(primary.get("data_dir"), data_dir):
        return []
    if host is not None or port is not None:
        rec = os_service.read_service_record(data_dir) or {}
        where = f"{host or '(setting)'}:{port or '(setting)'}"
        out["bind_source"] = "command_line"
        out["pinned_command_line"] = {"host": host, "port": port, "by_choice": rec.get("pinned") is True}
        if rec.get("pinned") is True:
            return []
        return [
            f"pinned to {where} by the login item — run `{REPAIR_COMMAND}` again to let the Network setting apply"
        ]
    out["bind_source"] = "network_setting"
    try:
        from .network_exposure import baseline_env
        from .runtime_config import resolve_network_setting

        setting = resolve_network_setting(data_dir, env=baseline_env(env))
    except Exception as exc:  # noqa: BLE001 - status never raises
        return [f"the Network setting is unreadable ({type(exc).__name__}: {exc}); `abstractgateway network status` shows why"]
    out["network_setting"] = {"mode": setting.get("mode"), "port": setting.get("port"), "source": setting.get("source"), "port_source": setting.get("port_source")}
    if setting.get("source") != "stored" or setting.get("port_source") != "stored":
        return [
            f"the Network setting is not stored, so the login item's gateway would take the built-in default "
            f"({setting.get('bind_host')}:{setting.get('port')}) — run `{REPAIR_COMMAND}` again to store it"
        ]
    return []


def autostart_status(
    *,
    data_dir: Path,
    platform: Optional[str] = None,
    home: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    runner: Optional[Runner] = None,
    registry: Any = None,
    uid: Optional[int] = None,
    probe: bool = True,
) -> Dict[str, Any]:
    """Would THIS gateway (data dir) start at the next login? Never raises."""
    plat = normalize_platform(platform)
    home_p = Path(home) if home is not None else Path.home()
    env_d = dict(os.environ if env is None else env)
    found: List[Dict[str, Any]] = []
    if plat == "darwin":
        path = os_service.service_file_path(plat, home_p, env_d)
        if path.exists():
            found.append(_read_launchd(path, runner=runner, uid=uid, probe=probe))
        default_mech = "launchd-agent"
    elif plat == "linux":
        unit = os_service.service_file_path(plat, home_p, env_d, linux_mechanism="systemd")
        desktop = os_service.xdg_autostart_path(home_p, env_d)
        if unit.exists():
            found.append(_read_systemd(unit, home=home_p, runner=runner, probe=probe))
        if desktop.exists():
            found.append(_read_xdg(desktop))
        default_mech = ("systemd-user" if detect_linux_mechanism(runner) == "systemd" else "xdg-autostart") if (probe and not found) else "systemd-user"
    else:
        reg = registry if registry is not None else os_service.default_registry(plat)
        info = _read_windows(reg, home=home_p, env=env_d)
        if info is not None:
            found.append(info)
        default_mech = "registry-run"
    out: Dict[str, Any] = {
        "schema": AUTOSTART_SCHEMA,
        "platform": plat,
        "data_dir": str(data_dir),
        "experimental": plat == "windows",
        "registrations": found,
    }
    if not found:
        out.update(state=STATE_OFF, mechanism=default_mech, location=None, problems=[], summary="Off — nothing starts the gateway at login")
        return out
    # Two registrations (systemd AND xdg) would start two gateways: that alone is broken.
    primary = found[0]
    problems = list(primary.get("problems") or [])
    for extra in found[1:]:
        problems.append(f"{MECHANISM_WORDS.get(extra['mechanism'], extra['mechanism'])} is ALSO registered ({extra['location']}); two would start at login")
    out.update(
        mechanism=primary["mechanism"],
        location=primary.get("location"),
        registered_argv=primary.get("argv") or [],
        registered_program=primary.get("program"),
        registered_data_dir=primary.get("data_dir"),
        registered_port=primary.get("port"),
        registered_host=primary.get("host"),
        loaded=primary.get("loaded"),
        problems=problems,
    )
    words = MECHANISM_WORDS.get(primary["mechanism"], primary["mechanism"])
    repairs = _bind_repairs(primary, data_dir=Path(data_dir), env=env_d, out=out)
    out["repairs"] = repairs
    out["needs_repair"] = bool(repairs)
    if problems:
        out["state"] = STATE_BROKEN
        out["summary"] = f"Registered ({words}) but it will not start: {problems[0]}"
        out["problems"] = problems + repairs
    elif repairs and not (primary.get("data_dir") and not _same_path(primary.get("data_dir"), data_dir)):
        # It starts, but not the way the Network setting says: "needs repair"
        # in the tray (whose click re-enables = rewrites it), never silent.
        out["state"] = STATE_BROKEN
        out["problems"] = repairs
        out["summary"] = f"Registered ({words}); it starts the gateway at login but needs repair: {repairs[0]}"
    elif primary.get("data_dir") and not _same_path(primary.get("data_dir"), data_dir):
        out["state"] = STATE_OTHER
        out["summary"] = f"Registered ({words}) for another gateway (data folder {primary.get('data_dir')})"
    else:
        out["state"] = STATE_ON
        pin = out.get("pinned_command_line") or {}
        if pin:
            out["summary"] = (
                f"On — {words} starts the gateway at login, pinned to {pin.get('host')}:{pin.get('port')} on its command "
                "line (--pin-command-line): the Network setting does not apply"
            )
        else:
            net = out.get("network_setting") or {}
            out["summary"] = f"On — {words} starts the gateway at login" + (
                f"; the Network setting binds it ({net.get('mode')}, port {net.get('port')})" if net.get("mode") else ""
            )
        if primary.get("unverified"):
            out["unverified"] = primary["unverified"]
            out["summary"] += f" ({primary['unverified']})"
    return out


# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------


def enable_autostart(
    *,
    data_dir: Path,
    host: Optional[str] = None,
    port: Optional[int] = None,
    pin_command_line: bool = False,
    running_port: Optional[int] = None,
    actor: str = "service",
    platform: Optional[str] = None,
    home: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    exe_argv: Optional[Sequence[str]] = None,
    start_now: bool = False,
    runner: Optional[Runner] = None,
    registry: Any = None,
    start_detached: Optional[Callable[[Sequence[str]], None]] = None,
    uid: Optional[int] = None,
    linux_mechanism: Optional[str] = None,
    echo: Callable[[str], None] = lambda _l: None,
    status_kwargs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Register THIS gateway (data dir) to start at login, then read the
    registration back. `ok` is the read-back, not the write.

    The registration runs plain `serve`; the bind lives in the Network
    setting, which is seeded first (`os_service.resolve_service_bind`):
    `host`/`port` given = an explicit request, written INTO the setting;
    omitted (the tray) = the stored setting, else `localhost` on the running
    gateway's / previous / first free port. `pin_command_line=True` is the
    old shape (`serve --host --port`, the setting untouched and overridden).
    A refused setting write registers nothing and is `error`."""
    plat = normalize_platform(platform)
    home_p = Path(home) if home is not None else Path.home()
    env_d = dict(os.environ if env is None else env)
    kw = dict(platform=plat, home=home_p, env=env_d, runner=runner, registry=registry, uid=uid)
    before = autostart_status(data_dir=data_dir, **kw)
    if running_port is None and not port:
        from .first_run import read_serve_record

        serve = read_serve_record(Path(data_dir)) or {}
        if serve.get("alive") is not False and serve.get("port"):
            running_port = int(serve["port"])
    try:
        bind = os_service.resolve_service_bind(Path(data_dir), host=host, port=port, running_port=running_port, pin_command_line=pin_command_line, env=env_d)
    except SystemExit as exc:
        return {"ok": False, "action": "enable", "error": str(exc), "before": before, "after": before, "plan": None, "results": [], "network": None}
    lm = linux_mechanism or (detect_linux_mechanism(runner) if plat == "linux" else None)
    exe = list(exe_argv) if exe_argv else os_service.current_gateway_argv(plat)
    plan = os_service.build_install_plan(
        platform=plat, home=home_p, host=str(bind["host"]), port=int(bind["port"]), data_dir=Path(data_dir), exe_argv=exe,
        uid=uid, env=env_d, linux_mechanism=lm, pinned=bool(bind["pinned"]), network=bind,
    )
    if plat == "linux" and lm == "systemd":
        # An XDG entry from an earlier fallback would start a SECOND gateway.
        plan.remove.append(str(os_service.xdg_autostart_path(home_p, env_d)))
    if not start_now:
        plan.commands = os_service.without_start(plan)
        plan.notes.append("Registered for the next login; the gateway running now is not restarted.")
    if not start_now and before.get("pinned_command_line") and not bind["pinned"]:
        plan.notes.append(
            "The gateway running now was started by the OLD registration (with --host/--port) and a restart replays "
            "that command line: the Network setting applies once the login item starts it again — "
            "`abstractgateway service install` restarts it now, or log out and back in."
        )
    error: Optional[str] = None
    results: List[Dict[str, Any]] = []
    seeded: Optional[Dict[str, Any]] = None
    try:
        # The setting first: a refused mode must not leave a registration behind.
        seeded = os_service.seed_network_setting(Path(data_dir), bind, actor=f"{actor}/enable", env=env_d, status_kwargs=status_kwargs)
        echo(seeded["message"])
        results = os_service.execute_plan(plan, runner=runner, echo=echo, registry=registry if registry is not None else os_service.default_registry(plat), start_detached=start_detached)
        os_service.write_service_record(plan)
    except SystemExit as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - reported, never raised into a GUI
        error = f"{type(exc).__name__}: {exc}"
    after = autostart_status(data_dir=data_dir, **kw)
    ok = error is None and after["state"] == STATE_ON
    if error is None and not ok:
        error = after["summary"]
    return {"ok": ok, "action": "enable", "error": error, "before": before, "after": after, "plan": plan.public_dict(), "results": results, "network": seeded}


def disable_autostart(
    *,
    data_dir: Path,
    platform: Optional[str] = None,
    home: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    stop_now: bool = False,
    runner: Optional[Runner] = None,
    registry: Any = None,
    uid: Optional[int] = None,
    echo: Callable[[str], None] = lambda _l: None,
) -> Dict[str, Any]:
    """Remove the login registration, then read back that nothing is left."""
    plat = normalize_platform(platform)
    home_p = Path(home) if home is not None else Path.home()
    env_d = dict(os.environ if env is None else env)
    kw = dict(platform=plat, home=home_p, env=env_d, runner=runner, registry=registry, uid=uid)
    before = autostart_status(data_dir=data_dir, **kw)
    plan = os_service.build_uninstall_plan(platform=plat, home=home_p, data_dir=Path(data_dir), uid=uid, env=env_d, stop=stop_now)
    error: Optional[str] = None
    results: List[Dict[str, Any]] = []
    try:
        results = os_service.execute_plan(plan, runner=runner, echo=echo, registry=registry if registry is not None else os_service.default_registry(plat))
    except SystemExit as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    after = autostart_status(data_dir=data_dir, **kw)
    ok = error is None and after["state"] == STATE_OFF
    if error is None and not ok:
        error = after["summary"]
    return {"ok": ok, "action": "disable", "error": error, "before": before, "after": after, "plan": plan.public_dict(), "results": results}


__all__ = [
    "STATE_ON",
    "STATE_OFF",
    "STATE_BROKEN",
    "STATE_OTHER",
    "REPAIR_COMMAND",
    "autostart_status",
    "enable_autostart",
    "disable_autostart",
    "detect_linux_mechanism",
]


if __name__ == "__main__":  # pragma: no cover - `python -m abstractgateway.autostart` = status
    import json

    from .host_paths import apply_data_dir_default

    print(json.dumps(autostart_status(data_dir=apply_data_dir_default().path), indent=2, sort_keys=True))
    sys.exit(0)
