"""Gateway security middleware (backlog 309).

Security goals (v0):
- Bearer token auth for gateway endpoints (mutating + read endpoints by default).
- Origin allowlist checks when Origin is present (DNS rebinding / browser-origin defense).
- Abuse resistance: request body limits, concurrency limits, auth failure lockouts.

Design constraints:
- Must not break durability semantics (command idempotency, replay-first ledger).
- Must stay dependency-light (stdlib + Starlette/FastAPI already in the server).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple


logger = logging.getLogger(__name__)


def _as_bool(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if not s:
        return default
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _split_csv(raw: Optional[str]) -> list[str]:
    if raw is None:
        return []
    out: list[str] = []
    for part in str(raw).split(","):
        p = part.strip()
        if p:
            out.append(p)
    return out


def _is_loopback_ip(host: str) -> bool:
    h = str(host or "").strip().lower()
    return h in {"127.0.0.1", "::1", "localhost", "testclient"}


def _env(name: str, fallback: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is not None and str(v).strip():
        return v
    if fallback:
        v2 = os.getenv(fallback)
        if v2 is not None and str(v2).strip():
            return v2
    return None


@dataclass(frozen=True)
class GatewayAuthPolicy:
    """Configuration for the Run Gateway security layer."""

    # Enable/disable middleware entirely (escape hatch).
    enabled: bool = True

    # Auth tokens (shared secret list; any token is accepted).
    tokens: Tuple[str, ...] = ()

    # Default: protect both reads and writes.
    protect_read_endpoints: bool = True
    protect_write_endpoints: bool = True

    # Dev-only convenience: allow unauthenticated reads *from loopback only*.
    dev_allow_unauthenticated_reads_on_loopback: bool = False

    # Origin allowlist. Only applied when Origin header is present.
    # Supports '*' suffix wildcard for prefix matches (e.g., 'http://localhost:*').
    allowed_origins: Tuple[str, ...] = ("http://localhost:*", "http://127.0.0.1:*")

    # Abuse resistance
    max_body_bytes: int = 256_000
    # Upload endpoints can legitimately exceed `max_body_bytes` (multipart). 0 means "auto"
    # (derived from the explicit endpoint caps).
    max_upload_body_bytes: int = 0
    # Endpoint caps (mirrors /attachments/upload and /bundles/upload defaults).
    max_attachment_bytes: int = 25 * 1024 * 1024
    max_bundle_bytes: int = 75 * 1024 * 1024
    max_concurrency: int = 64
    max_sse_connections: int = 32

    # Auth failure lockout
    lockout_after_failures: int = 5
    lockout_base_s: float = 1.0
    lockout_max_s: float = 60.0

    # Proxy trust (X-Forwarded-For)
    trust_proxy: bool = False


def load_gateway_auth_policy_from_env() -> GatewayAuthPolicy:
    """Load GatewayAuthPolicy from environment variables.

    Canonical env vars:
    - ABSTRACTGATEWAY_SECURITY=1|0
    - ABSTRACTGATEWAY_AUTH_TOKEN / ABSTRACTGATEWAY_AUTH_TOKENS (comma-separated)
    - ABSTRACTGATEWAY_PROTECT_READ=1|0
    - ABSTRACTGATEWAY_PROTECT_WRITE=1|0
    - ABSTRACTGATEWAY_DEV_READ_NO_AUTH=1|0 (loopback only)
    - ABSTRACTGATEWAY_ALLOWED_ORIGINS (comma-separated; supports '*' suffix wildcard)
    - ABSTRACTGATEWAY_MAX_BODY_BYTES
    - ABSTRACTGATEWAY_MAX_UPLOAD_BODY_BYTES
    - ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES
    - ABSTRACTGATEWAY_MAX_BUNDLE_BYTES
    - ABSTRACTGATEWAY_MAX_CONCURRENCY
    - ABSTRACTGATEWAY_MAX_SSE
    - ABSTRACTGATEWAY_LOCKOUT_AFTER
    - ABSTRACTGATEWAY_LOCKOUT_BASE_S
    - ABSTRACTGATEWAY_LOCKOUT_MAX_S
    - ABSTRACTGATEWAY_TRUST_PROXY=1|0

    Compatibility fallbacks (legacy):
    - ABSTRACTFLOW_GATEWAY_*
    """

    enabled = _as_bool(_env("ABSTRACTGATEWAY_SECURITY", "ABSTRACTFLOW_GATEWAY_SECURITY") or "1", True)

    tokens = []
    tokens.extend(_split_csv(_env("ABSTRACTGATEWAY_AUTH_TOKEN", "ABSTRACTFLOW_GATEWAY_AUTH_TOKEN")))
    tokens.extend(_split_csv(_env("ABSTRACTGATEWAY_AUTH_TOKENS", "ABSTRACTFLOW_GATEWAY_AUTH_TOKENS")))
    # Deduplicate while preserving order
    seen: set[str] = set()
    tokens2: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            tokens2.append(t)

    protect_read = _as_bool(_env("ABSTRACTGATEWAY_PROTECT_READ", "ABSTRACTFLOW_GATEWAY_PROTECT_READ") or "1", True)
    protect_write = _as_bool(_env("ABSTRACTGATEWAY_PROTECT_WRITE", "ABSTRACTFLOW_GATEWAY_PROTECT_WRITE") or "1", True)
    dev_read_no_auth = _as_bool(_env("ABSTRACTGATEWAY_DEV_READ_NO_AUTH", "ABSTRACTFLOW_GATEWAY_DEV_READ_NO_AUTH") or "0", False)

    allowed_origins_raw = _env("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "ABSTRACTFLOW_GATEWAY_ALLOWED_ORIGINS")
    allowed_origins = (
        tuple(_split_csv(allowed_origins_raw))
        if allowed_origins_raw
        else ("http://localhost:*", "http://127.0.0.1:*")
    )

    def _as_int(name: str, fallback: str, default: int) -> int:
        raw = _env(name, fallback)
        if raw is None or not str(raw).strip():
            return default
        try:
            return int(str(raw).strip())
        except Exception:
            return default

    def _as_float(name: str, fallback: str, default: float) -> float:
        raw = _env(name, fallback)
        if raw is None or not str(raw).strip():
            return default
        try:
            return float(str(raw).strip())
        except Exception:
            return default

    max_body = _as_int("ABSTRACTGATEWAY_MAX_BODY_BYTES", "ABSTRACTFLOW_GATEWAY_MAX_BODY_BYTES", 256_000)
    max_upload_body = _as_int("ABSTRACTGATEWAY_MAX_UPLOAD_BODY_BYTES", "ABSTRACTFLOW_GATEWAY_MAX_UPLOAD_BODY_BYTES", 0)
    max_attach = _as_int(
        "ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES",
        "ABSTRACTFLOW_GATEWAY_MAX_ATTACHMENT_BYTES",
        25 * 1024 * 1024,
    )
    max_bundle = _as_int(
        "ABSTRACTGATEWAY_MAX_BUNDLE_BYTES",
        "ABSTRACTFLOW_GATEWAY_MAX_BUNDLE_BYTES",
        75 * 1024 * 1024,
    )
    max_conc = _as_int("ABSTRACTGATEWAY_MAX_CONCURRENCY", "ABSTRACTFLOW_GATEWAY_MAX_CONCURRENCY", 64)
    max_sse = _as_int("ABSTRACTGATEWAY_MAX_SSE", "ABSTRACTFLOW_GATEWAY_MAX_SSE", 32)
    lockout_after = _as_int("ABSTRACTGATEWAY_LOCKOUT_AFTER", "ABSTRACTFLOW_GATEWAY_LOCKOUT_AFTER", 5)
    lockout_base = _as_float("ABSTRACTGATEWAY_LOCKOUT_BASE_S", "ABSTRACTFLOW_GATEWAY_LOCKOUT_BASE_S", 1.0)
    lockout_max = _as_float("ABSTRACTGATEWAY_LOCKOUT_MAX_S", "ABSTRACTFLOW_GATEWAY_LOCKOUT_MAX_S", 60.0)
    trust_proxy = _as_bool(_env("ABSTRACTGATEWAY_TRUST_PROXY", "ABSTRACTFLOW_GATEWAY_TRUST_PROXY") or "0", False)

    return GatewayAuthPolicy(
        enabled=enabled,
        tokens=tuple(tokens2),
        protect_read_endpoints=bool(protect_read),
        protect_write_endpoints=bool(protect_write),
        dev_allow_unauthenticated_reads_on_loopback=bool(dev_read_no_auth),
        allowed_origins=tuple(allowed_origins),
        max_body_bytes=max(0, int(max_body)),
        max_upload_body_bytes=max(0, int(max_upload_body)),
        max_attachment_bytes=max(1, int(max_attach)) if int(max_attach) > 0 else 25 * 1024 * 1024,
        max_bundle_bytes=max(1, int(max_bundle)) if int(max_bundle) > 0 else 75 * 1024 * 1024,
        max_concurrency=max(1, int(max_conc)),
        max_sse_connections=max(1, int(max_sse)),
        lockout_after_failures=max(1, int(lockout_after)),
        lockout_base_s=max(0.0, float(lockout_base)),
        lockout_max_s=max(0.0, float(lockout_max)),
        trust_proxy=bool(trust_proxy),
    )


class _AuthLockoutTracker:
    """In-memory auth failure tracker (v0).

    This is intentionally process-local. In production, prefer infra-level rate limiting
    at the reverse proxy + WAF, and treat this as a safety net.
    """

    def __init__(
        self,
        *,
        after_failures: int,
        base_s: float,
        max_s: float,
        max_entries: int = 10_000,
    ) -> None:
        self._after = max(1, int(after_failures))
        self._base = max(0.0, float(base_s))
        self._max = max(0.0, float(max_s))
        self._max_entries = max(100, int(max_entries))
        self._lock = threading.Lock()
        # ip -> (fail_count, locked_until_epoch_s)
        self._state: Dict[str, Tuple[int, float]] = {}

    def check_locked(self, ip: str) -> Optional[int]:
        now = time.time()
        with self._lock:
            fc, until = self._state.get(ip, (0, 0.0))
            if until > now:
                return int(max(0.0, until - now))
            return None

    def record_failure(self, ip: str) -> Optional[int]:
        now = time.time()
        with self._lock:
            if len(self._state) > self._max_entries:
                # best-effort pruning: drop arbitrary entries
                for k in list(self._state.keys())[:1000]:
                    self._state.pop(k, None)
            fc, until = self._state.get(ip, (0, 0.0))
            fc += 1

            if fc < self._after:
                self._state[ip] = (fc, 0.0)
                return None

            # Exponential backoff from the threshold.
            exp = max(0, fc - self._after)
            lock_s = self._base * (2**exp) if self._base > 0 else 0.0
            if self._max > 0:
                lock_s = min(lock_s, self._max)
            until2 = now + lock_s
            self._state[ip] = (fc, until2)
            return int(lock_s)

    def record_success(self, ip: str) -> None:
        with self._lock:
            if ip in self._state:
                self._state.pop(ip, None)


class GatewaySecurityMiddleware:
    """ASGI middleware to secure /api/gateway/* endpoints."""

    def __init__(self, app: Any, *, policy: GatewayAuthPolicy):
        self._app = app
        self._policy = policy
        self._sema = asyncio.Semaphore(int(policy.max_concurrency))
        self._sse_sema = asyncio.Semaphore(int(policy.max_sse_connections))
        self._lockouts = _AuthLockoutTracker(
            after_failures=policy.lockout_after_failures,
            base_s=policy.lockout_base_s,
            max_s=policy.lockout_max_s,
        )

        if policy.enabled:
            if policy.protect_write_endpoints and not policy.tokens:
                logger.warning(
                    "Gateway security enabled, but no auth token configured "
                    "(ABSTRACTGATEWAY_AUTH_TOKEN). Mutating endpoints will be rejected."
                )

    # ---------------------------
    # Helpers
    # ---------------------------

    def _header(self, scope: dict, name: str) -> Optional[str]:
        key = name.lower().encode("utf-8")
        for k, v in scope.get("headers") or []:
            if k == key:
                try:
                    return v.decode("utf-8")
                except Exception:
                    return None
        return None

    def _client_ip(self, scope: dict) -> str:
        if self._policy.trust_proxy:
            xff = self._header(scope, "x-forwarded-for")
            if xff:
                first = xff.split(",")[0].strip()
                if first:
                    return first
        client = scope.get("client")
        if isinstance(client, (list, tuple)) and client and isinstance(client[0], str):
            return client[0]
        return "unknown"

    def _origin_allowed(self, origin: str) -> bool:
        o = str(origin or "").strip()
        if not o:
            return True
        allowed = self._policy.allowed_origins or ()
        if not allowed:
            return False
        for pattern in allowed:
            p = str(pattern or "").strip()
            if not p:
                continue
            if p == "*":
                return True
            if p.endswith("*"):
                if o.startswith(p[:-1]):
                    return True
                continue
            if o == p:
                return True
        return False

    def _token_valid(self, token: str) -> bool:
        # Constant-time compare against any configured token.
        for t in self._policy.tokens:
            if hmac.compare_digest(str(token), str(t)):
                return True
        return False

    async def _send_json(
        self,
        send,
        *,
        status: int,
        payload: Dict[str, Any],
        headers: Optional[list[tuple[bytes, bytes]]] = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("utf-8")),
        ]
        if headers:
            hdrs.extend(headers)
        await send({"type": "http.response.start", "status": int(status), "headers": hdrs})
        await send({"type": "http.response.body", "body": body})

    async def _reject(
        self, send, *, status: int, detail: str, headers: Optional[list[tuple[bytes, bytes]]] = None
    ) -> None:
        await self._send_json(send, status=status, payload={"detail": detail}, headers=headers)

    async def _try_acquire(self, sem: asyncio.Semaphore, *, timeout_s: float = 0.01) -> bool:
        try:
            await asyncio.wait_for(sem.acquire(), timeout=float(timeout_s))
            return True
        except Exception:
            return False

    # ---------------------------
    # ASGI entrypoint
    # ---------------------------

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self._app(scope, receive, send)

        path = str(scope.get("path") or "")
        if not path.startswith("/api/gateway"):
            return await self._app(scope, receive, send)

        if not self._policy.enabled:
            return await self._app(scope, receive, send)

        method = str(scope.get("method") or "GET").upper()
        is_read = method in {"GET", "HEAD"}
        is_write = method in {"POST", "PUT", "PATCH", "DELETE"}
        ip = self._client_ip(scope)

        # Origin checks (only when Origin is present).
        origin = self._header(scope, "origin")
        if origin is not None and not self._origin_allowed(origin):
            await self._reject(send, status=403, detail="Forbidden (origin not allowed)")
            return

        # Lockout handling (only meaningful when auth is enabled).
        locked = self._lockouts.check_locked(ip)
        if locked is not None and locked > 0:
            await self._reject(
                send,
                status=429,
                detail="Too Many Requests (auth lockout)",
                headers=[(b"retry-after", str(int(locked)).encode("utf-8"))],
            )
            return

        # Auth decision.
        auth_required = False
        if is_write and self._policy.protect_write_endpoints:
            auth_required = True
        if is_read and self._policy.protect_read_endpoints:
            # Optional dev escape hatch (loopback-only).
            if self._policy.dev_allow_unauthenticated_reads_on_loopback and _is_loopback_ip(ip):
                auth_required = False
            else:
                auth_required = True

        # OPTIONS preflight: allow through (but still origin-checked above).
        if method == "OPTIONS":
            return await self._app(scope, receive, send)

        if auth_required:
            if not self._policy.tokens:
                await self._reject(send, status=503, detail="Gateway auth required but no token configured")
                return
            auth = self._header(scope, "authorization") or ""
            token = ""
            if auth.lower().startswith("bearer "):
                token = auth.split(" ", 1)[1].strip()
            if not token or not self._token_valid(token):
                lock = self._lockouts.record_failure(ip)
                if lock is not None and lock > 0:
                    await self._reject(
                        send,
                        status=429,
                        detail="Too Many Requests (auth lockout)",
                        headers=[(b"retry-after", str(int(lock)).encode("utf-8"))],
                    )
                    return
                await self._reject(
                    send,
                    status=401,
                    detail="Unauthorized",
                    headers=[(b"www-authenticate", b"Bearer")],
                )
                return
            self._lockouts.record_success(ip)

        def _upload_kind(path: str) -> str:
            p = str(path or "").rstrip("/")
            if p.endswith("/attachments/upload"):
                return "attachments"
            if p.endswith("/bundles/upload"):
                return "bundles"
            return ""

        # Body size limits (for mutating endpoints).
        buffered_body: Optional[bytes] = None
        max_body_bytes = int(self._policy.max_body_bytes)
        upload_kind = _upload_kind(path)
        if upload_kind:
            # Multipart requests include boundary + part headers in `Content-Length`.
            # This overhead is typically tiny (KBs), but we allow a conservative cushion so the
            # security layer cannot reject a valid upload that is still within the endpoint's
            # per-file cap (e.g. 25MB for /attachments/upload).
            overhead = 2 * 1024 * 1024
            endpoint_cap = (
                int(self._policy.max_attachment_bytes) if upload_kind == "attachments" else int(self._policy.max_bundle_bytes)
            )
            max_body_bytes = max(0, int(endpoint_cap) + int(overhead))
            if int(self._policy.max_upload_body_bytes) > 0:
                max_body_bytes = min(max_body_bytes, int(self._policy.max_upload_body_bytes))

        if is_write and max_body_bytes > 0:
            cl = self._header(scope, "content-length")
            if cl is not None:
                try:
                    if int(cl) > int(max_body_bytes):
                        await self._reject(send, status=413, detail="Payload Too Large")
                        return
                except Exception:
                    pass
            else:
                # No content-length: buffer up to limit+1, then replay.
                limit = int(max_body_bytes)
                chunks: list[bytes] = []
                size = 0
                more = True
                while more:
                    message = await receive()
                    if message.get("type") != "http.request":
                        continue
                    body = message.get("body", b"") or b""
                    more = bool(message.get("more_body", False))
                    if body:
                        chunks.append(body)
                        size += len(body)
                        if size > limit:
                            await self._reject(send, status=413, detail="Payload Too Large")
                            return
                buffered_body = b"".join(chunks)

        # Concurrency limits: separate pool for SSE streams.
        is_sse = path.endswith("/ledger/stream")
        sem = self._sse_sema if is_sse else self._sema
        acquired = await self._try_acquire(sem)
        if not acquired:
            await self._reject(
                send,
                status=429,
                detail="Too Many Requests (concurrency limit)",
                headers=[(b"retry-after", b"1")],
            )
            return

        try:
            if buffered_body is None:
                return await self._app(scope, receive, send)

            # Replay buffered body to downstream app.
            sent = False

            async def _replay_receive():
                nonlocal sent
                if sent:
                    return {"type": "http.request", "body": b"", "more_body": False}
                sent = True
                return {"type": "http.request", "body": buffered_body, "more_body": False}

            return await self._app(scope, _replay_receive, send)
        finally:
            try:
                sem.release()
            except Exception:
                pass
