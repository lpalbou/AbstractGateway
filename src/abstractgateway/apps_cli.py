"""`abstractgateway apps ...` — the console's Apps page from a shell.

Every verb calls the RUNNING gateway's `/api/gateway/apps/*` routes (the same
code the console drives): the gateway, not this short-lived command, owns the
app processes, so an app started here keeps running after the command ends
and stops with the gateway.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from typing import Any, Dict

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
_POLL_S = 0.5


def add_apps_subparser(sub: Any) -> None:
    apps = sub.add_parser("apps", help="Apps (Flow, Code, Observer, Continuum, Entity, and the desktop Assistant): install, launch, stop, update")
    verbs = apps.add_subparsers(dest="apps_cmd", required=True)

    def conn(p: argparse.ArgumentParser) -> None:
        p.add_argument("--url", default=None, help="Gateway base URL (default: $ABSTRACTGATEWAY_URL, else the gateway running for this data dir, else http://127.0.0.1:8080)")
        p.add_argument("--token", default=None, help="Bearer token (default: $ABSTRACTGATEWAY_AUTH_TOKEN, else this data dir's bootstrap admin token for a loopback URL)")
        p.add_argument("--data-dir", default=None, help="Gateway data dir used to find the running gateway and its token")
        p.add_argument("--timeout-s", type=float, default=120.0, help="HTTP timeout per call (default: 120)")
        p.add_argument("--json", action="store_true", help="Emit the route's JSON payload")

    p = verbs.add_parser("list", help="Node.js status and every app: installed version, latest, running, URL")
    p.add_argument("--no-latest", action="store_true", help="Do not ask the npm registry for the latest versions")
    conn(p)
    p = verbs.add_parser("runtime", help="Install Node.js for the apps (only when no Node.js 18+ is found)")
    p.add_argument("--no-wait", action="store_true", help="Return once the job is queued")
    conn(p)
    for name, helptext in (("install", "Download and install an app from the npm registry"), ("update", "Update an installed app to the latest version (restarts it when running)")):
        p = verbs.add_parser(name, help=helptext)
        p.add_argument("app", help="flow | code | observer | continuum | entity | assistant (install: the browser app plus, where available, its terminal app; assistant: into the gateway's Python)")
        p.add_argument("--version", default=None, help="Exact version (default: latest)")
        if name == "install":
            p.add_argument("--launch", action="store_true", help="Start the app after installing it (and keep it enabled)")
        p.add_argument("--no-wait", action="store_true", help="Return once the job is queued")
        conn(p)
    for name, helptext in (
        ("launch", "Start an installed app (it then starts with the gateway)"),
        ("stop", "Stop an app (it no longer starts with the gateway)"),
        ("open", "Print a one-time link that opens the app signed in (2 minutes, loopback browsers)"),
    ):
        p = verbs.add_parser(name, help=helptext)
        p.add_argument("app")
        conn(p)
    # Terminal versions: parity with the console's "Install for
    # Terminal" and "Open in Terminal".
    p = verbs.add_parser("install-tui", help="Install an app's terminal version (Code): prebuilt release binary, checksum-verified")
    p.add_argument("app", help="code")
    p.add_argument("--no-wait", action="store_true", help="Return once the job is queued")
    conn(p)
    p = verbs.add_parser("tui-command", help="Print the command that opens an app's terminal version here, plus a one-time sign-in line (2 minutes, this machine)")
    p.add_argument("app", help="code")
    conn(p)
    p = verbs.add_parser("logs", help="Show the end of an app's log")
    p.add_argument("app")
    p.add_argument("--tail", type=int, default=100)
    conn(p)
    p = verbs.add_parser("jobs", help="Recent install/update jobs (or one job with its full log)")
    p.add_argument("job_id", nargs="?", default=None)
    conn(p)
    # Settings: the stored apps.* runtime-config keys, edited in
    # this data dir's settings store (the same store the console writes).
    cfg = verbs.add_parser("config", help="Apps settings (Node.js, ports, listening address, mirrors): get or set")
    cverbs = cfg.add_subparsers(dest="apps_config_cmd", required=True)
    g = cverbs.add_parser("get", help="Every apps setting (or one) with its value and where it comes from")
    g.add_argument("name", nargs="?", default=None, help="node | ports | host | npm_registry | pypi_url")
    g.add_argument("--json", action="store_true")
    g.add_argument("--data-dir", default=None, help="Gateway data dir (default: same resolution as `serve`)")
    st = cverbs.add_parser("set", help="Store one apps setting (an empty value clears it back to the default)")
    st.add_argument("name", help="node | ports | host | npm_registry | pypi_url")
    st.add_argument("value", help="the new value; \"\" clears the stored one")
    st.add_argument("--json", action="store_true")
    st.add_argument("--data-dir", default=None, help="Gateway data dir (default: same resolution as `serve`)")


def _transport(args: argparse.Namespace) -> Any:
    from .models_engines_cli import _Http

    return _Http(args)


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _fail(answer: Any) -> int:
    data = answer.data if isinstance(answer.data, dict) else {}
    print(f"error: {answer.message()}", file=sys.stderr)
    if data.get("hint"):
        print(f"  {data['hint']}", file=sys.stderr)
    if data.get("details"):
        print(str(data["details"]), file=sys.stderr)
    # terminal-app refusals carry what the user can run instead
    for key, label in (("install_command", "install it with"), ("command", "run")):
        if data.get(key):
            print(f"  {label}: {data[key]}", file=sys.stderr)
    if data.get("signin_command"):
        print(f"  sign in there once: {data['signin_command']}", file=sys.stderr)
    return EXIT_REFUSED if answer.status in (401, 403, 409) else EXIT_ERROR


def _follow(t: Any, job: Dict[str, Any], *, quiet: bool) -> Dict[str, Any]:
    last = None
    try:
        while job.get("state") not in ("succeeded", "failed", "cancelled"):
            line = (round(float(job.get("percent") or 0)), job.get("message"))
            if not quiet and line != last:
                total = job.get("bytes_total")
                size = f" [{int(job.get('bytes_done') or 0):,}/{int(total):,} B]" if total else ""
                print(f"  {line[0]:>3}% {job.get('message') or job.get('state')}{size}", file=sys.stderr)
                last = line
            time.sleep(_POLL_S)
            answer = t.call("GET", f"/apps/jobs/{urllib.parse.quote(str(job['id']), safe='')}")
            if not answer.ok:
                print(f"  (lost the job: {answer.message()})", file=sys.stderr)
                return job
            job = answer.data["job"]
    except KeyboardInterrupt:
        print(f"\nStopped watching; the job keeps running on the gateway: abstractgateway apps jobs {job.get('id')}", file=sys.stderr)
    return job


def _finish(args: argparse.Namespace, t: Any, payload: Dict[str, Any]) -> int:
    job = payload.get("job")
    if not job:
        print(payload.get("message") or "Nothing to do.")
        return EXIT_OK
    if not getattr(args, "no_wait", False):
        job = _follow(t, job, quiet=bool(args.json))
    if args.json:
        _print_json(job)
    else:
        state = job.get("state")
        result = job.get("result") or {}
        if state == "succeeded":
            print(f"done: {result.get('message') or job.get('message')}")
        elif state == "failed":
            err = job.get("error") or {}
            print(f"FAILED: {err.get('message') or job.get('message')}", file=sys.stderr)
            if err.get("hint"):
                print(f"  {err['hint']}", file=sys.stderr)
            print(f"  full log: {job.get('log_path')}", file=sys.stderr)
        else:
            print(f"{state}: {job.get('message')} (follow: abstractgateway apps jobs {job.get('id')})")
    return EXIT_OK if job.get("state") in ("succeeded", "queued", "running") else EXIT_ERROR


def _list(args: argparse.Namespace, t: Any) -> int:
    answer = t.call("GET", "/apps" + ("?latest=false" if args.no_latest else ""))
    if not answer.ok:
        return _fail(answer)
    data = answer.data
    if args.json:
        _print_json(data)
        return EXIT_OK
    node = data["runtime"]["node"]
    print(f"Node.js: {node['message']}" + (f"  [{node['path']}]" if node.get("path") else ""))
    reg = data.get("registry") or {}
    if reg.get("reachable") is False:
        print(f"npm registry: NOT reachable ({reg.get('error')})")
    print(f"{'app':<10} {'installed':<10} {'latest':<10} {'status':<13} url")
    for a in data["apps"]:
        latest = (a.get("latest_version") or "?") + (" *" if a.get("update_available") else "")
        print(f"{a['id']:<10} {a.get('version') or '-':<10} {latest:<10} {a['status']:<13} {a.get('url') or ''}")
        if a.get("last_error") and not a.get("running"):
            print(f"{'':<10} {a['last_error']}")
        if a.get("source") == "external" and isinstance(a.get("external"), dict):
            print(f"{'':<10} {a['external'].get('detail') or 'started outside the gateway'} (open only: stop it where it was started)")
        for i in a.get("interfaces") or []:
            if isinstance(i, dict) and i.get("kind") == "tui":
                print(f"{'':<10} {_tui_line(i, a['id'])}")
    ct = data.get("console_tui")
    if isinstance(ct, dict) and ct.get("command"):
        print(f"gateway console (terminal): {_tui_line(ct, None)}")
    return EXIT_OK


def _tui_line(i: Dict[str, Any], app_id: Any) -> str:
    """One line for a terminal interface: where it is and what to run."""
    if i.get("installed"):
        where = "installed by the gateway" if i.get("source") == "gateway" else "found on this computer"
        head = f"terminal: {i.get('version') or '?'} ({where})"
        if i.get("update_available") and app_id:
            head += f", {i.get('latest_version')} available: abstractgateway apps install-tui {app_id}"
        opener = f"; open signed in: abstractgateway apps tui-command {app_id}" if app_id else ""
        return f"{head} · run: {i.get('command')}{opener}"
    if i.get("install_available") and app_id:
        return f"terminal: not installed · install: abstractgateway apps install-tui {app_id}"
    reason = i.get("install_blocked_reason") or "not installable here"
    return f"terminal: not installed · {reason}" + (f" · {i['install_command']}" if i.get("install_command") and i.get("install_method") == "cargo" else "")


def _config_data_dir(args: argparse.Namespace) -> Any:
    from pathlib import Path

    if getattr(args, "data_dir", None):
        return Path(str(args.data_dir)).expanduser().resolve()
    from .host_paths import resolve_data_dir

    return resolve_data_dir().path


def _print_apps_setting(row: Dict[str, Any]) -> None:
    value = row.get("value")
    print(f"{row['key']:<18} {value if value not in (None, '') else '(empty)'!s:<34} [{row['source']}]  {row['label']}")
    for k in ("note", "invalid_stored", "invalid_env"):
        if row.get(k):
            print(f"{'':<18} {k.replace('_', ' ')}: {row[k]}")


def _run_apps_config(args: argparse.Namespace) -> int:
    import getpass

    from .runtime_config import APPS_SETTINGS, RuntimeConfigError, read_runtime_config, write_runtime_config

    data_dir = _config_data_dir(args)
    known = [r["name"] for r in APPS_SETTINGS]
    name = getattr(args, "name", None)
    if name is not None and name.startswith("apps."):
        name = name[len("apps."):]
    if name is not None and name not in known:
        print(f"error: unknown apps setting {name!r}; one of {known}", file=sys.stderr)
        return EXIT_ERROR
    if args.apps_config_cmd == "set":
        try:
            login = getpass.getuser() or "operator"
        except Exception:
            login = "operator"
        try:
            out = write_runtime_config(data_dir, {f"apps.{name}": args.value}, actor=f"cli/{login}")
        except RuntimeConfigError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        row = out["apps"][name]
        if args.json:
            _print_json({"applied": out.get("applied"), "setting": row})
        else:
            _print_apps_setting(row)
            print("applies to the next app start (host, ports, node) or the next download (registry URLs)", file=sys.stderr)
        return EXIT_OK
    rows = read_runtime_config(data_dir)["apps"]
    if name is not None:
        rows = {name: rows[name]}
    if args.json:
        _print_json(rows)
    else:
        for r in rows.values():
            _print_apps_setting(r)
    return EXIT_OK


def run_apps_command(args: argparse.Namespace) -> int:
    if args.apps_cmd == "config":
        return _run_apps_config(args)
    t = _transport(args)
    verb = args.apps_cmd
    if verb == "list":
        return _list(args, t)
    if verb == "runtime":
        answer = t.call("POST", "/apps/runtime/install", {})
        return _finish(args, t, answer.data) if answer.ok else _fail(answer)
    if verb == "jobs":
        if args.job_id:
            answer = t.call("GET", f"/apps/jobs/{urllib.parse.quote(args.job_id, safe='')}")
            if not answer.ok:
                return _fail(answer)
            job = answer.data["job"]
            if args.json:
                _print_json(job)
            else:
                print(f"{job['id']} {job['kind']} {job['state']} {job['percent']}% {job['message']}")
                print(job.get("details") or "\n".join(job.get("log_tail") or []))
            return EXIT_OK
        answer = t.call("GET", "/apps/jobs")
        if not answer.ok:
            return _fail(answer)
        if args.json:
            _print_json(answer.data)
        else:
            for j in answer.data["jobs"]:
                print(f"{j['id']:<24} {j['kind']:<16} {j['state']:<10} {j['percent']:>5}%  {j['message']}")
        return EXIT_OK
    app = urllib.parse.quote(str(args.app), safe="")
    if verb in ("install", "update"):
        body: Dict[str, Any] = {}
        if args.version:
            body["version"] = args.version
        if verb == "install" and args.launch:
            body["launch"] = True
        answer = t.call("POST", f"/apps/{app}/{verb}", body, timeout=args.timeout_s)
        return _finish(args, t, answer.data) if answer.ok else _fail(answer)
    if verb in ("launch", "stop"):
        answer = t.call("POST", f"/apps/{app}/{verb}", {}, timeout=args.timeout_s)
        if not answer.ok:
            return _fail(answer)
        row = answer.data["app"]
        if args.json:
            _print_json(row)
        elif verb == "launch" and row.get("kind") == "desktop":
            print(answer.data.get("message") or f"{row['name']} is starting.")
        elif verb == "launch":
            print(f"{row['name']} {row.get('version')} is running at {row.get('url')} (starts with the gateway; `abstractgateway apps open {row['id']}` prints a signed-in link)")
        else:
            print(f"{row['name']} stopped (it no longer starts with the gateway).")
        return EXIT_OK
    if verb == "open":
        answer = t.call("POST", f"/apps/{app}/open", {})
        if not answer.ok:
            return _fail(answer)
        link = t.url.rstrip("/") + answer.data["open_url"]
        if args.json:
            _print_json(dict(answer.data, link=link))
        else:
            print(link)
            print(f"  (works once, within {answer.data.get('expires_in_s')} s, in a browser on this machine)", file=sys.stderr)
        return EXIT_OK
    if verb == "install-tui":
        answer = t.call("POST", f"/apps/{app}/install-tui", {}, timeout=args.timeout_s)
        return _finish(args, t, answer.data) if answer.ok else _fail(answer)
    if verb == "tui-command":
        answer = t.call("POST", f"/apps/{app}/tui-command", {}, timeout=args.timeout_s)
        if not answer.ok:
            return _fail(answer)
        d = answer.data
        if args.json:
            _print_json(d)
        else:
            print(f"Run this in a terminal on this machine within {d.get('expires_in_s')} s; it opens the app signed in as you (works once):")
            print(f"  {d['signin_command']}")
            print("Without the one-time line (it then uses its own saved `login`), open it with:")
            print(f"  {d['command']}")
        return EXIT_OK
    if verb == "logs":
        answer = t.call("GET", f"/apps/{app}/logs?tail={int(args.tail)}")
        if not answer.ok:
            return _fail(answer)
        if args.json:
            _print_json(answer.data)
        else:
            print("\n".join(answer.data["lines"]))
            print(f"-- {answer.data['path']}", file=sys.stderr)
        return EXIT_OK
    print(f"unknown apps verb {verb}", file=sys.stderr)
    return EXIT_ERROR
