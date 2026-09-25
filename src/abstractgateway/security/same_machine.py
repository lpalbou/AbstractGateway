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

Two fail-safes for requests relayed by an app-server (they carry the
app-server session header, `x-abstractgateway-session`):
- from loopback WITHOUT X-Forwarded-For: an older app-server that drops the
  header would make every LAN browser look local, so it is never "this
  machine";
- while the gateway trusts a reverse proxy (network setting `trust_proxy`),
  an app-server request is never "this machine": only direct requests are
  decided on the derived peer.

Why a peer equal to one of this host's own addresses is this host: the peer
is the source address of an ESTABLISHED TCP connection. A remote machine that
writes this host's address as its source cannot complete the handshake (the
SYN-ACK goes to this host, not to it), and the kernel does not accept a packet
that arrives from outside with one of its own addresses as source. So a
browser on the gateway machine that uses the LAN address
(`http://192.168.1.175:8080`) is recognised, and nobody else can pass as it.

The interface list comes from `network_exposure.discover_interfaces()` (the
same discovery the network status uses), cached for a few seconds.
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
    if xff:
        if not _is_loopback(peer):
            return peer  # a spoofed header from a non-loopback peer is ignored
        hosts = [h.strip() for h in xff.split(",") if h.strip()]
        for host in reversed(hosts):
            if not _is_loopback(host):
                return host
        return hosts[0] if hosts else peer
    if has_proxy_headers(headers):
        return None
    return peer


def _session_header() -> str:
    try:
        from .sessions import gateway_session_header_name

        return gateway_session_header_name()
    except Exception:  # noqa: BLE001
        return "x-abstractgateway-session"


def trust_proxy_mode() -> bool:
    """The gateway trusts a reverse proxy's client address (network setting
    `trust_proxy`, or its legacy launch environment)."""
    import os

    raw = str(os.getenv("ABSTRACTGATEWAY_TRUST_PROXY") or os.getenv("ABSTRACTFLOW_GATEWAY_TRUST_PROXY") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    try:
        from ..network_exposure import live_reverse_proxy

        return live_reverse_proxy().trust_proxy is True
    except Exception:  # noqa: BLE001 - unreadable settings: the strict answer below stays safe
        return False


def request_is_from_this_machine(
    request: Any, *, addresses: Optional[Iterable[str]] = None, trust_proxy: Optional[bool] = None
) -> bool:
    """The caller sits at the gateway machine (module docstring)."""
    headers = request.headers
    via_app_server = bool(headers.get(_session_header()))
    if via_app_server:
        if not str(headers.get("x-forwarded-for") or "").strip():
            return False
        if trust_proxy_mode() if trust_proxy is None else trust_proxy:
            return False
    peer = effective_peer(request)
    if peer is None:
        return False
    return peer_is_this_machine(peer, addresses=addresses)
