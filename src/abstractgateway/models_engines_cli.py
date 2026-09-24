"""`abstractgateway models …` and `abstractgateway engines …`: the Models and Engines tabs from a shell.

    abstractgateway models list [--provider P]
    abstractgateway models catalog [--engine E] [--fits] [--hub] [--tag T]
    abstractgateway models search QUERY [--engine E] [--fits] [--hub] [--tag T]
    abstractgateway models download PROVIDER ARTIFACT [--dry-run] [--no-wait]
    abstractgateway models delete PROVIDER ARTIFACT [--yes] [--force] [--dry-run]
    abstractgateway models jobs [JOB_ID] [--kind K] [--status S]
    abstractgateway models cancel JOB_ID
    abstractgateway engines status [--probe]
    abstractgateway engines install ENGINE [--yes] [--force] [--dry-run] [--no-wait] [--location auto|user|system]
    abstractgateway engines continue JOB_ID [--action approve_admin|install_tools|recheck] [--no-wait]
    abstractgateway engines cancel JOB_ID
    abstractgateway engines start|stop ENGINE
    abstractgateway engines open ENGINE [--no-browser]

The verbs, arguments and exit codes are AbstractCore's (`abstractcore models …`,
`abstractcore engines …`), so the `cli_equivalent` a console job card shows
runs here as printed. Exit codes: 0 ok, 1 error, 2 refused (admin or policy
refusal, delete blockers, or a destructive action without `--yes`). `--json`
prints the exact payload the matching `/api/gateway/*` route returns.

BY DEFAULT EVERY VERB TALKS TO THE RUNNING GATEWAY (the same routes, admin
rules, `allow_engine_install` knob and audit log as the console). The URL is
`--url`, else `$ABSTRACTGATEWAY_URL`, else the gateway recorded for this data
dir by `serve` / `service install`, else http://127.0.0.1:8080. The token is
`--token`, else `$ABSTRACTGATEWAY_AUTH_TOKEN`, else — for a loopback URL only —
this data dir's bootstrap admin token file.

`--local` runs the same AbstractCore functions in this process instead (no
gateway needed; jobs run in the foreground). That is the power-user path on
the machine itself, like `abstractcore models …`; nothing is audited by a
gateway because no gateway is involved.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2

_TERMINAL = {"completed", "failed", "cancelled"}
_KINDS = ["download", "delete", "engine_install"]
_STATUSES = ["queued", "running", "completed", "failed", "cancelled"]
_POLL_S = 1.0


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _connection_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--url", default=None, help="Gateway base URL (default: $ABSTRACTGATEWAY_URL, else the gateway running for this data dir, else http://127.0.0.1:8080)")
    p.add_argument("--token", default=None, help="Bearer token (default: $ABSTRACTGATEWAY_AUTH_TOKEN, else this data dir's bootstrap admin token for a loopback URL)")
    p.add_argument("--data-dir", default=None, help="Gateway data dir used to find the running gateway and its token (default: same resolution as `serve`)")
    p.add_argument("--local", action="store_true", help="Run in this process through AbstractCore instead of calling the gateway")
    p.add_argument("--timeout-s", type=float, default=120.0, help="HTTP timeout per call (default: 120)")
    p.add_argument("--json", action="store_true", help="Emit the route's JSON payload")


def _catalog_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--engine", default=None, help="Only artifacts for this engine/provider (ollama, lmstudio, mlx, huggingface, llamacpp)")
    p.add_argument("--fits", action="store_true", help="Only artifacts that fit this machine (fits or tight)")
    p.add_argument("--hub", action="store_true", help="Ask Hugging Face for exact sizes (and search results); cached 24 h")
    p.add_argument("--tag", action="append", default=None, help="Only rows with this tag (repeatable): chat, coding, vision, embedding...")


def add_models_verbs(models_sub: Any) -> None:
    """Register the Models-tab verbs on the existing `abstractgateway models` parser."""

    p = models_sub.add_parser("list", help="Installed models per engine on the gateway host, with sizes")
    p.add_argument("--provider", default=None, help="ollama | lmstudio | mlx | huggingface")
    _connection_args(p)

    p = models_sub.add_parser("catalog", help="Downloadable models with presence and a fit verdict for the gateway host")
    _catalog_args(p)
    _connection_args(p)

    p = models_sub.add_parser("search", help="Search the model catalog (and, with --hub, Hugging Face)")
    p.add_argument("query", help="Free text, e.g. 'qwen3 8b' or 'embedding'")
    _catalog_args(p)
    _connection_args(p)

    p = models_sub.add_parser("download", help="Download one model onto the gateway host (admin)")
    p.add_argument("provider", help="ollama | lmstudio | mlx | huggingface | mlx-gen | supertonic ...")
    p.add_argument("artifact", help="Exact artifact reference, quantization included (qwen3:8b, qwen/qwen3.5-9b@4bit)")
    p.add_argument("--dry-run", action="store_true", help="Show the command without downloading")
    p.add_argument("--no-wait", action="store_true", help="Start the job and exit (follow it with `models jobs <id>`)")
    _connection_args(p)

    p = models_sub.add_parser("delete", help="Delete one installed model from the gateway host (admin; refuses when loaded unless --force)")
    p.add_argument("provider", help="ollama | lmstudio | mlx | huggingface")
    p.add_argument("artifact", help="The installed artifact, as `models list` prints it")
    p.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    p.add_argument("--dry-run", action="store_true", help="Show what would be deleted")
    p.add_argument("--force", action="store_true", help="Delete even when loaded or shared")
    _connection_args(p)

    p = models_sub.add_parser("jobs", help="Download / delete / engine-install jobs on the gateway host (or one job)")
    p.add_argument("job_id", nargs="?", default=None, help="Show one job")
    p.add_argument("--kind", default=None, choices=_KINDS)
    p.add_argument("--status", default=None, choices=_STATUSES)
    _connection_args(p)

    p = models_sub.add_parser("cancel", help="Cancel a running host job (admin)")
    p.add_argument("job_id")
    _connection_args(p)


def add_engines_subparser(sub: Any) -> None:
    engines = sub.add_parser("engines", help="Local inference engines on the gateway host: status, install, download page")
    esub = engines.add_subparsers(dest="engines_cmd", required=True)

    p = esub.add_parser("status", help="Which engines are installed / running on the gateway host")
    p.add_argument("--probe", action="store_true", help="Also ask each local server whether it answers (one GET each)")
    _connection_args(p)

    p = esub.add_parser("install", help="Install an engine on the gateway host, user-level first (admin)")
    p.add_argument("engine", help="ollama | lmstudio | mlx | llamacpp | vllm | huggingface")
    p.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    p.add_argument("--dry-run", action="store_true", help="Only show the plan (steps, command preview, admin/tools needs)")
    p.add_argument("--force", action="store_true", help="Run even when the engine is already installed")
    p.add_argument("--no-wait", action="store_true", help="Start the job and exit")
    p.add_argument("--location", default="auto", choices=["auto", "user", "system"], help="Apps on macOS: auto (/Applications when writable, else ~/Applications), user, system")
    _connection_args(p)

    p = esub.add_parser("continue", help="Resume an install waiting for tools or an administrator (admin)")
    p.add_argument("job_id")
    p.add_argument("--action", default=None, choices=["approve_admin", "install_tools", "recheck"], help="Default: the job's first continue action")
    p.add_argument("--no-wait", action="store_true", help="Resume and exit")
    _connection_args(p)

    p = esub.add_parser("cancel", help="Cancel an engine install job (admin)")
    p.add_argument("job_id")
    _connection_args(p)

    for verb in ("start", "stop"):
        p = esub.add_parser(verb, help=f"{verb.capitalize()} a local engine server: ollama | lmstudio (admin)")
        p.add_argument("engine")
        _connection_args(p)

    p = esub.add_parser("open", help="Print (and open) an engine's download page")
    p.add_argument("engine")
    p.add_argument("--no-browser", action="store_true", help="Only print the URL")
    _connection_args(p)


# ---------------------------------------------------------------------------
# Transport: the gateway's routes, or the seam in-process
# ---------------------------------------------------------------------------


class _Answer:
    def __init__(self, status: int, data: Any, error: Optional[str] = None):
        self.status = int(status)
        self.data = data
        self.error = error

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not (isinstance(self.data, dict) and self.data.get("ok") is False)

    @property
    def refused(self) -> bool:
        if self.status in (401, 403, 409):
            return True
        return self.status == 404 and isinstance(self.data, dict) and bool(self.data.get("delete_blockers"))

    def message(self) -> str:
        if isinstance(self.data, dict):
            for key in ("message", "detail", "error"):
                value = self.data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, dict) and isinstance(value.get("message"), str):
                    return value["message"]
        return str(self.error or f"HTTP {self.status}")


def _is_loopback_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").strip("[]").lower()
    except Exception:
        return False
    if host == "localhost":
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _resolve_connection(args: argparse.Namespace) -> Tuple[str, str, str]:
    """(base_url, token, where-it-came-from) for the HTTP transport."""

    from .firstrun_cli import _apply_data_dir_flag, _claim_base_url
    from .host_paths import apply_data_dir_default

    _apply_data_dir_flag(getattr(args, "data_dir", None))
    data_dir = apply_data_dir_default().path
    if args.url:
        url, source = str(args.url).rstrip("/"), "flag"
    elif os.environ.get("ABSTRACTGATEWAY_URL"):
        url, source = str(os.environ["ABSTRACTGATEWAY_URL"]).rstrip("/"), "env"
    else:
        found = _claim_base_url(argparse.Namespace(url=None, port=None, host="127.0.0.1"), data_dir)
        url, source = str(found["url"]), str(found["source"])
    token = args.token if args.token is not None else os.environ.get("ABSTRACTGATEWAY_AUTH_TOKEN", "")
    if not token and _is_loopback_url(url):
        token_file = Path(data_dir) / "auth" / "bootstrap-admin-token"
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except Exception:
            token = ""
    return url, str(token or ""), source


class _Http:
    def __init__(self, args: argparse.Namespace):
        from .tray.client import GatewayClient

        url, token, self.source = _resolve_connection(args)
        self.url = url
        self.timeout = float(args.timeout_s)
        self._client = GatewayClient(url, token)

    def call(self, method: str, path: str, body: Any = None, *, timeout: Optional[float] = None) -> _Answer:
        res = self._client._request(method, path, body=body, timeout=timeout or self.timeout)
        if res.status == 0:
            return _Answer(0, None, error=f"cannot reach the gateway at {self.url} ({res.error}); start it with `abstractgateway serve`, pass --url, or use --local")
        return _Answer(res.status, res.data, error=res.error)


class _Local:
    """The same payloads, from the seam in this process (no gateway, no HTTP)."""

    url = "local"

    def call(self, method: str, path: str, body: Any = None, *, timeout: Optional[float] = None) -> _Answer:
        from . import core_config as seam

        parsed = urllib.parse.urlparse(path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        tags = urllib.parse.parse_qs(parsed.query).get("tag")
        parts = [p for p in parsed.path.split("/") if p]
        truthy = lambda v: str(v or "").lower() in {"1", "true", "yes", "on"}  # noqa: E731
        body = dict(body or {})
        from . import engines_install as ei

        try:
            if parts[:1] == ["engines"]:
                # The person at this terminal is on the host: installs are
                # allowed, like `abstractcore engines install`, and run in the
                # foreground (a job that needs tools/admin pauses and returns).
                reg = ei.default_registry()
                if parts == ["engines"] or (len(parts) == 2 and parts[1] != "jobs"):
                    core = seam.core_engine_inventory(probe=truthy(query.get("probe")))
                    accel = (core.get("host") or {}).get("accelerator")
                    data = ei.engines_payload(ei.default_installer(accelerator=accel), core, reg, install_allowed=True)
                    data["install_allowed"] = True
                    if parts == ["engines"]:
                        return _Answer(200, data)
                    for row in data["engines"]:
                        if row.get("id") == parts[1]:
                            return _Answer(200, row)
                    return _Answer(404, {"ok": False, "status": "refused", "reason": "unknown_engine", "message": f"unknown engine {parts[1]!r}"})
                if len(parts) == 3 and parts[2] == "install":
                    if body.get("dry_run"):
                        return _Answer(200, ei.dry_run_payload(ei.default_installer(), parts[1], location=str(body.get("location") or "auto")))
                    snap, _ = reg.start(parts[1], force=bool(body.get("force")), location=str(body.get("location") or "auto"), run_inline=True)
                    job = reg.get(snap["job_id"])
                    return _Answer(200, job.snapshot() if job else snap)
                if len(parts) == 3 and parts[1] == "jobs":
                    job = reg.get(parts[2])
                    return _Answer(200, job.snapshot()) if job else _Answer(404, {"ok": False, "status": "not_found", "message": f"no job {parts[2]}"})
                if len(parts) == 4 and parts[1] == "jobs" and parts[3] == "continue":
                    reg.continue_job(parts[2], body.get("action"), run_inline=True)
                    job = reg.get(parts[2])
                    return _Answer(200, job.snapshot() if job else {})
                if len(parts) == 4 and parts[1] == "jobs" and parts[3] == "cancel":
                    snap = reg.cancel(parts[2])
                    return _Answer(200, snap) if snap else _Answer(404, {"ok": False, "status": "not_found", "message": f"no job {parts[2]}"})
                if len(parts) == 3 and parts[2] in {"start", "stop"} and parts[1] in ei.SERVER_ENGINES:
                    inst = reg.service_installer()
                    fn = getattr(inst, f"{parts[2]}_{parts[1]}")
                    return _Answer(200, {"ok": True, "engine": parts[1], "action": parts[2], **fn()})
            if parts == ["models", "catalog"]:
                data = seam.core_model_catalog(
                    query.get("q") or None, engine=query.get("engine") or None, fits_only=truthy(query.get("fits")),
                    hub=truthy(query.get("hub")), tags=tags,
                )
                return _Answer(200, data)
            if parts == ["models", "installed"]:
                return _Answer(200, seam.core_installed_models(query.get("provider") or None))
            if parts == ["models", "download"]:
                job = seam.core_start_model_download(
                    str(body.get("provider") or ""), str(body.get("artifact") or ""), dry_run=bool(body.get("dry_run")), run_inline=True
                )
                return _Answer(200, {"ok": True, "job": job})
            if parts == ["models", "delete"]:
                job = seam.core_model_delete(
                    str(body.get("provider") or ""), str(body.get("artifact") or ""), dry_run=bool(body.get("dry_run")),
                    force=bool(body.get("force")), run_inline=True,
                )
                return _Answer(200, job)
            if parts == ["jobs"]:
                return _Answer(200, seam.core_host_jobs(kind=query.get("kind") or None, status=query.get("status") or None))
            if len(parts) == 2 and parts[0] == "jobs":
                job = seam.core_host_job(parts[1])
                return _Answer(200, job) if job is not None else _Answer(404, {"ok": False, "status": "not_found", "message": f"no job {parts[1]}"})
            if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
                job = seam.core_host_job_cancel(parts[1], by="cli")
                return _Answer(200, job) if job is not None else _Answer(404, {"ok": False, "status": "not_found", "message": f"no job {parts[1]}"})
        except ei.JobStateError as exc:
            return _Answer(exc.status_code, {"ok": False, "status": "refused", "reason": exc.reason, "message": str(exc)})
        except ei.InstallFailed as exc:
            return _Answer(502, {"ok": False, "status": "failed", "reason": exc.code, "message": exc.message})
        except seam.HostActionRefused as exc:
            return _Answer(exc.status_code, exc.payload())
        except seam.CoreTooOld as exc:
            return _Answer(501, {"ok": False, "status": "unsupported", "reason": "abstractcore_too_old", "message": str(exc)})
        except RuntimeError as exc:
            return _Answer(503, {"ok": False, "status": "unavailable", "message": str(exc)})
        return _Answer(404, {"ok": False, "message": f"no local handler for {method} {path}"})


def _transport(args: argparse.Namespace) -> Any:
    return _Local() if getattr(args, "local", False) else _Http(args)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _gb(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    return f"{value / 1e9:.1f} GB"


def _fail(args: argparse.Namespace, answer: _Answer, verb: str) -> int:
    if args.json:
        _print_json(answer.data if answer.data is not None else {"ok": False, "status": answer.status, "message": answer.message()})
    print(f"abstractgateway {verb}: {answer.message()}", file=sys.stderr)
    return EXIT_REFUSED if answer.refused else EXIT_ERROR


def _job_of(payload: Any) -> Optional[Dict[str, Any]]:
    if isinstance(payload, dict) and payload.get("job_id"):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("job"), dict):
        return payload["job"]
    return None


def _print_job(job: Dict[str, Any]) -> None:
    target = job.get("engine") or f"{job.get('provider')} {job.get('artifact')}"
    pct = f" {job['percent']:.0f}%" if isinstance(job.get("percent"), (int, float)) else ""
    status = job.get("host_status") or job.get("status")
    print(f"  {job.get('job_id', ''):<18} {job.get('kind', ''):<15} {status:<10}{pct:>5}  {target}  {job.get('message') or ''}")


def _confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def _follow(transport: Any, job: Dict[str, Any], *, quiet: bool) -> Dict[str, Any]:
    """Poll `/jobs/{id}` until the job ends. Ctrl-C stops watching, not the job."""

    last = None
    try:
        while str(job.get("host_status") or job.get("status")) not in _TERMINAL:
            line = (job.get("status"), job.get("percent"), job.get("message"))
            if not quiet and line != last:
                pct = f"{job['percent']:.0f}% " if isinstance(job.get("percent"), (int, float)) else ""
                print(f"  {pct}{job.get('message') or job.get('status')}", file=sys.stderr)
                last = line
            time.sleep(_POLL_S)
            answer = transport.call("GET", f"/jobs/{urllib.parse.quote(str(job['job_id']), safe='')}")
            if not answer.ok:
                print(f"  (lost the job: {answer.message()})", file=sys.stderr)
                return job
            job = answer.data
    except KeyboardInterrupt:
        print(
            f"\nStopped watching; the job keeps running on the gateway. Follow it: abstractgateway models jobs {job.get('job_id')}"
            f" -- cancel it: abstractgateway models cancel {job.get('job_id')}",
            file=sys.stderr,
        )
    return job


def _finish_job(args: argparse.Namespace, transport: Any, job: Dict[str, Any], verb: str) -> int:
    if not getattr(args, "no_wait", False) and str(job.get("host_status") or job.get("status")) not in _TERMINAL:
        job = _follow(transport, job, quiet=bool(args.json))
    if args.json:
        _print_json(job)
    else:
        result = job.get("result") or {}
        status = str(job.get("host_status") or job.get("status"))
        glyph = {"completed": "done", "failed": "FAILED", "cancelled": "cancelled"}.get(status, status)
        print(f"{glyph}: {result.get('message') or job.get('message') or ''}".rstrip())
        if job.get("command"):
            print(f"  command: {' '.join(str(c) for c in job['command'])}")
        if job.get("cli_equivalent"):
            print(f"  cli: {job['cli_equivalent']}")
        if status not in _TERMINAL:
            print(f"  job {job.get('job_id')} is {status}; follow it: abstractgateway models jobs {job.get('job_id')}")
    status = str(job.get("host_status") or job.get("status"))
    if (job.get("result") or {}).get("status") == "refused":
        return EXIT_REFUSED
    return EXIT_OK if status in {"completed", "queued", "running"} else EXIT_ERROR


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------


def _catalog_path(args: argparse.Namespace, query: Optional[str]) -> str:
    params: List[Tuple[str, str]] = []
    if query:
        params.append(("q", query))
    if args.engine:
        params.append(("engine", args.engine))
    if args.fits:
        params.append(("fits", "1"))
    if args.hub:
        params.append(("hub", "1"))
    for tag in args.tag or []:
        params.append(("tag", tag))
    return "/models/catalog" + ("?" + urllib.parse.urlencode(params) if params else "")


def _models_list(args: argparse.Namespace, t: Any) -> int:
    path = "/models/installed" + ("?" + urllib.parse.urlencode({"provider": args.provider}) if args.provider else "")
    answer = t.call("GET", path)
    if not answer.ok:
        return _fail(args, answer, "models list")
    payload = answer.data
    if args.json:
        _print_json(payload)
        return EXIT_OK
    rows = payload.get("rows") or []
    total = (payload.get("totals") or {}).get("size_bytes")
    print(f"Installed models on {t.url} ({len(rows)}, {_gb(total)})")
    for row in rows:
        flags = []
        if row.get("loaded"):
            flags.append("loaded")
        if row.get("delete_blockers"):
            flags.append("blocked:" + ",".join(row["delete_blockers"]))
        print(f"  {row.get('provider', ''):<11} {row.get('artifact', ''):<58} {str(row.get('quant') or '-'):<8} {_gb(row.get('size_bytes')):>9}  {' '.join(flags)}")
    for name, error in (payload.get("errors") or {}).items():
        print(f"  ! {name}: {error}")
    return EXIT_OK


def _models_catalog(args: argparse.Namespace, t: Any) -> int:
    answer = t.call("GET", _catalog_path(args, getattr(args, "query", None)))
    if not answer.ok:
        return _fail(args, answer, "models catalog")
    payload = answer.data
    if args.json:
        _print_json(payload)
        return EXIT_OK
    host = payload.get("host_profile") or {}
    print(f"Model catalog for {t.url}: {host.get('accelerator')} {host.get('gpu_name') or ''}, ceiling {_gb(host.get('ceiling_bytes'))}, free now {_gb(host.get('free_now_bytes'))}")
    for row in payload.get("rows") or []:
        print(f"\n{row.get('display_name')}  [{row.get('id')}]  {', '.join(row.get('tags') or [])}")
        for art in row.get("artifacts") or []:
            fit = art.get("fit") or {}
            mark = "*" if art.get("recommended") else " "
            status = (art.get("presence") or {}).get("status")
            size = _gb(art.get("download_bytes")) + ("~" if art.get("size_source") == "estimate" else "")
            print(f"  {mark} {art.get('provider', ''):<11} {art.get('artifact', ''):<52} {size:>10}  {status:<9} {fit.get('verdict')}")
    print("\n* = pre-selected for this machine.  Download: abstractgateway models download <provider> <artifact>")
    return EXIT_OK


def _models_download(args: argparse.Namespace, t: Any) -> int:
    body = {"provider": args.provider, "artifact": args.artifact, "dry_run": bool(args.dry_run)}
    answer = t.call("POST", "/models/download", body)
    if not answer.ok:
        return _fail(args, answer, "models download")
    job = _job_of(answer.data)
    if job is None:
        print(f"abstractgateway models download: unexpected answer {answer.data!r}", file=sys.stderr)
        return EXIT_ERROR
    return _finish_job(args, t, job, "models download")


def _models_delete(args: argparse.Namespace, t: Any) -> int:
    if not args.yes and not args.dry_run:
        question = f"Delete {args.provider} {args.artifact} from the gateway host ({t.url})?"
        if args.json or not _confirm(question):
            payload = {
                "ok": False, "status": "refused", "reason": "not_confirmed", "provider": args.provider,
                "artifact": args.artifact, "delete_blockers": [],
                "message": "not confirmed: pass --yes to delete without a prompt",
            }
            _print_json(payload) if args.json else print(f"abstractgateway models delete: {payload['message']}", file=sys.stderr)
            return EXIT_REFUSED
    body = {"provider": args.provider, "artifact": args.artifact, "dry_run": bool(args.dry_run), "force": bool(args.force)}
    answer = t.call("POST", "/models/delete", body)
    if not answer.ok:
        return _fail(args, answer, "models delete")
    return _finish_job(args, t, answer.data, "models delete")


def _models_jobs(args: argparse.Namespace, t: Any) -> int:
    if args.job_id:
        answer = t.call("GET", f"/jobs/{urllib.parse.quote(args.job_id, safe='')}")
        if not answer.ok:
            return _fail(args, answer, "models jobs")
        if args.json:
            _print_json(answer.data)
        else:
            _print_job(answer.data)
            for line in answer.data.get("log_tail") or []:
                print(f"   | {line}")
        return EXIT_OK
    params = {k: v for k, v in (("kind", args.kind), ("status", args.status)) if v}
    answer = t.call("GET", "/jobs" + ("?" + urllib.parse.urlencode(params) if params else ""))
    if not answer.ok:
        return _fail(args, answer, "models jobs")
    if args.json:
        _print_json(answer.data)
        return EXIT_OK
    jobs = answer.data.get("jobs") or []
    if not jobs:
        print("No jobs.")
    for job in jobs:
        _print_job(job)
    return EXIT_OK


def _models_cancel(args: argparse.Namespace, t: Any) -> int:
    answer = t.call("POST", f"/jobs/{urllib.parse.quote(args.job_id, safe='')}/cancel")
    if not answer.ok:
        return _fail(args, answer, "models cancel")
    if args.json:
        _print_json(answer.data)
    else:
        print(f"cancel requested for {answer.data.get('job_id')} ({answer.data.get('status')})")
    return EXIT_OK


def _engines_status(args: argparse.Namespace, t: Any) -> int:
    answer = t.call("GET", "/engines" + ("?probe=1" if args.probe else ""))
    if not answer.ok:
        return _fail(args, answer, "engines status")
    payload = answer.data
    if args.json:
        _print_json(payload)
        return EXIT_OK
    print(f"Local engines on {t.url}" + (" (probed)" if args.probe else "") + ("" if payload.get("install_allowed", True) else "  [installs disabled: allow_engine_install is off]"))
    for e in payload.get("engines") or []:
        if not e.get("supported", e.get("supported_on_host")):
            state = f"not for this machine: {e.get('support_reason') or e.get('unsupported_reason')}"
        elif e.get("installed"):
            state = f"installed {e.get('version') or ''}".strip()
            if e.get("running") is True:
                state += ", running"
            if e.get("models_count") is not None:
                state += f", {e['models_count']} models"
        else:
            plan = e.get("install") or {}
            how = plan.get("notes") or " ".join(plan.get("argv") or []) or plan.get("url") or "no install command"
            flags = [f for f, on in (("needs administrator", plan.get("needs_admin")), ("needs tools", plan.get("needs_tools"))) if on]
            state = f"not installed -- {plan.get('method') or '?'}" + (f" [{', '.join(flags)}]" if flags else "") + f": {how}"
        job = e.get("active_job")
        if job:
            state += f"  (install job {job.get('job_id')}: {job.get('state')})"
        print(f"  {e.get('name', e.get('id')):<14} {state}")
    return EXIT_OK


def _engines_install(args: argparse.Namespace, t: Any) -> int:
    if not args.yes and not args.dry_run:
        plan_answer = t.call("GET", f"/engines/{urllib.parse.quote(args.engine, safe='')}")
        plan = ((plan_answer.data or {}).get("install") or {}) if plan_answer.ok else {}
        argv = plan.get("notes") or " ".join(plan.get("argv") or []) or "(no install command)"
        question = f"Install {args.engine} on the gateway host ({t.url})? {argv}"
        if args.json or not _confirm(question):
            payload = {
                "ok": False, "status": "refused", "reason": "not_confirmed", "engine": args.engine, "install": plan or None,
                "message": "not confirmed: pass --yes to install without a prompt (or --dry-run to see the command)",
            }
            _print_json(payload) if args.json else print(f"abstractgateway engines install: {payload['message']}", file=sys.stderr)
            return EXIT_REFUSED
    body = {"dry_run": bool(args.dry_run), "force": bool(args.force), "location": getattr(args, "location", "auto") or "auto"}
    answer = t.call("POST", f"/engines/{urllib.parse.quote(args.engine, safe='')}/install", body)
    if not answer.ok:
        return _fail(args, answer, "engines install")
    if answer.data.get("dry_run"):
        return _finish_job(args, t, answer.data, "engines install")
    return _finish_engine_job(args, t, answer.data)


_ENGINE_TERMINAL = {"done", "failed", "cancelled"}
_ENGINE_PAUSED = {"needs_admin", "needs_tools"}


def _finish_engine_job(args: argparse.Namespace, t: Any, job: Dict[str, Any]) -> int:
    """Follow `/engines/jobs/{id}`; a job waiting for tools/admin stops here with what to do next."""

    last = None
    try:
        while not getattr(args, "no_wait", False) and str(job.get("state")) not in _ENGINE_TERMINAL | _ENGINE_PAUSED:
            line = (job.get("state"), job.get("message"))
            if not args.json and line != last:
                pct = f"{job['percent']:.0f}% " if isinstance(job.get("percent"), (int, float)) else ""
                print(f"  {pct}{job.get('message') or job.get('state')}", file=sys.stderr)
                last = line
            time.sleep(_POLL_S)
            answer = t.call("GET", f"/engines/jobs/{urllib.parse.quote(str(job['job_id']), safe='')}")
            if not answer.ok:
                print(f"  (lost the job: {answer.message()})", file=sys.stderr)
                return EXIT_ERROR
            job = answer.data
    except KeyboardInterrupt:
        print(f"\nStopped watching; the job keeps running. Follow it: abstractgateway engines status -- cancel: abstractgateway engines cancel {job.get('job_id')}", file=sys.stderr)
    state = str(job.get("state"))
    if args.json:
        _print_json(job)
    else:
        print(f"{state}: {job.get('message') or ''}".rstrip())
        prompt = job.get("admin_prompt") or {}
        tools = job.get("tools_prompt") or {}
        if state == "needs_admin":
            print(f"  why: {prompt.get('reason')}")
            print(f"  it will run, as administrator: {prompt.get('command')}")
            print(f"  continue (shows the password dialog on {prompt.get('where')}): abstractgateway engines continue {job.get('job_id')}")
        elif state == "needs_tools":
            action = tools.get("action") or {}
            print(f"  needs: {tools.get('tools')} -- {tools.get('reason')}")
            if action.get("available"):
                print(f"  install them (Apple's own installer dialog): abstractgateway engines continue {job.get('job_id')} --action install_tools")
            else:
                print(f"  install them on the host: {action.get('command')}")
            print(f"  then: abstractgateway engines continue {job.get('job_id')} --action recheck")
        elif state == "failed" and job.get("details"):
            print(f"  full log: {job.get('log_path') or '(in the job details: --json)'}")
    if state == "done" or (getattr(args, "no_wait", False) and state not in {"failed", "cancelled"}):
        return EXIT_OK
    return EXIT_REFUSED if state in _ENGINE_PAUSED else EXIT_ERROR


def _engines_continue(args: argparse.Namespace, t: Any) -> int:
    body = {"action": args.action} if args.action else {}
    answer = t.call("POST", f"/engines/jobs/{urllib.parse.quote(args.job_id, safe='')}/continue", body, timeout=max(args.timeout_s, 900.0))
    if not answer.ok:
        return _fail(args, answer, "engines continue")
    return _finish_engine_job(args, t, answer.data)


def _engines_cancel(args: argparse.Namespace, t: Any) -> int:
    answer = t.call("POST", f"/engines/jobs/{urllib.parse.quote(args.job_id, safe='')}/cancel")
    if not answer.ok:
        return _fail(args, answer, "engines cancel")
    _print_json(answer.data) if args.json else print(f"{answer.data.get('state')}: {answer.data.get('message')}")
    return EXIT_OK


def _engines_server(args: argparse.Namespace, t: Any) -> int:
    answer = t.call("POST", f"/engines/{urllib.parse.quote(args.engine, safe='')}/{args.engines_cmd}", timeout=max(args.timeout_s, 180.0))
    if not answer.ok:
        return _fail(args, answer, f"engines {args.engines_cmd}")
    data = answer.data or {}
    if args.json:
        _print_json(data)
    else:
        state = "running" if data.get("running") else ("stopped" if data.get("running") is False else "unknown")
        print(f"{args.engine}: {state}" + (f" at {data.get('base_url')}" if data.get("base_url") else "") + (f" -- {data['message']}" if data.get("message") else ""))
    return EXIT_OK if (data.get("running") is True) == (args.engines_cmd == "start") else EXIT_ERROR


def _engines_open(args: argparse.Namespace, t: Any) -> int:
    url = ""
    if isinstance(t, _Local):
        from . import core_config as seam

        try:
            url = seam.core_engine_download_url(args.engine)
        except seam.HostActionRefused as exc:
            return _fail(args, _Answer(exc.status_code, exc.payload()), "engines open")
    else:
        answer = t.call("GET", f"/engines/{urllib.parse.quote(args.engine, safe='')}")
        if not answer.ok:
            return _fail(args, answer, "engines open")
        row = answer.data or {}
        url = str((row.get("install") or {}).get("url") or row.get("docs_url") or "")
    if not url:
        print(f"abstractgateway engines open: no download page known for {args.engine}", file=sys.stderr)
        return EXIT_ERROR
    opened = False
    if not args.no_browser:
        try:
            import webbrowser

            opened = bool(webbrowser.open(url))
        except Exception:
            opened = False
    if args.json:
        _print_json({"engine": args.engine, "url": url, "opened": opened})
    else:
        print(url)
    return EXIT_OK


_MODELS: Dict[str, Callable[[argparse.Namespace, Any], int]] = {
    "list": _models_list,
    "catalog": _models_catalog,
    "search": _models_catalog,
    "download": _models_download,
    "delete": _models_delete,
    "jobs": _models_jobs,
    "cancel": _models_cancel,
}
_ENGINES: Dict[str, Callable[[argparse.Namespace, Any], int]] = {
    "status": _engines_status,
    "install": _engines_install,
    "continue": _engines_continue,
    "cancel": _engines_cancel,
    "start": _engines_server,
    "stop": _engines_server,
    "open": _engines_open,
}

MODELS_VERBS = tuple(_MODELS)


def run_models_verb(args: argparse.Namespace) -> int:
    return _MODELS[args.models_cmd](args, _transport(args))


def run_engines_command(args: argparse.Namespace) -> int:
    return _ENGINES[args.engines_cmd](args, _transport(args))
