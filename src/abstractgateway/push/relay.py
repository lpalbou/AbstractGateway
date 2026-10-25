"""`abstractgateway push-relay`: the stateless Apple push relay (framework backlog 1049).

The publisher (or anyone with their own Apple developer key) runs it; gateways call it. It holds the
APNs signing key (`.p8`), accepts no text and stores nothing:

    POST /v1/push  {topic, env, token, kind, collapse, seq, reg}    strict JSON, at most 1 KB
    GET  /v1/info  {topics, version}

It builds the APNs request itself, always the same fixed localized message per kind
(`title-loc-key: push.<kind>.title`, `loc-key: push.<kind>.body`), so a stolen device token can
never carry words under the app's name; the app's notification service extension replaces the
generic text with the real event fetched from its own gateway.

Answers: 200 `{status: sent}`; 200 `{status: gone, since_ms}` (APNs 410, BadDeviceToken,
DeviceTokenNotForTopic); 200 `{status: rejected, reason}` (any other APNs 4xx); 429/503
`{status: retry, after_s}` (its own limits, APNs 429/5xx, network). A malformed request is 400
`{status: invalid, reason}` (413 past 1 KB).

Limits (in memory): 30/h and 300/day per device token, 600/h per source IP. Logs carry
`sha256(token)[:12]`, never a token. The provider JWT (ES256) is reused for at most 40 minutes.
APNs speaks HTTP/2 only: the relay needs `httpx[http2]` and `cryptography`, the
`abstractgateway[push-relay]` extra.
"""

from __future__ import annotations

import argparse
import base64
import collections
import hashlib
import json
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterable, Optional, Tuple

from fastapi import FastAPI, Request

from .settings import add_relay_arguments  # noqa: F401 - the CLI's arguments (light module)
from fastapi.responses import JSONResponse

logger = logging.getLogger("abstractgateway.push.relay")

MAX_BODY = 1024
FIELDS = ("topic", "env", "token", "kind", "collapse", "seq", "reg")
KINDS = ("approval", "question", "notify", "result", "failure")
URGENT = ("approval", "question")
ENVS = {"production": "https://api.push.apple.com", "sandbox": "https://api.sandbox.push.apple.com"}
TOKEN_RE = re.compile(r"^[0-9a-f]{64,200}$")
COLLAPSE_RE = re.compile(r"^[0-9a-f]{1,64}$")
REG_RE = re.compile(r"^[0-9a-f]{16,32}$")
TEAM_RE = re.compile(r"^[A-Z0-9]{10}$")
EXPIRATION_S = 86400
JWT_MAX_AGE_S = 40 * 60
TOKEN_PER_HOUR, TOKEN_PER_DAY, IP_PER_HOUR = 30, 300, 600
GONE_REASONS = ("BadDeviceToken", "DeviceTokenNotForTopic")
PROVIDER_TOKEN_REASONS = ("ExpiredProviderToken", "InvalidProviderToken")


class RelayRequestError(ValueError):
    """A request the relay refuses as malformed (400)."""


def token_tag(token: str) -> str:
    """What the logs may hold of a device token."""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------------------------
# The request and the APNs message
# ---------------------------------------------------------------------------------------------


