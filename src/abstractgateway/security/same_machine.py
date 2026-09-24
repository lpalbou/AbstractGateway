"""Is this HTTP request coming from the gateway machine itself? (mission HH, 2026-09-24)

One test, used wherever "the person at the gateway's keyboard" gets a default
the rest of the network does not (installing apps and engines on this host):

    peer address is loopback, OR one of this host's own interface addresses
    AND the request carries no proxy header (Forwarded, X-Forwarded-For,
    X-Forwarded-Host, X-Real-IP).

Why a peer equal to one of this host's own addresses is this host: the peer
is the source address of an ESTABLISHED TCP connection. A remote machine that
writes this host's address as its source cannot complete the handshake (the
SYN-ACK goes to this host, not to it), and the kernel does not accept a packet
that arrives from outside with one of its own addresses as source. So a
browser on the gateway machine that uses the LAN address
(`http://192.168.1.175:8080`) is recognised, and nobody else can pass as it.

Why the proxy headers matter: a reverse proxy on this same host connects from
loopback (or from this host's own address) on behalf of a REMOTE visitor; by
peer alone every visitor would look local. A request that went through a proxy
is therefore never "this machine" (and uvicorn's own proxy-header rewriting
keeps the header on the request, so a rewritten peer is refused here too).

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


def has_proxy_headers(headers: Any) -> bool:
    return any(headers.get(h) for h in PROXY_HEADERS)


def request_is_from_this_machine(request: Any, *, addresses: Optional[Iterable[str]] = None) -> bool:
    """The caller sits at the gateway machine: a loopback or own-address peer,
    and no proxy in between (module docstring)."""
    client = getattr(request, "client", None)
    peer = getattr(client, "host", "") if client is not None else ""
    if has_proxy_headers(request.headers):
        return False
    return peer_is_this_machine(peer, addresses=addresses)
