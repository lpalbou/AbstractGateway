from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any, Dict, Optional

from .config import GatewayHostConfig
from .capability_defaults import (
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
            "Use abstractcore-config for provider API keys and provider base URLs.",
            "Use ABSTRACTVOICE_* and ABSTRACTVISION_* only for capability-package backend settings.",
            "Run abstractgateway serve after setting a strong ABSTRACTGATEWAY_AUTH_TOKEN.",
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
        "# Provider credentials and provider base URLs belong in abstractcore-config / AbstractCore env.",
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


def _cmd_bootstrap_admin(args: argparse.Namespace) -> None:
    from .users import GatewayUserRegistry, gateway_data_dir_from_env, generate_gateway_token

    tenant_id = str(args.tenant_id or "default").strip() or "default"
    user_id = str(args.user_id or "admin").strip() or "admin"
    runtime_id = str(args.runtime_id or "").strip() or ("default" if tenant_id == "default" and user_id == "admin" else user_id)
    email = str(args.email or "").strip() or None
    token_file = Path(args.token_file).expanduser() if args.token_file else (gateway_data_dir_from_env() / "auth" / "bootstrap-admin-token")
    explicit_token = str(args.token or os.getenv("ABSTRACTGATEWAY_BOOTSTRAP_ADMIN_TOKEN") or "").strip()
    existing_file_token = _read_token_file(token_file)
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
        if not bool(args.no_token_file):
            _write_token_file(token_file, issued_token, force=True)
    else:
        desired_roles = tuple(dict.fromkeys([*record.roles, "admin", "user"]))
        needs_update = (
            not record.enabled
            or desired_roles != tuple(record.roles)
            or (email is not None and email != record.email)
            or bool(args.rotate_token)
        )
        update_token: Optional[str] = None
        if bool(args.rotate_token):
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
            if issued_token and not bool(args.no_token_file):
                _write_token_file(token_file, issued_token, force=True)
        if issued_token is None and existing_file_token:
            principal = registry.authenticate(existing_file_token)
            if principal is not None and principal.user_id == user_id and principal.tenant_id == tenant_id:
                issued_token = existing_file_token
                token_source = "token-file"

    payload = {
        "ok": True,
        "changed": bool(changed),
        "user": record.public_dict(),
        "token_file": None if bool(args.no_token_file) else str(token_file),
        "token_available": bool(issued_token),
        "token_source": token_source,
    }
    if bool(args.print_token) and issued_token:
        payload["token"] = issued_token

    if bool(args.json):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    print("Gateway admin user ready.")
    print(f"- tenant: {record.tenant_id}")
    print(f"- user: {record.user_id}")
    print(f"- runtime: {record.runtime_id or record.user_id}")
    print(f"- roles: {', '.join(record.roles)}")
    if not bool(args.no_token_file):
        print(f"- token_file: {token_file}")
    if bool(args.print_token):
        if issued_token:
            print(f"- token: {issued_token}")
        else:
            print("- token: unchanged; rotate or read the token file if available")
    elif issued_token:
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
    print(f"- data_dir: {gw['data_dir']}")
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
    print("Use abstractcore-config for provider credentials and provider base URLs.")


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
    print("Run abstractcore-config separately for provider keys and provider base URLs.")


def _cmd_defaults(args: argparse.Namespace) -> None:
    payload = gateway_capability_defaults_payload()
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
    )
    print(f"Set execution-host capability default: {args.route}")


def _cmd_clear_default(args: argparse.Namespace) -> None:
    clear_gateway_capability_default(args.route)
    print(f"Cleared execution-host capability default: {args.route}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="abstractgateway-config",
        description="Configure and inspect the AbstractGateway deployment entry point.",
    )
    sub = parser.add_subparsers(dest="cmd")

    status = sub.add_parser("status", help="Show Gateway/Core/memory readiness without starting the server")
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    status.set_defaults(func=_cmd_status)

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
    defaults.set_defaults(func=_cmd_defaults)

    set_default = sub.add_parser("set-default", help="Persist one execution-host Core/Runtime capability routing default")
    set_default.add_argument("route", help="Capability route, e.g. output.text, input.image, output.voice")
    set_default.add_argument("--provider", default=None, help="Provider/backend id")
    set_default.add_argument("--model", default=None, help="Model id")
    set_default.add_argument("--base-url", default=None, help="Optional provider base URL")
    set_default.add_argument("--option", action="append", default=[], metavar="KEY=VALUE", help="Optional JSON-capable parameter; repeatable")
    set_default.set_defaults(func=_cmd_set_default)

    clear_default = sub.add_parser("clear-default", help="Clear one execution-host capability routing default")
    clear_default.add_argument("route", help="Capability route, e.g. output.text")
    clear_default.set_defaults(func=_cmd_clear_default)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        args = parser.parse_args(["status", *(argv or [])])
    args.func(args)


if __name__ == "__main__":  # pragma: no cover
    main()
