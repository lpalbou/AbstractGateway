"""The Automations API error envelope (automations contract C9 / F).

Every non-2xx response on `/api/gateway/automations…` and
`/api/gateway/trigger-sources` has ONE shape:

    {"detail": {"reason_code": "...", "message": "...", "field"?: "...", "command_id"?: "..."}}

Routes raise `AutomationError` for domain failures (the envelope is built
there, with the contract's reason codes). Everything else that can answer on
those paths is normalized by `AutomationErrorEnvelopeMiddleware`, a pure-ASGI
middleware mounted OUTSIDE the auth layer:

- the auth layer's early rejections (401 `unauthorized`, 403 `forbidden`),
  which answer before any route or FastAPI exception handler runs;
- request-validation failures and malformed JSON (FastAPI's 422 list body)
  -> 422 `invalid_request` with the first offending `field`;
- any other plain `{"detail": "text"}` error -> the envelope, reason_code
  from the status.

The rewrite is scoped by path prefix: no other route changes its error shape.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)

AUTOMATION_PATH_PREFIXES: tuple[str, ...] = ("/api/gateway/automations", "/api/gateway/trigger-sources")

#: Contract reason codes by HTTP status.
DOMAIN_REASON_CODES: Dict[int, tuple[str, ...]] = {
    401: ("unauthorized",),
    403: ("forbidden",),
    404: ("automation_not_found", "occurrence_not_found"),
    409: ("revision_conflict", "automation_busy", "invalid_state", "identity_conflict", "cursor_expired"),
    422: ("invalid_request", "invalid_definition", "unsupported_feature", "unknown_trigger_source"),
}

# reason_code for a plain-text error on a scoped path, by status. 401/403/422
# are contract codes; the others (never produced by the automation routes
# themselves) keep the envelope shape for the rare transport-level answers.
_STATUS_REASON: Dict[int, str] = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "invalid_request",
    409: "invalid_state",
    413: "invalid_request",
    415: "invalid_request",
    422: "invalid_request",
    429: "rate_limited",
    503: "unavailable",
}


def is_automation_path(path: str) -> bool:
    p = str(path or "")
    return any(p == prefix or p.startswith(prefix + "/") for prefix in AUTOMATION_PATH_PREFIXES)


def error_detail(reason_code: str, message: str, *, field: Optional[str] = None, command_id: Optional[str] = None) -> Dict[str, Any]:
    detail: Dict[str, Any] = {"reason_code": str(reason_code), "message": str(message)}
    if field:
        detail["field"] = str(field)
    if command_id:
        detail["command_id"] = str(command_id)
    return detail


class AutomationError(HTTPException):
    """A domain failure on an Automations route, already in envelope form."""

    def __init__(
        self,
        status_code: int,
        reason_code: str,
        message: str,
        *,
        field: Optional[str] = None,
        command_id: Optional[str] = None,
    ) -> None:
        super().__init__(status_code=int(status_code), detail=error_detail(reason_code, message, field=field, command_id=command_id))
        self.reason_code = str(reason_code)


def _is_envelope(detail: Any) -> bool:
    return isinstance(detail, dict) and isinstance(detail.get("reason_code"), str) and isinstance(detail.get("message"), str)


def _validation_field(loc: Any) -> Optional[str]:
    parts = [str(p) for p in (loc or []) if not isinstance(p, bool)]
    if parts and parts[0] in {"body", "query", "path", "header"}:
        parts = parts[1:]
    return ".".join(parts) or None


def normalize_error_body(status: int, body: Any) -> Dict[str, Any]:
    """The envelope for one error response body (already-enveloped bodies pass through)."""
    detail = body.get("detail") if isinstance(body, dict) else body
    if _is_envelope(detail):
        return {"detail": detail}
    if isinstance(detail, list):
        # FastAPI request validation / malformed JSON (`json_invalid`).
        first = next((d for d in detail if isinstance(d, dict)), {})
        if first.get("type") == "json_invalid":
            return {"detail": error_detail("invalid_request", "The request body is not valid JSON.")}
        message = str(first.get("msg") or "Invalid request.")
        field = _validation_field(first.get("loc"))
        return {"detail": error_detail("invalid_request", f"{field}: {message}" if field else message, field=field)}
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("detail") or json.dumps(detail, ensure_ascii=False)
    elif detail is None:
        message = f"HTTP {status}"
    else:
        message = str(detail)
    if status >= 500:
        reason = "unavailable" if status == 503 else "internal_error"
    else:
        reason = _STATUS_REASON.get(int(status), "invalid_request")
    return {"detail": error_detail(reason, str(message))}


class AutomationErrorEnvelopeMiddleware:
    """Pure-ASGI: rewrite every non-2xx JSON/text body on the automation paths."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001 - ASGI signature
        if scope.get("type") != "http" or not is_automation_path(str(scope.get("path") or "")):
            return await self._app(scope, receive, send)

        start: Optional[Dict[str, Any]] = None
        chunks: List[bytes] = []
        passthrough = False
        started = False

        async def _send(message: Dict[str, Any]) -> None:
            nonlocal start, passthrough, started
            if message.get("type") == "http.response.start":
                status = int(message.get("status") or 0)
                if status < 400:
                    passthrough = True
                    started = True
                    await send(message)
                    return
                start = message
                return
            if message.get("type") != "http.response.body" or passthrough:
                await send(message)
                return
            chunks.append(bytes(message.get("body") or b""))
            if message.get("more_body"):
                return
            await _flush()

        async def _flush() -> None:
            nonlocal started
            assert start is not None
            status = int(start.get("status") or 500)
            raw = b"".join(chunks)
            try:
                body: Any = json.loads(raw.decode("utf-8")) if raw else None
            except Exception:
                body = {"detail": raw.decode("utf-8", errors="replace").strip() or None}
            out = json.dumps(normalize_error_body(status, body), ensure_ascii=False).encode("utf-8")
            headers = [
                (k, v)
                for k, v in list(start.get("headers") or [])
                if bytes(k).lower() not in {b"content-length", b"content-type", b"content-encoding"}
            ]
            headers.append((b"content-type", b"application/json; charset=utf-8"))
            headers.append((b"content-length", str(len(out)).encode("ascii")))
            started = True
            await send({"type": "http.response.start", "status": status, "headers": headers})
            await send({"type": "http.response.body", "body": out})

        try:
            await self._app(scope, receive, _send)
        except Exception as exc:
            if started:
                raise
            logger.exception("Unhandled exception on automation path %s", scope.get("path"))
            out = json.dumps(
                {"detail": error_detail("internal_error", f"Internal error: {type(exc).__name__}: {exc}")},
                ensure_ascii=False,
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(out)).encode("ascii"))],
                }
            )
            await send({"type": "http.response.body", "body": out})


__all__ = [
    "AUTOMATION_PATH_PREFIXES",
    "AutomationError",
    "AutomationErrorEnvelopeMiddleware",
    "DOMAIN_REASON_CODES",
    "error_detail",
    "is_automation_path",
    "normalize_error_body",
]
