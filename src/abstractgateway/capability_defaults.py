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
            return _apply_principal_overlay(payload, base_dir=base_dir)
        except Exception as exc:
            payload = {
                "ok": False,
                "authority": "abstractcore.server",
                "writable": False,
                "routes": [],
                "errors": [str(exc)],
                "config_hint": "Gateway is configured to use a remote AbstractCore server; capability defaults must be read from that execution host.",
            }
            return _apply_principal_overlay(payload, base_dir=base_dir)

    return _apply_principal_overlay(_local_core_payload(), base_dir=base_dir)


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

    overlay_path = _principal_overlay_path(base_dir)
    if overlay_path is not None:
        _save_principal_overlay_route(
            overlay_path,
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

    overlay_path = _principal_overlay_path(base_dir)
    if overlay_path is not None:
        _clear_principal_overlay_route(overlay_path, kind, modality)
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


def _principal_overlay_path(base_dir: Optional[Path]) -> Optional[Path]:
    if base_dir is None:
        return None
    try:
        from .users import gateway_user_auth_enabled

        if not gateway_user_auth_enabled():
            return None
    except Exception:
        return None
    try:
        return Path(base_dir).expanduser().resolve() / "config" / "capability_defaults.json"
    except Exception:
        return Path(base_dir) / "config" / "capability_defaults.json"


def _gateway_overlay_path() -> Optional[Path]:
    try:
        from .users import gateway_data_dir_from_env, gateway_user_auth_enabled

        if not gateway_user_auth_enabled():
            return None
        return gateway_data_dir_from_env() / "config" / "capability_defaults.json"
    except Exception:
        return None


def _load_principal_overlay(path: Path) -> Dict[str, Any]:
    try:
        if not path.exists():
            return {"version": 1, "routes": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"version": 1, "routes": {}}
        routes = data.get("routes")
        if not isinstance(routes, dict):
            data["routes"] = {}
        data["version"] = 1
        return data
    except Exception:
        return {"version": 1, "routes": {}}


def _write_principal_overlay(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _route_key(kind: str, modality: str) -> str:
    try:
        from abstractcore.config.capability_defaults import capability_route_key

        return capability_route_key(kind, modality)
    except Exception:
        return f"{str(kind or '').strip().lower()}.{str(modality or '').strip().lower()}"


def _save_principal_overlay_route(
    path: Path,
    kind: str,
    modality: str,
    *,
    provider: Optional[str],
    model: Optional[str],
    base_url: Optional[str],
    options: Dict[str, Any],
) -> None:
    key = _route_key(kind, modality)
    data = _load_principal_overlay(path)
    routes = data.setdefault("routes", {})
    if not isinstance(routes, dict):
        routes = {}
        data["routes"] = routes
    row: Dict[str, Any] = {}
    if provider:
        row["provider"] = provider
    if model:
        row["model"] = model
    if base_url:
        row["base_url"] = base_url
    if options:
        row["options"] = dict(options)
    if row:
        routes[key] = row
    else:
        routes.pop(key, None)
    _write_principal_overlay(path, data)


def _clear_principal_overlay_route(path: Path, kind: str, modality: str) -> None:
    data = _load_principal_overlay(path)
    routes = data.setdefault("routes", {})
    if isinstance(routes, dict):
        routes.pop(_route_key(kind, modality), None)
    _write_principal_overlay(path, data)


def _same_path(a: Optional[Path], b: Optional[Path]) -> bool:
    if a is None or b is None:
        return False
    try:
        return a.expanduser().resolve() == b.expanduser().resolve()
    except Exception:
        return str(a) == str(b)


def _apply_overlay_routes(payload: Dict[str, Any], *, overlay_path: Path, source: str) -> tuple[Dict[str, Any], bool]:
    data = _load_principal_overlay(overlay_path)
    raw_routes = data.get("routes")
    if not isinstance(raw_routes, dict) or not raw_routes:
        return payload, False

    try:
        from abstractcore.config.capability_defaults import capability_defaults_from_dict, iter_capability_default_specs

        defaults = capability_defaults_from_dict(data)
        overlay_routes = {key: route.to_dict() for key, route in defaults.routes.items() if route.configured()}
        specs = {spec.key: spec.to_dict() for spec in iter_capability_default_specs()}
    except Exception:
        overlay_routes = {str(k): dict(v) for k, v in raw_routes.items() if isinstance(v, dict)}
        specs = {}

    existing_rows = payload.get("routes") if isinstance(payload.get("routes"), list) else []
    rows_by_key: Dict[str, Dict[str, Any]] = {}
    for row in existing_rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if key:
            rows_by_key[key] = dict(row)
    for key, route in overlay_routes.items():
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
    return payload, bool(overlay_routes)


def _apply_principal_overlay(payload: Dict[str, Any], *, base_dir: Optional[Path]) -> Dict[str, Any]:
    principal_path = _principal_overlay_path(base_dir)
    gateway_path = _gateway_overlay_path()
    if principal_path is None and gateway_path is None:
        return payload

    payload = dict(payload)
    gateway_applied = False
    principal_applied = False
    if gateway_path is not None:
        payload, gateway_applied = _apply_overlay_routes(
            payload,
            overlay_path=gateway_path,
            source="abstractgateway.gateway",
        )
        payload.setdefault("gateway_defaults", bool(gateway_applied))
        payload.setdefault("gateway_config_file", str(gateway_path))

    if principal_path is not None:
        payload.setdefault("principal_config_file", str(principal_path))
        if not _same_path(principal_path, gateway_path):
            payload, principal_applied = _apply_overlay_routes(
                payload,
                overlay_path=principal_path,
                source="abstractgateway.principal",
            )
        payload["principal_defaults"] = bool(principal_applied)

    payload["ok"] = bool(payload.get("ok", True))
    payload["writable"] = True
    if principal_applied:
        payload["authority"] = "abstractgateway.principal"
        payload["source"] = "abstractgateway.principal"
    elif gateway_applied or _same_path(principal_path, gateway_path):
        payload["authority"] = "abstractgateway.gateway"
        payload["source"] = "abstractgateway.gateway"
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
