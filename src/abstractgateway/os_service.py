"""`abstractgateway service install|uninstall|status`: start the gateway at login,
per user, without admin rights (2026-09-23).

- macOS: a LaunchAgent `~/Library/LaunchAgents/ai.abstractframework.gateway.plist`
  (`RunAtLoad`, `KeepAlive`, logs under `~/Library/Logs/AbstractGateway/`),
  loaded with `launchctl bootstrap gui/<uid>`.
- Linux: a systemd USER unit `~/.config/systemd/user/abstractgateway.service`
  (`Restart=on-failure`), enabled with `systemctl --user enable --now`. It runs
  only while the user is logged in unless lingering is enabled
  (`loginctl enable-linger $USER`, printed as a hint, never run for you).
  Where no systemd user manager answers (non-systemd distros, WSL, some
  containers) the fallback is an XDG autostart entry
  `~/.config/autostart/abstractgateway.desktop`, started by the desktop
  session at graphical login (`linux_mechanism="xdg"`, chosen by
  `abstractgateway.autostart`).
- Windows (EXPERIMENTAL, unverified on a real VM): a per-user `HKCU\\...\\Run`
  value `AbstractGateway` (no admin) whose command is
  `pythonw.exe -m abstractgateway.os_service launch ...`, so no console window
  appears, with output going to a log file. It replaced the Startup-folder
  shortcut of 2026-09-23 (2026-09-24): a Run value is a plain string, so the
  registration can be READ BACK and verified (a `.lnk` cannot without COM),
  and Task Manager's "Startup apps" switch for it is readable
  (`StartupApproved\\Run`). An older shortcut is removed by install/uninstall.

Every generated artifact is a pure function of (platform, home, executable,
data dir[, host, port when pinned]), so tests render all three OSes on any OS.
`--dry-run` prints the files and the exact commands and touches nothing.

The bind is the NETWORK SETTING's (2026-09-24, mission T): every registration
starts plain `serve` — no `--host`, no `--port` — so `abstractgateway network
set localhost|lan|internet [--port N]` (the tray's Network menu, the console's
panel) applies at the next start. A command-line flag would win over the
setting forever (`overridden_by_cli`), which is why the 2026-09-23
registrations (`serve --host 127.0.0.1 --port N`) made the setting inert.
Install/enable SEED the setting first (`resolve_service_bind` +
`seed_network_setting`, through `network_exposure.apply_network_change`):
nothing stored yet -> `localhost` on the chosen port, i.e. exactly the old
bind; `--host/--port` given -> written into the SETTING, loudly. Only
`--pin-command-line` keeps `--host/--port` on the command line (the old
behaviour, recorded as `pinned` and reported as an override). A registration
still carrying `--host/--port` without that record is reported by
`autostart.autostart_status` as needing repair (`service enable` rewrites it).

launchd and systemd do not read shell profiles: the executable is ABSOLUTE and
PATH is set explicitly (with `~/.local/bin`, `~/.lmstudio/bin`, Homebrew and
`/usr/local/bin`), so the gateway can find `lms`, `ollama` and friends.
"""

from __future__ import annotations

import datetime
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import sysconfig
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .first_run import browser_base_url, is_loopback_host, port_is_free
from .host_paths import normalize_platform

LAUNCHD_LABEL = "ai.abstractframework.gateway"
SYSTEMD_UNIT = "abstractgateway.service"
XDG_AUTOSTART_FILE = "abstractgateway.desktop"
WINDOWS_SHORTCUT = "AbstractGateway.lnk"  # the 2026-09-23 mechanism; removed when found
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
WINDOWS_RUN_VALUE = "AbstractGateway"
LINUX_MECHANISMS = ("systemd", "xdg")
SERVICE_RECORD = "service.json"
SERVICE_SCHEMA = "gateway_service_v1"
DEFAULT_PORT = 8080
PORT_SEARCH_SPAN = 20

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


@dataclass
class ServicePlan:
    action: str
    platform: str
    host: str
    port: int
    data_dir: Path
    url: str
    exe_argv: List[str]
    files: List[Dict[str, Any]] = field(default_factory=list)  # {path, content, mode}
    # Created before the service manager is asked to start anything: launchd
    # and systemd both refuse to spawn into a missing WorkingDirectory, and
    # launchd does not create the parent of StandardErrorPath.
    dirs: List[str] = field(default_factory=list)
    remove: List[str] = field(default_factory=list)
    commands: List[List[str]] = field(default_factory=list)
    # Windows only: HKCU writes, executed through a registry backend (tests
    # pass a recording double). {op: set|delete, key, name, value?, missing_ok?}
    registry: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    experimental: bool = False
    mechanism: str = ""
    # True only for `--pin-command-line`: `serve --host H --port P` on the
    # command line, which overrides the Network setting (the 2026-09-23 shape).
    pinned: bool = False
    # The bind resolution + the setting write it needs (resolve_service_bind).
    network: Dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> Dict[str, Any]:
        return {
            "schema": SERVICE_SCHEMA,
            "action": self.action,
            "platform": self.platform,
            "mechanism": self.mechanism,
            "host": self.host,
            "port": self.port,
            "url": self.url,
            "console_url": self.url + "/console",
            "data_dir": str(self.data_dir),
            "exe_argv": list(self.exe_argv),
            "dirs": list(self.dirs),
            "files": [{"path": f["path"], "mode": oct(int(f.get("mode") or 0o644)), "content": f["content"]} for f in self.files],
            "remove": list(self.remove),
            "commands": [list(c) for c in self.commands],
            "registry": [dict(r) for r in self.registry],
            "notes": list(self.notes),
            "experimental": bool(self.experimental),
            "pinned_command_line": bool(self.pinned),
            "bind_source": "command_line" if self.pinned else "network_setting",
            "serve_args": _serve_args(self.host, self.port, pinned=self.pinned),
            "network": dict(self.network),
        }


