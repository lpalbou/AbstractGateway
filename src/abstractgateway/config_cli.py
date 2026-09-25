from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .config import GatewayHostConfig
from .core_config import (
    clear_gateway_capability_default,
    core_server_token,
    gateway_capability_defaults_payload,
    save_gateway_capability_default,
)
from .memory_store import resolve_memory_store_config


def _package_status(module_name: str, dist_name: Optional[str] = None) -> Dict[str, Any]:
    try:
        if importlib.util.find_spec(module_name) is None:
            raise ModuleNotFoundError(f"No module named '{module_name}'")
        version = None
        for candidate in (dist_name, module_name):
            if not candidate:
                continue
            try:
                version = importlib.metadata.version(candidate)
                break
            except Exception:
                continue
        return {"installed": True, "version": version}
    except Exception as e:
        return {"installed": False, "error": str(e)}


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


STATUS_SCHEMA = "gateway_config_status_v1"


def _install_status(data_dir: Path) -> Dict[str, Any]:
    """The first-run / doctor block of `status --json` (schema
    `gateway_config_status_v1`; `abstractframework doctor` consumes these keys,
    so they are ADDITIVE-ONLY: never rename or retype one).

    - `data_dir_source`: env | legacy_cwd_runtime | os_default (+ `data_dir_reason`)
    - `auth_mode`: users | token | users+token | open | loopback_auto (+ `auth`)
    - `service`: the per-user login service (file presence + record; no subprocess)
    - `claim_pending`: an unexpired, unredeemed first-run link exists (+ `claims`)
    - `first_run`: whether the console's first-run wizard was completed
    - `serve`: the running gateway's serve record for this data dir, or null
    """
    from .first_run import auth_mode_summary, first_run_state, pending_claims, read_serve_record
    from .host_paths import resolve_data_dir
    from .os_service import service_status

    res = resolve_data_dir()
    auth = auth_mode_summary()
    claims = pending_claims(data_dir)
    serve = read_serve_record(data_dir)
    return {
        "schema": STATUS_SCHEMA,
        "data_dir": str(data_dir),
        "data_dir_source": res.source,
        "data_dir_reason": res.reason,
        "auth_mode": auth["mode"],
        "auth": auth,
        "service": service_status(data_dir=data_dir, probe=False),
        "claim_pending": bool(claims.get("pending")),
        "claims": claims,
        "first_run": first_run_state(data_dir),
        "serve": serve,
    }


def _status_payload() -> Dict[str, Any]:
    cfg = GatewayHostConfig.from_env()
    memory_cfg = resolve_memory_store_config(base_dir=cfg.data_dir)
    gateway_token = os.getenv("ABSTRACTGATEWAY_AUTH_TOKEN") or os.getenv("ABSTRACTGATEWAY_AUTH_TOKENS")
    core_url = os.getenv("ABSTRACTCORE_SERVER_BASE_URL")
    core_token = (
        os.getenv("ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_AUTH_TOKEN")
        or os.getenv("ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_API_KEY")
        or core_server_token()
    )

    return {
        **_install_status(cfg.data_dir),
        "gateway": {
            "data_dir": str(cfg.data_dir),
            "flows_dir": str(cfg.flows_dir),
            "workflow_source": os.getenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle"),
            "store_backend": cfg.store_backend,
            "db_path": str(cfg.db_path) if cfg.db_path else None,
            "runner_enabled": bool(cfg.runner_enabled),
            "auth_configured": bool(str(gateway_token or "").strip()),
            "allowed_origins": os.getenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS") or None,
            "tool_mode": os.getenv("ABSTRACTGATEWAY_TOOL_MODE", "approval"),
            "max_attachment_bytes": os.getenv("ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES") or None,
        },
        "runtime": {"prompt_cache": os.getenv("ABSTRACTGATEWAY_PROMPT_CACHE") or None},
        "memory": memory_cfg.public_dict(),
        "capability_defaults": gateway_capability_defaults_payload(),
        "core_server": {
            "base_url": core_url or None,
            "auth_configured": bool(str(core_token or "").strip()),
            "note": "Gateway auth is separate from Core server auth and provider API keys.",
        },
        "packages": {
            "abstractruntime": _package_status("abstractruntime", "AbstractRuntime"),
            "abstractcore": _package_status("abstractcore", "abstractcore"),
            "abstractmemory": _package_status("abstractmemory", "AbstractMemory"),
            "abstractvoice": _package_status("abstractvoice", "abstractvoice"),
            "abstractvision": _package_status("abstractvision", "abstractvision"),
            "fastapi": _package_status("fastapi", "fastapi"),
            "uvicorn": _package_status("uvicorn", "uvicorn"),
        },
        "next_steps": [
            "Use Gateway Console /console for provider connections, API keys, endpoint base URLs, users, and defaults.",
            "Use ABSTRACTVOICE_* and ABSTRACTVISION_* only for capability-package backend settings.",
            "Browser apps sign in with a Gateway account token. With user accounts on, every account "
            "signs in to its own runtime; with user accounts off, only admin accounts can sign in.",
        ],
    }