def _no_duplicates(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise RelayRequestError(f"duplicate field {k!r}")
        out[k] = v
    return out


def _no_constants(name: str) -> Any:
    raise RelayRequestError(f"{name} is not JSON")


def parse_push_request(raw: bytes, *, topics: Iterable[str]) -> Dict[str, Any]:
    """The validated request, or RelayRequestError. Exactly the seven fields, nothing else."""
    if len(raw) > MAX_BODY:
        raise RelayRequestError(f"the request is larger than {MAX_BODY} bytes")
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except RelayRequestError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise RelayRequestError("the request is not JSON") from exc
    if not isinstance(body, dict):
        raise RelayRequestError("the request must be a JSON object")
    unknown = sorted(set(body) - set(FIELDS))
    if unknown:
        raise RelayRequestError(f"unknown field(s) {unknown}")
    missing = [f for f in FIELDS if f not in body]
    if missing:
        raise RelayRequestError(f"missing field(s) {missing}")
    for f in ("topic", "env", "token", "kind", "collapse", "reg"):
        if not isinstance(body[f], str):
            raise RelayRequestError(f"{f} must be a string")
    if body["topic"] not in set(topics):
        raise RelayRequestError("topic is not one of this relay's apps")
    if body["env"] not in ENVS:
        raise RelayRequestError("env must be production or sandbox")
    if not TOKEN_RE.match(body["token"]):
        raise RelayRequestError("token must be 64 to 200 lowercase hex characters")
    if body["kind"] not in KINDS:
        raise RelayRequestError(f"kind must be one of {list(KINDS)}")
    if not COLLAPSE_RE.match(body["collapse"]):
        raise RelayRequestError("collapse must be 1 to 64 lowercase hex characters")
    seq = body["seq"]
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0 or seq > 2**53:
        raise RelayRequestError("seq must be a non-negative integer")
    if not REG_RE.match(body["reg"]):
        raise RelayRequestError("reg must be 16 to 32 lowercase hex characters")
    return body


def apns_message(req: Dict[str, Any], *, now: float) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """(headers without authorization, body) of the APNs request for a validated push request."""
    kind = req["kind"]
    urgent = kind in URGENT
    headers = {
        "apns-push-type": "alert",
        "apns-topic": req["topic"],
        "apns-priority": "10" if urgent else "5",
        "apns-collapse-id": req["collapse"],
        "apns-expiration": str(int(now) + EXPIRATION_S),
    }
    body = {
        "aps": {
            "alert": {"title-loc-key": f"push.{kind}.title", "loc-key": f"push.{kind}.body"},
            "sound": "default",
            "mutable-content": 1,
            "interruption-level": "time-sensitive" if urgent else "passive",
            "thread-id": kind,
        },
        "s": int(req["seq"]),
        "r": req["reg"],
    }
    return headers, body


# ---------------------------------------------------------------------------------------------
# Provider token (ES256 JWT)
# ---------------------------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class ProviderToken:
    """The APNs provider JWT, signed with the team's `.p8` key and reused for at most 40 minutes
    (APNs refuses a refresh more often than every 20 minutes and a token older than 60)."""

    def __init__(self, *, key_pem: bytes, key_id: str, team_id: str, clock: Callable[[], float] = time.time) -> None:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        key = load_pem_private_key(key_pem, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError("the APNs key must be an ES256 (P-256) private key, the .p8 file Apple gives")
        self._key = key
        self.key_id = key_id
        self.team_id = team_id
        self.clock = clock
        self._lock = threading.Lock()
        self._cached: Optional[Tuple[str, float]] = None
        self.signed = 0

    def _sign(self, iat: int) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        header = _b64url(json.dumps({"alg": "ES256", "kid": self.key_id}, separators=(",", ":")).encode())
        claims = _b64url(json.dumps({"iss": self.team_id, "iat": iat}, separators=(",", ":")).encode())
        signing_input = f"{header}.{claims}".encode("ascii")
        r, s = decode_dss_signature(self._key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
        self.signed += 1
        return f"{header}.{claims}.{_b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"

    def get(self) -> str:
        now = float(self.clock())
        with self._lock:
            if self._cached is None or now - self._cached[1] >= JWT_MAX_AGE_S:
                iat = int(now)
                self._cached = (self._sign(iat), float(iat))
            return self._cached[0]

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None


# ---------------------------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------------------------


class SlidingLimiter:
    """In-memory sliding windows: `(window_s, max)` rules per key. Nothing on disk."""

    def __init__(self, rules: Tuple[Tuple[float, int], ...], *, clock: Callable[[], float] = time.time) -> None:
        self.rules = rules
        self.horizon = max(w for w, _ in rules)
        self.clock = clock
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()
        self._last_sweep = float(clock())

    def hit(self, key: str) -> Optional[float]:
        """Count one hit; None when allowed, else the seconds until it would be."""
        now = float(self.clock())
        with self._lock:
            if now - self._last_sweep > 600:
                self._sweep(now)
            q = self._hits.setdefault(key, collections.deque())
            while q and now - q[0] >= self.horizon:
                q.popleft()
            for window, limit in self.rules:
                recent = [t for t in q if now - t < window]
                if len(recent) >= limit:
                    return max(1.0, window - (now - recent[0]))
            q.append(now)
            return None

    def _sweep(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] >= self.horizon]:
            self._hits.pop(k, None)
        self._last_sweep = now


# ---------------------------------------------------------------------------------------------
# The app
# ---------------------------------------------------------------------------------------------


@dataclass
class RelayConfig:
    key_pem: bytes
    key_id: str
    team_id: str
    topics: Tuple[str, ...]
    version: str = field(default_factory=lambda: _gateway_version())


def _gateway_version() -> str:
    try:
        import abstractgateway

        return str(getattr(abstractgateway, "__version__", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def build_apns_client() -> Any:
    """The HTTP/2 client APNs requires. Raises RuntimeError naming the extra when `h2` is missing."""
    try:
        import h2  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "the push relay talks to Apple over HTTP/2, which needs the `h2` package: "
            "pip install 'abstractgateway[push-relay]'"
        ) from exc
    import httpx

    return httpx.AsyncClient(http2=True, timeout=httpx.Timeout(15.0, connect=10.0))


def build_relay_app(config: RelayConfig, *, apns_client: Any = None, clock: Callable[[], float] = time.time) -> Any:
    """The relay's ASGI app. `apns_client`: an httpx.AsyncClient (HTTP/2 in production, a fake in tests)."""
    jwt = ProviderToken(key_pem=config.key_pem, key_id=config.key_id, team_id=config.team_id, clock=clock)
    per_token = SlidingLimiter(((3600.0, TOKEN_PER_HOUR), (86400.0, TOKEN_PER_DAY)), clock=clock)
    per_ip = SlidingLimiter(((3600.0, IP_PER_HOUR),), clock=clock)
    state: Dict[str, Any] = {"client": apns_client}

    import contextlib

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Any):
        try:
            yield
        finally:
            client = state.get("client")
            if client is not None and hasattr(client, "aclose"):
                await client.aclose()

    app = FastAPI(title="AbstractGateway push relay", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    app.state.provider_token = jwt

    def _client() -> Any:
        if state["client"] is None:
            state["client"] = build_apns_client()
        return state["client"]

    def _answer(status_code: int, payload: Dict[str, Any]) -> Any:
        return JSONResponse(payload, status_code=status_code)

    @app.get("/v1/info")
    async def info() -> Dict[str, Any]:
        return {"topics": list(config.topics), "version": config.version}

    @app.post("/v1/push")
    async def push(request: Request) -> Any:
        ip = request.client.host if request.client else "unknown"
        wait = per_ip.hit(ip)
        if wait is not None:
            return _answer(429, {"status": "retry", "after_s": int(wait)})
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY):
            return _answer(413, {"status": "invalid", "reason": f"the request is larger than {MAX_BODY} bytes"})
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_BODY:
                return _answer(413, {"status": "invalid", "reason": f"the request is larger than {MAX_BODY} bytes"})
        try:
            req = parse_push_request(raw, topics=config.topics)
        except RelayRequestError as exc:
            return _answer(400, {"status": "invalid", "reason": str(exc)})
        tag = token_tag(req["token"])
        wait = per_token.hit(req["token"])
        if wait is not None:
            logger.info("push token=%s kind=%s limited", tag, req["kind"])
            return _answer(429, {"status": "retry", "after_s": int(wait)})
        headers, body = apns_message(req, now=float(clock()))
        url = f"{ENVS[req['env']]}/3/device/{req['token']}"
        for attempt in (1, 2):
            headers["authorization"] = f"bearer {jwt.get()}"
            try:
                resp = await _client().post(url, headers=headers, content=json.dumps(body, separators=(",", ":")).encode())
            except Exception as exc:  # noqa: BLE001 - network: the gateway retries
                logger.warning("push token=%s kind=%s network %s", tag, req["kind"], type(exc).__name__)
                return _answer(503, {"status": "retry", "after_s": 30})
            reason = ""
            try:
                detail = resp.json() if resp.content else {}
            except ValueError:
                detail = {}
            if isinstance(detail, dict):
                reason = str(detail.get("reason") or "")
            if resp.status_code == 403 and reason in PROVIDER_TOKEN_REASONS and attempt == 1:
                jwt.invalidate()
                continue
            break
        code = resp.status_code
        logger.info("push token=%s kind=%s apns=%s %s", tag, req["kind"], code, reason)
        if code == 200:
            return _answer(200, {"status": "sent"})
        if code == 410:
            since = detail.get("timestamp") if isinstance(detail, dict) else None
            since_ms = int(since) if isinstance(since, (int, float)) and not isinstance(since, bool) else int(float(clock()) * 1000)
            return _answer(200, {"status": "gone", "since_ms": since_ms})
        if code == 400 and reason in GONE_REASONS:
            return _answer(200, {"status": "gone", "since_ms": int(float(clock()) * 1000)})
        if code == 429:
            return _answer(429, {"status": "retry", "after_s": 60})
        if code >= 500 or (code == 403 and reason in PROVIDER_TOKEN_REASONS):
            return _answer(503, {"status": "retry", "after_s": 30})
        return _answer(200, {"status": "rejected", "reason": reason or f"APNs {code}"})

    return app


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def _listen(value: str) -> Tuple[str, int]:
    host, sep, port = str(value).rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"--listen takes HOST:PORT, not {value!r}")
    return (host.strip("[]") or "127.0.0.1"), int(port)


def load_config(args: argparse.Namespace) -> RelayConfig:
    key_pem = Path(args.apns_key_file).expanduser().read_bytes()
    key_id = str(args.key_id).strip()
    team_id = str(args.team_id).strip()
    if not TEAM_RE.match(key_id):
        raise ValueError("--key-id is the 10-character Key ID Apple shows for the key")
    if not TEAM_RE.match(team_id):
        raise ValueError("--team-id is your 10-character Apple Team ID")
    topics = tuple(dict.fromkeys(t.strip() for t in args.topic if t.strip()))
    if not topics:
        raise ValueError("give at least one --topic (an app's bundle id)")
    return RelayConfig(key_pem=key_pem, key_id=key_id, team_id=team_id, topics=topics)


def run_relay_command(args: argparse.Namespace) -> int:
    try:
        config = load_config(args)
        host, port = _listen(args.listen)
        client = build_apns_client()
        app = build_relay_app(config, apns_client=client)
        ProviderToken(key_pem=config.key_pem, key_id=config.key_id, team_id=config.team_id).get()  # the key signs
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] push-relay: {exc}", file=sys.stderr, flush=True)
        return 2
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(f"[INFO] push-relay: listening on {host}:{port} for {', '.join(config.topics)}", file=sys.stderr, flush=True)
    uvicorn.run(app, host=host, port=port, proxy_headers=True, forwarded_allow_ips=args.forwarded_allow_ips, log_level="warning", access_log=False)
    return 0


__all__ = [
    "FIELDS",
    "KINDS",
    "ProviderToken",
    "RelayConfig",
    "RelayRequestError",
    "SlidingLimiter",
    "add_relay_arguments",
    "apns_message",
    "build_apns_client",
    "build_relay_app",
    "parse_push_request",
    "run_relay_command",
    "token_tag",
]
