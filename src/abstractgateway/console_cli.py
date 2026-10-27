"""`abstractgateway console [--url URL] [--token TOKEN]` — open the terminal console signed in.

The terminal console is the Rust app `abstractgateway-console` (the installer builds it into
the folder that holds `abstractgateway`; by hand: `cargo install abstractgateway-console`).
This verb finds it, resolves the gateway URL and token the way every other verb does
(`--url` / `--token`, else the environment, else this data dir's running gateway and its
bootstrap admin token for a loopback URL) and replaces this process with it, so the console
owns the terminal. The token goes to the console on its command line, never to this
command's output.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional

EXIT_OK = 0
EXIT_REFUSED = 2

BINARY = "abstractgateway-console"
INSTALL_COMMAND = f"cargo install {BINARY}"


def add_console_subparser(sub: Any) -> None:
    p = sub.add_parser(
        "console",
        help="Open the terminal console (abstractgateway-console) signed in to this gateway",
        description=(
            "Open the gateway's terminal console, signed in. Finds abstractgateway-console next to this "
            "command (the installer builds it there) or on PATH, and starts it with the gateway's address "
            "and your token."
        ),
    )
    p.add_argument("--url", "--gateway-url", dest="url", default=None, help="Gateway base URL (default: $ABSTRACTGATEWAY_URL, else the gateway running for this data dir, else http://127.0.0.1:8080)")
    p.add_argument("--token", default=None, help="Your token (default: $ABSTRACTGATEWAY_AUTH_TOKEN, else this data dir's admin token for a loopback URL)")
    p.add_argument("--data-dir", default=None, help="Gateway data dir used to find the running gateway and its token")


def exe_name(platform: str = sys.platform) -> str:
    return BINARY + (".exe" if platform.startswith("win") else "")


def find_console_binary(*, argv0: Optional[str] = None, platform: str = sys.platform, which: Any = None) -> Optional[Path]:
    """Where the terminal console is: the folder of the `abstractgateway` command that runs
    (the installer builds the console there), then uv's tool bin folder, then PATH, then
    ~/.cargo/bin. None when it is nowhere."""
    name = exe_name(platform)
    candidates: List[Path] = []
    head = argv0 if argv0 is not None else sys.argv[0]
    if head:
        try:
            candidates.append(Path(head).resolve().parent / name)
        except OSError:
            pass
    try:
        from .apps_manager import uv_tool_bin_dir

        tool_bin = uv_tool_bin_dir()
        if tool_bin is not None:
            candidates.append(tool_bin / name)
    except Exception:  # noqa: BLE001 - no receipt, no folder
        pass
    found = (which or shutil.which)(BINARY)
    if found:
        candidates.append(Path(found))
    candidates.append(Path.home() / ".cargo" / "bin" / name)
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def console_argv(binary: Path, url: str, token: str) -> List[str]:
    argv = [str(binary), "--gateway-url", url]
    if token:
        argv += ["--token", token]
    return argv


def run_console(args: argparse.Namespace, *, execv: Any = None, platform: str = sys.platform, binary: Optional[Path] = None) -> int:
    from .models_engines_cli import _resolve_connection

    exe = binary or find_console_binary(platform=platform)
    if exe is None:
        print(
            f"The terminal console ({BINARY}) is not installed. The AbstractFramework installer builds it: "
            f"run the one-line install again, or `{INSTALL_COMMAND}`.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    url, token, source = _resolve_connection(args)
    if not token:
        print(f"No token found for {url} ({source}); the console asks for one on its Connection screen.", file=sys.stderr)
    argv = console_argv(exe, url, token)
    if platform.startswith("win"):
        return int(subprocess.call(argv))
    sys.stdout.flush()
    sys.stderr.flush()
    (execv or os.execv)(str(exe), argv)
    return EXIT_OK  # an injected execv that returns (tests)
