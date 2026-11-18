"""The AbstractGateway icon on the Windows desktop and in the Start menu.

For people who do not start the gateway at login: a double-click starts it (when it is not
already running) and opens its console.

- `abstractgateway shortcut install|remove` writes or deletes `AbstractGateway.lnk` in the
  user's Desktop and Start-menu folders (no admin).
- The shortcut runs `pythonw -m abstractgateway.desktop_shortcut --data-dir D` (`open_main`):
  no console window. It starts the gateway exactly as the login item does (the same launcher,
  `os_service.launch_main`: the log file, the tray icon, the Network setting's address), waits
  until it answers, then opens the console the way the tray does (`tray.signin.console_link`).
  A failure is a message box naming the log.
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

START_WAIT_S = 180.0  # the first start loads the engines
ICON = Path(__file__).with_name("assets") / "AbstractGateway.ico"

# The two folders are asked from Windows (a Desktop moved to OneDrive is still the Desktop).
# No target (remove): the shortcuts are deleted.
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in 'Desktop', 'Programs') {
    $path = Join-Path ([Environment]::GetFolderPath($folder)) ($env:AG_SHORTCUT_NAME + '.lnk')
    if ($env:AG_SHORTCUT_TARGET) {
        $lnk = $shell.CreateShortcut($path)
        $lnk.TargetPath = $env:AG_SHORTCUT_TARGET
        $lnk.Arguments = $env:AG_SHORTCUT_ARGS
        $lnk.WorkingDirectory = $env:AG_SHORTCUT_DIR
        $lnk.IconLocation = $env:AG_SHORTCUT_ICON
        $lnk.Description = 'Start AbstractGateway and open its console'
        $lnk.Save()
    } elseif (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    } else { continue }
    $path
}
"""


def launch_argv(data_dir: Path) -> List[str]:
    """What the shortcut runs."""
    from .os_service import windows_pythonw

    return [windows_pythonw(sys.executable), "-m", "abstractgateway.desktop_shortcut", "--data-dir", str(data_dir)]


def write_shortcuts(data_dir: Optional[Path]) -> List[str]:
    """Create the two shortcuts for `data_dir`, or delete them (None). Returns the paths touched."""
    from .os_service import windows_join, windows_run_value

    env = dict(os.environ, AG_SHORTCUT_NAME=windows_run_value(), AG_SHORTCUT_TARGET="")
    if data_dir is not None:
        argv = launch_argv(data_dir)
        # Started in the home folder: what the gateway starts (a terminal app) must not inherit its data folder.
        env.update(AG_SHORTCUT_TARGET=argv[0], AG_SHORTCUT_ARGS=windows_join(argv[1:]), AG_SHORTCUT_DIR=str(Path.home()), AG_SHORTCUT_ICON=str(ICON))
    powershell = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    encoded = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")
    done = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=60, stdin=subprocess.DEVNULL,
    )
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout or "powershell failed").strip())
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def _answers(url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=3) as r:  # noqa: S310 - this machine's gateway
            return int(getattr(r, "status", 200)) == 200
    except Exception:
        return False


def start_and_open(data_dir: Path) -> Optional[str]:
    """Start the gateway of `data_dir` unless it runs, wait for it, open its console.
    Returns None, or the sentence to show when it did not come up."""
    from .first_run import local_gateway
    from .os_service import _start_detached, current_gateway_argv, log_dir, windows_launch_argv
    from .tray.signin import console_link

    log_file = log_dir(sys.platform, Path.home(), data_dir) / "gateway.log"
    here = local_gateway(data_dir)
    if not _answers(here["url"]):
        # A live serve record: it is starting (a second double-click), so only wait.
        if not (here["serve"] or {}).get("alive"):
            _start_detached(windows_launch_argv(exe_argv=current_gateway_argv(), host="", port=0, data_dir=data_dir, log_file=log_file))
        deadline = time.monotonic() + START_WAIT_S
        while True:
            time.sleep(1.0)
            here = local_gateway(data_dir)  # the address it took is in its serve record
            if _answers(here["url"]):
                break
            if time.monotonic() > deadline:
                return f"AbstractGateway did not start within {int(START_WAIT_S)} seconds.\n\nIts log: {log_file}"
    import webbrowser

    url = console_link(here["url"], "/console", data_dir).url
    if not webbrowser.open(url):
        return f"AbstractGateway is running, but no browser could be opened.\n\nIts console: {here['url']}/console"
    return None


def open_main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(prog="python -m abstractgateway.desktop_shortcut")
    p.add_argument("--data-dir", required=True)
    args = p.parse_args(argv)
    data_dir = Path(args.data_dir)
    os.environ["ABSTRACTGATEWAY_DATA_DIR"] = str(data_dir)
    try:
        problem = start_and_open(data_dir)
    except Exception as exc:  # noqa: BLE001 - under pythonw nothing else would show it
        problem = f"AbstractGateway could not be started.\n\n{type(exc).__name__}: {exc}"
    if problem and os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, problem, "AbstractGateway", 0x30)  # type: ignore[attr-defined]
    raise SystemExit(1 if problem else 0)


def add_shortcut_subparser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    sc = sub.add_parser("shortcut", help="Windows: the AbstractGateway icon on the desktop and in the Start menu (starts the gateway, opens its console)")
    ssub = sc.add_subparsers(dest="shortcut_cmd", required=True)
    for name, help_text in (("install", "Create the two shortcuts"), ("remove", "Delete them")):
        s = ssub.add_parser(name, help=help_text)
        s.add_argument("--data-dir", default=None, help="Gateway data dir (default: same resolution as `serve`)")


def run_shortcut(args: argparse.Namespace) -> int:
    if os.name != "nt":
        print("`abstractgateway shortcut` is for Windows.", file=sys.stderr)
        return 2
    from .firstrun_cli import _apply_data_dir_flag
    from .host_paths import apply_data_dir_default
    from .instances import apply_instance_env

    _apply_data_dir_flag(getattr(args, "data_dir", None))
    res = apply_data_dir_default()
    apply_instance_env(res.path)
    install = args.shortcut_cmd == "install"
    try:
        paths = write_shortcuts(res.path if install else None)
    except Exception as exc:  # noqa: BLE001
        print(f"could not {'create' if install else 'delete'} the shortcuts: {exc}", file=sys.stderr)
        return 1
    for path in paths:
        print(f"{'created' if install else 'deleted'} {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    open_main()
