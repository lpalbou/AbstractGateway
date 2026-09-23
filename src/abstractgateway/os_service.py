"""`abstractgateway service install|uninstall|status`: start the gateway at login,
per user, without admin rights (2026-09-23).

- macOS: a LaunchAgent `~/Library/LaunchAgents/ai.abstractframework.gateway.plist`
  (`RunAtLoad`, `KeepAlive`, logs under `~/Library/Logs/AbstractGateway/`),
  loaded with `launchctl bootstrap gui/<uid>`.
- Linux: a systemd USER unit `~/.config/systemd/user/abstractgateway.service`
  (`Restart=on-failure`), enabled with `systemctl --user enable --now`. It runs
  only while the user is logged in unless lingering is enabled
  (`loginctl enable-linger $USER`, printed as a hint, never run for you).
- Windows (EXPERIMENTAL, unverified on a real VM): a Startup-folder shortcut
  that launches `pythonw.exe -m abstractgateway.os_service launch ...`, so no
  console window appears, with output going to a log file.

Every generated artifact is a pure function of (platform, home, executable,
host, port, data dir), so tests render all three OSes on any OS. `--dry-run`
prints the files and the exact commands and touches nothing.

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
WINDOWS_SHORTCUT = "AbstractGateway.lnk"
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
    notes: List[str] = field(default_factory=list)
    experimental: bool = False

    def public_dict(self) -> Dict[str, Any]:
        return {
            "schema": SERVICE_SCHEMA,
            "action": self.action,
            "platform": self.platform,
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
            "notes": list(self.notes),
            "experimental": bool(self.experimental),
        }


# ---------------------------------------------------------------------------
# Paths and executable
# ---------------------------------------------------------------------------


def service_file_path(platform: str, home: Path, env: Optional[Dict[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    plat = normalize_platform(platform)
    if plat == "darwin":
        return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    if plat == "windows":
        appdata = str(env.get("APPDATA") or "").strip()
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / WINDOWS_SHORTCUT
    xdg = str(env.get("XDG_CONFIG_HOME") or "").strip()
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".config"
    return base / "systemd" / "user" / SYSTEMD_UNIT


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


def _serve_args(host: str, port: int) -> List[str]:
    return ["serve", "--host", str(host), "--port", str(int(port))]


def render_launchd_plist(*, exe_argv: Sequence[str], host: str, port: int, data_dir: Path, home: Path) -> str:
    logs = log_dir("darwin", home, data_dir)
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [*exe_argv, *_serve_args(host, port)],
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


def render_systemd_unit(*, exe_argv: Sequence[str], host: str, port: int, data_dir: Path, home: Path) -> str:
    argv = [_systemd_path(a, home) for a in exe_argv]
    exec_start = " ".join(_systemd_quote(a) for a in [*argv, *_serve_args(host, port)])
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


def windows_launch_argv(*, exe_argv: Sequence[str], host: str, port: int, data_dir: Path, log_file: Path) -> List[str]:
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
        *_serve_args(host, port),
    ]


def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _win_arg(a: str) -> str:
    return '"' + a.replace('"', '\\"') + '"' if (not a or any(c in a for c in ' \t"')) else a


def render_windows_shortcut_script(*, shortcut: Path, launch_argv: Sequence[str], data_dir: Path) -> str:
    args = " ".join(_win_arg(a) for a in launch_argv[1:])
    return (
        "$ErrorActionPreference = 'Stop'; "
        f"New-Item -ItemType Directory -Force -Path {_ps_quote(str(shortcut.parent))} | Out-Null; "
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(" + _ps_quote(str(shortcut)) + "); "
        f"$s.TargetPath = {_ps_quote(launch_argv[0])}; "
        f"$s.Arguments = {_ps_quote(args)}; "
        f"$s.WorkingDirectory = {_ps_quote(str(data_dir))}; "
        "$s.WindowStyle = 7; "
        "$s.Description = 'AbstractGateway (starts at login)'; "
        "$s.Save()"
    )


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
) -> ServicePlan:
    plat = normalize_platform(platform)
    target = service_file_path(plat, home, env)
    plan = ServicePlan(
        action="install",
        platform=plat,
        host=host,
        port=int(port),
        data_dir=Path(data_dir),
        url=browser_base_url(host, port),
        exe_argv=list(exe_argv),
    )
    plan.dirs.append(str(Path(data_dir)))
    plan.dirs.append(str(log_dir(plat, home, data_dir)))
    if not is_loopback_host(host):
        plan.notes.append(
            f"Binding {host} (non-loopback): the gateway refuses to start without explicit auth configuration "
            "(ABSTRACTGATEWAY_AUTH_TOKEN or ABSTRACTGATEWAY_USER_AUTH=1) in the service environment."
        )
    if plat == "darwin":
        uid_v = os.getuid() if uid is None and hasattr(os, "getuid") else int(uid or 0)
        plan.files.append({"path": str(target), "content": render_launchd_plist(exe_argv=exe_argv, host=host, port=port, data_dir=data_dir, home=home), "mode": 0o644})
        domain = f"gui/{uid_v}"
        # bootout first: bootstrap refuses an already-loaded label (a reinstall).
        plan.commands.append(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
        plan.commands.append(["launchctl", "bootstrap", domain, str(target)])
        plan.notes.append(f"Logs: {log_dir('darwin', home, data_dir)}/gateway.err.log")
        plan.notes.append("A LaunchAgent runs while you are logged in (it starts again at every login).")
    elif plat == "linux":
        plan.files.append({"path": str(target), "content": render_systemd_unit(exe_argv=exe_argv, host=host, port=port, data_dir=data_dir, home=home), "mode": 0o644})
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
        launch = windows_launch_argv(exe_argv=exe_argv, host=host, port=port, data_dir=data_dir, log_file=logf)
        script = render_windows_shortcut_script(shortcut=target, launch_argv=launch, data_dir=data_dir)
        plan.commands.append(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script])
        plan.commands.append(["@start-detached", *launch])
        plan.notes.append("EXPERIMENTAL on Windows: a Startup-folder shortcut (no admin); not yet validated on a real Windows VM.")
        plan.notes.append(f"Logs: {logf}")
    return plan


def without_start(plan: ServicePlan) -> List[List[str]]:
    """The install commands minus "start it now" (the service still starts at login).

    macOS: a plist in ~/Library/LaunchAgents is loaded at the next login by
    itself, so no launchctl call. Linux: `enable` without `--now`. Windows:
    create the shortcut, skip the detached start."""
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
) -> ServicePlan:
    plat = normalize_platform(platform)
    target = service_file_path(plat, home, env)
    rec = read_service_record(data_dir) or {}
    host = str(rec.get("host") or "127.0.0.1")
    port = int(rec.get("port") or DEFAULT_PORT)
    plan = ServicePlan(action="uninstall", platform=plat, host=host, port=port, data_dir=Path(data_dir), url=browser_base_url(host, port), exe_argv=list(rec.get("exe_argv") or []))
    if plat == "darwin":
        uid_v = os.getuid() if uid is None and hasattr(os, "getuid") else int(uid or 0)
        plan.commands.append(["launchctl", "bootout", f"gui/{uid_v}/{LAUNCHD_LABEL}"])
        plan.remove.append(str(target))
    elif plat == "linux":
        plan.commands.append(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
        plan.remove.append(str(target))
        plan.commands.append(["systemctl", "--user", "daemon-reload"])
    else:
        plan.experimental = True
        plan.remove.append(str(target))
        plan.notes.append(
            "The shortcut is removed; a gateway already running keeps running until you stop it "
            "(tray: Quit, or the console's Shutdown)."
        )
    plan.remove.append(str(service_record_path(data_dir)))
    plan.notes.append(f"Your data is kept: {data_dir}")
    return plan


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=60)


def _start_detached(argv: Sequence[str]) -> None:  # pragma: no cover - Windows only
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= int(getattr(subprocess, name, 0))
    subprocess.Popen(list(argv), creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def execute_plan(plan: ServicePlan, *, runner: Optional[Runner] = None, echo: Callable[[str], None] = print) -> List[Dict[str, Any]]:
    run = runner or _default_runner
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
    for cmd in plan.commands:
        if cmd and cmd[0] == "@start-detached":
            _start_detached(cmd[1:])
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
        "unit_path": plan.files[0]["path"] if plan.files else str(service_file_path(plan.platform, Path.home())),
        "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "experimental": plan.experimental,
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
) -> Dict[str, Any]:
    """Stable shape for `service status --json` and `config status --json`.

    `probe=False` (the default) never runs a subprocess: file presence + the
    persisted record. `probe=True` asks launchctl/systemctl whether it is loaded."""
    plat = normalize_platform(platform)
    home = Path(home) if home is not None else Path.home()
    target = service_file_path(plat, home, env)
    rec = read_service_record(data_dir)
    out: Dict[str, Any] = {
        "schema": SERVICE_SCHEMA,
        "platform": plat,
        "mechanism": {"darwin": "launchd-agent", "linux": "systemd-user", "windows": "startup-shortcut"}[plat],
        "installed": target.exists(),
        "unit_path": str(target),
        "port": (rec or {}).get("port"),
        "host": (rec or {}).get("host"),
        "url": (rec or {}).get("url"),
        "installed_at": (rec or {}).get("installed_at"),
        "record_for_this_data_dir": rec is not None,
        "loaded": None,
        "experimental": plat == "windows",
    }
    if probe and out["installed"] and plat in {"darwin", "linux"}:
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
