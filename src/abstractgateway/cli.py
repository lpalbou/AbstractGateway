from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time
import warnings
import json
import sys
import urllib.parse
import copy
from pathlib import Path
from typing import Any


def _stderr(line: str) -> None:
    print(str(line), file=sys.stderr)


def _migrate_legacy_core_config_store() -> None:
    """Retire `<data_dir>/config/abstractcore.json` into the AbstractCore store.

    Startup is the only moment at which this can happen before a reader sees
    the divergence, so it runs here rather than lazily on first access. Every
    outcome is reported on stderr, including "nothing to do" being silent.
    """

    try:
        from .core_config_migration import (
            format_migration_report,
            migrate_legacy_gateway_stores,
        )

        report = migrate_legacy_gateway_stores()
        for line in format_migration_report(report):
            _stderr(line)
    except Exception as exc:  # pragma: no cover - a migration must not block boot
        _stderr(f"[WARN] legacy AbstractCore config migration skipped: {exc}")


def _resolve_default_console_level() -> int:
    """Return Gateway's default console log level (ERROR-only by default)."""
    level_map: dict[str, int] = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
        "NONE": logging.CRITICAL + 10,
    }
    for key in ("ABSTRACTGATEWAY_CONSOLE_LOG_LEVEL", "ABSTRACTGATEWAY_LOG_LEVEL", "LOG_LEVEL"):
        lvl = str(os.getenv(key) or "").strip().upper()
        if lvl:
            return level_map.get(lvl, logging.ERROR)
    return logging.ERROR


def _exit_now_if_nondaemon_stragglers() -> None:
    """Exit the process hard when non-daemon threads survive shutdown.

    Lifespan shutdown has already run by the time this is called, so every
    durable store is closed/flushed; a surviving non-daemon thread only
    proves some subsystem forgot to stop it (or marked it non-daemon by
    accident). Without this belt the interpreter joins those threads at
    exit FOREVER and the operator's TERM becomes a SIGKILL. We log names +
    full stacks (the forensics the root fix needs), then _exit(0) — the
    shutdown itself completed.
    """
    try:
        stragglers = [
            t
            for t in threading.enumerate()
            if t is not threading.main_thread() and t.is_alive() and not t.daemon
        ]
        if not stragglers:
            return
        names = ", ".join(sorted(t.name for t in stragglers))
        print(
            f"[WARN] shutdown complete but {len(stragglers)} non-daemon thread(s) survived: {names} — "
            "dumping stacks and exiting hard (file the named thread as a stop-path bug)",
            file=sys.stderr,
            flush=True,
        )
        try:
            import faulthandler

            faulthandler.dump_traceback(file=sys.stderr)
        except Exception:
            pass
    except Exception:
        pass
    os._exit(0)


def _uvicorn_log_level(console_level: int) -> str:
    if console_level <= logging.DEBUG:
        return "debug"
    if console_level <= logging.INFO:
        return "info"
    if console_level <= logging.WARNING:
        return "warning"
    if console_level <= logging.ERROR:
        return "error"
    return "critical"


def _configure_console_logging(level: int) -> None:
    """Best-effort console logging config aligned with Gateway's default format."""
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)
    root = logging.getLogger()
    if root.handlers:
        for h in list(root.handlers):
            try:
                h.setFormatter(formatter)
            except Exception:
                continue
        try:
            root.setLevel(int(level))
        except Exception:
            pass
        return
    logging.basicConfig(level=int(level), format=fmt, datefmt=datefmt)
    # `basicConfig` created handlers; set our preferred formatter.
    for h in list(logging.getLogger().handlers):
        try:
            h.setFormatter(formatter)
        except Exception:
            continue


def _build_uvicorn_log_config(*, uvicorn, silence_gpu_metrics_access_log: bool) -> dict:
    """Return a uvicorn log_config dict aligned with Gateway-style formatting.

    Note: access logs must use uvicorn's AccessFormatter so fields like client_addr/request_line/status_code
    are derived correctly from the positional args tuple.
    """
    try:
        base = getattr(getattr(uvicorn, "config", None), "LOGGING_CONFIG", None)
        if not isinstance(base, dict):
            return {}
        log_config = copy.deepcopy(base)
    except Exception:
        return {}

    datefmt = "%H:%M:%S"
    default_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    access_fmt = '%(asctime)s [%(levelname)s] %(name)s: %(client_addr)s - "%(request_line)s" %(status_code)s'

    fmts = log_config.setdefault("formatters", {})

    fmts["default"] = {"()": "uvicorn.logging.DefaultFormatter", "fmt": default_fmt, "datefmt": datefmt}
    fmts["access"] = {"()": "uvicorn.logging.AccessFormatter", "fmt": access_fmt, "datefmt": datefmt}

    if silence_gpu_metrics_access_log:
        log_config.setdefault("filters", {})["suppress_gpu_metrics"] = {
            "()": "abstractgateway.cli._UvicornAccessLogFilter"
        }
        access_handler = log_config.setdefault("handlers", {}).setdefault("access", {})
        filters = access_handler.get("filters")
        if isinstance(filters, list):
            if "suppress_gpu_metrics" not in filters:
                filters.append("suppress_gpu_metrics")
        else:
            access_handler["filters"] = ["suppress_gpu_metrics"]

    return log_config


def _is_loopback_host(host: str) -> bool:
    h = str(host or "").strip().lower()
    return h in {"127.0.0.1", "::1", "localhost"}


def _is_public_bind_host(host: str) -> bool:
    h = str(host or "").strip().lower()
    return h in {"0.0.0.0", "::"}


