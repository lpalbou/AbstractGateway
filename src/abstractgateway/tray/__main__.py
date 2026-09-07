"""`python -m abstractgateway.tray` — the helper's entry point.

    python -m abstractgateway.tray [--parent-pid N]      the tray icon (default)
    python -m abstractgateway.tray monitor               the Activity window

Both read ONE JSON line on stdin: the handshake the parent wrote (base URL,
token, pids, data dir, version). Nothing secret is on argv or in the
environment. Without a handshake (a human running it by hand) the helper
explains what it needs and exits 2.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, Optional


def _read_handshake(*, allow_env_fallback: bool = True) -> Optional[Dict[str, Any]]:
    line = ""
    try:
        if not sys.stdin.isatty():
            line = sys.stdin.readline()
    except Exception:
        line = ""
    if line.strip():
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                return data
        except Exception:
            return None
    return None


def _configure_logging() -> None:
    logging.basicConfig(level=logging.WARNING, format="[tray] %(levelname)s %(name)s: %(message)s", stream=sys.stderr)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="abstractgateway.tray", description="AbstractGateway desktop tray helper")
    parser.add_argument("mode", nargs="?", default="tray", choices=["tray", "monitor"], help="tray icon (default) or the Activity window")
    parser.add_argument("--parent-pid", type=int, default=0, help="pid of the process that spawned this helper (exit when it is gone)")
    args = parser.parse_args(argv)
    _configure_logging()

    handshake = _read_handshake()
    if handshake is None:
        # A human ran this by hand: explain (stderr) — no ready line is
        # written because no supervisor is listening.
        sys.stderr.write(
            "This helper is started by `abstractgateway serve` and expects a JSON handshake on stdin.\n"
            "Run the gateway instead: abstractgateway serve  (the tray icon appears when the `tray` extra is installed).\n"
        )
        return 2
    if args.parent_pid and not handshake.get("parent_pid"):
        handshake["parent_pid"] = int(args.parent_pid)

    if args.mode == "monitor":
        from .monitor import run_monitor

        return int(run_monitor(handshake))

    from .app import TrayApp

    return int(TrayApp(handshake).run())


if __name__ == "__main__":
    sys.exit(main())
