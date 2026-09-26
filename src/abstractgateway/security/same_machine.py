"""Is this HTTP request coming from the gateway machine itself? (mission HH, 2026-09-24)

One rule, used wherever "the person at the gateway's keyboard" gets a default
the rest of the network does not (installing apps and engines on this host,
opening a folder on this host, the workspace routes' `caller_is_this_machine`):

    the caller's address -- the socket peer, or, when that peer is this
    machine's loopback, the address that local proxy wrote in
    X-Forwarded-For -- is loopback or one of this host's own addresses.

The derivation is uvicorn's own (`serve` pins `forwarded_allow_ips` to the
loopback addresses, whatever the environment says): X-Forwarded-For is only
believed from a loopback peer, i.e. from an app-server proxy running on this
machine (the AbstractCode web server, the abstractuic app-server), which
OVERWRITES the header with the real socket address of the browser it serves.
`effective_peer` applies the same derivation again, so the rule holds under
any ASGI server and is idempotent on a request uvicorn already rewrote. So:
- a LAN browser through the local proxy -> its LAN address -> not this machine;
- a browser on this machine through the proxy -> loopback -> this machine;
- X-Forwarded-For sent by a non-loopback peer -> ignored (the peer counts).

A request that carries OTHER proxy headers (Forwarded, X-Forwarded-Host,
X-Real-IP) but no X-Forwarded-For went through a proxy this rule cannot read,
so it is never "this machine".

Requests relayed by an app-server proxy carry its marker header
`X-AbstractFramework-App-Proxy: <app id>` (the AbstractCode web server and the
abstractuic app-server send it). Two fail-safes key on that marker (never on
the session header, which native clients such as the Assistant send
directly):
- a marked request from loopback WITHOUT X-Forwarded-For is never "this
  machine" (a proxy that dropped the header would make every browser local);
- while the gateway trusts a reverse proxy (the stored network setting
  `trust_proxy`; the legacy launch environment only when nothing is stored),
  a marked request is never "this machine".
A forwarded header (X-Forwarded-For, Forwarded, X-Forwarded-Host, X-Real-IP)
sent by a peer that is NOT loopback is never "this machine" either: that
peer is an untrusted proxy (a reverse proxy on this host reaching the
gateway through its LAN address would otherwise make every visitor local).
"""

from __future__ import annotations

import ipaddress
import threading
import time
from typing import Any, Callable, FrozenSet, Iterable, Optional, Tuple

PROXY_HEADERS: Tuple[str, ...] = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")
OWN_ADDRESSES_TTL_S = 10.0

_cache_lock = threading.Lock()
_cache: Optional[Tuple[float, FrozenSet[str]]] = None


def _normalize(addr: Any) -> str:
    raw = str(addr or "").strip().strip("[]").lower()
    if raw.startswith("::ffff:"):
        raw = raw[len("::ffff:"):]
    return raw.split("%", 1)[0]


def _discover_own_addresses() -> FrozenSet[str]:
    from ..network_exposure import discover_interfaces

    ifaces, _method = discover_interfaces()
    return frozenset(_normalize(i.address) for i in ifaces if i.address)


def own_addresses(*, discover: Optional[Callable[[], Iterable[str]]] = None, now: Optional[float] = None) -> FrozenSet[str]:
    """Every address of this host's interfaces (cached OWN_ADDRESSES_TTL_S).
    `discover` replaces the discovery (tests)."""
    global _cache
    if discover is not None:
        return frozenset(_normalize(a) for a in discover())
    t = time.monotonic() if now is None else now
    with _cache_lock:
        if _cache is not None and t - _cache[0] < OWN_ADDRESSES_TTL_S:
            return _cache[1]
    try:
        found = _discover_own_addresses()
    except Exception:  # noqa: BLE001 - no discovery: only loopback counts
        found = frozenset()
    with _cache_lock:
        _cache = (t, found)
    return found


def peer_is_this_machine(peer: Any, *, addresses: Optional[Iterable[str]] = None) -> bool:
    """The socket peer is loopback or one of this host's own addresses."""
    host = _normalize(peer)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # "testclient", a hostname, nothing: never this machine
    if ip.is_loopback:
        return True
    if ip.is_unspecified or ip.is_multicast:
        return False
    own = frozenset(_normalize(a) for a in addresses) if addresses is not None else own_addresses()
    return str(ip) in own


# uvicorn's `forwarded_allow_ips` for `serve` (cli.py passes it explicitly):
# X-Forwarded-For is believed only from these peers.
TRUSTED_PROXY_PEERS: Tuple[str, ...] = ("127.0.0.1", "::1")


def has_proxy_headers(headers: Any) -> bool:
    return any(headers.get(h) for h in PROXY_HEADERS)


def _is_loopback(addr: Any) -> bool:
    try:
        return ipaddress.ip_address(_normalize(addr)).is_loopback
    except ValueError:
        return False


def effective_peer(request: Any) -> Optional[str]:
    """The caller's address, derived like uvicorn's proxy-headers middleware
    with `forwarded_allow_ips` = loopback: the socket peer, unless that peer
    is loopback and sent X-Forwarded-For, in which case the right-most
    X-Forwarded-For entry that is not loopback (all loopback: the left-most).
    None = a proxy this rule cannot read (other proxy headers only)."""
    client = getattr(request, "client", None)
    peer = str(getattr(client, "host", "") or "") if client is not None else ""
    headers = request.headers
    xff = str(headers.get("x-forwarded-for") or "").strip()
    if has_proxy_headers(headers) and not _is_loopback(peer):
        return None  # forwarded by an untrusted peer: never read as local
    if xff:
        hosts = [h.strip() for h in xff.split(",") if h.strip()]
        for host in reversed(hosts):
            if not _is_loopback(host):
                return host
        return hosts[0] if hosts else peer
    if has_proxy_headers(headers):
        return None
    return peer


APP_PROXY_HEADER = "x-abstractframework-app-proxy"


def trust_proxy_mode() -> bool:
    """The gateway trusts a reverse proxy's client address: the STORED
    network setting `trust_proxy` first; the legacy launch environment
    (ABSTRACTGATEWAY_TRUST_PROXY) only when nothing is stored."""
    import os

    try:
        from ..runtime_config import _read_store
        from ..users import gateway_data_dir_from_env

        net = _read_store(gateway_data_dir_from_env()).get("network")
        if isinstance(net, dict) and isinstance(net.get("trust_proxy"), bool):
            return bool(net["trust_proxy"])
    except Exception:  # noqa: BLE001 - unreadable store: the legacy rung, then off
        pass
    raw = str(os.getenv("ABSTRACTGATEWAY_TRUST_PROXY") or os.getenv("ABSTRACTFLOW_GATEWAY_TRUST_PROXY") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def request_is_from_this_machine(
    request: Any, *, addresses: Optional[Iterable[str]] = None, trust_proxy: Optional[bool] = None
) -> bool:
    """The caller sits at the gateway machine (module docstring)."""
    headers = request.headers
    if str(headers.get(APP_PROXY_HEADER) or "").strip():
        if not str(headers.get("x-forwarded-for") or "").strip():
            return False
        if trust_proxy_mode() if trust_proxy is None else trust_proxy:
            return False
    peer = effective_peer(request)
    if peer is None:
        return False
    return peer_is_this_machine(peer, addresses=addresses)
