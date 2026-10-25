"""Trigger connections on the gateway: the per-plane store, the webhook ingress (framework
backlog 1041/1042).

A trigger CONNECTION is one user's instance of a trigger source (a webhook endpoint and its
signing secret, a feed URL); AbstractRuntime owns the record and its sealed secrets
(`abstractruntime.triggers.connections.TriggerConnectionStore`). The gateway owns:

- where each principal's store lives: the principal's runtime data folder (the service's
  `config.data_dir`, beside its `event_inbox/`), secrets sealed with the data folder's key
  (`<data_dir>/secrets/sealing.key`, security/sealing.py) — `store_for_service`;
- the owner string of a principal's connections, `"<tenant>:<user>"` (`principal_key`);
- the wiring of a built runtime to its plane's store (`wire_runtime_connections`, so automation
  creation can check a binding's `config.connection_id`);
- the PUBLIC webhook ingress: `POST /api/gateway/triggers/webhooks/{connection_id}` carries no
  bearer token (the Standard Webhooks signature with the connection's secret is the auth), so
  the connection is found by id across the planes (`locate_connection`), every request is rate
  limited per connection and per client address (`WebhookRateLimiter`) and its body capped
  (`WEBHOOK_MAX_BODY_BYTES`).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, Iterator, List, Optional, Tuple

from abstractruntime.triggers.connections import (
    CONNECTION_ID_RE,
    TriggerConnection,
    TriggerConnectionStore,
    connection_principal,
)

from .security.principal import GatewayPrincipal, local_admin_principal

# The largest webhook body accepted (bytes): above it, 413 before anything is read further.
WEBHOOK_MAX_BODY_BYTES = 512 * 1024
# Requests per window per connection, and per client address across connections.
WEBHOOK_RATE_PER_CONNECTION = 60
WEBHOOK_RATE_PER_ADDRESS = 120
WEBHOOK_RATE_WINDOW_S = 60.0
# Standard Webhooks: a timestamp more than this many seconds from now is stale (replay window).
WEBHOOK_TOLERANCE_S = 300


def principal_key(principal: GatewayPrincipal) -> str:
    """The owner string of `principal`'s connections: `"<tenant>:<user>"`."""
    return connection_principal(principal.tenant_id or "default", principal.user_id)


def _data_root() -> Path:
    from .users import gateway_data_dir_from_env

    return Path(gateway_data_dir_from_env())


def store_for_data_dir(data_dir: Path) -> TriggerConnectionStore:
    """The trigger connections of the runtime plane whose data folder is `data_dir`."""
    from .security.sealing import sealing_key_path

    # The vault makes the key on the first secret it seals (nothing is created here).
    return TriggerConnectionStore(Path(data_dir), sealing_key_file=sealing_key_path(_data_root()))


def store_for_service(svc: Any) -> TriggerConnectionStore:
    """The trigger connections of a principal's service (its runtime data folder)."""
    return store_for_data_dir(Path(svc.config.data_dir))


def wire_runtime_connections(runtime: Any, data_dir: Path) -> None:
    """Give a built runtime its plane's connection store (automation create/revise checks a
    binding's `config.connection_id` against it). A runtime without the seam fails loudly."""
    runtime.set_trigger_connection_store(store_for_data_dir(Path(data_dir)))


# ---------------------------------------------------------------------------------------
# Finding a connection by id (the public ingress has no principal)
# ---------------------------------------------------------------------------------------


def _candidate_planes() -> Iterator[Tuple[GatewayPrincipal, Path]]:
    """Every (principal, runtime data folder) a connection may live in: the default runtime's
    (the operator), then each active human account's when user accounts are on."""
    from .service import _config_for_principal, gateway_multi_user_enabled

    if not gateway_multi_user_enabled():
        principal = local_admin_principal()
        yield principal, Path(_config_for_principal(principal).data_dir)
        return
    from .users import GatewayUserRegistry

    seen: set = set()
    principals: List[GatewayPrincipal] = [local_admin_principal()]
    for rec in GatewayUserRegistry().list_users():
        if rec.archived or not rec.enabled or rec.principal_kind == "entity":
            continue
        principals.append(rec.to_principal())
    for principal in principals:
        data_dir = Path(_config_for_principal(principal).data_dir).resolve()
        if data_dir in seen:
            continue
        seen.add(data_dir)
        yield principal, data_dir


@dataclass(frozen=True)
class LocatedConnection:
    principal: GatewayPrincipal
    data_dir: Path
    store: TriggerConnectionStore
    connection: TriggerConnection


def locate_connection(connection_id: str) -> Optional[LocatedConnection]:
    """The connection `connection_id` wherever it lives, or None (unknown, malformed, or in an
    archived / disabled account's plane). One `stat` per plane, nothing is created."""
    if not isinstance(connection_id, str) or not CONNECTION_ID_RE.match(connection_id):
        return None
    for principal, data_dir in _candidate_planes():
        if not (data_dir / "triggers" / "connections" / f"{connection_id}.json").is_file():
            continue
        store = store_for_data_dir(data_dir)
        conn = store.get(connection_id)
        if conn is not None:
            return LocatedConnection(principal=principal, data_dir=data_dir, store=store, connection=conn)
    return None


# ---------------------------------------------------------------------------------------
# Standard Webhooks verification and the append (STUB until AbstractRuntime ships
# `verify_standard_webhook` / `append_webhook_event` in `abstractruntime.triggers`; the gateway
# then calls those and this block goes)
# ---------------------------------------------------------------------------------------


