"""Sign a terminal app in to this gateway, then become it (mission Y, 2026-09-24).

Run by the one-use launcher script the gateway writes when the console (or
the tray) asks for "Open in Terminal" (`apps_manager.launch_tui`):

    <python> -I tui_signin.py --gateway <url> --app <id> -- <terminal app binary>

The launcher puts a ONE-TIME handover code (2 minutes, single use) in this
process's environment. This helper trades it on loopback at
`POST <gateway>/apps/tui-handover` for a bearer token that works from this
machine only and acts as whoever clicked, then replaces itself with the
terminal app, the token in the app's ENVIRONMENT (`ABSTRACTCODE_GATEWAY_TOKEN`
for Code) — never on its command line, never in a file.

Standard library only, on purpose: it runs with `python -I` (no site
packages, no PYTHON* variables) and must never import the gateway.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import List, Optional

HANDOVER_ENV = "ABSTRACTGATEWAY_TUI_HANDOVER"


def _pause(message: str) -> int:
    print(message, file=sys.stderr, flush=True)
    try:
        if sys.stdin and sys.stdin.isatty():
            input("Press Enter to close this window. ")
    except (EOFError, KeyboardInterrupt):
        pass
    return 1


def redeem(gateway: str, code: str, *, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        gateway.rstrip("/") + "/apps/tui-handover",
        data=json.dumps({"code": code}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "abstractgateway-tui-signin"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - the gateway on loopback
        return json.loads(resp.read().decode("utf-8"))


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    gateway, app_id, binary = "", "", ""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--gateway" and i + 1 < len(args):
            gateway, i = args[i + 1], i + 2
        elif a == "--app" and i + 1 < len(args):
            app_id, i = args[i + 1], i + 2
        elif a == "--":
            binary = args[i + 1] if i + 1 < len(args) else ""
            break
        else:
            return _pause(f"Unexpected argument {a!r}.")
    code = os.environ.pop(HANDOVER_ENV, "")
    if not (gateway and app_id and binary):
        return _pause("This launcher is incomplete. Open the app again from the gateway console.")
    if not code:
        return _pause("This launcher has no sign-in code. Open the app again from the gateway console.")
    try:
        out = redeem(gateway, code)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {}
        msg = body.get("message") or f"HTTP {exc.code}"
        hint = body.get("hint") or "Open the app again from the gateway console."
        return _pause(f"Could not sign in to the gateway at {gateway}: {msg}\n{hint}")
    except Exception as exc:  # noqa: BLE001
        return _pause(f"Could not reach the gateway at {gateway}: {exc}\nIs it still running? Open the app again from the gateway console.")
    token = str(out.get("token") or "")
    token_env = str(out.get("token_env") or "")
    url = str(out.get("gateway_url") or gateway)
    flag = str(out.get("gateway_flag") or "--gateway")
    if not token or not token_env:
        return _pause("The gateway did not hand over a sign-in. Open the app again from the gateway console.")
    env = dict(os.environ)
    env[token_env] = token
    if out.get("url_env"):
        env[str(out["url_env"])] = url
    argv_app = [binary, flag, url]
    print(f"Signed in to {url} as {out.get('user') or 'you'}. Starting {os.path.basename(binary)}…", flush=True)
    if sys.platform.startswith("win"):
        try:
            return subprocess.call(argv_app, env=env)
        except OSError as exc:
            return _pause(f"Could not start {binary}: {exc}")
    try:
        os.execve(binary, argv_app, env)
    except OSError as exc:
        return _pause(f"Could not start {binary}: {exc}")
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main())