def _quote_env(value: Any) -> str:
    text = str(value if value is not None else "")
    if text == "" or any(ch.isspace() or ch in {'"', "'", "#", "$", "\\"} for ch in text):
        return json.dumps(text)
    return text


def _parse_capability_options(items: list[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for item in items or []:
        raw = str(item or "").strip()
        if not raw:
            continue
        if "=" not in raw:
            raise SystemExit(f"Invalid --option value {raw!r}; expected KEY=VALUE")
        key, value_raw = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"Invalid --option value {raw!r}; key is empty")
        value_text = value_raw.strip()
        try:
            value = json.loads(value_text)
        except Exception:
            value = value_text
        out[key] = value
    return out


def _write_env_file(path: Path, values: Dict[str, Any], *, force: bool) -> None:
    path = Path(path).expanduser()
    if path.exists() and not force:
        raise SystemExit(f"Refusing to overwrite existing env file: {path} (pass --force to replace it)")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Generated by abstractgateway-config init.",
        "# Browser apps should use Gateway user auth and Gateway Console provider connections.",
        "# Legacy ABSTRACTGATEWAY_AUTH_TOKEN is a server/operator bearer token, not a browser user token.",
    ]
    for key, value in values.items():
        if value is None:
            continue
        lines.append(f"{key}={_quote_env(value)}")
    data = "\n".join(lines).rstrip() + "\n"

    if path.exists():
        path.write_text(data, encoding="utf-8")
    else:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fd = -1
                fh.write(data)
        finally:
            if fd != -1:
                os.close(fd)
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def _chmod_private(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def _read_token_file(path: Path) -> str:
    try:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    return ""


def _write_token_file(path: Path, token: str, *, force: bool = False) -> None:
    token = str(token or "").strip()
    if not token:
        return
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        existing = _read_token_file(path)
        if existing:
            return
    if path.exists():
        path.write_text(token + "\n", encoding="utf-8")
    else:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fd = -1
                fh.write(token + "\n")
        finally:
            if fd != -1:
                os.close(fd)
    _chmod_private(path)


def ensure_bootstrap_admin_user(
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    runtime_id: str | None = None,
    email: str | None = None,
    token: str | None = None,
    token_file: str | Path | None = None,
    no_token_file: bool = False,
    rotate_token: bool = False,
) -> Dict[str, Any]:
    """Ensure the file-backed admin user used by browser sessions exists.

    The returned payload may include the raw token when it is newly generated,
    explicitly provided, rotated, or recoverable from the bootstrap token file.
    Callers decide whether to print it.
    """
    from .users import GatewayUserRegistry, gateway_data_dir_from_env, generate_gateway_token

    tenant_id = str(tenant_id or "default").strip() or "default"
    user_id = str(user_id or "admin").strip() or "admin"
    runtime_id = str(runtime_id or "").strip() or ("default" if tenant_id == "default" and user_id == "admin" else user_id)
    email = str(email or "").strip() or None
    resolved_token_file = (
        Path(token_file).expanduser()
        if token_file
        else (gateway_data_dir_from_env() / "auth" / "bootstrap-admin-token")
    )
    explicit_token = str(token or os.getenv("ABSTRACTGATEWAY_BOOTSTRAP_ADMIN_TOKEN") or "").strip()
    existing_file_token = "" if bool(no_token_file) else _read_token_file(resolved_token_file)
    registry = GatewayUserRegistry()

    record = registry.get_user(user_id, tenant_id=tenant_id)
    issued_token: Optional[str] = None
    changed = False
    token_source = "none"

    if record is None:
        token = explicit_token or existing_file_token or generate_gateway_token()
        record, issued_token = registry.create_user(
            user_id=user_id,
            tenant_id=tenant_id,
            roles=["admin", "user"],
            runtime_id=runtime_id,
            email=email,
            token=token,
        )
        changed = True
        token_source = "env" if explicit_token else ("token-file" if existing_file_token else "generated")
        if not bool(no_token_file):
            _write_token_file(resolved_token_file, issued_token, force=True)
    else:
        desired_roles = tuple(dict.fromkeys([*record.roles, "admin", "user"]))
        needs_update = (
            not record.enabled
            or desired_roles != tuple(record.roles)
            or (email is not None and email != record.email)
            or bool(rotate_token)
        )
        update_token: Optional[str] = None
        if bool(rotate_token):
            update_token = explicit_token or generate_gateway_token()
            token_source = "env" if explicit_token else "generated"
        if needs_update:
            record, issued_token = registry.update_user(
                user_id=user_id,
                tenant_id=tenant_id,
                roles=list(desired_roles),
                enabled=True,
                email=email if email is not None else record.email,
                token=update_token,
            )
            changed = True
            if issued_token and not bool(no_token_file):
                _write_token_file(resolved_token_file, issued_token, force=True)
        if issued_token is None and existing_file_token:
            principal = registry.authenticate(existing_file_token)
            if principal is not None and principal.user_id == user_id and principal.tenant_id == tenant_id:
                issued_token = existing_file_token
                token_source = "token-file"

    payload = {
        "ok": True,
        "changed": bool(changed),
        "user": record.public_dict(),
        "token_file": None if bool(no_token_file) else str(resolved_token_file),
        "token_available": bool(issued_token),
        "token_source": token_source,
    }
    if issued_token:
        payload["token"] = issued_token
    return payload


def _cmd_bootstrap_admin(args: argparse.Namespace) -> None:
    payload = ensure_bootstrap_admin_user(
        tenant_id=args.tenant_id,
        user_id=args.user_id,
        runtime_id=args.runtime_id,
        email=args.email,
        token=args.token,
        token_file=args.token_file,
        no_token_file=bool(args.no_token_file),
        rotate_token=bool(args.rotate_token),
    )
    record = payload["user"]
    token = payload.get("token") if bool(args.print_token) else None
    if not bool(args.print_token):
        payload.pop("token", None)

    if bool(args.json):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    print("Gateway admin user ready.")
    print(f"- tenant: {record.get('tenant_id')}")
    print(f"- user: {record.get('user_id')}")
    print(f"- runtime: {record.get('runtime_id') or record.get('user_id')}")
    print(f"- roles: {', '.join(record.get('roles') or [])}")
    if not bool(args.no_token_file):
        print(f"- token_file: {payload.get('token_file')}")
    if bool(args.print_token):
        if token:
            print(f"- token: {token}")
        else:
            print("- token: unchanged; rotate or read the token file if available")
    elif bool(payload.get("token_available")):
        print("- token: available in token_file")


def _cmd_status(args: argparse.Namespace) -> None:
    payload = _status_payload()
    if bool(args.json):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    gw = payload["gateway"]
    mem = payload["memory"]
    core = payload["core_server"]
    print("AbstractGateway configuration status")
    print(f"- data_dir: {gw['data_dir']} ({payload['data_dir_source']}: {payload['data_dir_reason']})")
    auth = payload.get("auth") or {}
    auth_note = {
        "loopback_auto": "nothing configured: `serve` on 127.0.0.1 enables user auth automatically",
    }.get(str(payload.get("auth_mode")), f"source: {auth.get('source')}")
    print(f"- auth_mode: {payload.get('auth_mode')} ({auth_note})")
    svc = payload.get("service") or {}
    print(f"- service: {'installed' if svc.get('installed') else 'not installed'} ({svc.get('mechanism')}: {svc.get('unit_path')})")
    serve = payload.get("serve") or None
    if serve and serve.get("alive") is not False:
        print(f"- running: {serve.get('console_url')} (pid {serve.get('pid')})")
    fr = payload.get("first_run") or {}
    print(f"- first_run: {'completed' if fr.get('completed') else 'not completed'}; claim link pending: {bool(payload.get('claim_pending'))}")
    print(f"- flows_dir: {gw['flows_dir']}")
    print(f"- store_backend: {gw['store_backend']}")
    print(f"- runner_enabled: {gw['runner_enabled']}")
    print(f"- gateway_auth_configured: {gw['auth_configured']}")
    print(f"- memory_backend: {mem['backend']} ({mem.get('path') or 'process memory'})")
    print(f"- core_server: {core.get('base_url') or 'not configured'}")
    configured_defaults = [
        item
        for item in payload.get("capability_defaults", {}).get("routes", [])
        if isinstance(item, dict) and bool(item.get("configured"))
    ]
    if configured_defaults:
        print("- capability_defaults:")
        for item in configured_defaults:
            key = item.get("key") or f"{item.get('kind')}.{item.get('modality')}"
            provider = item.get("provider") or "-"
            model = item.get("model") or "-"
            source = item.get("source") or "-"
            print(f"  - {key}: {provider}/{model} ({source})")
    print("")
    print("Use Gateway Console /console for provider connections, API keys, endpoint base URLs, users, and defaults.")


def _cmd_claim_url(args: argparse.Namespace) -> None:
    from .firstrun_cli import run_claim

    raise SystemExit(run_claim(args))


def _cmd_init(args: argparse.Namespace) -> None:
    data_dir = Path(args.data_dir).expanduser()
    flows_dir = Path(args.flows_dir).expanduser()
    token = str(args.auth_token or "").strip() or secrets.token_urlsafe(32)
    store_backend = str(args.store_backend or "file").strip().lower()
    db_path = str(args.db_path or "").strip() or None
    if store_backend == "sqlite" and not db_path:
        db_path = str(data_dir / "gateway.sqlite3")

    values: Dict[str, Any] = {
        "ABSTRACTGATEWAY_AUTH_TOKEN": token,
        "ABSTRACTGATEWAY_ALLOWED_ORIGINS": args.allowed_origins,
        "ABSTRACTGATEWAY_DATA_DIR": str(data_dir),
        "ABSTRACTGATEWAY_FLOWS_DIR": str(flows_dir),
        "ABSTRACTGATEWAY_WORKFLOW_SOURCE": args.workflow_source,
        "ABSTRACTGATEWAY_STORE_BACKEND": store_backend,
        "ABSTRACTGATEWAY_DB_PATH": db_path,
        "ABSTRACTGATEWAY_RUNNER": "1" if bool(args.runner) else "0",
        "ABSTRACTGATEWAY_TOOL_MODE": args.tool_mode,
        "ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES": str(int(args.max_attachment_bytes)),
        "ABSTRACTGATEWAY_MEMORY_STORE_BACKEND": args.memory_backend,
        "ABSTRACTCORE_SERVER_BASE_URL": args.core_server_url,
        "ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_AUTH_TOKEN": args.core_server_auth_token,
    }

    out_path = Path(args.env_file).expanduser()
    _write_env_file(out_path, values, force=bool(args.force))
    print(f"Wrote {out_path}")
    print("Gateway auth token generated. Keep this file private.")
    print("Use abstractgateway-config set-default for framework capability routes.")
    print("Use Gateway Console /console for provider connections, API keys, endpoint base URLs, users, and defaults.")


def _cmd_defaults(args: argparse.Namespace) -> None:
    payload = gateway_capability_defaults_payload(base_dir=_defaults_scope_base_dir(args))
    if bool(args.json):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print("Execution-host capability defaults")
    print(f"- config_file: {payload.get('config_file')}")
    for item in payload.get("routes", []):
        if not isinstance(item, dict):
            continue
        key = item.get("key") or f"{item.get('kind')}.{item.get('modality')}"
        provider = item.get("provider") or "-"
        model = item.get("model") or "-"
        source = item.get("source") or "-"
        print(f"- {key}: {provider}/{model} ({source})")


def _cmd_set_default(args: argparse.Namespace) -> None:
    options = _parse_capability_options(args.option or [])
    save_gateway_capability_default(
        args.route,
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        options=options,
        base_dir=_defaults_scope_base_dir(args),
    )
    print(f"Set execution-host capability default: {args.route}")


def _cmd_clear_default(args: argparse.Namespace) -> None:
    clear_gateway_capability_default(args.route, base_dir=_defaults_scope_base_dir(args))
    print(f"Cleared execution-host capability default: {args.route}")


def _defaults_scope_base_dir(args: argparse.Namespace) -> Optional[Path]:
    scope = str(getattr(args, "scope", "gateway") or "gateway").strip().lower()
    if scope in {"local", "core"}:
        return None

    from .security.principal import safe_principal_component
    from .users import GatewayUserRegistry, gateway_data_dir_from_env, gateway_user_auth_enabled

    if scope == "gateway":
        if gateway_user_auth_enabled():
            return gateway_data_dir_from_env()
        return None
    if scope != "user":
        raise SystemExit(f"Unsupported defaults scope: {scope}")

    tenant_id = safe_principal_component(getattr(args, "tenant", None), default="default")
    user_id = safe_principal_component(getattr(args, "user", None), default="")
    if not user_id:
        raise SystemExit("--user is required when --scope user")
    record = GatewayUserRegistry().get_user(user_id, tenant_id=tenant_id)
    if record is None:
        raise SystemExit(f"Gateway user not found: {tenant_id}/{user_id}")
    runtime_id = safe_principal_component(getattr(args, "runtime", None) or record.runtime_id or record.user_id, default=record.user_id)
    if tenant_id == "default" and user_id == "admin" and runtime_id in {"admin", "default"}:
        return gateway_data_dir_from_env()
    return gateway_data_dir_from_env() / "users" / tenant_id / runtime_id / "runtime"


def _add_default_scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scope",
        choices=["gateway", "user", "local", "core"],
        default="gateway",
        help="Defaults target: gateway baseline, one Gateway user runtime, or local AbstractCore config",
    )
    parser.add_argument("--tenant", default="default", help="Tenant id for --scope user")
    parser.add_argument("--user", default=None, help="Gateway user id for --scope user")
    parser.add_argument("--runtime", default=None, help="Runtime id override for --scope user")



