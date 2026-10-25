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
    "WebhookRateLimiter",
    "locate_connection",
    "principal_key",
    "store_for_data_dir",
    "store_for_service",
    "webhook_rate_limiter",
    "wire_runtime_connections",
]
