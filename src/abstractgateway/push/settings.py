"""The push relay flag of THIS process: `abstractgateway serve|runner --push-relay <URL|off>`.

A CLI flag only (operator rule: new switches are flags, never environment variables). The packaged
default is `off` until the publisher's relay is live; from the release in which it is, the default
becomes its URL (`PUBLISHER_RELAY_URL`). `off` (or no flag) means: no dispatcher thread, no outbound
call, ever.
"""

from __future__ import annotations

import threading
from typing import Any, Optional
from urllib.parse import urlsplit

# The publisher's relay, once it is hosted (backlog 1049, operator decision). None = not live yet,
# so the packaged default stays `off`.
PUBLISHER_RELAY_URL: Optional[str] = None
PACKAGED_DEFAULT = PUBLISHER_RELAY_URL or "off"

FLAG_HELP = (
    "Where this gateway sends Apple push requests for the watch and iPhone apps: the URL of a push relay "
    "(`abstractgateway push-relay`), or off. Default: off (the packaged default becomes the publisher's relay "
    "once it is live). People configure nothing: an app registers itself at sign-in. The gateway calls the relay "
    "only once a device has registered, and sends it a device token, the app, an alert kind and a sequence number, "
    "never text. With --no-runner, give it to the `abstractgateway runner` process too (that process sends). "
    "There is no environment variable."
)

_LOCK = threading.Lock()
_relay_url: Optional[str] = None
_source: Optional[str] = None


def parse_relay_value(value: Optional[str]) -> Optional[str]:
    """`off`/empty -> None; an http(s) URL -> that URL without a trailing slash. Raises ValueError."""
    raw = str(value or "").strip()
    if not raw or raw.lower() == "off":
        return None
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"--push-relay takes an http(s) URL or off, not {raw!r}")
    if parts.query or parts.fragment:
        raise ValueError("--push-relay takes a base URL without a query or fragment")
    return raw.rstrip("/")


def set_push_relay(value: Optional[str], *, source: str) -> Optional[str]:
    """Record the flag for this process (serve/runner main, before the boot). Returns the URL or None."""
    global _relay_url, _source
    url = parse_relay_value(value if value is not None else PACKAGED_DEFAULT)
    with _LOCK:
        _relay_url = url
        _source = str(source)
    return url


def push_relay_url() -> Optional[str]:
    """The relay this process sends to, or None (off)."""
    with _LOCK:
        return _relay_url


def push_relay_source() -> Optional[str]:
    with _LOCK:
        return _source


def add_relay_arguments(p: Any) -> None:
    """The arguments of `abstractgateway push-relay` (here, so the CLI parser does not import the relay)."""
    p.add_argument("--apns-key-file", required=True, metavar="PATH", help="The APNs authentication key (.p8) from your Apple developer account.")
    p.add_argument("--key-id", required=True, help="The key's 10-character Key ID.")
    p.add_argument("--team-id", required=True, help="Your 10-character Apple Team ID.")
    p.add_argument(
        "--topic", required=True, action="extend", nargs="+", metavar="BUNDLE_ID",
        help="The apps this relay pushes to (their bundle ids); any other topic is refused.",
    )
    p.add_argument("--listen", default="127.0.0.1:8095", metavar="HOST:PORT", help="Where to listen (default 127.0.0.1:8095; put TLS in front).")
    p.add_argument(
        "--forwarded-allow-ips", default="127.0.0.1", metavar="IPS",
        help="Proxies trusted for X-Forwarded-For, so per-IP limits see the caller (default 127.0.0.1).",
    )


def _reset_for_tests() -> None:
    global _relay_url, _source
    with _LOCK:
        _relay_url, _source = None, None


__all__ = [
    "FLAG_HELP",
    "add_relay_arguments",
    "PACKAGED_DEFAULT",
    "PUBLISHER_RELAY_URL",
    "parse_relay_value",
    "push_relay_source",
    "push_relay_url",
    "set_push_relay",
]