# ---- `abstractgateway config get|set|unset` (mission II) -------------------
# The runtime settings from a terminal: the same store and the same
# validation as the console form and Continuum's Settings (the one door,
# runtime_config.write_runtime_config). When a gateway is serving THIS data
# dir (its serve record is live) the change goes through its HTTP door, so
# it applies at once (the exec runner starts/stops) and lands in its audit
# log; otherwise the store is written directly and the next start reads it.

_SOURCE_WORDS = {
    "flag": "launch flag",
    "stored": "saved setting",
    "env": "environment (legacy)",
    "default": "default",
    "account": "account",
}


def _runtime_data_dir(args: argparse.Namespace) -> Path:
    if getattr(args, "data_dir", None):
        return Path(str(args.data_dir)).expanduser().resolve()
    from .host_paths import resolve_data_dir

    return resolve_data_dir().path


_AGENT_PREFIX = "agents.default_workflow."


def _setting_row(cfg: Dict[str, Any], key: str) -> Optional[Dict[str, Any]]:
    if key.startswith(_AGENT_PREFIX):
        row = ((cfg.get("agents") or {}).get("default_workflow") or {}).get(key[len(_AGENT_PREFIX):])
        return row if isinstance(row, dict) else None
    if key.startswith("apps."):
        row = (cfg.get("apps") or {}).get(key[len("apps."):])
        return row if isinstance(row, dict) else None
    row = cfg.get(key)
    return row if isinstance(row, dict) else None