class WebhookRefused(Exception):
    """A refused webhook request: HTTP `status`, wire `reason_code`, a sentence."""

    def __init__(self, status: int, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.reason_code = reason_code

    def to_detail(self) -> Dict[str, Any]:
        return {"reason_code": self.reason_code, "message": str(self)}


def _secret_key(secret: str) -> bytes:
    import base64

    raw = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    try:
        return base64.b64decode(raw, validate=True)
    except Exception:  # noqa: BLE001 - a non-base64 secret is used as its bytes
        return raw.encode("utf-8")


def verify_standard_webhook(*, secret: str, headers: Any, body: bytes, now: Optional[float] = None, tolerance_s: int = WEBHOOK_TOLERANCE_S) -> Tuple[str, int]:
    """Standard Webhooks (v1, HMAC-SHA256): `webhook-signature` is one or more space-separated
    `v1,<base64 HMAC(secret, "<webhook-id>.<webhook-timestamp>.<body>")>`. Returns
    `(webhook_id, timestamp)`. Raises WebhookRefused: 401 `webhook_unsigned` (a header missing),
    401 `webhook_signature_invalid`, 409 `webhook_stale` (timestamp outside the window)."""
    import base64
    import hashlib
    import hmac

    wid = str(headers.get("webhook-id") or "").strip()
    ts_raw = str(headers.get("webhook-timestamp") or "").strip()
    sig_raw = str(headers.get("webhook-signature") or "").strip()
    if not wid or not ts_raw or not sig_raw:
        raise WebhookRefused(401, "webhook_unsigned", "webhook-id, webhook-timestamp and webhook-signature are required.")
    try:
        ts = int(ts_raw)
    except ValueError:
        raise WebhookRefused(401, "webhook_signature_invalid", "webhook-timestamp must be Unix seconds.") from None
    signed = wid.encode("utf-8") + b"." + ts_raw.encode("utf-8") + b"." + bytes(body)
    expected = base64.b64encode(hmac.new(_secret_key(secret), signed, hashlib.sha256).digest()).decode("ascii")
    ok = False
    for part in sig_raw.split():
        version, _, value = part.partition(",")
        if version == "v1" and hmac.compare_digest(value.strip(), expected):
            ok = True
    if not ok:
        raise WebhookRefused(401, "webhook_signature_invalid", "The webhook signature does not match this connection's secret.")
    at = time.time() if now is None else float(now)
    if abs(at - ts) > int(tolerance_s):
        raise WebhookRefused(409, "webhook_stale", f"webhook-timestamp is more than {int(tolerance_s)} s from the gateway's clock.")
    return wid, ts


def webhook_event_data(body: bytes, content_type: str) -> Dict[str, Any]:
    """The CloudEvents `data` of a webhook body: the JSON object itself, other JSON values as
    `{"body": value}`, anything else as `{"body": <text>, "content_type": ...}`."""
    import json

    text = bytes(body).decode("utf-8", errors="replace")
    if "json" in str(content_type or "").lower() or text.lstrip()[:1] in ("{", "["):
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        else:
            return dict(value) if isinstance(value, dict) else {"body": value}
    return {"body": text, "content_type": str(content_type or "") or None}


def append_webhook_event(inbox: Any, located: "LocatedConnection", *, webhook_id: str, timestamp: int, body: bytes, content_type: str) -> Any:
    """Append the webhook to the connection's stream (deduplicated on the webhook id).
    Returns the inbox AppendResult, or None when the id was already appended."""
    import datetime as _dt

    from abstractruntime.triggers.connections import append_connection_event

    return append_connection_event(
        inbox,
        located.connection,
        event_id=webhook_id,
        data=webhook_event_data(body, content_type),
        time=_dt.datetime.fromtimestamp(int(timestamp), tz=_dt.timezone.utc).isoformat(),
        subject=webhook_id,
        dedupekey=webhook_id,
        trust="untrusted",
        store=located.store,
    )


# ---------------------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------------------


class WebhookRateLimiter:
    """Sliding-window counters per connection id and per client address (in memory)."""

    def __init__(
        self,
        *,
        per_connection: int = WEBHOOK_RATE_PER_CONNECTION,
        per_address: int = WEBHOOK_RATE_PER_ADDRESS,
        window_s: float = WEBHOOK_RATE_WINDOW_S,
    ) -> None:
        self.per_connection = int(per_connection)
        self.per_address = int(per_address)
        self.window_s = float(window_s)
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def _take(self, key: str, limit: int, now: float) -> Optional[float]:
        q = self._hits.setdefault(key, deque())
        while q and q[0] <= now - self.window_s:
            q.popleft()
        if len(q) >= limit:
            return max(0.0, q[0] + self.window_s - now)
        q.append(now)
        return None

    def check(self, *, connection_id: str, address: str, now: Optional[float] = None) -> Optional[int]:
        """None when allowed (and counted), else the seconds to wait (Retry-After)."""
        at = time.monotonic() if now is None else float(now)
        with self._lock:
            if len(self._hits) > 10_000:
                self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > at - self.window_s}
            for key, limit in ((f"addr:{address}", self.per_address), (f"conn:{connection_id}", self.per_connection)):
                wait = self._take(key, limit, at)
                if wait is not None:
                    return max(1, int(wait + 0.999))
        return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


webhook_rate_limiter = WebhookRateLimiter()


__all__ = [
    "LocatedConnection",
    "WEBHOOK_MAX_BODY_BYTES",
    "WEBHOOK_RATE_PER_ADDRESS",
    "WEBHOOK_RATE_PER_CONNECTION",
    "WEBHOOK_RATE_WINDOW_S",
    "WEBHOOK_TOLERANCE_S",
    "WebhookRateLimiter",
    "WebhookRefused",
    "append_webhook_event",
    "verify_standard_webhook",
    "webhook_event_data",
    "locate_connection",
    "principal_key",
    "store_for_data_dir",
    "store_for_service",
    "webhook_rate_limiter",
    "wire_runtime_connections",
]
