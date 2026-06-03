from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


def gateway_capability_defaults_payload(*, base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Return execution-host capability defaults through the Gateway control plane.

    Gateway is not the persistence owner for these defaults. In embedded/local
    deployments it edits the local AbstractCore config. In split deployments it
    proxies to the configured AbstractCore server.
    """

    core_base_url = core_server_base_url()
    if core_base_url:
        try:
            payload = _core_server_json("GET", "/config/capability-defaults")
            payload.setdefault("authority", "abstractcore.server")
            payload.setdefault("writable", True)
            payload.setdefault("source", "abstractcore.server")
            return _apply_scoped_core_defaults(payload, base_dir=base_dir)
        except Exception as exc:
            payload = {
                "ok": False,
                "authority": "abstractcore.server",
                "writable": False,
                "routes": [],
                "errors": [str(exc)],
                "config_hint": "Gateway is configured to use a remote AbstractCore server; capability defaults must be read from that execution host.",
            }
            return _apply_scoped_core_defaults(payload, base_dir=base_dir)

    gateway_config_path = _gateway_core_config_path()
    if gateway_config_path is not None:
        payload = _core_config_payload(
            gateway_config_path,
            authority="abstractcore.gateway_runtime",
            source="abstractcore.gateway_runtime",
        )
    else:
        payload = _local_core_payload()
    return _apply_scoped_core_defaults(payload, base_dir=base_dir)


def save_gateway_capability_default(
    kind: str,
    modality: Optional[str] = None,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    if modality is None:
        kind, modality = _split_route(kind)

    body = {
        "provider": _clean(provider),
        "model": _clean(model),
        "base_url": _clean(base_url),
        "options": dict(options or {}) if isinstance(options, dict) else {},
    }

    scoped_config_path = _writable_scoped_core_config_path(base_dir)
    if scoped_config_path is not None:
        _save_core_config_route(
            scoped_config_path,
            kind,
            modality,
            provider=body["provider"],
            model=body["model"],
            base_url=body["base_url"],
            options=body["options"],
        )
        return gateway_capability_defaults_payload(base_dir=base_dir)

    if core_server_base_url():
        payload = _core_server_json("PUT", f"/config/capability-defaults/{kind}/{modality}", body)
        payload.setdefault("authority", "abstractcore.server")
        payload.setdefault("writable", True)
        return payload

    from abstractcore.config.manager import ConfigurationManager

    manager = ConfigurationManager()
    if not manager.set_capability_default(
        kind,
        modality,
        provider=body["provider"],
        model=body["model"],
        base_url=body["base_url"],
        options=body["options"],
    ):
        raise ValueError(f"Failed to set capability default {kind}.{modality}")
    return _local_core_payload()


def clear_gateway_capability_default(kind: str, modality: Optional[str] = None, *, base_dir: Optional[Path] = None) -> Dict[str, Any]:
    if modality is None:
        kind, modality = _split_route(kind)

    scoped_config_path = _writable_scoped_core_config_path(base_dir)
    if scoped_config_path is not None:
        _clear_core_config_route(scoped_config_path, kind, modality)
        return gateway_capability_defaults_payload(base_dir=base_dir)

    if core_server_base_url():
        payload = _core_server_json("DELETE", f"/config/capability-defaults/{kind}/{modality}")
        payload.setdefault("authority", "abstractcore.server")
        payload.setdefault("writable", True)
        return payload

    from abstractcore.config.manager import ConfigurationManager

    manager = ConfigurationManager()
    if not manager.clear_capability_default(kind, modality):
        raise ValueError(f"Failed to clear capability default {kind}.{modality}")
    return _local_core_payload()


def _local_core_payload() -> Dict[str, Any]:
    from abstractcore.config.manager import ConfigurationManager

    manager = ConfigurationManager()
    return {
        "ok": True,
        "version": 1,
        "authority": "abstractcore.local",
        "writable": True,
        "source": "abstractcore.local",
        "config_file": str(manager.config_file),
        "routes": manager.list_capability_defaults(),
        "errors": [],
    }


def _core_config_payload(path: Path, *, authority: str, source: str) -> Dict[str, Any]:
    from abstractcore.config.manager import ConfigurationManager

    manager = ConfigurationManager(config_file=path, apply_env=False)
    routes = manager.list_capability_defaults()
    for row in routes:
        if isinstance(row, dict) and bool(row.get("configured")):
            row["source"] = source
    return {
        "ok": True,
        "version": 1,
        "authority": authority,
        "writable": True,
        "source": source,
        "config_file": str(manager.config_file),
        "routes": routes,
        "errors": [],
    }


def _writable_scoped_core_config_path(base_dir: Optional[Path]) -> Optional[Path]:
    try:
        from .users import gateway_data_dir_from_env, gateway_user_auth_enabled

        if not gateway_user_auth_enabled():
            return None
        if base_dir is not None:
            return _core_config_path_for_base_dir(base_dir)
        return gateway_data_dir_from_env() / "config" / "abstractcore.json"
    except Exception:
        return None


def _core_config_path_for_base_dir(base_dir: Path) -> Path:
    try:
        return Path(base_dir).expanduser().resolve() / "config" / "abstractcore.json"
    except Exception:
        return Path(base_dir) / "config" / "abstractcore.json"


def _gateway_core_config_path() -> Optional[Path]:
    try:
        from .users import gateway_data_dir_from_env, gateway_user_auth_enabled

        if not gateway_user_auth_enabled():
            return None
        return gateway_data_dir_from_env() / "config" / "abstractcore.json"
    except Exception:
        return None


def _save_core_config_route(
    path: Path,
    kind: str,
    modality: str,
    *,
    provider: Optional[str],
    model: Optional[str],
    base_url: Optional[str],
    options: Dict[str, Any],
) -> None:
    from abstractcore.config.manager import ConfigurationManager

    manager = ConfigurationManager(config_file=path, apply_env=False)
    if not manager.set_capability_default(
        kind,
        modality,
        provider=provider,
        model=model,
        base_url=base_url,
        options=options,
    ):
        raise ValueError(f"Failed to set capability default {kind}.{modality}")


def _clear_core_config_route(path: Path, kind: str, modality: str) -> None:
    from abstractcore.config.manager import ConfigurationManager

    manager = ConfigurationManager(config_file=path, apply_env=False)
    if not manager.clear_capability_default(kind, modality):
        raise ValueError(f"Failed to clear capability default {kind}.{modality}")


def _same_path(a: Optional[Path], b: Optional[Path]) -> bool:
    if a is None or b is None:
        return False
    try:
        return a.expanduser().resolve() == b.expanduser().resolve()
    except Exception:
        return str(a) == str(b)


def _load_configured_routes_from_core_config(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        from abstractcore.config.manager import ConfigurationManager

        manager = ConfigurationManager(config_file=path, apply_env=False)
        routes: Dict[str, Dict[str, Any]] = {}
        for row in manager.list_capability_defaults():
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").strip()
            if key and bool(row.get("configured")):
                routes[key] = dict(row)
        return routes
    except Exception:
        return {}


def _apply_core_config_routes(payload: Dict[str, Any], *, config_path: Path, source: str) -> tuple[Dict[str, Any], bool]:
    configured_routes = _load_configured_routes_from_core_config(config_path)
    if not configured_routes:
        return payload, False

    try:
        from abstractcore.config.capability_defaults import iter_capability_default_specs

        specs = {spec.key: spec.to_dict() for spec in iter_capability_default_specs()}
    except Exception:
        specs = {}

    existing_rows = payload.get("routes") if isinstance(payload.get("routes"), list) else []
    rows_by_key: Dict[str, Dict[str, Any]] = {}
    for row in existing_rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if key:
            rows_by_key[key] = dict(row)
    for key, route in configured_routes.items():
        row = dict(specs.get(key, {"key": key}))
        row.update(route)
        row["key"] = key
        if "." in key:
            row.setdefault("kind", key.split(".", 1)[0])
            row.setdefault("modality", key.split(".", 1)[1])
        row["configured"] = True
        row["source"] = source
        rows_by_key[key] = row

    payload = dict(payload)
    payload["routes"] = [rows_by_key[key] for key in sorted(rows_by_key)]
    return payload, bool(configured_routes)


def _apply_scoped_core_defaults(payload: Dict[str, Any], *, base_dir: Optional[Path]) -> Dict[str, Any]:
    principal_path = _core_config_path_for_base_dir(base_dir) if base_dir is not None and _gateway_core_config_path() is not None else None
    gateway_path = _gateway_core_config_path()
    if principal_path is None and gateway_path is None:
        return payload

    payload = dict(payload)
    gateway_applied = False
    principal_applied = False
    if gateway_path is not None:
        payload.setdefault("gateway_config_file", str(gateway_path))
        if not _same_path(Path(str(payload.get("config_file") or "")), gateway_path):
            payload, gateway_applied = _apply_core_config_routes(
                payload,
                config_path=gateway_path,
                source="abstractcore.gateway_runtime",
            )
        else:
            gateway_applied = any(bool(row.get("configured")) for row in payload.get("routes", []) if isinstance(row, dict))
        payload["gateway_defaults"] = bool(gateway_applied)

    if principal_path is not None:
        payload.setdefault("principal_config_file", str(principal_path))
        if not _same_path(principal_path, gateway_path):
            payload, principal_applied = _apply_core_config_routes(
                payload,
                config_path=principal_path,
                source="abstractcore.runtime",
            )
        payload["principal_defaults"] = bool(principal_applied)

    payload["ok"] = bool(payload.get("ok", True))
    payload["writable"] = True
    if principal_applied:
        payload["authority"] = "abstractcore.runtime"
        payload["source"] = "abstractcore.runtime"
    elif gateway_applied or _same_path(principal_path, gateway_path):
        payload["authority"] = "abstractcore.gateway_runtime"
        payload["source"] = "abstractcore.gateway_runtime"
    return payload


def _core_server_json(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = core_server_base_url()
    if not base:
        raise RuntimeError("ABSTRACTCORE_SERVER_BASE_URL is not configured")
    url = core_server_url(base, path)
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    token = core_server_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=8.0) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AbstractCore config route returned HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"AbstractCore config route unavailable: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("AbstractCore config route returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("AbstractCore config route returned a non-object payload")
    return payload


def core_server_base_url() -> str:
    raw = os.getenv("ABSTRACTCORE_SERVER_BASE_URL")
    return raw.strip().rstrip("/") if isinstance(raw, str) and raw.strip() else ""


def core_server_token() -> str:
    for name in (
        "ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_AUTH_TOKEN",
        "ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_API_KEY",
        "ABSTRACTCORE_AUTH_TOKEN",
        "ABSTRACTCORE_SERVER_API_KEY",
    ):
        raw = os.getenv(name)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def core_server_url(base: str, path: str) -> str:
    base = base.rstrip("/")
    clean_path = "/" + path.lstrip("/")
    if urllib.parse.urlsplit(base).path.rstrip("/").endswith("/v1"):
        return f"{base}{clean_path}"
    return f"{base}/v1{clean_path}"


def _core_server_base_url() -> str:
    return core_server_base_url()


def _core_server_token() -> str:
    return core_server_token()


def _core_server_url(base: str, path: str) -> str:
    return core_server_url(base, path)


def _split_route(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if "." in raw:
        left, right = raw.split(".", 1)
        return left, right
    if ":" in raw:
        left, right = raw.split(":", 1)
        return left, right
    raise ValueError("Capability route must be written as kind.modality, for example output.text.")


def _clean(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None