# ---------------------------------------------------------------------------
# Paths and executable
# ---------------------------------------------------------------------------


def _xdg_config_home(home: Path, env: Dict[str, str]) -> Path:
    xdg = str(env.get("XDG_CONFIG_HOME") or "").strip()
    return Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".config"


def windows_startup_shortcut_path(home: Path, env: Optional[Dict[str, str]] = None) -> Path:
    """The Startup-folder shortcut the 2026-09-23 installer wrote (now legacy)."""
    env = os.environ if env is None else env
    appdata = str(env.get("APPDATA") or "").strip()
    base = Path(appdata) if appdata else home / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / WINDOWS_SHORTCUT


def xdg_autostart_path(home: Path, env: Optional[Dict[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    return _xdg_config_home(home, env) / "autostart" / XDG_AUTOSTART_FILE


def windows_run_display() -> str:
    return "HKCU\\" + WINDOWS_RUN_KEY + "\\" + WINDOWS_RUN_VALUE


def service_file_path(platform: str, home: Path, env: Optional[Dict[str, str]] = None, *, linux_mechanism: Optional[str] = None) -> Path:
    """The FILE a registration lives in. Windows has none any more (a registry
    value, see `windows_run_display`): the legacy shortcut path is returned so
    callers that only check files still find an old registration."""
    env = os.environ if env is None else env
    plat = normalize_platform(platform)
    if plat == "darwin":
        return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    if plat == "windows":
        return windows_startup_shortcut_path(home, env)
    if linux_mechanism == "xdg":
        return xdg_autostart_path(home, env)
    return _xdg_config_home(home, env) / "systemd" / "user" / SYSTEMD_UNIT


def mechanism_name(platform: str, linux_mechanism: Optional[str] = None) -> str:
    plat = normalize_platform(platform)
    if plat == "darwin":
        return "launchd-agent"
    if plat == "windows":
        return "registry-run"
    return "xdg-autostart" if linux_mechanism == "xdg" else "systemd-user"


def log_dir(platform: str, home: Path, data_dir: Path) -> Path:
    plat = normalize_platform(platform)
    if plat == "darwin":
        return home / "Library" / "Logs" / "AbstractGateway"
    return data_dir / "logs"


def service_path_env(platform: str, home: Path, exe_argv: Sequence[str]) -> str:
    """PATH for a process started by the service manager (no shell profile)."""
    plat = normalize_platform(platform)
    exe_dir = str(Path(exe_argv[0]).parent) if exe_argv else ""
    if plat == "windows":
        parts = [exe_dir, str(home / ".local" / "bin"), str(home / ".lmstudio" / "bin")]
        sep = ";"
    else:
        parts = [
            exe_dir,
            str(home / ".local" / "bin"),
            str(home / ".lmstudio" / "bin"),
            *(["/opt/homebrew/bin"] if plat == "darwin" else []),
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        sep = ":"
    seen: List[str] = []
    for p in parts:
        if p and p not in seen:
            seen.append(p)
    return sep.join(seen)


def current_gateway_argv(platform: Optional[str] = None) -> List[str]:
    """Absolute argv prefix that runs THIS installation's `abstractgateway`.

    The console-script shim next to the running interpreter is preferred (it
    is what `uv tool upgrade` keeps stable); otherwise `python -m abstractgateway`
    with the absolute interpreter. Never a bare name looked up on PATH."""
    plat = normalize_platform(platform)
    scripts = Path(sysconfig.get_path("scripts") or Path(sys.executable).parent)
    cand = scripts / ("abstractgateway.exe" if plat == "windows" else "abstractgateway")
    if cand.is_file():
        return [str(cand)]
    return [str(Path(sys.executable).resolve()), "-m", "abstractgateway"]


def windows_pythonw(python_exe: str) -> str:
    """`pythonw.exe` next to `python.exe` (no console window), else the input."""
    p = Path(python_exe)
    cand = p.with_name("pythonw.exe")
    return str(cand) if cand.name.lower() != p.name.lower() else str(p)


# ---------------------------------------------------------------------------
# Renderers (pure)
# ---------------------------------------------------------------------------


def _serve_args(host: str, port: int, *, pinned: bool = False) -> List[str]:
    """What every registration runs after the executable.

    Plain `serve` (the Network setting decides host and port at each start);
    `serve --host H --port P` only for a registration pinned on purpose
    (`--pin-command-line`): flags on the command line override the setting."""
    if not pinned:
        return ["serve"]
    return ["serve", "--host", str(host), "--port", str(int(port))]


def render_launchd_plist(*, exe_argv: Sequence[str], host: str, port: int, data_dir: Path, home: Path, pinned: bool = False) -> str:
    logs = log_dir("darwin", home, data_dir)
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [*exe_argv, *_serve_args(host, port, pinned=pinned)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        # A crash loop must not spin: launchd waits this long between relaunches.
        "ThrottleInterval": 10,
        "WorkingDirectory": str(data_dir),
        "EnvironmentVariables": {
            "PATH": service_path_env("darwin", home, exe_argv),
            "ABSTRACTGATEWAY_DATA_DIR": str(data_dir),
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(logs / "gateway.out.log"),
        "StandardErrorPath": str(logs / "gateway.err.log"),
        "ProcessType": "Interactive",
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")


def _systemd_path(path: str, home: Path) -> str:
    """`%h`-relative when under the home directory (the unit survives a moved $HOME)."""
    h = str(home).rstrip("/")
    p = str(path)
    if p == h:
        return "%h"
    if p.startswith(h + "/"):
        return "%h" + p[len(h):]
    return p


def _systemd_quote(arg: str) -> str:
    # A literal "%" is a systemd specifier; only our own "%h" prefix is meant.
    arg = arg.replace("%", "%%").replace("%%h", "%h")
    if arg and not any(c in arg for c in ' \t"\'\\;'):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd_unit(*, exe_argv: Sequence[str], host: str, port: int, data_dir: Path, home: Path, pinned: bool = False) -> str:
    argv = [_systemd_path(a, home) for a in exe_argv]
    exec_start = " ".join(_systemd_quote(a) for a in [*argv, *_serve_args(host, port, pinned=pinned)])
    path_env = ":".join(_systemd_path(p, home) for p in service_path_env("linux", home, exe_argv).split(":"))
    data = _systemd_path(str(data_dir), home)
    return (
        "# Generated by `abstractgateway service install`. Remove with `abstractgateway service uninstall`.\n"
        "[Unit]\n"
        "Description=AbstractGateway (AbstractFramework durable run gateway)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        f"WorkingDirectory={_systemd_quote(data)}\n"
        f"Environment={_systemd_quote('PATH=' + path_env)}\n"
        f"Environment={_systemd_quote('ABSTRACTGATEWAY_DATA_DIR=' + data)}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "TimeoutStopSec=150\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def windows_launch_argv(*, exe_argv: Sequence[str], host: str, port: int, data_dir: Path, log_file: Path, pinned: bool = False) -> List[str]:
    """What the Startup shortcut runs: pythonw + our log-redirecting launcher."""
    python = exe_argv[0] if len(exe_argv) >= 3 and exe_argv[1] == "-m" else sys.executable
    return [
        windows_pythonw(python),
        "-m",
        "abstractgateway.os_service",
        "launch",
        "--log-file",
        str(log_file),
        "--data-dir",
        str(data_dir),
        "--",
        *_serve_args(host, port, pinned=pinned),
    ]


def windows_join(argv: Sequence[str]) -> str:
    """One Windows command line (CommandLineToArgvW rules, what a Run value holds)."""
    return subprocess.list2cmdline([str(a) for a in argv])


def windows_split(cmdline: str) -> List[str]:
    """Inverse of `windows_join` (CommandLineToArgvW rules), pure Python so a
    Run value can be verified on any OS (tests) and without ctypes."""
    out: List[str] = []
    s = str(cmdline or "")
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            break
        arg: List[str] = []
        in_quotes = False
        while i < n and (in_quotes or s[i] not in " \t"):
            if s[i] == "\\":
                j = i
                while j < n and s[j] == "\\":
                    j += 1
                count = j - i
                if j < n and s[j] == '"':
                    arg.append("\\" * (count // 2))
                    if count % 2:
                        arg.append('"')
                        i = j + 1
                    else:
                        i = j
                    continue
                arg.append("\\" * count)
                i = j
                continue
            if s[i] == '"':
                if in_quotes and i + 1 < n and s[i + 1] == '"':
                    arg.append('"')
                    i += 2
                    continue
                in_quotes = not in_quotes
                i += 1
                continue
            arg.append(s[i])
            i += 1
        out.append("".join(arg))
    return out


_DESKTOP_RESERVED = set(' \t\n"\'\\><~|&;$*?#()`')


def _desktop_quote(arg: str) -> str:
    """Desktop Entry `Exec` quoting (freedesktop spec): reserved characters
    force double quotes, inside which `"`, `` ` ``, `$` and `\\` are escaped;
    a literal `%` is `%%` (field codes)."""
    a = str(arg).replace("%", "%%")
    if a and not any(c in _DESKTOP_RESERVED for c in a):
        return a
    return '"' + "".join("\\" + c if c in '"`$\\' else c for c in a) + '"'


def desktop_exec_split(value: str) -> List[str]:
    """Parse a Desktop Entry `Exec` value back into argv (inverse of `_desktop_quote`)."""
    out: List[str] = []
    s = str(value or "")
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            break
        arg: List[str] = []
        if s[i] == '"':
            i += 1
            while i < n and s[i] != '"':
                if s[i] == "\\" and i + 1 < n:
                    arg.append(s[i + 1])
                    i += 2
                    continue
                arg.append(s[i])
                i += 1
            i += 1
        else:
            while i < n and s[i] not in " \t":
                arg.append(s[i])
                i += 1
        out.append("".join(arg).replace("%%", "%"))
    return out


def xdg_launch_argv(*, exe_argv: Sequence[str], host: str, port: int, data_dir: Path, home: Path, pinned: bool = False) -> List[str]:
    """What the XDG autostart entry runs. A desktop session neither redirects
    output nor reads the shell profile, so `env` sets PATH + the data dir and
    the log-redirecting launcher (the same one Windows uses) owns the output."""
    python = exe_argv[0] if len(exe_argv) >= 3 and exe_argv[1] == "-m" else sys.executable
    logf = log_dir("linux", home, data_dir) / "gateway.log"
    return [
        "/usr/bin/env",
        "PATH=" + service_path_env("linux", home, exe_argv),
        "ABSTRACTGATEWAY_DATA_DIR=" + str(data_dir),
        str(python),
        "-m",
        "abstractgateway.os_service",
        "launch",
        "--log-file",
        str(logf),
        "--data-dir",
        str(data_dir),
        "--",
        *_serve_args(host, port, pinned=pinned),
    ]


def render_xdg_desktop(*, launch_argv: Sequence[str]) -> str:
    return (
        "# Generated by `abstractgateway service`. Remove with `abstractgateway service disable`.\n"
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=AbstractGateway\n"
        "Comment=Starts the AbstractGateway at login\n"
        f"Exec={' '.join(_desktop_quote(a) for a in launch_argv)}\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


# ---------------------------------------------------------------------------
# Windows registry backend (HKCU only: no admin, per user)
# ---------------------------------------------------------------------------


class WindowsRegistry:
    """HKEY_CURRENT_USER reads/writes through `winreg`. Tests pass a double
    with the same three methods; nothing here runs off Windows."""

    def get(self, key: str, name: str) -> Any:
        import winreg  # type: ignore[import-not-found]

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                value, _typ = winreg.QueryValueEx(k, name)
                return value
        except FileNotFoundError:
            return None

    def set_string(self, key: str, name: str, value: str) -> None:
        import winreg  # type: ignore[import-not-found]

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, str(value))

    def delete(self, key: str, name: str) -> bool:
        import winreg  # type: ignore[import-not-found]

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
                return True
        except FileNotFoundError:
            return False


def default_registry(platform: Optional[str] = None) -> Optional[WindowsRegistry]:
    """The real registry when THIS process runs on Windows, else None."""
    return WindowsRegistry() if os.name == "nt" and normalize_platform(platform) == "windows" else None


# ---------------------------------------------------------------------------
# Port selection + persisted record
# ---------------------------------------------------------------------------


def service_record_path(data_dir: Path) -> Path:
    return Path(data_dir) / SERVICE_RECORD


def read_service_record(data_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        rec = json.loads(service_record_path(data_dir).read_text(encoding="utf-8"))
    except Exception:
        return None
    return rec if isinstance(rec, dict) else None


def choose_port(
    *,
    host: str,
    requested: Optional[int],
    persisted: Optional[int],
    free: Callable[[str, int], bool] = port_is_free,
) -> Dict[str, Any]:
    """An explicit `--port` is used as-is; otherwise the persisted port (a
    reinstall keeps its URL), else the first free port from 8080 upwards."""
    if requested:
        return {"port": int(requested), "source": "flag", "busy_skipped": []}
    if persisted:
        return {"port": int(persisted), "source": "persisted", "busy_skipped": []}
    skipped: List[int] = []
    for p in range(DEFAULT_PORT, DEFAULT_PORT + PORT_SEARCH_SPAN):
        if free(host, p):
            return {"port": p, "source": "probe", "busy_skipped": skipped}
        skipped.append(p)
    raise SystemExit(
        f"No free port in {DEFAULT_PORT}-{DEFAULT_PORT + PORT_SEARCH_SPAN - 1} on {host}; pass --port explicitly."
    )


# ---------------------------------------------------------------------------
# The bind: the Network setting, seeded at install/enable (2026-09-24)
# ---------------------------------------------------------------------------


def _mode_for_host_flag(host: str, stored_mode: Optional[str]) -> str:
    """`--host` given to `service install|enable` -> the Network mode it means.

    Loopback -> `localhost`; a wildcard (0.0.0.0 / ::) -> the stored network
    mode when it is one (lan and internet share the bind), else `lan`. A
    specific interface address is no mode: refused, with the way out."""
    from .network_exposure import is_wildcard_host

    if is_loopback_host(host):
        return "localhost"
    if is_wildcard_host(host):
        return str(stored_mode) if stored_mode in ("lan", "internet") else "lan"
    raise SystemExit(
        f"--host {host}: the Network setting binds 127.0.0.1 (localhost) or 0.0.0.0 (lan / internet), not one "
        f"address. Use --host 127.0.0.1 or --host 0.0.0.0, or add --pin-command-line to put `--host {host}` on the "
        "login item's command line (the Network setting then no longer applies to this gateway)."
    )


def resolve_service_bind(
    data_dir: Path,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    running_port: Optional[int] = None,
    pin_command_line: bool = False,
    env: Optional[Dict[str, str]] = None,
    free: Callable[[str, int], bool] = port_is_free,
) -> Dict[str, Any]:
    """Which bind the login item's gateway gets, and what the Network setting
    must hold for it. Pure except for reading the setting and port probes.

    Mode: `--host` (mapped, `_mode_for_host_flag`) > the stored setting >
    `localhost` (the bind every registration had before 2026-09-24).
    Port: `--port` > the stored setting's port > the running gateway's
    (`enable`) > the previous registration's > the first free from 8080.
    `seed` is the setting write (`{mode, port}`), None when it already holds
    both; `pinned` (--pin-command-line) never touches the setting."""
    from .network_exposure import MODE_BIND_HOST, baseline_env
    from .runtime_config import resolve_network_setting

    env_d = dict(os.environ if env is None else env)
    setting = resolve_network_setting(Path(data_dir), env=baseline_env(env_d))
    current = {"mode": setting["mode"], "source": setting["source"], "port": setting["port"], "port_source": setting["port_source"]}
    stored_mode = setting["mode"] if setting["source"] == "stored" else None
    stored_port = int(setting["port"]) if setting["port_source"] == "stored" else None
    rec = read_service_record(Path(data_dir)) or {}

    if pin_command_line:
        bind_host = str(host or "127.0.0.1")
        if port:
            chosen = {"port": int(port), "source": "flag", "busy_skipped": []}
        elif running_port:
            chosen = {"port": int(running_port), "source": "running", "busy_skipped": []}
        else:
            chosen = choose_port(host=bind_host, requested=None, persisted=rec.get("port"), free=free)
        return {
            "pinned": True,
            "host": bind_host,
            "port": int(chosen["port"]),
            "mode": None,
            "mode_source": "command_line",
            "port_source": chosen["source"],
            "busy_skipped": list(chosen.get("busy_skipped") or []),
            "setting": current,
            "seed": None,
        }

    if host:
        mode, mode_source = _mode_for_host_flag(str(host), stored_mode), "flag"
    elif stored_mode:
        mode, mode_source = stored_mode, "stored"
    else:
        mode, mode_source = "localhost", "default"
    bind_host = MODE_BIND_HOST[mode]
    busy: List[int] = []
    if port:
        port_v, port_source = int(port), "flag"
    elif stored_port:
        port_v, port_source = stored_port, "stored"
    elif running_port:
        port_v, port_source = int(running_port), "running"
    else:
        chosen = choose_port(host="127.0.0.1", requested=None, persisted=rec.get("port"), free=free)
        port_v, port_source, busy = int(chosen["port"]), str(chosen["source"]), list(chosen.get("busy_skipped") or [])
    seed = None if (stored_mode == mode and stored_port == port_v) else {"mode": mode, "port": port_v}
    return {
        "pinned": False,
        "host": bind_host,
        "port": port_v,
        "mode": mode,
        "mode_source": mode_source,
        "port_source": port_source,
        "busy_skipped": busy,
        "setting": current,
        "seed": seed,
    }


def describe_seed(bind: Dict[str, Any]) -> str:
    """One line for the plan/--dry-run: what the setting write does (or not)."""
    if bind.get("pinned"):
        return (
            f"Network setting: not changed (--pin-command-line: `--host {bind['host']} --port {bind['port']}` on the "
            "command line override it)."
        )
    cur = bind.get("setting") or {}
    was = (
        f"'{cur.get('mode')}' on port {cur.get('port')}"
        if cur.get("source") == "stored" and cur.get("port_source") == "stored"
        else f"not fully stored (mode {cur.get('source')}, port {cur.get('port_source')})"
    )
    seed = bind.get("seed")
    if not seed:
        return f"Network setting: keeps '{bind['mode']}' on port {bind['port']} (already stored)."
    why = {"flag": "from --host", "stored": "the stored mode", "default": "the default: this machine only"}.get(str(bind.get("mode_source")), "")
    return (
        f"Network setting: stores '{seed['mode']}' ({why}) on port {seed['port']} "
        f"(port {'from --port' if bind.get('port_source') == 'flag' else 'source: ' + str(bind.get('port_source'))}); was {was}."
    )


def seed_network_setting(
    data_dir: Path,
    bind: Dict[str, Any],
    *,
    actor: str,
    env: Optional[Dict[str, str]] = None,
    status_kwargs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Write the bind into the Network setting through its ONE change door
    (`network_exposure.apply_network_change`: auth refusals, internet
    acknowledgement, audit fields). A refusal raises SystemExit with the
    reason and the fix: NOTHING is registered with a setting that cannot
    apply. Returns {action: seeded|unchanged|pinned, message, ...}."""
    if bind.get("pinned"):
        return {"action": "pinned", "message": describe_seed(bind)}
    seed = bind.get("seed")
    if not seed:
        return {"action": "unchanged", "mode": bind.get("mode"), "port": bind.get("port"), "message": describe_seed(bind)}
    from .network_exposure import apply_network_change

    cur = bind.get("setting") or {}
    # `internet` is only reachable here when it is ALREADY the stored,
    # acknowledged mode (a --host flag maps to localhost or lan).
    ack = seed["mode"] == "internet" and cur.get("mode") == "internet" and cur.get("source") == "stored"
    status, body = apply_network_change(
        Path(data_dir),
        mode=seed["mode"],
        port=seed["port"],
        acknowledge_internet=ack,
        actor=actor,
        env=dict(os.environ if env is None else env),
        in_process=False,
        status_kwargs=status_kwargs,
    )
    if status != 200:
        raise SystemExit(
            f"The Network setting refused '{seed['mode']}' on port {seed['port']}: {body.get('refused_reason')}"
            + (f" Fix: {body['fix']}" if body.get("fix") else "")
            + " Nothing was registered."
        )
    return {"action": "seeded", "mode": seed["mode"], "port": int(seed["port"]), "message": describe_seed(bind), "configured": body.get("configured")}


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def build_install_plan(
    *,
    platform: str,
    home: Path,
    host: str,
    port: int,
    data_dir: Path,
    exe_argv: Sequence[str],
    uid: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
    linux_mechanism: Optional[str] = None,
    pinned: bool = False,
    network: Optional[Dict[str, Any]] = None,
) -> ServicePlan:
    """The registration. `host`/`port` are the bind the NEXT start gets (the
    health URL, the notes); they reach the command line only when `pinned`.
    Otherwise the Network setting must hold them: `seed_network_setting`
    writes it before `execute_plan` (the CLI and `autostart` both do)."""
    plat = normalize_platform(platform)
    lm = "xdg" if (plat == "linux" and linux_mechanism == "xdg") else ("systemd" if plat == "linux" else None)
    target = service_file_path(plat, home, env, linux_mechanism=lm)
    plan = ServicePlan(
        action="install",
        platform=plat,
        host=host,
        port=int(port),
        data_dir=Path(data_dir),
        url=browser_base_url(host, port),
        exe_argv=list(exe_argv),
        mechanism=mechanism_name(plat, lm),
        pinned=bool(pinned),
        network=dict(network or {}),
    )
    plan.dirs.append(str(Path(data_dir)))
    plan.dirs.append(str(log_dir(plat, home, data_dir)))
    if pinned:
        plan.notes.append(
            f"--pin-command-line: the login item runs `serve --host {host} --port {int(port)}`; those flags override "
            "the Network setting (`abstractgateway network`), which therefore does NOT apply to this gateway "
            "(reported as overridden_by_cli). Re-run `service enable` without --pin-command-line to undo."
        )
        if not is_loopback_host(host):
            plan.notes.append(
                f"Binding {host} (non-loopback): the gateway refuses to start without explicit auth configuration "
                "(ABSTRACTGATEWAY_AUTH_TOKEN or ABSTRACTGATEWAY_USER_AUTH=1) in the service environment."
            )
    else:
        mode = str((network or {}).get("mode") or ("localhost" if is_loopback_host(host) else "lan"))
        plan.notes.append(
            f"Bind: the Network setting ('{mode}', {host}:{int(port)}) decides it at every start; the login item runs "
            "plain `serve`. Change it with `abstractgateway network set localhost|lan|internet [--port N]`, the tray's "
            "Network menu or the console, then restart the gateway."
        )
    if plat == "darwin":
        uid_v = os.getuid() if uid is None and hasattr(os, "getuid") else int(uid or 0)
        plan.files.append({"path": str(target), "content": render_launchd_plist(exe_argv=exe_argv, host=host, port=port, data_dir=data_dir, home=home, pinned=pinned), "mode": 0o644})
        domain = f"gui/{uid_v}"
        # bootout first: bootstrap refuses an already-loaded label (a reinstall).
        plan.commands.append(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
        plan.commands.append(["launchctl", "bootstrap", domain, str(target)])
        plan.notes.append(f"Logs: {log_dir('darwin', home, data_dir)}/gateway.err.log")
        plan.notes.append("A LaunchAgent runs while you are logged in (it starts again at every login).")
    elif plat == "linux" and lm == "xdg":
        launch = xdg_launch_argv(exe_argv=exe_argv, host=host, port=port, data_dir=data_dir, home=home, pinned=pinned)
        plan.files.append({"path": str(target), "content": render_xdg_desktop(launch_argv=launch), "mode": 0o644})
        plan.commands.append(["@start-detached", *launch])
        plan.notes.append(f"Logs: {log_dir('linux', home, data_dir) / 'gateway.log'}")
        plan.notes.append(
            "No systemd user manager here: an XDG autostart entry starts the gateway when you log in to a "
            "desktop session (not at boot, not over SSH)."
        )
    elif plat == "linux":
        plan.files.append({"path": str(target), "content": render_systemd_unit(exe_argv=exe_argv, host=host, port=port, data_dir=data_dir, home=home, pinned=pinned), "mode": 0o644})
        plan.commands.append(["systemctl", "--user", "daemon-reload"])
        plan.commands.append(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT])
        plan.notes.append(f"Logs: journalctl --user -u {SYSTEMD_UNIT} -f")
        plan.notes.append(
            "A user unit runs while you are logged in. To keep it running after logout / start it at boot, "
            "run once: loginctl enable-linger \"$USER\" (may ask for your password)."
        )
    else:
        plan.experimental = True
        logf = log_dir("windows", home, data_dir) / "gateway.log"
        launch = windows_launch_argv(exe_argv=exe_argv, host=host, port=port, data_dir=data_dir, log_file=logf, pinned=pinned)
        plan.registry.append({"op": "set", "key": WINDOWS_RUN_KEY, "name": WINDOWS_RUN_VALUE, "value": windows_join(launch)})
        # A "disabled" switch Task Manager left for an earlier registration
        # would silently veto the new one: registering means ON.
        plan.registry.append({"op": "delete", "key": WINDOWS_STARTUP_APPROVED_KEY, "name": WINDOWS_RUN_VALUE, "missing_ok": True})
        # The 2026-09-23 Startup shortcut would start a SECOND gateway at login.
        plan.remove.append(str(windows_startup_shortcut_path(home, env)))
        plan.commands.append(["@start-detached", *launch])
        plan.notes.append(f"EXPERIMENTAL on Windows: {windows_run_display()} (per user, no admin); not yet validated on a real Windows VM.")
        plan.notes.append(f"Logs: {logf}")
    return plan


def without_start(plan: ServicePlan) -> List[List[str]]:
    """The install commands minus "start it now" (the service still starts at login).

    macOS: a plist in ~/Library/LaunchAgents is loaded at the next login by
    itself, so no launchctl call. Linux: `enable` without `--now`. Windows:
    write the Run value (plan.registry), skip the detached start. Linux XDG:
    write the autostart entry, skip the detached start."""
    out: List[List[str]] = []
    for c in plan.commands:
        if c[:1] == ["launchctl"] or c[:1] == ["@start-detached"]:
            continue
        if c[:3] == ["systemctl", "--user", "enable"]:
            out.append([a for a in c if a != "--now"])
            continue
        out.append(list(c))
    return out


def build_uninstall_plan(
    *,
    platform: str,
    home: Path,
    data_dir: Path,
    uid: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
    stop: bool = True,
) -> ServicePlan:
    """Remove the login registration. `stop=True` (`service uninstall`) also
    stops a gateway the service manager started; `stop=False` (`service
    disable`, the tray's switch) only unregisters: the gateway serving you
    right now — possibly the tray's own parent — keeps running."""
    plat = normalize_platform(platform)
    rec = read_service_record(data_dir) or {}
    host = str(rec.get("host") or "127.0.0.1")
    port = int(rec.get("port") or DEFAULT_PORT)
    plan = ServicePlan(
        action="uninstall" if stop else "disable",
        platform=plat,
        host=host,
        port=port,
        data_dir=Path(data_dir),
        url=browser_base_url(host, port),
        exe_argv=list(rec.get("exe_argv") or []),
    )
    keeps_running = "A gateway already running keeps running until you quit it (tray: Quit, or the console's Shutdown)."
    if plat == "darwin":
        target = service_file_path(plat, home, env)
        plan.mechanism = mechanism_name(plat)
        if stop:
            uid_v = os.getuid() if uid is None and hasattr(os, "getuid") else int(uid or 0)
            plan.commands.append(["launchctl", "bootout", f"gui/{uid_v}/{LAUNCHD_LABEL}"])
        else:
            plan.notes.append(keeps_running + " launchd forgets the removed agent at logout.")
        plan.remove.append(str(target))
    elif plat == "linux":
        unit = service_file_path(plat, home, env, linux_mechanism="systemd")
        desktop = xdg_autostart_path(home, env)
        plan.mechanism = mechanism_name(plat, "xdg" if (desktop.exists() and not unit.exists()) else "systemd")
        # systemctl only when a unit exists: `disable` of an unknown unit fails
        # (and would abort an uninstall that has nothing to do there).
        if unit.exists():
            plan.commands.append(["systemctl", "--user", "disable", *(["--now"] if stop else []), SYSTEMD_UNIT])
            plan.remove.append(str(unit))
            plan.commands.append(["systemctl", "--user", "daemon-reload"])
        plan.remove.append(str(desktop))
        if not stop or desktop.exists():
            plan.notes.append(keeps_running)
    else:
        plan.experimental = True
        plan.mechanism = mechanism_name(plat)
        plan.registry.append({"op": "delete", "key": WINDOWS_RUN_KEY, "name": WINDOWS_RUN_VALUE, "missing_ok": True})
        plan.registry.append({"op": "delete", "key": WINDOWS_STARTUP_APPROVED_KEY, "name": WINDOWS_RUN_VALUE, "missing_ok": True})
        plan.remove.append(str(windows_startup_shortcut_path(home, env)))
        plan.notes.append(keeps_running)
    plan.remove.append(str(service_record_path(data_dir)))
    plan.notes.append(f"Your data is kept: {data_dir}")
    return plan


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=60)


def _start_detached(argv: Sequence[str]) -> None:  # pragma: no cover - spawns a real process
    if os.name == "nt":
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            flags |= int(getattr(subprocess, name, 0))
        subprocess.Popen(list(argv), creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    subprocess.Popen(list(argv), start_new_session=True, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


StartDetached = Callable[[Sequence[str]], None]


def execute_plan(
    plan: ServicePlan,
    *,
    runner: Optional[Runner] = None,
    echo: Callable[[str], None] = print,
    registry: Any = None,
    start_detached: Optional[StartDetached] = None,
) -> List[Dict[str, Any]]:
    """Run a plan. Doubles: `runner` (launchctl/systemctl), `registry`
    (Windows HKCU; default: the real one on Windows), `start_detached`."""
    run = runner or _default_runner
    spawn = start_detached or _start_detached
    results: List[Dict[str, Any]] = []
    for d in plan.dirs:
        Path(d).mkdir(parents=True, exist_ok=True)
    for f in plan.files:
        p = Path(f["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f["content"], encoding="utf-8")
        try:
            os.chmod(p, int(f.get("mode") or 0o644))
        except Exception:
            pass
        echo(f"wrote {p}")
    if plan.registry:
        reg = registry if registry is not None else default_registry(plan.platform)
        if reg is None:
            raise SystemExit(f"service {plan.action} needs the Windows registry ({windows_run_display()}); run it on Windows.")
        for op in plan.registry:
            where = f"HKCU\\{op['key']}\\{op['name']}"
            if op["op"] == "set":
                reg.set_string(op["key"], op["name"], str(op["value"]))
                results.append({"registry": "set", "key": op["key"], "name": op["name"], "value": op["value"]})
                echo(f"registry: set {where}")
            else:
                removed = bool(reg.delete(op["key"], op["name"]))
                if not removed and not op.get("missing_ok"):
                    raise SystemExit(f"service {plan.action} failed: {where} is not there to delete")
                results.append({"registry": "delete", "key": op["key"], "name": op["name"], "removed": removed})
                echo(f"registry: delete {where} -> {'removed' if removed else 'not present (fine)'}")
    for cmd in plan.commands:
        if cmd and cmd[0] == "@start-detached":
            spawn(cmd[1:])
            results.append({"argv": cmd, "returncode": 0})
            echo("started: " + " ".join(shlex.quote(c) for c in cmd[1:]))
            continue
        try:
            cp = run(cmd)
            rc, out, err = int(cp.returncode), str(cp.stdout or ""), str(cp.stderr or "")
        except FileNotFoundError as e:
            rc, out, err = 127, "", str(e)
        except subprocess.TimeoutExpired as e:
            rc, out, err = 124, "", f"timed out: {e}"
        results.append({"argv": list(cmd), "returncode": rc, "stdout": out[-2000:], "stderr": err[-2000:]})
        # `bootout` of a label that is not loaded fails; that is the state we want.
        tolerated = list(cmd[:2]) == ["launchctl", "bootout"]
        label = "ok" if rc == 0 else ("not loaded (fine)" if tolerated else f"FAILED rc={rc}")
        echo(f"ran: {' '.join(shlex.quote(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''} -> {label}")
        if rc != 0 and not tolerated:
            detail = (err or out).strip()
            raise SystemExit(f"service {plan.action} failed at: {' '.join(cmd[:4])}\n{detail}")
    for r in plan.remove:
        p = Path(r)
        if p.exists():
            p.unlink()
            results.append({"removed": str(p)})
            echo(f"removed {p}")
    return results


def write_service_record(plan: ServicePlan) -> Path:
    path = service_record_path(plan.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SERVICE_SCHEMA,
        "platform": plan.platform,
        "host": plan.host,
        "port": plan.port,
        "url": plan.url,
        "exe_argv": plan.exe_argv,
        "mechanism": plan.mechanism,
        "unit_path": plan.files[0]["path"] if plan.files else (windows_run_display() if plan.platform == "windows" else str(service_file_path(plan.platform, Path.home()))),
        "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "experimental": plan.experimental,
        # 2026-09-24: `pinned` false = plain `serve`, the Network setting binds;
        # true = --pin-command-line. A record WITHOUT the key predates it.
        "pinned": bool(plan.pinned),
        "bind_source": "command_line" if plan.pinned else "network_setting",
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def service_status(
    *,
    platform: Optional[str] = None,
    home: Optional[Path] = None,
    data_dir: Path,
    probe: bool = False,
    runner: Optional[Runner] = None,
    uid: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
    registry: Any = None,
) -> Dict[str, Any]:
    """Stable shape for `service status --json` and `config status --json`.

    `probe=False` (the default) never runs a subprocess: file presence + the
    persisted record. `probe=True` asks launchctl/systemctl whether it is loaded.
    Whether the registration would actually START the gateway (a stale plist
    pointing at a removed binary, a unit that is not enabled, a Run value
    Task Manager disabled) is `abstractgateway.autostart.autostart_status`."""
    plat = normalize_platform(platform)
    home = Path(home) if home is not None else Path.home()
    lm: Optional[str] = None
    if plat == "linux":
        unit = service_file_path(plat, home, env, linux_mechanism="systemd")
        lm = "xdg" if (not unit.exists() and xdg_autostart_path(home, env).exists()) else "systemd"
    target = service_file_path(plat, home, env, linux_mechanism=lm)
    installed = target.exists()
    unit_path = str(target)
    if plat == "windows":
        reg = registry if registry is not None else default_registry(plat)
        value = None
        if reg is not None:
            try:
                value = reg.get(WINDOWS_RUN_KEY, WINDOWS_RUN_VALUE)
            except Exception:  # noqa: BLE001 - status never raises
                value = None
        if value:
            installed, unit_path = True, windows_run_display()
    rec = read_service_record(data_dir)
    out: Dict[str, Any] = {
        "schema": SERVICE_SCHEMA,
        "platform": plat,
        "mechanism": mechanism_name(plat, lm),
        "installed": installed,
        "unit_path": unit_path,
        "port": (rec or {}).get("port"),
        "host": (rec or {}).get("host"),
        "url": (rec or {}).get("url"),
        "installed_at": (rec or {}).get("installed_at"),
        "record_for_this_data_dir": rec is not None,
        # True/False from the record; None = no record, or one written before
        # 2026-09-24 (whose registration pins --host/--port: see
        # autostart_status for the read-back and the repair wording).
        "pinned": (rec or {}).get("pinned") if isinstance((rec or {}).get("pinned"), bool) else None,
        "loaded": None,
        "experimental": plat == "windows",
    }
    if probe and out["installed"] and (plat == "darwin" or (plat == "linux" and lm == "systemd")):
        run = runner or _default_runner
        if plat == "darwin":
            uid_v = os.getuid() if uid is None and hasattr(os, "getuid") else int(uid or 0)
            cmd = ["launchctl", "print", f"gui/{uid_v}/{LAUNCHD_LABEL}"]
        else:
            cmd = ["systemctl", "--user", "is-active", SYSTEMD_UNIT]
        try:
            cp = run(cmd)
            out["loaded"] = int(cp.returncode) == 0
        except Exception as e:  # noqa: BLE001
            out["loaded"] = None
            out["probe_error"] = str(e)
    return out


def wait_for_health(url: str, *, timeout_s: float = 60.0, interval_s: float = 1.0) -> bool:
    """Bounded poll of `<url>/api/health` (the service we just started)."""
    import urllib.request

    deadline = time.monotonic() + max(1.0, float(timeout_s))
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=3) as r:  # noqa: S310 - our own loopback URL
                if int(getattr(r, "status", 200)) == 200:
                    return True
        except Exception:
            pass
        time.sleep(interval_s)
    return False


# ---------------------------------------------------------------------------
# Windows launcher: `pythonw -m abstractgateway.os_service launch --log-file F -- serve ...`
# ---------------------------------------------------------------------------


def launch_main(argv: Optional[List[str]] = None) -> None:
    """Re-open stdout/stderr onto a log file, then run the gateway CLI.

    Under `pythonw.exe` there is no console: `sys.stdout`/`sys.stderr` are
    None, which silently swallows prints and breaks logging handlers. The
    service shortcut therefore starts the gateway through this launcher."""
    import argparse

    p = argparse.ArgumentParser(prog="python -m abstractgateway.os_service")
    sub = p.add_subparsers(dest="cmd", required=True)
    la = sub.add_parser("launch")
    la.add_argument("--log-file", required=True)
    la.add_argument("--data-dir", default=None)
    la.add_argument("rest", nargs=argparse.REMAINDER)
    args = p.parse_args(argv)
    log = Path(args.log_file)
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives for the process
    sys.stdout = fh
    sys.stderr = fh
    if args.data_dir:
        os.environ["ABSTRACTGATEWAY_DATA_DIR"] = str(args.data_dir)
    rest = list(args.rest or [])
    if rest and rest[0] == "--":
        rest = rest[1:]
    from .cli import main as gateway_main

    gateway_main(rest)


if __name__ == "__main__":  # pragma: no cover
    launch_main()
