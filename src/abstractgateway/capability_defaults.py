from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


def gateway_capability_defaults_payload() -> Dict[str, Any]:
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
            return payload
        except Exception as exc:
            return {
                "ok": False,
                "authority": "abstractcore.server",
                "writable": False,
                "routes": [],
                "errors": [str(exc)],
                "config_hint": "Gateway is configured to use a remote AbstractCore server; capability defaults must be read from that execution host.",
            }

    return _local_core_payload()


def save_gateway_capability_default(
    kind: str,
    modality: Optional[str] = None,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if modality is None:
        kind, modality = _split_route(kind)

    body = {
        "provider": _clean(provider),
        "model": _clean(model),
        "base_url": _clean(base_url),
        "options": dict(options or {}) if isinstance(options, dict) else {},
    }

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


def clear_gateway_capability_default(kind: str, modality: Optional[str] = None) -> Dict[str, Any]:
    if modality is None:
        kind, modality = _split_route(kind)

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