def _format_value(value: Any) -> str:
    if value is True:
        return "on"
    if value is False:
        return "off"
    if value in (None, ""):
        return "(none)"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _print_setting(key: str, row: Dict[str, Any]) -> None:
    value = row.get("value") if "value" in row else row.get("configured")
    source = str(row.get("source") or "")
    print(f"{key} = {_format_value(value)}  [{_SOURCE_WORDS.get(source, source)}]")
    if row.get("label"):
        print(f"  {row['label']}: {row.get('help') or ''}".rstrip())
    if row.get("available") is False:
        print(f"  NOT AVAILABLE: {row.get('reason')}")
    if isinstance(row.get("resolved"), dict):
        r = row["resolved"]
        print(f"  runs {r.get('workflow_id')} ({r.get('name')}, {r.get('registry_scope')})")
    if isinstance(row.get("eligible"), list) and row.get("key", "").startswith(_AGENT_PREFIX):
        choices = [e.get("value") for e in row["eligible"] if isinstance(e, dict)]
        print(f"  choices on this gateway: {', '.join(choices) if choices else '(no workflow declares this interface)'}")
    if row.get("source") == "default" and row.get("default_path") and row.get("exists") is False:
        print("  (the gateway creates this folder with a starter overview and template on first use)")
    if row.get("env_shadowed"):
        print(f"  note: {row.get('env_name') or 'an environment value'} is also set; the {_SOURCE_WORDS.get(source, source)} wins")
    if source == "flag":
        print("  note: the running gateway was started with a launch flag for this; a saved value applies once it restarts without it")