def _env_truthy(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_falsey(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() in {"0", "false", "no", "n", "off"}


def _maybe_bootstrap_user_auth_admin(*, host: str, port: int | None = None, print_token: bool | None = None) -> None:
    """Ensure `default/admin` exists and say how to get in.

    The admin token is printed on a loopback bind (the single-machine default),
    so a first launch shows the credential the browser apps ask for. It is
    hidden on a non-loopback bind, where stderr usually lands in shared logs.
    `serve --print-token` / `--no-print-token` override that default;
    `print_token=None` means "not given on the command line". On a loopback
    bind whose first run is not done yet, a one-time claim link (10 min,
    single use) is printed as well."""
    if _env_falsey("ABSTRACTGATEWAY_BOOTSTRAP_ADMIN") or _env_falsey("ABSTRACTGATEWAY_AUTO_BOOTSTRAP_ADMIN"):
        _stderr("Gateway user auth: enabled; admin auto-bootstrap disabled by env.")
        return

    from .config_cli import ensure_bootstrap_admin_user

    token_file = os.getenv("ABSTRACTGATEWAY_BOOTSTRAP_ADMIN_TOKEN_FILE") or None
    payload = ensure_bootstrap_admin_user(token_file=token_file)
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    tenant_id = str(user.get("tenant_id") or "default")
    user_id = str(user.get("user_id") or "admin")
    runtime_id = str(user.get("runtime_id") or user_id)
    token_path = str(payload.get("token_file") or "")
    token = str(payload.get("token") or "").strip()
    if print_token is None:
        # Legacy alias kept for existing service configs; the flag is the documented switch.
        print_token = _env_truthy("ABSTRACTGATEWAY_BOOTSTRAP_PRINT_TOKEN") or _is_loopback_host(host)
    can_print_raw_token = bool(token) and bool(print_token)

    _stderr("Gateway user auth: enabled.")
    _stderr(f"Gateway admin user: {tenant_id}/{user_id} (runtime: {runtime_id})")
    if token_path:
        _stderr(f"Gateway admin token file: {token_path}")
    if can_print_raw_token:
        _stderr(f"Gateway admin token: {token}")
    elif token_path:
        _stderr(f"Gateway admin token: in {token_path} (0600; not printed; `serve --print-token` prints it)")
    else:
        _stderr("Gateway admin token: unavailable; run `abstractgateway-config bootstrap-admin --rotate-token --print-token`.")

    if port is None:
        return
    try:
        from .first_run import browser_base_url, claim_url, first_run_state, mint_claim
        from .users import gateway_data_dir_from_env

        data_dir = gateway_data_dir_from_env()
        base = browser_base_url(host, int(port))
        if first_run_state(data_dir).get("completed"):
            _stderr(f"Console: {base}/console  (one-time sign-in link: `abstractgateway claim`)")
            return
        if not (_is_loopback_host(host) or _is_public_bind_host(host)):
            # Bound to one specific non-loopback address: a claim link needs a
            # loopback peer, which that bind cannot serve.
            _stderr(f"Console: {base}/console  (sign in with the admin token file above)")
            return
        minted = mint_claim(data_dir=data_dir, tenant_id=tenant_id, user_id=user_id, created_by="serve")
        _stderr(f"First run: open {claim_url(base, minted['code'])}")
        _stderr("           (one-time link, valid 10 minutes, works from this machine only; "
                "a fresh one: `abstractgateway claim --open`)")
    except Exception as exc:  # noqa: BLE001 - the link is a convenience; never block boot
        _stderr(f"[WARN] could not mint a first-run link ({exc}); run `abstractgateway claim`.")


def _is_weak_token(token: str) -> bool:
    t = str(token or "").strip()
    if not t:
        return True
    if t.lower() in {"dev-token", "devtoken", "token", "changeme", "password", "admin", "god", "zeus", "root", "superuser"}:
        return True
    # Heuristic: short shared secrets are easy to brute-force / leak.
    return len(t) < 15


def _looks_like_public_origin_pattern(pattern: str) -> bool:
    p = str(pattern or "").strip().lower()
    if not p:
        return False
    if p == "*":
        return True
    if "ngrok" in p and "*" in p:
        return True
    if "localhost" in p or "127.0.0.1" in p or "::1" in p:
        return False
    # Any wildcard on a non-loopback origin is risky.
    return "*" in p


class _UvicornAccessLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover
        # Silence the extremely high-frequency GPU metrics polling logs (200 OK only).
        # Keep non-200 logs as a security/ops signal.
        try:
            args = record.args
            # Uvicorn access logs pass args as:
            #   (client_addr, method, full_path, http_version, status_code)
            if isinstance(args, tuple) and len(args) >= 5:
                full_path = str(args[2] or "")
                status_code = args[4]
                if "/api/gateway/host/metrics/" in full_path or "/api/gateway/host/runner" in full_path:
                    try:
                        if int(status_code) == 200:
                            return False
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            msg = record.getMessage()
        except Exception:
            return True

        if ("/api/gateway/host/metrics/" in msg or "/api/gateway/host/runner" in msg) and msg.rstrip().endswith(" 200"):
            return False
        return True


def _run_data_command(args: Any) -> None:
    """`abstractgateway data list|purge` — CLI parity with the console's
    Data & Caches view (c1580 1c). Same facade, same verbatim refusals."""
    import json as _json

    from .data_homes import list_homes_with_sizes, purge_home, register_gateway_data_homes

    if args.data_cmd == "list":
        data_dir = Path(args.data_dir).expanduser() if getattr(args, "data_dir", None) else _default_data_dir()
        try:
            register_gateway_data_homes(data_dir)
        except Exception as e:  # noqa: BLE001
            print(f"#FALLBACK registration pass failed: {e}")
        rows, warnings = list_homes_with_sizes()
        for w in warnings:
            print(w)
        if not rows:
            print("no registered data homes")
            return
        for r in rows:
            size = r.get("size_bytes")
            # size_bytes=None ⇔ the path no longer exists (stale row) — say
            # so, never a "?" (operator honesty rule 2026-08-19).
            size_h = f"{size/1e6:,.1f} MB" if isinstance(size, (int, float)) else "missing"
            safe = "purgeable" if r.get("safe_to_purge") else "PROTECTED"
            print(f"{r.get('name')}  [{r.get('kind')}] {size_h}  {safe}  owner={r.get('owner')}  {r.get('path')}")
        return

    if args.data_cmd == "purge":
        if not args.dry_run and not args.yes:
            print("A real purge requires --yes (or use --dry-run to see the accounting first).")
            raise SystemExit(2)
        try:
            accounting = purge_home(str(args.name), dry_run=bool(args.dry_run))
        except Exception as e:  # noqa: BLE001 - registry refusals print verbatim
            print(str(e))
            raise SystemExit(1)
        print(_json.dumps(accounting, indent=2, ensure_ascii=False))
        return


def _default_data_dir() -> Path:
    from .users import gateway_data_dir_from_env

    return gateway_data_dir_from_env()


def _reserve_gguf_metal() -> None:
    """Reserve GPU offload for GGUF models before anything imports torch.

    Measured 2026-08-07: every GGUF model served by this gateway ran entirely on
    CPU — ~31 tokens/sec against ~1900 for the same box on the GPU, a 60x
    slowdown, with GPU utilisation at 0%. Cause: llama.cpp only takes the GPU
    when it is loaded before PyTorch, and the memory store's embedder imports
    PyTorch during boot. By the time a model is requested it is already too late,
    every time. Not a race — it happened on every single run.

    This must stay the FIRST thing `main` does. Anything that imports torch
    above this line silently restores the 60x slowdown.

    Logged, never fatal: a False return means GGUF will be slow, which the
    operator deserves to see, but it is not a reason to refuse to start.
    """
    try:
        import importlib.util
        import platform

        # Metal offload exists only on Apple silicon, and only for an installed
        # llama-cpp-python. Anywhere else AbstractCore's reservation is a no-op
        # that answers False, and the warning below would blame PyTorch on
        # every light install and every Linux host.
        if platform.system().lower() != "darwin" or platform.machine().lower() != "arm64":
            return
        if importlib.util.find_spec("llama_cpp") is None:
            return

        import abstractcore

        reserve = getattr(abstractcore, "enable_gguf_metal", None)
        if reserve is None:
            return  # older abstractcore; nothing to reserve
        if reserve():
            logging.getLogger(__name__).debug("GGUF GPU offload reserved")
        else:
            # `logger.warning` ALONE IS INVISIBLE HERE, twice over: abstractcore
            # sets the root logger to ERROR on import, and `main()` has already
            # called `_configure_console_logging(_resolve_default_console_level())`
            # — default ERROR — on the line above. So the first version of this
            # branch reproduced the exact bug it was written to report: silent
            # CPU-only GGUF, roughly 60x slower, with nothing on the console.
            # `warnings.warn` is on by default and is what actually reaches the
            # operator.
            msg = (
                "GGUF GPU offload could NOT be reserved: GGUF models served by "
                "this gateway will run on CPU (roughly 60x slower — measured "
                "~31 tok/s against ~1900). Cause: PyTorch was imported before "
                "this gateway started. Note `serve --reload` re-imports the app "
                "in a child process that never runs main(), which also loses the "
                "reservation."
            )
            logging.getLogger(__name__).warning(msg)
            warnings.warn(msg, RuntimeWarning, stacklevel=2)
    except Exception as exc:  # noqa: BLE001
        msg = f"GGUF GPU offload reservation failed ({exc}); GGUF models will run on CPU."
        logging.getLogger(__name__).warning(msg)
        warnings.warn(msg, RuntimeWarning, stacklevel=2)


def _tray_base_url(bind_host: str, port: int) -> str:
    """The URL the tray (and its 'Open Console' item) uses to reach THIS
    server: loopback for wildcard/loopback binds, the bound address itself
    otherwise (a 192.168.x.x bind does not answer on 127.0.0.1)."""
    h = str(bind_host or "").strip()
    if h in {"0.0.0.0", "127.0.0.1", "localhost", ""}:
        host = "127.0.0.1"
    elif h in {"::", "::1"}:
        host = "[::1]"
    elif ":" in h and not h.startswith("["):
        host = f"[{h}]"
    else:
        host = h
    return f"http://{host}:{int(port)}"


def _serve_with_host_controls(*, uvicorn: Any, args: Any, run_kwargs: dict, argv: list[str]) -> None:
    """Run uvicorn with the host-control seam (restart/shutdown from the tray
    or console) and the desktop tray helper (2026-09-05).

    `--reload` keeps uvicorn's own supervisor (the app lives in a child that
    never runs main(), so neither restart nor the tray can work there — both
    say so instead of half-working). Otherwise the Server is built explicitly
    so a request handler can ask for a graceful exit through
    `host_control`; the tray child is started right before serving and
    stopped right after, whatever the exit path.
    """
    from . import host_control
    from .self_update import installed_version
    from .tray_supervisor import get_tray_supervisor, record_serve_context, tray_decision
    from .users import gateway_data_dir_from_env

    data_dir = gateway_data_dir_from_env()
    base_url = _tray_base_url(str(args.host), int(args.port))
    version = installed_version()
    reload = bool(run_kwargs.get("reload"))
    record_serve_context(base_url=base_url, data_dir=data_dir, version=version, reload=reload, runner_only=False)

    if reload:
        host_control.register_server(
            None,
            restartable=False,
            block_reason="`serve --reload` runs the app in uvicorn's reloader child; restart from the tray is unavailable",
        )
        _stderr("Desktop tray: not started (dev_reload: `serve --reload` runs the app in a reloader child; start without --reload)")
        try:
            uvicorn.run("abstractgateway.app:app", **run_kwargs)
        finally:
            host_control.unregister_server()
        return

    if not (callable(getattr(uvicorn, "Config", None)) and callable(getattr(uvicorn, "Server", None))):
        # A uvicorn without the Server API (or a test double exposing only
        # run()): serve the old way and say what is unavailable.
        host_control.register_server(None, restartable=False, block_reason="this uvicorn exposes no Server API; restart from the tray is unavailable")
        try:
            uvicorn.run("abstractgateway.app:app", **run_kwargs)
        finally:
            host_control.unregister_server()
        return

    kwargs = {k: v for k, v in dict(run_kwargs).items() if k != "reload"}
    config = uvicorn.Config("abstractgateway.app:app", **kwargs)
    server = uvicorn.Server(config)
    host_control.register_server(server, restartable=True, relaunch_argv=list(argv))
    if str(os.getenv("FORWARDED_ALLOW_IPS") or "").strip() == "*":
        _stderr(
            "[WARN] This gateway was started with FORWARDED_ALLOW_IPS=* (uvicorn's proxy trust) in its environment: "
            "any client can rewrite its peer address through X-Forwarded-For, so the desktop tray's loopback-only "
            "token is no longer bound to this machine. A concrete proxy address there keeps it bound."
        )

    tray = get_tray_supervisor()
    # NO SETTING TO READ (operator ruling 2026-09-06): while `serve` runs on a
    # desktop that can hold an icon, the icon is there. What is left in
    # `tray_decision` is only what this machine can or cannot do.
    decision = tray_decision(reload=False, runner_only=False)
    if decision.start:
        # Spawn AFTER the listener is bound (a second `serve` on a busy port
        # must never point a tray at the FIRST gateway with the wrong token
        # — ten 401s lock every loopback client out). `server.started` flips
        # once uvicorn accepts connections; boot (minutes) is separate and
        # the tray shows "Starting…" meanwhile.
        def _start_tray_when_bound() -> None:
            deadline = time.monotonic() + 120.0
            while time.monotonic() < deadline:
                if getattr(server, "should_exit", False):
                    return
                if getattr(server, "started", False):
                    break
                time.sleep(0.1)
            else:
                return
            st = tray.start(base_url=base_url, data_dir=data_dir, version=version, decision=decision)
            if st.get("running"):
                _stderr(f"Desktop tray: started (pid {st.get('pid')}); console at {base_url}/console")
            else:
                _stderr(f"[WARN] Desktop tray: failed to start: {st.get('error') or st.get('failure') or 'unknown error'}")

        threading.Thread(target=_start_tray_when_bound, name="gateway-tray-launch", daemon=True).start()
    elif decision.reason != "headless":
        # A headless host does not care; a desktop that could have had the
        # icon deserves the one line that says how to get it.
        hint = f": {decision.hint}" if decision.hint else ""
        _stderr(f"Desktop tray: not started ({decision.reason}{hint})")

    try:
        server.run()
        # A normal return: uvicorn drained and (when a signal arrived) is
        # about to re-raise it — a restart request may now be honoured.
        host_control.mark_clean_exit()
    except KeyboardInterrupt:
        # uvicorn.run()'s own contract: Ctrl-C is a quiet stop, not a traceback
        # — and never a relaunch.
        host_control.clear_requests()
    except BaseException:
        host_control.clear_requests()
        raise
    finally:
        tray.stop()
        host_control.unregister_server()
    # uvicorn.run()'s own contract: a server that never started (port in
    # use, bad bind) exits with STARTUP_FAILURE so launchers see it.
    if not getattr(server, "started", True):
        try:
            from uvicorn.main import STARTUP_FAILURE as _startup_failure
        except Exception:  # noqa: BLE001
            _startup_failure = 3
        raise SystemExit(int(_startup_failure))


def _record_serve_start(args: Any) -> None:
    """`<data>/run/gateway-serve.json`: how to reach THIS gateway (host, port,
    URL, auth mode). `abstractgateway claim` reads it to build its link."""
    try:
        from .first_run import auth_mode_summary, write_serve_record
        from .host_paths import resolve_data_dir
        from .self_update import installed_version

        res = resolve_data_dir()
        write_serve_record(
            data_dir=res.path,
            host=str(args.host),
            port=int(args.port),
            auth=auth_mode_summary(),
            data_dir_source=res.source,
            version=installed_version(),
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        _stderr(f"[WARN] could not write the serve record: {exc}")


def _record_serve_stop() -> None:
    try:
        from .first_run import clear_serve_record
        from .host_paths import resolve_data_dir

        clear_serve_record(resolve_data_dir().path)
    except Exception:
        pass


def _apply_backlog_launch_flags(args: Any, data_dir: Path) -> None:
    """`serve --backlog-root PATH` / `--exec-runner on|off` (mission II).

    Validated like every other door (a refusal stops the start with the
    plain sentence), then recorded for this run under <data dir>/run/ so the
    resolver — in this process, `--reload` workers, and `config get` —
    reports them as `source: flag`. Without flags the record is removed, so
    an earlier run's flag never outlives it. No environment variable is set.
    """
    from .runtime_config import RuntimeConfigError, record_launch_settings, validate_backlog_root

    values: dict[str, object] = {}
    raw_root = getattr(args, "backlog_root", None)
    if raw_root:
        try:
            values["triage_repo_root"] = str(validate_backlog_root(raw_root, data_dir, what="--backlog-root"))
        except RuntimeConfigError as exc:
            raise SystemExit(f"Refusing to start: {exc}")
        _stderr(f"Backlog folder: {values['triage_repo_root']} (from --backlog-root, for this run)")
    raw_runner = getattr(args, "exec_runner", None)
    if raw_runner:
        values["backlog_exec_runner"] = str(raw_runner).strip().lower() == "on"
    record_launch_settings(data_dir, values)


def _clear_backlog_launch_flags(data_dir: Path) -> None:
    try:
        from .runtime_config import clear_launch_settings

        clear_launch_settings(data_dir)
    except Exception:
        pass


def _backlog_exec_repo_root(args: Any, data_dir: Path) -> Path:
    """`backlog-exec-runner`: `--repo-root` > the backlog-folder resolution."""
    from .runtime_config import RuntimeConfigError, resolve_backlog_root, validate_backlog_root

    if getattr(args, "repo_root", None):
        try:
            return validate_backlog_root(args.repo_root, data_dir, what="--repo-root")
        except RuntimeConfigError as exc:
            raise SystemExit(str(exc))
    res = resolve_backlog_root(data_dir, ensure=True)
    if not res.get("available"):
        raise SystemExit(f"The backlog folder {res.get('value')} is not available: {res.get('reason')}.")
    return Path(str(res["value"]))


def _run_models_command(args: argparse.Namespace) -> int:
    """`abstractgateway models loaded|load|unload` -> the console's routes. Prints
    the gateway's JSON answer; exit 0 only when the gateway says it worked."""
    import json as _json

    from .tray.client import GatewayClient

    token = args.token if args.token is not None else os.environ.get("ABSTRACTGATEWAY_AUTH_TOKEN", "")
    client = GatewayClient(args.url, token)
    if args.models_cmd == "loaded":
        query = {k: v for k, v in (("provider", args.provider), ("model", args.model)) if v}
        path = "/models/loaded" + (("?" + urllib.parse.urlencode(query)) if query else "")
        res = client._request("GET", path, timeout=args.timeout_s)
    else:
        body = {"provider": args.provider, "model": args.model}
        if args.models_cmd == "unload" and getattr(args, "force", False):
            body["force"] = True
        res = client._request("POST", f"/models/{args.models_cmd}", body=body, timeout=args.timeout_s)
    print(_json.dumps(res.data if res.data is not None else {"error": res.error, "status": res.status},
                      indent=2, default=str))
    worked = res.ok  # GatewayClient: 2xx AND not an in-band {"ok": false}
    if not worked:
        print(f"abstractgateway models {args.models_cmd}: {res.detail}", file=sys.stderr)
    return 0 if worked else 1


def main(argv: list[str] | None = None) -> None:
    console_level = _resolve_default_console_level()
    _configure_console_logging(console_level)
    _argv0 = (list(argv) if argv is not None else sys.argv[1:])[:1]
    if _argv0 not in (
        ["claim"], ["service"], ["models"], ["engines"], ["apps"], ["network"], ["config"],
        ["--version"], ["-h"], ["--help"],
    ):
        # The first-run verbs, the models/engines verbs, `config` and the
        # version/help flags never load a model in this process; the
        # reservation (and its CPU-fallback warning) is noise for them.
        _reserve_gguf_metal()
    from . import __version__ as _gateway_version

    parser = argparse.ArgumentParser(prog="abstractgateway", description="AbstractGateway (Run Gateway host)")
    parser.add_argument("--version", action="version", version=f"abstractgateway {_gateway_version}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="Run the AbstractGateway HTTP/SSE server")
    serve.add_argument(
        "--host",
        default=None,
        help="Bind host; overrides the network exposure setting (`abstractgateway network`). Default: the "
        "setting (localhost 127.0.0.1, lan/internet 0.0.0.0); with none stored, 127.0.0.1 when no auth is "
        "configured (a bare `serve` starts with user auth on), 0.0.0.0 when an auth token or user auth is configured",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port; overrides the network setting's port (default: the setting's, else 8080)",
    )
    serve.add_argument(
        "--print-token",
        action=argparse.BooleanOptionalAction,
        default=None,
        dest="print_token",
        help="Print the admin token at startup (default: yes on a loopback bind, no otherwise; "
        "the token always stays in <data dir>/auth/bootstrap-admin-token)",
    )
    serve.add_argument("--reload", action="store_true", help="Enable auto-reload (dev only)")
    serve.add_argument(
        "--no-runner",
        action="store_true",
        help="Serve the HTTP API without starting the runner (use `abstractgateway runner` in another process).",
    )
    serve.add_argument(
        "--data-dir",
        default=None,
        help="Data dir (default: $ABSTRACTGATEWAY_DATA_DIR, else ./runtime if it exists, else the per-OS user data dir)",
    )
    serve.add_argument(
        "--backlog-root",
        default=None,
        metavar="PATH",
        help="Backlog folder for this run: a folder containing docs/backlog (Continuum's Board and Backlog read it). "
        "Wins over the saved setting (`abstractgateway config set triage_repo_root PATH`) until the gateway stops. "
        "Default: the saved setting, else the gateway's own folder <data dir>/backlog, created on first use.",
    )
    serve.add_argument(
        "--exec-runner",
        default=None,
        choices=["on", "off"],
        help="Run queued backlog executions on this machine for this run (wins over the saved setting "
        "`abstractgateway config set backlog_exec_runner on|off`). Default: the saved setting, else off.",
    )

    runner = sub.add_parser("runner", help="Run the AbstractGateway runner worker (no HTTP)")

    cfg_cmd = sub.add_parser("config", help="Run the AbstractGateway configuration helper")
    cfg_cmd.add_argument("config_args", nargs=argparse.REMAINDER, help="Arguments forwarded to abstractgateway-config")

    tg = sub.add_parser("telegram-auth", help="One-time TDLib authentication bootstrap for Telegram Secret Chats (E2EE)")
    tg.add_argument("--timeout-s", type=float, default=120.0, help="Max seconds to wait for TDLib authorization (default: 120)")

    mig = sub.add_parser("migrate", help="Migrate durable stores between backends (best-effort)")
    mig.add_argument("--from", dest="src", default="file", choices=["file"], help="Source backend (default: file)")
    mig.add_argument("--to", dest="dst", default="sqlite", choices=["sqlite"], help="Destination backend (default: sqlite)")
    mig.add_argument(
        "--data-dir",
        default=None,
        help="Source data dir (default: the gateway data dir, see `abstractgateway-config status`)",
    )
    mig.add_argument(
        "--db-path",
        default=None,
        help="Destination sqlite file path (defaults to <data-dir>/gateway.sqlite3)",
    )
    mig.add_argument("--overwrite", action="store_true", help="Overwrite destination DB if it exists")

    triage = sub.add_parser("triage-reports", help="Triage /bug and /feature reports (decision queue + optional backlog drafts)")
    triage.add_argument(
        "--data-dir",
        default=None,
        help="Gateway data dir (default: the gateway data dir, see `abstractgateway-config status`)",
    )
    triage.add_argument(
        "--repo-root",
        default=None,
        help="Repo root containing docs/backlog (auto-detected from CWD if omitted)",
    )
    triage.add_argument("--write-drafts", action="store_true", help="Write backlog drafts into docs/backlog/proposed/")
    triage.add_argument("--llm", action="store_true", help="Enable optional LLM assist (env/config required)")
    triage.add_argument("--action-base-url", default=None, help="Base URL for triage action links (e.g., https://<host>)")
    triage.add_argument("--print-actions", action="store_true", help="Print approve/defer/reject action links for pending decisions")
    triage.add_argument("--notify", action="store_true", help="Send a notification (Telegram/email) when pending decisions exist")
    triage.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")

    triage_apply = sub.add_parser("triage-apply", help="Apply a triage decision action (approve/reject/defer)")
    triage_apply.add_argument("decision_id", help="Decision id (stable hash) under <data_dir>/triage_queue/")
    triage_apply.add_argument("action", choices=["approve", "reject", "defer"], help="Action to apply")
    triage_apply.add_argument(
        "--data-dir",
        default=None,
        help="Gateway data dir (default: the gateway data dir, see `abstractgateway-config status`)",
    )
    triage_apply.add_argument(
        "--repo-root",
        default=None,
        help="Repo root containing docs/backlog (auto-detected from CWD if omitted)",
    )

    be = sub.add_parser("backlog-exec-runner", help="Run backlog execution runner (consumes backlog_exec_queue)")
    be.add_argument(
        "--data-dir",
        default=None,
        help="Gateway data dir (default: the gateway data dir, see `abstractgateway-config status`)",
    )
    be.add_argument(
        "--repo-root",
        default=None,
        help="Folder containing docs/backlog (default: the gateway's backlog folder setting, "
        "`abstractgateway config get triage_repo_root`)",
    )

    from .entity_cli import add_entity_subparser

    add_entity_subparser(sub)

    # Data & Caches CLI parity (operator priority 18:19, c1580 1c): the same
    # registry truth the console renders — list with live sizes, purge with
    # the registry's verbatim refusals, dry-run for the cautious.
    data_cmd = sub.add_parser("data", help="Registered data homes: list sizes, purge safe rows")
    data_sub = data_cmd.add_subparsers(dest="data_cmd", required=True)
    data_list = data_sub.add_parser("list", help="All registered data homes with live sizes")
    data_list.add_argument("--data-dir", default=None, help="Gateway data dir (registers its homes before listing)")
    data_purge = data_sub.add_parser("purge", help="Purge one registered home's CONTENTS (owner-declared safe rows only)")
    data_purge.add_argument("name", help="Registered row name (see `data list`)")
    data_purge.add_argument("--dry-run", action="store_true", help="Account without deleting")
    data_purge.add_argument("--yes", action="store_true", help="Confirm the real purge (required without --dry-run)")

    # Network exposure (2026-09-24): the tray/console/TUI setting from a shell,
    # through the same functions as GET/POST /api/gateway/network.
    from .network_exposure import add_network_subparser

    add_network_subparser(sub)

    # Model residency from a shell (2026-09-23): the SAME routes the web console,
    # the console TUI and the tray drive (GET /models/loaded, POST /models/load,
    # POST /models/unload) — list, warm, eject — against a RUNNING gateway.
    models_cmd = sub.add_parser(
        "models",
        help="Models on the gateway host: installed, catalog, download, delete, jobs; load / eject (console routes)",
    )
    models_sub = models_cmd.add_subparsers(dest="models_cmd", required=True)
    for _name, _help in (
        ("loaded", "What is resident (every local provider the gateway can see)"),
        ("load", "Load (warm) a provider/model"),
        ("unload", "Eject a provider/model: in-flight calls on it are cancelled first"),
    ):
        _p = models_sub.add_parser(_name, help=_help)
        _p.add_argument("--url", default=os.environ.get("ABSTRACTGATEWAY_URL") or "http://127.0.0.1:8080",
                        help="Gateway base URL (default: $ABSTRACTGATEWAY_URL or http://127.0.0.1:8080)")
        _p.add_argument("--token", default=None, help="Bearer token (default: $ABSTRACTGATEWAY_AUTH_TOKEN)")
        _p.add_argument("--provider", required=_name != "loaded", default=None)
        _p.add_argument("--model", required=_name != "loaded", default=None)
        _p.add_argument("--timeout-s", type=float, default={"loaded": 60.0, "load": 1800.0, "unload": 600.0}[_name],
                        help="HTTP timeout for this call (explicit; a model load can take minutes)")
        if _name == "unload":
            _p.add_argument("--force", action="store_true", help="Unload even a locked model")

    # The Models and Engines tabs from a shell (AbstractCore >= 2.14.0 through
    # the seam): the same verbs and arguments as `abstractcore models|engines`,
    # so a console job card's `cli_equivalent` runs here as printed.
    from .models_engines_cli import add_engines_subparser, add_models_verbs

    add_models_verbs(models_sub)
    add_engines_subparser(sub)

    from .firstrun_cli import add_claim_arguments, add_service_subparser

    claim = sub.add_parser("claim", help="Print a one-time console sign-in link for this machine (first run)")
    add_claim_arguments(claim)
    add_service_subparser(sub)

    # Browser apps (Node.js for the user, npm installs, supervised app
    # servers): the console's Apps page from a shell, through the running
    # gateway's /api/gateway/apps routes.
    from .apps_cli import add_apps_subparser

    add_apps_subparser(sub)

    args = parser.parse_args(argv)

    if args.cmd == "apps":
        from .apps_cli import run_apps_command

        raise SystemExit(run_apps_command(args))

    if args.cmd == "claim":
        from .firstrun_cli import run_claim

        raise SystemExit(run_claim(args))

    if args.cmd == "service":
        from .firstrun_cli import run_service

        raise SystemExit(run_service(args))

    if args.cmd in ("serve", "network") and getattr(args, "data_dir", None):
        from .host_paths import DATA_DIR_SOURCE_ENV

        import pathlib as _pathlib  # `Path` is function-local in main() (later branches import it)

        os.environ["ABSTRACTGATEWAY_DATA_DIR"] = str(_pathlib.Path(args.data_dir).expanduser().resolve())
        os.environ.pop(DATA_DIR_SOURCE_ENV, None)

    if args.cmd not in ("models", "engines"):
        # ONE data dir for this process and every child it spawns: an unset
        # ABSTRACTGATEWAY_DATA_DIR is resolved here (./runtime in a checkout,
        # else the per-OS user data dir) and exported with its provenance.
        from .host_paths import apply_data_dir_default, describe_resolution

        _data_dir_resolution = apply_data_dir_default()
        if args.cmd == "serve":
            _stderr(describe_resolution(_data_dir_resolution))

    if args.cmd == "models":
        from .models_engines_cli import MODELS_VERBS, run_models_verb

        if args.models_cmd in MODELS_VERBS:
            raise SystemExit(run_models_verb(args))
        raise SystemExit(_run_models_command(args))

    if args.cmd == "engines":
        from .models_engines_cli import run_engines_command

        raise SystemExit(run_engines_command(args))

    if args.cmd == "entity":
        from .entity_cli import run_entity_command

        run_entity_command(args)
        return

    if args.cmd == "data":
        _run_data_command(args)
        return

    if args.cmd == "network":
        from .network_exposure import run_network_command

        raise SystemExit(run_network_command(args))

    if args.cmd == "serve":
        # ------------------------------------------------------------------
        # ONE STORE (operator ruling 2026-08-01). A legacy Gateway-scoped
        # AbstractCore store under the data dir is merged into THE Core store
        # and renamed, before anything reads a capability default. Loud on
        # stderr, idempotent, and never fatal: a Gateway must still start.
        # ------------------------------------------------------------------
        _migrate_legacy_core_config_store()

        # ------------------------------------------------------------------
        # First run (2026-09-23): an unconfigured `serve` binds loopback and
        # turns user auth on, so it STARTS; explicit configuration always wins
        # and a non-loopback bind without auth still refuses below.
        # ------------------------------------------------------------------
        from .first_run import apply_loopback_auth_default

        # Network exposure (2026-09-24): host/port = `--host/--port` > the
        # stored setting (localhost / lan / internet, runtime-config key
        # `network`) > the historical default. Every override, auth export
        # and refusal is printed here and reported by GET /api/gateway/network.
        from .host_paths import resolve_data_dir
        from .network_exposure import prepare_serve_bind, record_serve_bind

        _serve_data_dir = resolve_data_dir().path
        _bind = prepare_serve_bind(
            cli_host=getattr(args, "host", None) or None,
            cli_port=getattr(args, "port", None),
            data_dir=_serve_data_dir,
        )
        args.host, args.port = _bind.host, _bind.port
        for _msg in _bind.messages:
            _stderr(_msg)
        record_serve_bind(_serve_data_dir, _bind)
        _apply_backlog_launch_flags(args, _serve_data_dir)
        # The bind host decides host-safety defaults inside the app (engine
        # installs are allowed by default on a loopback bind, and otherwise
        # only for a caller on this machine).
        from .runtime_config import BIND_HOST_ENV

        os.environ[BIND_HOST_ENV] = str(args.host)
        if apply_loopback_auth_default(str(args.host)):
            _stderr(
                f"Gateway auth: user auth enabled automatically (bound to loopback {args.host}, no auth posture in "
                "this gateway's environment): every person signs in with an account."
            )

        # ------------------------------------------------------------------
        # Startup security self-checks (fail-fast on missing auth token).
        # ------------------------------------------------------------------
        try:
            from .security import load_gateway_auth_policy_from_env
        except Exception:
            load_gateway_auth_policy_from_env = None  # type: ignore[assignment]

        if load_gateway_auth_policy_from_env is not None:
            policy = load_gateway_auth_policy_from_env()
            host = str(getattr(args, "host", "") or "")

            if (
                bool(policy.enabled)
                and bool(policy.protect_write_endpoints)
                and not tuple(policy.tokens or ())
                and not bool(getattr(policy, "user_auth_enabled", False))
            ):
                raise SystemExit(
                    "Refusing to start: no sign-in would protect this gateway.\n\n"
                    f"--host {host or '0.0.0.0'} listens beyond this computer, and this gateway was started with "
                    "neither accounts (user auth) nor a token.\n\n"
                    "Choose who can reach it with the network setting instead of --host (accounts are turned on "
                    "for you):\n"
                    "  abstractgateway network set lan      # or: internet --acknowledge-internet\n"
                    "  abstractgateway serve\n"
                    "\n"
                    "Or keep it on this computer only: `abstractgateway serve --host 127.0.0.1`."
                )

            if bool(getattr(policy, "user_auth_enabled", False)):
                _maybe_bootstrap_user_auth_admin(
                    host=host, port=int(args.port), print_token=getattr(args, "print_token", None)
                )

            if _is_public_bind_host(host):
                _stderr(
                    "[WARN] Gateway is binding to 0.0.0.0/:: (non-loopback). "
                    "If you expose this service (ngrok/LAN), ensure you use strong Gateway auth and restrict origins."
                )
                # Mission Z: hardening steps are settings and commands, never
                # environment variables (operator rule 2026-09-24).
                _stderr("       Hardening:")
                if bool(getattr(policy, "user_auth_enabled", False)):
                    _stderr("         every person signs in with their own account (console: Users); create the")
                    _stderr("         admin if there is none: abstractgateway-config bootstrap-admin --print-token")
                else:
                    _stderr("         this gateway was started with a shared token and no accounts: whoever holds")
                    _stderr("         the token is its admin, so it must be long and random")
                _stderr("         browser origins behind a proxy or tunnel: console Network -> Reverse proxy, or")
                _stderr("           abstractgateway network set --allowed-origins https://<your public origin>")
                _stderr("         keep the backlog exec runner off unless needed (runtime setting backlog_exec_runner)")
                if any(_is_weak_token(t) for t in tuple(policy.tokens or ())):
                    raise SystemExit(
                        "Refusing to start: weak auth token detected while binding to a non-loopback host.\n"
                        "The shared token this gateway was started with is weak (fewer than 15 characters, or guessable); "
                        "it needs a long random one."
                    )

            origins = tuple(getattr(policy, "allowed_origins", ()) or ())
            public_wildcard_origins = any(_looks_like_public_origin_pattern(o) for o in origins)
            if public_wildcard_origins:
                _stderr(
                    "[WARN] The browser origins this gateway was started with (its environment) contain public "
                    "wildcard patterns. This weakens browser-origin protections."
                )
                if not _is_loopback_host(host) and any(_is_weak_token(t) for t in tuple(policy.tokens or ())):
                    raise SystemExit(
                        "Refusing to start: weak auth token detected while using public wildcard origins.\n"
                        "The shared token this gateway was started with is weak (fewer than 15 characters, or guessable); "
                        "it needs a long random one."
                    )

            # Backlog exec runner can run code/tools; warn loudly when enabled
            # (THE resolution: --exec-runner > saved setting > legacy env > off).
            from .runtime_config import resolve_exec_runner

            _runner = resolve_exec_runner(_serve_data_dir)
            if _runner.get("value"):
                _why = {"flag": "from --exec-runner", "stored": "the saved setting", "env": "set by the environment"}
                _stderr(
                    f"[WARN] Backlog exec runner is enabled ({_why.get(str(_runner.get('source')), _runner.get('source'))}). "
                    "It can execute queued backlog tasks on this machine. Only enable this in trusted environments."
                )

        if str(os.getenv("ABSTRACTGATEWAY_SILENCE_GPU_METRICS_ACCESS_LOG", "1")).strip().lower() in {"1", "true", "yes", "on"}:
            silence_gpu_metrics_access_log = True
        else:
            silence_gpu_metrics_access_log = False

        prev_runner_env = os.environ.get("ABSTRACTGATEWAY_RUNNER")
        if bool(getattr(args, "no_runner", False)):
            # Override env for this process: do not start the background runner loop in the HTTP API process.
            os.environ["ABSTRACTGATEWAY_RUNNER"] = "0"

        try:
            import uvicorn
        except Exception as e:
            raise SystemExit(
                "AbstractGateway HTTP server dependencies are missing.\n"
                "Install with: `pip install abstractgateway` (or from source: `pip install .`).\n"
                f"(import failed: {e})"
            )

        try:
            run_kwargs: dict[str, object] = {
                "host": str(args.host),
                "port": int(args.port),
                "reload": bool(args.reload),
            }
            run_kwargs["log_level"] = _uvicorn_log_level(int(console_level))

            # Bounded graceful shutdown (resilience wave 2026-07-21, adversary
            # P1-4): without this, a wedged in-flight request or a hung
            # lifespan-shutdown step holds SIGTERM forever and the supervisor
            # must SIGKILL. 120s covers the runner drain (30s) + bounded
            # entity close_all (60s) with margin.
            try:
                shutdown_s = int(str(os.getenv("ABSTRACTGATEWAY_GRACEFUL_SHUTDOWN_S") or "120").strip())
            except ValueError:
                shutdown_s = 120
            if shutdown_s > 0:
                run_kwargs["timeout_graceful_shutdown"] = shutdown_s

            log_config = _build_uvicorn_log_config(
                uvicorn=uvicorn,
                silence_gpu_metrics_access_log=bool(silence_gpu_metrics_access_log),
            )
            if log_config:
                run_kwargs["log_config"] = log_config

            _record_serve_start(args)
            _serve_with_host_controls(uvicorn=uvicorn, args=args, run_kwargs=run_kwargs, argv=list(argv if argv is not None else sys.argv[1:]))
        finally:
            _record_serve_stop()
            _clear_backlog_launch_flags(_serve_data_dir)
            if prev_runner_env is None:
                os.environ.pop("ABSTRACTGATEWAY_RUNNER", None)
            else:
                os.environ["ABSTRACTGATEWAY_RUNNER"] = prev_runner_env
            # Restart requested from the tray/console (host_control,
            # 2026-09-05): ONLY after uvicorn returned cleanly (a Ctrl-C or
            # an exception during the drain means "stop", never "bounce"),
            # the tray child is gone — replace this process with a fresh
            # gateway BEFORE the straggler belt below can _exit() us.
            try:
                from . import host_control as _host_control

                if _host_control.should_relaunch():
                    _host_control.relaunch_process()  # never returns
            except Exception as exc:  # noqa: BLE001 - a failed relaunch must be loud, then exit normally
                _stderr(f"[ERROR] gateway restart failed: {exc}")
            # Deterministic-exit belt (shutdown-forensics, 2026-07-24): after
            # uvicorn returns, ANY surviving non-daemon thread makes the
            # interpreter wait forever at exit — the true "TERM hangs, operator
            # SIGKILLs" class (stage logging in _stop_gateway_service_instance
            # covers the bounded-but-silent class). Lifespan shutdown already
            # ran, so nothing durable is at risk: name the stragglers, dump
            # every stack for the root fix, then exit hard.
            _exit_now_if_nondaemon_stragglers()
        return

    if args.cmd == "runner":
        # Force runner enabled for this process.
        prev_runner_env = os.environ.get("ABSTRACTGATEWAY_RUNNER")
        os.environ["ABSTRACTGATEWAY_RUNNER"] = "1"

        from .service import start_gateway_runner, stop_gateway_runner

        stop = threading.Event()

        def _handle(_signum, _frame) -> None:  # pragma: no cover
            stop.set()

        try:
            signal.signal(signal.SIGINT, _handle)
            signal.signal(signal.SIGTERM, _handle)
        except Exception:
            # Some platforms (or embedded interpreters) may not support signals.
            pass

        start_gateway_runner()
        try:
            while not stop.is_set():
                stop.wait(0.5)
        finally:
            stop_gateway_runner()
            if prev_runner_env is None:
                os.environ.pop("ABSTRACTGATEWAY_RUNNER", None)
            else:
                os.environ["ABSTRACTGATEWAY_RUNNER"] = prev_runner_env
        return

    if args.cmd == "config":
        from .config_cli import main as config_main

        config_main(list(getattr(args, "config_args", []) or []))
        return

    if args.cmd == "telegram-auth":
        # This command is intentionally interactive. It is meant to be run once to
        # create the TDLib session under ABSTRACT_TELEGRAM_DB_DIR.
        import getpass

        try:
            from abstractruntime.integrations.abstractcore import bootstrap_telegram_auth_from_env
        except Exception as e:
            raise SystemExit(
                "TDLib bootstrap requires Gateway's Runtime Telegram integration. "
                "Install/repair with: `pip install abstractgateway` "
                f"(import failed: {e})"
            )

        code = input("Telegram login code (leave blank if not needed): ").strip() or None
        pw = getpass.getpass("Telegram 2FA password (leave blank if none): ").strip() or None

        try:
            payload = bootstrap_telegram_auth_from_env(
                login_code=code,
                two_factor_password=pw,
                timeout_s=float(args.timeout_s),
            )
        except Exception as e:
            raise SystemExit(str(e)) from e

        if not bool(payload.get("success")) or not bool(payload.get("ready")):
            raise SystemExit(str(payload.get("error") or "Timed out waiting for TDLib authorization"))

        print("TDLib authorization: OK (session stored in TDLib database directory).")
        return

    if args.cmd == "migrate":
        from pathlib import Path

        from .migrate import migrate_file_to_sqlite

        data_dir = Path(str(args.data_dir or "")).expanduser().resolve() if args.data_dir else None
        if data_dir is None:
            data_dir = _default_data_dir()
        db_path = Path(str(args.db_path or "")).expanduser().resolve() if args.db_path else (data_dir / "gateway.sqlite3")

        if str(args.src).strip().lower() != "file" or str(args.dst).strip().lower() != "sqlite":
            raise SystemExit("Only --from=file --to=sqlite is supported in v0")

        migrate_file_to_sqlite(base_dir=data_dir, db_path=db_path, overwrite=bool(args.overwrite))
        print(f"Migrated file stores from {data_dir} to sqlite DB {db_path}")
        return

    if args.cmd == "triage-reports":
        from pathlib import Path

        from .maintenance.action_tokens import build_action_links
        from .maintenance.notifier import send_email_notification, send_telegram_notification
        from .maintenance.triage import triage_reports
        from .maintenance.triage_queue import decisions_dir, iter_decisions

        data_dir = Path(str(args.data_dir or "")).expanduser().resolve() if args.data_dir else None
        if data_dir is None:
            data_dir = _default_data_dir()

        repo_root = Path(str(args.repo_root)).expanduser().resolve() if args.repo_root else None

        out = triage_reports(
            gateway_data_dir=data_dir,
            repo_root=repo_root,
            write_drafts=bool(args.write_drafts),
            enable_llm=bool(args.llm),
        )
        pending = []
        qdir = decisions_dir(gateway_data_dir=data_dir)
        pending = [d for d in iter_decisions(qdir) if d.status == "pending"]

        if (bool(args.print_actions) or bool(args.notify)) and args.action_base_url:
            secret = os.getenv("ABSTRACTGATEWAY_TRIAGE_ACTION_SECRET") or os.getenv("ABSTRACT_TRIAGE_ACTION_SECRET") or ""
            secret = str(secret).strip()
            if secret:
                out["pending_decisions"] = len(pending)
                out["action_links"] = {}
                for d in pending[:25]:
                    out["action_links"][d.decision_id] = build_action_links(
                        decision_id=d.decision_id,
                        base_url=str(args.action_base_url),
                        secret=secret,
                    )
            else:
                out["action_links_error"] = "Missing TRIAGE_ACTION_SECRET (links disabled)"

        if bool(args.notify) and pending:
            # Compose a compact, actionable digest.
            lines = [f"Triage: {len(pending)} pending report decisions"]
            for d in pending[:10]:
                lines.append(f"- {d.decision_id}: {d.report_relpath}")
                if d.missing_fields:
                    lines.append(f"  missing: {', '.join(d.missing_fields[:3])}")
                links = (out.get("action_links") or {}).get(d.decision_id) if isinstance(out.get("action_links"), dict) else None
                if isinstance(links, dict) and links:
                    lines.append(f"  approve: {links.get('approve')}")
                    lines.append(f"  defer 1d: {links.get('defer_1d')}")
                    lines.append(f"  defer 7d: {links.get('defer_7d')}")
                    lines.append(f"  reject: {links.get('reject')}")
                else:
                    lines.append(f"  approve: abstractgateway triage-apply {d.decision_id} approve")
                    lines.append(f"  reject: abstractgateway triage-apply {d.decision_id} reject")
                    lines.append(f"  defer:  ABSTRACT_TRIAGE_DEFER_DAYS=7 abstractgateway triage-apply {d.decision_id} defer")
            body = "\n".join(lines).strip() + "\n"
            # Telegram first (short), then email (full). The short channel is
            # bounded by Telegram's own 4096-char protocol limit — but a
            # silent cut would hide pending triage items from the operator
            # entirely (ADR-0026 §1), so it names the cut and the full channel.
            if len(body) > 3500:
                #[WARNING:TRUNCATION] telegram triage digest bounded; email is full
                tg_text = (
                    body[:3500]
                    + f"\n… [#TRUNCATION: 3500 of {len(body)} chars for "
                      "Telegram; the email lists every pending decision]"
                )
            else:
                tg_text = body
            ok_tg, err_tg = send_telegram_notification(text=tg_text)
            ok_em, err_em = send_email_notification(subject=f"[AbstractFramework] Triage pending ({len(pending)})", body_text=body)
            out["notify"] = {
                "telegram": {"ok": ok_tg, "error": err_tg},
                "email": {"ok": ok_em, "error": err_em},
            }
        if bool(args.json):
            print(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(f"Reports scanned: {out.get('reports')}")
            print(f"Decision queue: {out.get('decisions_dir')}")
            if out.get("drafts_written"):
                print("Drafts written:")
                for p in out["drafts_written"]:
                    print(f"  - {p}")
            if out.get("action_links"):
                print("Action links (pending):")
                for did, links in list(out["action_links"].items())[:10]:
                    print(f"  - {did}:")
                    for k, v in links.items():
                        print(f"      {k}: {v}")
        return

    if args.cmd == "backlog-exec-runner":
        from pathlib import Path

        from .maintenance.backlog_exec_runner import BacklogExecRunner, BacklogExecRunnerConfig

        data_dir = Path(str(args.data_dir or "")).expanduser().resolve() if args.data_dir else None
        if data_dir is None:
            data_dir = _default_data_dir()

        # The folder flows as an ARGUMENT (mission II): `--repo-root` > THE
        # backlog-folder resolution (saved setting > legacy env > the
        # gateway's own folder). Nothing is written to os.environ.
        repo_root = _backlog_exec_repo_root(args, data_dir)

        cfg = BacklogExecRunnerConfig.from_env()
        cfg = BacklogExecRunnerConfig(
            enabled=True,
            poll_interval_s=cfg.poll_interval_s,
            workers=getattr(cfg, "workers", 1),
            executor=cfg.executor,
            notify=cfg.notify,
            codex_bin=cfg.codex_bin,
            codex_model=cfg.codex_model,
            codex_reasoning_effort=getattr(cfg, "codex_reasoning_effort", ""),
            codex_sandbox=cfg.codex_sandbox,
            codex_approvals=cfg.codex_approvals,
            exec_mode_default=getattr(cfg, "exec_mode_default", "uat"),
        )

        stop = threading.Event()

        def _handle(_signum, _frame) -> None:  # pragma: no cover
            stop.set()

        try:
            signal.signal(signal.SIGINT, _handle)
            signal.signal(signal.SIGTERM, _handle)
        except Exception:
            pass

        runner = BacklogExecRunner(gateway_data_dir=data_dir, cfg=cfg, repo_root=repo_root)
        runner.start()
        try:
            while not stop.is_set():
                stop.wait(0.5)
        finally:
            runner.stop()
        return

    if args.cmd == "triage-apply":
        from pathlib import Path

        from .maintenance.triage import apply_decision_action

        data_dir = Path(str(args.data_dir or "")).expanduser().resolve() if args.data_dir else None
        if data_dir is None:
            data_dir = _default_data_dir()
        repo_root = Path(str(args.repo_root)).expanduser().resolve() if args.repo_root else None

        decision, err = apply_decision_action(
            gateway_data_dir=data_dir,
            decision_id=str(args.decision_id),
            action=str(args.action),
            repo_root=repo_root,
        )
        if err:
            raise SystemExit(err)
        if decision is None:
            raise SystemExit("No decision updated")
        print(f"Updated decision {decision.decision_id}: status={decision.status} draft={decision.draft_relpath or '(none)'}")
        return

    raise SystemExit(2)

if __name__ == "__main__":
    main()
