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
import datetime
import fnmatch
import hmac
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from ..users import GatewayUserRegistry, gateway_user_auth_enabled, token_fingerprint
from .principal import (
    GatewayPrincipal,
    local_admin_principal,
    local_readonly_principal,
    reset_current_gateway_principal,
    set_current_gateway_principal,
)
from .authorization import authorize_gateway_principal, gateway_route_authorization_requirement
from .sessions import (
    GatewaySessionStore,
    gateway_csrf_header_name,
    gateway_session_header_name,
    gateway_session_id_from_cookie_header,
    gateway_session_id_from_value,
    legacy_token_fingerprints,
)

logger = logging.getLogger(__name__)

_AUDIT_LOCK = threading.Lock()


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
    if h in {"localhost", "testclient"}:
        return True
    # Dual-stack binds (`--host ::`) hand a v4 client over as ::ffff:127.0.0.1;
    # 127.0.0.0/8 aliases (WSL, some distros) are loopback too.
    if h.startswith("::ffff:"):
        h = h[len("::ffff:"):]
    try:
        import ipaddress

        return bool(ipaddress.ip_address(h).is_loopback)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Ephemeral loopback-only admin tokens (desktop tray helper, 2026-09-05)
#
# The gateway mints one random token per process for the tray helper it
# spawns, hands it over on the child's stdin, and registers it here. The
# token is accepted ONLY when the request's raw socket peer is loopback —
# never through X-Forwarded-For, whatever `trust_proxy` says — and only for
# this process's lifetime (nothing is written to disk). It resolves to the
# same local admin principal as the static operator token. Registry access
# is constant-time per token like `_token_valid`.
# ---------------------------------------------------------------------------

_EPHEMERAL_TOKENS_LOCK = threading.Lock()
_EPHEMERAL_TOKENS: Dict[str, str] = {}  # token -> label


def register_ephemeral_loopback_token(token: str, *, label: str) -> None:
    tok = str(token or "").strip()
    if len(tok) < 16:
        raise ValueError("ephemeral loopback tokens must be at least 16 characters")
    with _EPHEMERAL_TOKENS_LOCK:
        _EPHEMERAL_TOKENS[tok] = str(label or "ephemeral")


def revoke_ephemeral_loopback_token(token: str) -> None:
    with _EPHEMERAL_TOKENS_LOCK:
        _EPHEMERAL_TOKENS.pop(str(token or "").strip(), None)


def ephemeral_loopback_token_valid(token: str, *, peer_ip: Optional[str]) -> bool:
    """Constant-time membership check, gated on a loopback SOCKET peer."""
    if not token or not _is_loopback_ip(str(peer_ip or "")):
        return False
    with _EPHEMERAL_TOKENS_LOCK:
        candidates = list(_EPHEMERAL_TOKENS.keys())
    for t in candidates:
        if hmac.compare_digest(str(token), str(t)):
            return True
    return False