def _live_gateway(data_dir: Path) -> Optional[Dict[str, str]]:
    """{url, token} of a gateway serving THIS data dir on this machine, or
    None. Only a live serve record counts (never a guessed default port: a
    CLI must not write into some other gateway)."""
    try:
        from .first_run import read_serve_record

        rec = read_serve_record(data_dir)
    except Exception:
        return None
    if not rec or rec.get("alive") is not True or not rec.get("url"):
        return None
    url = str(rec["url"]).rstrip("/")
    host = url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].strip("[]")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return None
    try:
        token = (Path(data_dir) / "auth" / "bootstrap-admin-token").read_text(encoding="utf-8").strip()
    except Exception:
        token = ""
    if not token:
        return None
    return {"url": url, "token": token}


def _post_runtime_config(live: Dict[str, str], change: Dict[str, Any]) -> tuple:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        live["url"] + "/api/gateway/admin/runtime-config",
        data=json.dumps(change).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {live['token']}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - loopback URL from this data dir's serve record
            return int(resp.status), json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            body = {}
        return int(exc.code), body
    except Exception as exc:  # noqa: BLE001 - unreachable: fall back to the store
        return 0, {"detail": str(exc)}


def _apply_runtime_change(args: argparse.Namespace, change: Dict[str, Any]) -> int:
    import getpass

    from .runtime_config import RuntimeConfigError, RuntimeConfigStoreCorrupt, write_runtime_config

    data_dir = _runtime_data_dir(args)
    key = next(iter(change))
    live = _live_gateway(data_dir)
    out: Optional[Dict[str, Any]] = None
    how = ""
    if live is not None:
        status, body = _post_runtime_config(live, change)
        if status == 200:
            out, how = body, f"applied by the running gateway ({live['url']})"
        elif status in (400, 409):
            print(f"refused: {body.get('detail') or body}", file=sys.stderr)
            return 2
        else:
            how = f"the running gateway at {live['url']} did not take it ({status or 'unreachable'}: {body.get('detail') or ''}); "
    if out is None:
        try:
            login = getpass.getuser() or "operator"
        except Exception:
            login = "operator"
        try:
            out = write_runtime_config(data_dir, change, actor=f"cli/{login}")
        except (RuntimeConfigError, RuntimeConfigStoreCorrupt) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        how += "saved in " + str(data_dir / "config" / "runtime_config.json")
        if key in ("backlog_exec_runner", "executor") and live is None:
            how += " (a running gateway for this data dir starts or stops the exec runner at its next start)"
    if bool(getattr(args, "json", False)):
        print(json.dumps({"applied": out.get("applied"), "setting": _setting_row(out, key)}, indent=2, default=str))
        return 0
    row = _setting_row(out, key)
    if row is not None:
        _print_setting(key, row)
    print(how, file=sys.stderr)
    return 0


