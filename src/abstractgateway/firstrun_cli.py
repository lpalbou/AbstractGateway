"""CLI verbs for the no-terminal first run (2026-09-23):

- `abstractgateway claim` / `abstractgateway-config claim-url [--open]`
- `abstractgateway service install|uninstall|status`

Both are LOCAL commands: they act on this machine's data dir and service
manager, never through the HTTP API, so they work before anyone has a token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional


def _stderr(line: str) -> None:
    print(str(line), file=sys.stderr)


def _apply_data_dir_flag(data_dir: Optional[str]) -> None:
    from .host_paths import DATA_DIR_SOURCE_ENV

    if data_dir:
        os.environ["ABSTRACTGATEWAY_DATA_DIR"] = str(Path(data_dir).expanduser().resolve())
        os.environ.pop(DATA_DIR_SOURCE_ENV, None)


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


def add_claim_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--url", default=None, help="Gateway base URL (default: the running gateway for this data dir, else http://127.0.0.1:<port>)")
    p.add_argument("--host", default="127.0.0.1", help="Host for the link when --port is given (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=None, help="Port for the link (default: the running gateway's port, else the installed service's, else 8080)")
    p.add_argument("--data-dir", default=None, help="Gateway data dir (default: same resolution as `serve`)")
    p.add_argument("--ttl-s", type=int, default=600, help="Link lifetime in seconds (default: 600, max 3600)")
    p.add_argument("--open", action="store_true", help="Open the link in the default browser")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")


def _claim_base_url(args: argparse.Namespace, data_dir: Path) -> Dict[str, Any]:
    from .first_run import browser_base_url, read_serve_record
    from .os_service import read_service_record

    if args.url:
        return {"url": str(args.url).rstrip("/"), "source": "flag", "serve": None}
    rec = read_serve_record(data_dir)
    if args.port:
        return {"url": browser_base_url(args.host or "127.0.0.1", int(args.port)), "source": "flag", "serve": rec}
    if rec and rec.get("alive") is not False and rec.get("url"):
        return {"url": str(rec["url"]), "source": "serve_record", "serve": rec}
    svc = read_service_record(data_dir)
    if svc and svc.get("url"):
        return {"url": str(svc["url"]), "source": "service_record", "serve": rec}
    return {"url": browser_base_url("127.0.0.1", 8080), "source": "default", "serve": rec}


def run_claim(args: argparse.Namespace) -> int:
    """Mint a one-time claim code and print its console link. Exit 0 / 2 (refused)."""
    from .config_cli import ensure_bootstrap_admin_user
    from .first_run import claim_url, mint_claim
    from .host_paths import apply_data_dir_default

    _apply_data_dir_flag(getattr(args, "data_dir", None))
    res = apply_data_dir_default()
    base = _claim_base_url(args, res.path)
    serve = base.get("serve") or {}
    running = bool(serve) and serve.get("alive") is not False
    auth = serve.get("auth") if isinstance(serve.get("auth"), dict) else {}
    if running and auth and not bool(auth.get("user_auth_enabled")):
        msg = (
            f"The gateway running at {serve.get('url')} (data dir {res.path}) does not use user auth "
            f"(auth mode: {auth.get('mode')}); it would refuse a first-run link. Sign in with its token, "
            "or restart it on 127.0.0.1 without ABSTRACTGATEWAY_AUTH_TOKEN."
        )
        if args.json:
            print(json.dumps({"ok": False, "reason_code": "claim_requires_user_auth", "message": msg}, indent=2))
        else:
            _stderr(msg)
        return 2

    admin = ensure_bootstrap_admin_user()
    user = admin.get("user") if isinstance(admin.get("user"), dict) else {}
    minted = mint_claim(
        data_dir=res.path,
        tenant_id=str(user.get("tenant_id") or "default"),
        user_id=str(user.get("user_id") or "admin"),
        ttl_s=int(getattr(args, "ttl_s", 600) or 600),
        created_by="cli",
    )
    url = claim_url(base["url"], minted["code"])
    payload = {
        "ok": True,
        "url": url,
        "base_url": base["url"],
        "base_url_source": base["source"],
        "expires_at": minted["expires_at"],
        "ttl_s": minted["ttl_s"],
        "user": f"{minted['tenant_id']}/{minted['user_id']}",
        "data_dir": str(res.path),
        "data_dir_source": res.source,
        "gateway_running": running,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(url)
        _stderr(f"One-time sign-in link for {payload['user']} (valid until {minted['expires_at']}, works once, from this machine only).")
        if not running:
            _stderr(
                f"Note: no running gateway recorded for data dir {res.path}; the link assumes {base['url']}. "
                "Start it with `abstractgateway serve` (or pass --port/--url)."
            )
    if getattr(args, "open", False):
        try:
            import webbrowser

            opened = webbrowser.open(url)
        except Exception:
            opened = False
        if not opened:
            _stderr("Could not open a browser here; paste the link above into one on this machine.")
    return 0


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


def add_service_subparser(sub: Any) -> None:
    svc = sub.add_parser("service", help="Start the gateway at login (LaunchAgent / systemd user unit / Windows Startup)")
    ssub = svc.add_subparsers(dest="service_cmd", required=True)
    for name, help_text in (
        ("install", "Install and start the per-user login service"),
        ("uninstall", "Stop and remove the login service (data is kept)"),
        ("status", "Show whether the login service is installed and loaded"),
    ):
        p = ssub.add_parser(name, help=help_text)
        p.add_argument("--data-dir", default=None, help="Gateway data dir (default: same resolution as `serve`)")
        p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
        if name == "install":
            p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
            p.add_argument("--port", type=int, default=None, help="Port (default: the previously installed port, else the first free from 8080)")
            p.add_argument("--dry-run", action="store_true", help="Print the files and commands; change nothing")
            p.add_argument("--no-start", action="store_true", help="Write the service file only (do not load/start it)")
            p.add_argument("--no-wait", action="store_true", help="Do not wait for /api/health after starting")
            p.add_argument("--wait-s", type=float, default=60.0, help="Seconds to wait for /api/health (default: 60)")
            p.add_argument("--no-claim", action="store_true", help="Do not mint a first-run sign-in link after start")
        if name == "uninstall":
            p.add_argument("--dry-run", action="store_true", help="Print what would be removed; change nothing")


def _print_plan(plan: Any) -> None:
    d = plan.public_dict()
    print(f"# abstractgateway service {d['action']} ({d['platform']}{', EXPERIMENTAL' if d['experimental'] else ''})")
    print(f"# data dir: {d['data_dir']}")
    if d["action"] == "install":
        print(f"# console:  {d['console_url']}")
    for dd in d.get("dirs") or []:
        print(f"mkdir -p {dd}")
    for f in d["files"]:
        print(f"# --- write {f['path']} (mode {f['mode']}) ---")
        print(f["content"].rstrip("\n"))
        print("# --- end ---")
    for c in d["commands"]:
        import shlex

        shown = [a for a in c if a != "@start-detached"]
        prefix = "start (detached, hidden): " if c and c[0] == "@start-detached" else "run: "
        print(prefix + " ".join(shlex.quote(a) for a in shown))
    for r in d["remove"]:
        print(f"remove: {r}")
    for n in d["notes"]:
        print(f"# note: {n}")


def run_service(args: argparse.Namespace, *, platform: Optional[str] = None, home: Optional[Path] = None) -> int:
    from . import os_service
    from .host_paths import apply_data_dir_default

    _apply_data_dir_flag(getattr(args, "data_dir", None))
    res = apply_data_dir_default()
    data_dir = res.path
    plat = platform or sys.platform
    home_p = Path(home) if home is not None else Path.home()

    if args.service_cmd == "status":
        st = os_service.service_status(platform=plat, home=home_p, data_dir=data_dir, probe=True)
        st["data_dir"] = str(data_dir)
        st["data_dir_source"] = res.source
        if args.json:
            print(json.dumps(st, indent=2, sort_keys=True))
        else:
            print(f"service ({st['mechanism']}): {'installed' if st['installed'] else 'not installed'}"
                  + ("" if st["loaded"] is None else f", {'loaded' if st['loaded'] else 'not loaded'}"))
            print(f"- unit: {st['unit_path']}")
            if st.get("url"):
                print(f"- console: {st['url']}/console")
            print(f"- data dir: {data_dir} ({res.source})")
        return 0

    if args.service_cmd == "uninstall":
        plan = os_service.build_uninstall_plan(platform=plat, home=home_p, data_dir=data_dir)
        if args.dry_run:
            if args.json:
                print(json.dumps({"dry_run": True, **plan.public_dict()}, indent=2))
            else:
                _print_plan(plan)
            return 0
        results = os_service.execute_plan(plan, echo=(lambda _l: None) if args.json else print)
        if args.json:
            print(json.dumps({"ok": True, **plan.public_dict(), "results": results}, indent=2))
        else:
            for n in plan.notes:
                print(n)
        return 0

    # install
    rec = os_service.read_service_record(data_dir) or {}
    chosen = os_service.choose_port(host=args.host, requested=args.port, persisted=rec.get("port"))
    exe = os_service.current_gateway_argv(plat)
    plan = os_service.build_install_plan(
        platform=plat,
        home=home_p,
        host=args.host,
        port=int(chosen["port"]),
        data_dir=data_dir,
        exe_argv=exe,
    )
    if chosen.get("busy_skipped"):
        plan.notes.insert(0, f"Ports in use, skipped: {', '.join(str(p) for p in chosen['busy_skipped'])}; using {chosen['port']}.")
    if args.no_start:
        plan.commands = os_service.without_start(plan)
        plan.notes.append("--no-start: the service is registered but not started now; it starts at next login.")
    if args.dry_run:
        if args.json:
            print(json.dumps({"dry_run": True, "port_choice": chosen, **plan.public_dict()}, indent=2))
        else:
            _print_plan(plan)
        return 0

    results = os_service.execute_plan(plan, echo=(lambda _l: None) if args.json else print)
    os_service.write_service_record(plan)
    healthy: Optional[bool] = None
    if not args.no_start and not args.no_wait:
        if not args.json:
            print(f"waiting for {plan.url}/api/health (up to {int(args.wait_s)}s; the first start can take a while)...")
        healthy = os_service.wait_for_health(plan.url, timeout_s=float(args.wait_s))
    claim: Optional[str] = None
    if healthy and not args.no_claim:
        from .config_cli import ensure_bootstrap_admin_user
        from .first_run import claim_url, first_run_state, mint_claim

        if not first_run_state(data_dir).get("completed"):
            admin = ensure_bootstrap_admin_user()
            user = admin.get("user") if isinstance(admin.get("user"), dict) else {}
            minted = mint_claim(data_dir=data_dir, tenant_id=str(user.get("tenant_id") or "default"), user_id=str(user.get("user_id") or "admin"))
            claim = claim_url(plan.url, minted["code"])
    if args.json:
        print(json.dumps({"ok": True, "healthy": healthy, "claim_url": claim, "port_choice": chosen, **plan.public_dict(), "results": results}, indent=2))
        return 0 if healthy is not False else 1
    for n in plan.notes:
        print(n)
    if healthy is False:
        print(f"The service did not answer {plan.url}/api/health within {int(args.wait_s)}s. Check the logs listed above.")
        return 1
    print(f"Console: {plan.url}/console")
    if claim:
        print(f"First run: open {claim}")
    return 0