def _env(name: str, fallback: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is not None and str(v).strip():
        return v
    if fallback:
        v2 = os.getenv(fallback)
        if v2 is not None and str(v2).strip():
            return v2
    return None


def _now_utc_iso() -> str:
    try:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()
    except Exception:
        # Best-effort: avoid ever throwing in security middleware.
        return ""


def _sha256_hex(text: str) -> str:
    try:
        h = hashlib.sha256()
        h.update(str(text or "").encode("utf-8", errors="ignore"))
        return h.hexdigest()
    except Exception:
        return ""


def _audit_data_dir_from_env() -> Path:
    raw = (
        os.getenv("ABSTRACTGATEWAY_DATA_DIR")
        or os.getenv("ABSTRACTFLOW_RUNTIME_DIR")
        or os.getenv("ABSTRACTFLOW_GATEWAY_DATA_DIR")
        or "./runtime"
    )
    try:
        return Path(str(raw)).expanduser().resolve()
    except Exception:
        return Path("./runtime").resolve()


def _audit_log_enabled(*, default: bool) -> bool:
    raw = os.getenv("ABSTRACTGATEWAY_AUDIT_LOG")
    if raw is None:
        return bool(default)
    return _as_bool(raw, bool(default))


def _audit_log_max_bytes() -> int:
    raw = os.getenv("ABSTRACTGATEWAY_AUDIT_LOG_MAX_BYTES") or ""
    try:
        v = int(str(raw).strip())
        return max(0, v)
    except Exception:
        # Default: 50MB (bounded, but large enough for multi-day local dev).
        return 50 * 1024 * 1024


def _audit_log_rotations() -> int:
    raw = os.getenv("ABSTRACTGATEWAY_AUDIT_LOG_ROTATIONS") or ""
    try:
        v = int(str(raw).strip())
        return max(0, min(200, v))
    except Exception:
        return 10


def _audit_header_allowlist() -> Tuple[str, ...]:
    raw = os.getenv("ABSTRACTGATEWAY_AUDIT_LOG_HEADERS") or ""
    if not str(raw).strip():
        return ("x-client-id", "x-client-version", "x-forwarded-for")
    out: list[str] = []
    for part in str(raw).split(","):
        p = part.strip().lower()
        if p:
            out.append(p)
    return tuple(out)


def _audit_redact_query(query_string: bytes) -> str:
    # Do not log full query params by default (can contain secrets). Keep only keys.
    try:
        qs = (query_string or b"").decode("utf-8", errors="replace")
    except Exception:
        return ""
    if not qs:
        return ""
    # Parse best-effort without importing urllib for speed/robustness.
    parts = []
    for kv in qs.split("&"):
        if not kv:
            continue
        k = kv.split("=", 1)[0].strip()
        if k:
            parts.append(k)
    if not parts:
        return ""
    parts = parts[:50]
    return "&".join([f"{k}=<redacted>" for k in parts])


@dataclass(frozen=True)
class GatewayAuthPolicy:
    """Configuration for the Run Gateway security layer."""

    # Enable/disable middleware entirely (escape hatch).
    enabled: bool = True

    # Auth tokens (shared secret list; any token is accepted).
    tokens: Tuple[str, ...] = ()

    # File-backed user registry auth. When enabled, bearer tokens resolve to
    # concrete principals and request-scoped Gateway services.
    user_auth_enabled: bool = False

    # Default: protect both reads and writes.
    protect_read_endpoints: bool = True
    protect_write_endpoints: bool = True

    # Dev-only convenience: allow unauthenticated reads *from loopback only*.
    dev_allow_unauthenticated_reads_on_loopback: bool = False

    # Origin allowlist. Only applied when Origin header is present.
    # Supports '*' suffix wildcard for prefix matches (e.g., 'http://localhost:*').
    allowed_origins: Tuple[str, ...] = ("http://localhost:*", "http://127.0.0.1:*")

    # Abuse resistance.
    #
    # 10MB, not the former 256KB. This limit is abuse resistance for API calls,
    # but VisualFlow documents go through the same write path, and a saved flow
    # is a whole authored artifact rather than a request payload: the editor
    # expands every flow on load (template pin docs, canonical pins, regenerated
    # code bodies), so a shipped 92-node example serializes to ~308KB and could
    # never be saved again after a single edit. Refusing to store a user's work
    # is a far worse outcome than accepting a large document.
    max_body_bytes: int = 10 * 1024 * 1024
    # Upload endpoints can legitimately exceed `max_body_bytes` (multipart). 0 means "auto"
    # (derived from the explicit endpoint caps).
    max_upload_body_bytes: int = 0
    # Endpoint caps (mirrors /attachments/upload and /bundles/upload defaults).
    max_attachment_bytes: int = 25 * 1024 * 1024
    max_bundle_bytes: int = 75 * 1024 * 1024
    max_concurrency: int = 64
    max_sse_connections: int = 32

    # Auth failure lockout. Threshold is per-IP and one box runs MANY apps
    # (c2341): 10 presented-invalid credentials within the decay window is
    # still instant refusal for a brute-forcer (exponential backoff from
    # there), while a stale cookie in one app no longer locks the machine.
    lockout_after_failures: int = 10
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

    max_body = _as_int("ABSTRACTGATEWAY_MAX_BODY_BYTES", "ABSTRACTFLOW_GATEWAY_MAX_BODY_BYTES", 10 * 1024 * 1024)
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
    lockout_after = _as_int("ABSTRACTGATEWAY_LOCKOUT_AFTER", "ABSTRACTFLOW_GATEWAY_LOCKOUT_AFTER", 10)
    lockout_base = _as_float("ABSTRACTGATEWAY_LOCKOUT_BASE_S", "ABSTRACTFLOW_GATEWAY_LOCKOUT_BASE_S", 1.0)
    lockout_max = _as_float("ABSTRACTGATEWAY_LOCKOUT_MAX_S", "ABSTRACTFLOW_GATEWAY_LOCKOUT_MAX_S", 60.0)
    trust_proxy = _as_bool(_env("ABSTRACTGATEWAY_TRUST_PROXY", "ABSTRACTFLOW_GATEWAY_TRUST_PROXY") or "0", False)
    user_auth = gateway_user_auth_enabled()

    return GatewayAuthPolicy(
        enabled=enabled,
        tokens=tuple(tokens2),
        user_auth_enabled=bool(user_auth),
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

    SEMANTICS (operator incident 2026-07-15, c2341 — every app on the box
    answered 429): the tracker throttles CREDENTIAL GUESSING, never
    presence. Only PRESENTED-and-invalid credentials count as failures;
    a request with no credential is a login prompt (401), not a guess —
    on a single machine every thin client feature-probes before sign-in,
    and counting those locked the shared loopback IP for everyone.
    Failures DECAY (a stale-cookie app retrying in the background must
    not ratchet the counter all day), and a VALID credential always
    passes and clears the state — enforced by the middleware ordering
    (verify first), which kills the deadlock where a locked IP rejected
    the very credentials that would have cleared it.
    """

    def __init__(
        self,
        *,
        after_failures: int,
        base_s: float,
        max_s: float,
        max_entries: int = 10_000,
        decay_s: float = 900.0,
    ) -> None:
        self._after = max(1, int(after_failures))
        self._base = max(0.0, float(base_s))
        self._max = max(0.0, float(max_s))
        self._max_entries = max(100, int(max_entries))
        self._decay_s = max(0.0, float(decay_s))
        self._lock = threading.Lock()
        # ip -> (fail_count, locked_until_epoch_s, last_failure_epoch_s)
        self._state: Dict[str, Tuple[int, float, float]] = {}

    def _decayed(self, ip: str, now: float) -> Tuple[int, float, float]:
        fc, until, last = self._state.get(ip, (0, 0.0, 0.0))
        # A quiet window resets the ratchet: old failures stop counting
        # toward the exponential backoff (all-day ratchet defect, c2341).
        if self._decay_s > 0 and last > 0 and (now - last) > self._decay_s and until <= now:
            self._state.pop(ip, None)
            return (0, 0.0, 0.0)
        return (fc, until, last)

    def check_locked(self, ip: str) -> Optional[int]:
        now = time.time()
        with self._lock:
            _fc, until, _last = self._decayed(ip, now)
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
            fc, until, _last = self._decayed(ip, now)
            fc += 1

            if fc < self._after:
                self._state[ip] = (fc, 0.0, now)
                return None

            # Exponential backoff from the threshold.
            exp = max(0, fc - self._after)
            lock_s = self._base * (2**exp) if self._base > 0 else 0.0
            if self._max > 0:
                lock_s = min(lock_s, self._max)
            until2 = now + lock_s
            self._state[ip] = (fc, until2, now)
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
            if policy.protect_write_endpoints and not policy.tokens and not policy.user_auth_enabled:
                logger.warning(
                    "Gateway security enabled, but no auth token configured "
                    "(ABSTRACTGATEWAY_AUTH_TOKEN). Mutating endpoints will be rejected."
                )

        self._audit_enabled = _audit_log_enabled(default=bool(policy.enabled))
        self._audit_max_bytes = int(_audit_log_max_bytes())
        self._audit_rotations = int(_audit_log_rotations())
        self._audit_headers = _audit_header_allowlist()

    def _audit_append(self, entry: Dict[str, Any]) -> None:
        if not self._audit_enabled:
            return
        try:
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        except Exception:
            return
        data = line.encode("utf-8", errors="replace")
        try:
            with _AUDIT_LOCK:
                path = (_audit_data_dir_from_env() / "audit_log.jsonl").resolve()
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                except Exception:
                    return

                # Rotate if needed (best-effort).
                try:
                    max_bytes = int(self._audit_max_bytes)
                except Exception:
                    max_bytes = 0
                if max_bytes > 0 and path.exists():
                    try:
                        size = int(path.stat().st_size)
                    except Exception:
                        size = 0
                    if size >= max_bytes:
                        ts = _now_utc_iso().replace(":", "").replace("-", "")
                        rotated = path.with_name(f"audit_log.{ts}.jsonl")
                        try:
                            path.replace(rotated)
                        except Exception:
                            # If rotation fails, keep appending to the same file.
                            pass
                        else:
                            # Best-effort pruning: keep only N rotated files.
                            keep = int(self._audit_rotations)
                            if keep > 0:
                                try:
                                    olds = sorted(
                                        path.parent.glob("audit_log.*.jsonl"),
                                        key=lambda p: p.name,
                                        reverse=True,
                                    )
                                    for p in olds[keep:]:
                                        try:
                                            p.unlink()
                                        except Exception:
                                            pass
                                except Exception:
                                    pass

                with open(path, "ab") as f:
                    f.write(data)
        except Exception:
            # Never break request handling due to audit logging.
            return

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

    @staticmethod
    def _socket_peer_ip(scope: dict) -> str:
        """The RAW socket peer — deliberately ignores X-Forwarded-For (the
        ephemeral tray token must never be reachable through a proxy)."""
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
            # Glob-style matching (fnmatch): supports patterns like
            # - http://localhost:*
            # - https://*.ngrok-free.app
            if any(ch in p for ch in ("*", "?", "[")):
                if fnmatch.fnmatchcase(o, p):
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

    # -- verified-token cache (idle-CPU incident, framework c4144) ----------
    #
    # Under user auth, EVERY bearer request ran the registry scan FIRST:
    # per enabled record one PBKDF2 verification at 260k iterations
    # (~100ms CPU each) — ~0.5s of pure CPU per request on a 5-user
    # registry, even when the token was the static operator token that the
    # cheap constant-time compare would accept afterwards. A handful of
    # connected app pollers (observer board, entity app, assistant) burned
    # a full core on a gateway with ZERO runs. PBKDF2's cost is the point
    # AT REST (an exfiltrated users.json resists brute force); re-deriving
    # it per request adds nothing — the cache keys on the token's sha256
    # and invalidates the instant the registry FILE identity changes
    # (mtime_ns, ino, size — same-mtime rewrites still caught by inode),
    # so revocation/rotation takes effect at the next request exactly as
    # before. Only SUCCESSFUL authentications are cached: unknown tokens
    # stay uncacheable (the lockout layer owns brute force; a failure
    # cache would be an attacker-fillable map).

    _AUTH_CACHE_MAX = 256

    def _registry_file_identity(self) -> tuple:
        try:
            from ..users import gateway_user_registry_path_from_env

            st = gateway_user_registry_path_from_env().stat()
            return (st.st_mtime_ns, st.st_ino, st.st_size)
        except FileNotFoundError:
            return ("absent",)
        except Exception:
            return ("unreadable",)

    def _authenticate_token(self, token: str, *, peer_ip: Optional[str] = None) -> Optional[GatewayPrincipal]:
        if not token:
            return None
        # Ephemeral loopback token (tray helper): checked FIRST and never
        # cached — the cache is keyed on the token alone and a cached hit
        # would answer for a non-loopback peer later. Cheap (hmac compare),
        # so it also spares the PBKDF2 registry scan under user auth.
        if peer_ip is not None and ephemeral_loopback_token_valid(token, peer_ip=peer_ip):
            import dataclasses

            # Same identity as the operator (one operator, one world), but the
            # audit log must be able to tell the desktop helper from a human.
            return dataclasses.replace(
                local_admin_principal(token_fingerprint=token_fingerprint(token)),
                source="loopback-ephemeral:desktop-tray",
            )
        user_auth = self._policy.user_auth_enabled or gateway_user_auth_enabled()
        cache_key = hashlib.sha256(str(token).encode("utf-8", errors="ignore")).hexdigest()
        registry_id = self._registry_file_identity() if user_auth else ("no-user-auth",)
        cache = getattr(self, "_auth_cache", None)
        if cache is None:
            cache = {}
            self._auth_cache = cache
        hit = cache.get(cache_key)
        if hit is not None and hit[0] == registry_id:
            return hit[1]

        principal: Optional[GatewayPrincipal] = None
        try:
            if user_auth:
                principal = GatewayUserRegistry().authenticate(token)
        except Exception as e:
            logger.warning("Gateway user registry auth failed: %s", e)
        if principal is None and self._token_valid(token):
            principal = local_admin_principal(token_fingerprint=token_fingerprint(token))
        if principal is not None:
            if len(cache) >= self._AUTH_CACHE_MAX:
                cache.clear()  # tiny cardinality in practice; simplest bound
            cache[cache_key] = (registry_id, principal)
        return principal

    def _authenticate_session_cookie(self, cookie_header: Optional[str]) -> Optional[tuple[GatewayPrincipal, str]]:
        session_id = gateway_session_id_from_cookie_header(cookie_header)
        if not session_id:
            return None
        principal = GatewaySessionStore().authenticate_session(
            session_id,
            legacy_token_fingerprints=legacy_token_fingerprints(tuple(self._policy.tokens or ())),
        )
        if principal is None:
            return None
        return principal, session_id

    def _authenticate_session_header(self, session_value: Optional[str]) -> Optional[tuple[GatewayPrincipal, str]]:
        session_id = gateway_session_id_from_value(session_value)
        if not session_id:
            return None
        principal = GatewaySessionStore().authenticate_session(
            session_id,
            legacy_token_fingerprints=legacy_token_fingerprints(tuple(self._policy.tokens or ())),
        )
        if principal is None:
            return None
        return principal, session_id

    def _public_auth_path(self, path: str, method: str) -> bool:
        if method == "POST" and path.rstrip("/") == "/api/gateway/session/login":
            return True
        return False

    def _route_authorization_requirement(self, path: str, method: str) -> Optional[dict[str, str]]:
        requirement = gateway_route_authorization_requirement(path, method)
        return requirement.public_dict() if requirement is not None else None

    async def _reject_forbidden_route(self, send, *, requirement: dict[str, str]) -> None:
        await self._send_json(
            send,
            status=403,
            payload={
                "detail": "Admin principal required",
                "reason_code": requirement.get("reason_code") or "admin_required",
                "required_role": "admin",
                "resource_class": requirement.get("resource") or "gateway",
                "action": requirement.get("action") or "",
            },
        )

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

        request_id = (self._header(scope, "x-request-id") or "").strip()
        if not request_id:
            request_id = uuid.uuid4().hex

        started_at_s = time.time()
        status_code: Optional[int] = None
        error: Optional[str] = None
        auth_required: bool = False
        presented_token_fp: str = ""
        principal: Optional[GatewayPrincipal] = None
        session_id: str = ""
        session_authenticated: bool = False

        response_started: bool = False

        async def _send_wrapped(message: dict) -> None:
            nonlocal status_code, response_started
            if message.get("type") == "http.response.start":
                response_started = True
                try:
                    status_code = int(message.get("status") or 0)
                except Exception:
                    status_code = 0

                try:
                    hdrs = list(message.get("headers") or [])
                except Exception:
                    hdrs = []
                has_rid = False
                for k, _v in hdrs:
                    try:
                        if bytes(k).lower() == b"x-request-id":
                            has_rid = True
                            break
                    except Exception:
                        continue
                if not has_rid:
                    hdrs.append((b"x-request-id", str(request_id).encode("utf-8")))
                message = dict(message)
                message["headers"] = hdrs
            await send(message)

        def _finalize_audit() -> None:
            if not is_write:
                return
            if not self._audit_enabled:
                return
            now = time.time()
            duration_ms = int(max(0.0, (now - float(started_at_s))) * 1000.0)
            qs = scope.get("query_string") or b""
            entry: Dict[str, Any] = {
                "ts": _now_utc_iso(),
                "request_id": str(request_id),
                "ip": str(ip),
                "method": str(method),
                "path": str(path),
                "query": _audit_redact_query(qs if isinstance(qs, (bytes, bytearray)) else b""),
                "status": int(status_code or 0),
                "duration_ms": int(duration_ms),
                "auth_required": bool(auth_required),
            }
            if presented_token_fp:
                entry["auth_token_fp"] = str(presented_token_fp)
            if principal is not None:
                entry["principal_user_id"] = str(principal.user_id)
                entry["principal_tenant_id"] = str(principal.tenant_id)

            origin = self._header(scope, "origin")
            if origin:
                entry["origin"] = str(origin)
            ua = self._header(scope, "user-agent")
            if ua:
                entry["user_agent"] = str(ua)
            cl = self._header(scope, "content-length")
            if cl and str(cl).strip().isdigit():
                try:
                    entry["bytes_in"] = int(str(cl).strip())
                except Exception:
                    pass

            headers_out: Dict[str, str] = {}
            for hn in self._audit_headers:
                try:
                    v = self._header(scope, hn)
                except Exception:
                    v = None
                if v is None:
                    continue
                headers_out[str(hn)] = str(v)
            if headers_out:
                entry["headers"] = headers_out

            if error:
                entry["error"] = str(error)

            self._audit_append(entry)

        try:
            # Origin checks (only when Origin is present).
            origin = self._header(scope, "origin")
            if origin is not None and not self._origin_allowed(origin):
                await self._reject(_send_wrapped, status=403, detail="Forbidden (origin not allowed)")
                return

            # NOTE (operator incident 2026-07-15, c2341): the lockout gate
            # deliberately runs AFTER credential verification now, inside
            # the invalid-credential branch below. The old pre-auth 429
            # rejected VALID credentials from a locked IP — on a one-box
            # deployment every app shares the loopback IP, so one app's
            # stale cookie locked out every other app AND the very sign-in
            # that would have cleared the state (a deadlock the operator
            # saw as "all apps: Too Many Requests"). Lockout throttles
            # credential GUESSING, never presence.

            # Auth decision.
            auth_required = False
            public_auth_path = self._public_auth_path(path, method)
            if is_write and self._policy.protect_write_endpoints and not public_auth_path:
                auth_required = True
            if is_read and self._policy.protect_read_endpoints:
                # Optional dev escape hatch (loopback-only).
                if self._policy.dev_allow_unauthenticated_reads_on_loopback and _is_loopback_ip(ip):
                    auth_required = False
                else:
                    auth_required = True

            # OPTIONS preflight: allow through (but still origin-checked above).
            if method == "OPTIONS":
                return await self._app(scope, receive, _send_wrapped)

            if auth_required:
                if not self._policy.tokens and not self._policy.user_auth_enabled and not gateway_user_auth_enabled():
                    await self._reject(_send_wrapped, status=503, detail="Gateway auth required but no token configured")
                    return
                auth = self._header(scope, "authorization") or ""
                token = ""
                if auth.lower().startswith("bearer "):
                    token = auth.split(" ", 1)[1].strip()
                if not token and is_read:
                    # SSE/EventSource cannot set headers: accept the
                    # credential as ?access_token= on READS ONLY (writes
                    # keep the header/session discipline). The audit log
                    # already redacts query VALUES (keys-only logging).
                    try:
                        from urllib.parse import parse_qs

                        qs = scope.get("query_string") or b""
                        values = parse_qs(qs.decode("utf-8", errors="replace")).get("access_token") or []
                        token = str(values[0]).strip() if values else ""
                    except Exception:
                        token = ""
                if token:
                    presented_token_fp = _sha256_hex(token)[:12]
                    principal = self._authenticate_token(token, peer_ip=self._socket_peer_ip(scope))
                else:
                    session_auth = self._authenticate_session_header(
                        self._header(scope, gateway_session_header_name())
                    )
                    if session_auth is None:
                        session_auth = self._authenticate_session_cookie(self._header(scope, "cookie"))
                    if session_auth is not None:
                        principal, session_id = session_auth
                        session_authenticated = True
                if principal is None:
                    presented_credential = bool(token) or bool(
                        self._header(scope, gateway_session_header_name())
                        or gateway_session_id_from_cookie_header(self._header(scope, "cookie"))
                    )
                    if presented_credential:
                        # A presented-and-INVALID credential is a guess:
                        # count it, and refuse 429 only when this IP is in
                        # a lockout window (checked here, post-verification
                        # — valid credentials never see the lock).
                        locked_now = self._lockouts.check_locked(ip)
                        lock = self._lockouts.record_failure(ip)
                        wait = max(int(locked_now or 0), int(lock or 0))
                        if wait > 0:
                            await self._reject(
                                _send_wrapped,
                                status=429,
                                detail="Too Many Requests (auth lockout)",
                                headers=[(b"retry-after", str(int(wait)).encode("utf-8"))],
                            )
                            return
                    # No credential presented = a login prompt, not a
                    # guess (feature probes from every app on the box
                    # counted as failures before c2341 — collective
                    # punishment on the shared loopback IP).
                    await self._reject(
                        _send_wrapped,
                        status=401,
                        detail="Unauthorized",
                        headers=[(b"www-authenticate", b"Bearer")],
                    )
                    return
                self._lockouts.record_success(ip)
            elif principal is None:
                if public_auth_path:
                    principal = None
                elif is_read and self._policy.dev_allow_unauthenticated_reads_on_loopback and _is_loopback_ip(ip):
                    principal = local_readonly_principal()
                else:
                    principal = local_admin_principal()

            if session_authenticated and is_write:
                csrf_token = self._header(scope, gateway_csrf_header_name()) or ""
                if not GatewaySessionStore().verify_csrf_token(session_id, csrf_token):
                    await self._send_json(
                        _send_wrapped,
                        status=403,
                        payload={
                            "detail": "Gateway browser session CSRF token missing or invalid",
                            "reason_code": "csrf_required",
                        },
                    )
                    return

            requirement = self._route_authorization_requirement(path, method)
            if requirement is not None:
                decision = authorize_gateway_principal(
                    principal,
                    resource=requirement.get("resource") or "gateway",
                    action=requirement.get("action") or method,
                    admin_required=True,
                )
                if not decision.allowed:
                    await self._reject_forbidden_route(_send_wrapped, requirement=requirement)
                    return

            try:
                state = scope.setdefault("state", {})
                if isinstance(state, dict):
                    state["gateway_principal"] = principal
                    if session_id:
                        state["gateway_session_id"] = session_id
                scope["abstractgateway.principal"] = principal
                if session_id:
                    scope["abstractgateway.session_id"] = session_id
            except Exception:
                pass

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
                    int(self._policy.max_attachment_bytes)
                    if upload_kind == "attachments"
                    else int(self._policy.max_bundle_bytes)
                )
                max_body_bytes = max(0, int(endpoint_cap) + int(overhead))
                if int(self._policy.max_upload_body_bytes) > 0:
                    max_body_bytes = min(max_body_bytes, int(self._policy.max_upload_body_bytes))

            if is_write and max_body_bytes > 0:
                cl = self._header(scope, "content-length")
                if cl is not None:
                    try:
                        content_length = int(cl)
                        if content_length > int(max_body_bytes):
                            await self._reject(
                                _send_wrapped,
                                status=413,
                                detail=f"Payload Too Large ({content_length} bytes > {int(max_body_bytes)} bytes)",
                            )
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
                                await self._reject(
                                    _send_wrapped, status=413, detail=f"Payload Too Large ({size} bytes > {limit} bytes)"
                                )
                                return
                    buffered_body = b"".join(chunks)

            # Concurrency limits: separate pool for SSE streams.
            is_sse = path.endswith("/ledger/stream") or path.endswith("/voice/tts/stream")
            sem = self._sse_sema if is_sse else self._sema
            acquired = await self._try_acquire(sem)
            if not acquired:
                await self._reject(
                    _send_wrapped,
                    status=429,
                    detail="Too Many Requests (concurrency limit)",
                    headers=[(b"retry-after", b"1")],
                )
                return

            principal_token = set_current_gateway_principal(principal)
            try:
                try:
                    if buffered_body is None:
                        return await self._app(scope, receive, _send_wrapped)

                    # Replay buffered body to downstream app.
                    sent = False

                    async def _replay_receive():
                        nonlocal sent
                        if sent:
                            return {"type": "http.request", "body": b"", "more_body": False}
                        sent = True
                        return {"type": "http.request", "body": buffered_body, "more_body": False}

                    return await self._app(scope, _replay_receive, _send_wrapped)
                finally:
                    try:
                        sem.release()
                    except Exception:
                        pass
            finally:
                reset_current_gateway_principal(principal_token)
        except Exception as e:
            try:
                error = str(e)
            except Exception:
                error = "error"
            # CORS-safe failure (resilience wave 2026-07-21, adversary P2-7):
            # an exception raised INSIDE this middleware (auth/session/CSRF
            # store faults) used to re-raise past the outer CORSMiddleware —
            # Starlette's ServerErrorMiddleware then returned a raw 500
            # WITHOUT CORS headers, which browsers mask as a generic
            # "Failed to fetch" (the exact incident class the app-level
            # handler was added for, reborn one layer out). Send a JSON 500
            # through _send_wrapped so it exits through CORSMiddleware; only
            # re-raise when the response already started (nothing sane can
            # be sent mid-stream).
            logging.getLogger("abstractgateway.security").exception(
                "GatewaySecurityMiddleware failed handling %s %s", method, path
            )
            if response_started:
                raise
            try:
                await self._send_json(
                    _send_wrapped,
                    status=500,
                    payload={"detail": f"Internal error: {type(e).__name__}: {e}"},
                )
            except Exception:
                raise e
        finally:
            _finalize_audit()