def _cmd_runtime_get(args: argparse.Namespace) -> None:
    from .runtime_config import BACKLOG_SETTINGS, read_runtime_config

    wants_agents = bool(args.key and str(args.key).startswith(_AGENT_PREFIX))
    cfg = read_runtime_config(_runtime_data_dir(args), include_agents=wants_agents)
    keys = [args.key] if args.key else [row["key"] for row in BACKLOG_SETTINGS]
    rows: Dict[str, Any] = {}
    for key in keys:
        row = _setting_row(cfg, key)
        if row is None:
            known = sorted(k for k, v in cfg.items() if isinstance(v, dict) and k != "apps")
            if key.startswith(_AGENT_PREFIX):
                ifaces = sorted(((cfg.get("agents") or {}).get("default_workflow") or {}).keys())
                raise SystemExit(f"unknown agent interface in {key!r}; this gateway knows {ifaces}")
            raise SystemExit(f"unknown setting {key!r}; one of {known}, apps.<name> or {_AGENT_PREFIX}<interface>")
        rows[key] = row
    if bool(args.json):
        print(json.dumps(rows if not args.key else rows[args.key], indent=2, default=str))
        return
    for key, row in rows.items():
        _print_setting(key, row)


def _cmd_runtime_set(args: argparse.Namespace) -> None:
    raise SystemExit(_apply_runtime_change(args, {args.key: args.value}))


