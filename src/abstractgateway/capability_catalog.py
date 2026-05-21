from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, Optional


class CoreCatalogProxyError(Exception):
    def __init__(self, *, status_code: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status_code = int(status_code)
        self.detail = detail


def _env_first(*keys: str) -> Optional[str]:
    for key in keys:
        value = os.getenv(str(key))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def core_catalog_base_url() -> Optional[str]:
    return _env_first("ABSTRACTCORE_SERVER_BASE_URL")


def _core_catalog_token() -> Optional[str]:
    return _env_first(
        "ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_AUTH_TOKEN",
        "ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_API_KEY",
        "ABSTRACTCORE_SERVER_API_KEY",
    )


def _timeout_s(default: float = 3.0) -> float:
    raw = _env_first("ABSTRACTGATEWAY_CORE_CATALOG_TIMEOUT_S", "ABSTRACTCORE_CATALOG_TIMEOUT_S")
    if raw is None:
        return float(default)
    try:
        return max(0.1, min(30.0, float(raw)))
    except Exception:
        return float(default)


def _join_core_url(base_url: str, path: str, *, v1: bool = True) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise CoreCatalogProxyError(status_code=503, detail="AbstractCore server base URL is not configured")
    suffix = str(path or "").strip()
    if not suffix.startswith("/"):
        suffix = "/" + suffix
    if not v1:
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return base + suffix
    if base.endswith("/v1"):
        return base + suffix
    if suffix.startswith("/v1/") or suffix == "/v1":
        return base + suffix
    return base + "/v1" + suffix


def _join_core_v1_url(base_url: str, path: str) -> str:
    return _join_core_url(base_url, path, v1=True)


def _decode_error_body(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
        return parsed
    except Exception:
        return text[:2000]


def fetch_core_catalog_json(
    path: str,
    *,
    query: Optional[Mapping[str, Any]] = None,
    provider_api_key: Optional[str] = None,
    timeout_s: Optional[float] = None,
    v1: bool = True,
) -> Dict[str, Any]:
    """Fetch a catalog route from the configured AbstractCore server."""

    base_url = core_catalog_base_url()
    if not base_url:
        raise CoreCatalogProxyError(status_code=503, detail="AbstractCore server base URL is not configured")

    url = _join_core_url(base_url, path, v1=bool(v1))
    params: Dict[str, str] = {}
    for key, value in dict(query or {}).items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            params[str(key)] = text
    if params:
        url = url + "?" + urllib.parse.urlencode(params)

    headers = {"Accept": "application/json"}
    token = _core_catalog_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if provider_api_key:
        headers["X-AbstractCore-Provider-API-Key"] = str(provider_api_key)

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s) if timeout_s is not None else _timeout_s()) as resp:  # noqa: S310 - operator-configured URL
            raw = resp.read(2_000_001)
            if len(raw) > 2_000_000:
                raise CoreCatalogProxyError(status_code=502, detail="AbstractCore catalog response is too large")
            data = json.loads(raw.decode("utf-8"))
    except CoreCatalogProxyError:
        raise
    except urllib.error.HTTPError as e:
        body = e.read(200_000)
        raise CoreCatalogProxyError(status_code=int(e.code), detail=_decode_error_body(body)) from e
    except Exception as e:
        raise CoreCatalogProxyError(status_code=502, detail=f"Failed to query AbstractCore catalog route: {e}") from e

    if not isinstance(data, dict):
        raise CoreCatalogProxyError(status_code=502, detail="AbstractCore catalog response was not a JSON object")
    return data


def post_core_json(
    path: str,
    *,
    body: Optional[Mapping[str, Any]] = None,
    provider_api_key: Optional[str] = None,
    timeout_s: Optional[float] = None,
    v1: bool = True,
) -> Dict[str, Any]:
    """POST a JSON control-plane request to the configured AbstractCore server."""

    base_url = core_catalog_base_url()
    if not base_url:
        raise CoreCatalogProxyError(status_code=503, detail="AbstractCore server base URL is not configured")

    url = _join_core_url(base_url, path, v1=bool(v1))
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    token = _core_catalog_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if provider_api_key:
        headers["X-AbstractCore-Provider-API-Key"] = str(provider_api_key)

    payload = json.dumps(dict(body or {}), ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s) if timeout_s is not None else _timeout_s()) as resp:  # noqa: S310 - operator-configured URL
            raw = resp.read(2_000_001)
            if len(raw) > 2_000_000:
                raise CoreCatalogProxyError(status_code=502, detail="AbstractCore response is too large")
            data = json.loads(raw.decode("utf-8"))
    except CoreCatalogProxyError:
        raise
    except urllib.error.HTTPError as e:
        body_raw = e.read(200_000)
        raise CoreCatalogProxyError(status_code=int(e.code), detail=_decode_error_body(body_raw)) from e
    except Exception as e:
        raise CoreCatalogProxyError(status_code=502, detail=f"Failed to query AbstractCore control route: {e}") from e

    if not isinstance(data, dict):
        raise CoreCatalogProxyError(status_code=502, detail="AbstractCore response was not a JSON object")
    return data