def _cmd_runtime_unset(args: argparse.Namespace) -> None:
    raise SystemExit(_apply_runtime_change(args, {args.key: None}))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="abstractgateway-config",
        description="Configure and inspect the AbstractGateway deployment entry point.",
    )
    sub = parser.add_subparsers(dest="cmd")

    status = sub.add_parser("status", help="Show Gateway/Core/memory readiness without starting the server")
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    status.set_defaults(func=_cmd_status)

    claim = sub.add_parser(
        "claim-url",
        help="Mint a one-time console sign-in link (10 min, single use, this machine only) and print it",
    )
    from .firstrun_cli import add_claim_arguments

    add_claim_arguments(claim)
    claim.set_defaults(func=_cmd_claim_url)

    init = sub.add_parser("init", help="Create a local Gateway .env file")
    init.add_argument("--env-file", default=".env", help="Output env file path (default: .env)")
    init.add_argument("--force", action="store_true", help="Overwrite an existing env file")
    init.add_argument("--auth-token", default=None, help="Gateway bearer token; generated when omitted")
    init.add_argument("--allowed-origins", default="http://localhost:*,http://127.0.0.1:*")
    init.add_argument("--data-dir", default="./runtime/gateway")
    init.add_argument("--flows-dir", default="./flows")
    init.add_argument("--workflow-source", choices=["bundle", "visualflow"], default="bundle")
    init.add_argument("--store-backend", choices=["file", "sqlite"], default="file")
    init.add_argument("--db-path", default=None)
    init.add_argument("--runner", action=argparse.BooleanOptionalAction, default=True)
    init.add_argument("--tool-mode", default="approval")
    init.add_argument("--max-attachment-bytes", type=int, default=25 * 1024 * 1024)
    init.add_argument("--memory-backend", choices=["lancedb", "sqlite", "memory"], default="lancedb")
    init.add_argument("--core-server-url", default=None)
    init.add_argument("--core-server-auth-token", default=None)
    init.set_defaults(func=_cmd_init)

    bootstrap = sub.add_parser("bootstrap-admin", help="Ensure a file-backed admin Gateway user exists")
    bootstrap.add_argument("--tenant-id", default="default", help="Admin tenant (default: default)")
    bootstrap.add_argument("--user-id", default="admin", help="Admin user id (default: admin)")
    bootstrap.add_argument("--runtime-id", default=None, help="Admin runtime id (default: default for default/admin)")
    bootstrap.add_argument("--email", default=None, help="Optional admin email metadata")
    bootstrap.add_argument("--token", default=None, help="Admin user token; generated when omitted")
    bootstrap.add_argument(
        "--token-file",
        default=None,
        help="Private file for the bootstrap token (default: <ABSTRACTGATEWAY_DATA_DIR>/auth/bootstrap-admin-token)",
    )
    bootstrap.add_argument("--no-token-file", action="store_true", help="Do not write the generated token to disk")
    bootstrap.add_argument("--rotate-token", action="store_true", help="Rotate the token if the admin user already exists")
    bootstrap.add_argument("--print-token", action="store_true", help="Print the raw token to stdout")
    bootstrap.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    bootstrap.set_defaults(func=_cmd_bootstrap_admin)

    defaults = sub.add_parser("defaults", help="Show effective execution-host capability routing defaults")
    defaults.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    _add_default_scope_args(defaults)
    defaults.set_defaults(func=_cmd_defaults)

    set_default = sub.add_parser("set-default", help="Persist one execution-host Core/Runtime capability routing default")
    set_default.add_argument("route", help="Capability route, e.g. output.text, input.image, output.voice")
    set_default.add_argument("--provider", default=None, help="Provider/backend id")
    set_default.add_argument("--model", default=None, help="Model id")
    set_default.add_argument("--base-url", default=None, help="Optional provider base URL")
    set_default.add_argument("--option", action="append", default=[], metavar="KEY=VALUE", help="Optional JSON-capable parameter; repeatable")
    _add_default_scope_args(set_default)
    set_default.set_defaults(func=_cmd_set_default)

    clear_default = sub.add_parser("clear-default", help="Clear one execution-host capability routing default")
    clear_default.add_argument("route", help="Capability route, e.g. output.text")
    _add_default_scope_args(clear_default)
    clear_default.set_defaults(func=_cmd_clear_default)

    # Runtime settings (mission II): the console / Continuum settings door
    # from a terminal. Keys: triage_repo_root (the backlog folder),
    # backlog_exec_runner, process_manager, executor, ... and apps.<name>.
    rt_get = sub.add_parser(
        "get",
        help="Show a runtime setting with its value and where it comes from "
        "(default: the backlog folder, the exec runner and the process manager)",
    )
    rt_get.add_argument("key", nargs="?", default=None, help="e.g. triage_repo_root, backlog_exec_runner, process_manager")
    rt_get.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    rt_get.add_argument("--data-dir", default=None, help="Gateway data dir (default: same resolution as `serve`)")
    rt_get.set_defaults(func=_cmd_runtime_get)

    rt_set = sub.add_parser(
        "set",
        help="Save a runtime setting (validated like the console and Continuum; applied at once when this "
        "data dir's gateway is running)",
    )
    rt_set.add_argument("key", help="e.g. triage_repo_root, backlog_exec_runner, process_manager")
    rt_set.add_argument("value", help="the new value: a folder path, or on/off for a switch")
    rt_set.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    rt_set.add_argument("--data-dir", default=None, help="Gateway data dir (default: same resolution as `serve`)")
    rt_set.set_defaults(func=_cmd_runtime_set)

    rt_unset = sub.add_parser("unset", help="Remove a saved runtime setting (back to the launch flag, environment or default)")
    rt_unset.add_argument("key")
    rt_unset.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    rt_unset.add_argument("--data-dir", default=None, help="Gateway data dir (default: same resolution as `serve`)")
    rt_unset.set_defaults(func=_cmd_runtime_unset)

    return parser


def main(argv: list[str] | None = None) -> None:
    # Same data dir as `serve` would use from here (env, ./runtime in a
    # checkout, else the per-OS user data dir), exported for this process.
    from .host_paths import apply_data_dir_default

    apply_data_dir_default()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        args = parser.parse_args(["status", *(argv or [])])
    args.func(args)


if __name__ == "__main__":  # pragma: no cover
    main()
