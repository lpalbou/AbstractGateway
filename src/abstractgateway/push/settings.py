"""Where this gateway sends Apple push requests: the push forwarder (relay) address.

Three layers, first match wins (operator rule: nobody configures push; the address is built in, a
self-hoster may point at their own forwarder):

1. `abstractgateway serve|runner --push-relay <URL|off>`: this run only (a CLI flag, never an
   environment variable);
2. the saved admin setting "Push forwarder address" (`<gateway data dir>/push/settings.json`,
   `{forwarder: {mode: default|off|url, url}}`; `default` falls through; nothing in it is secret);
3. the packaged default, `PUBLISHER_RELAY_URL`: None until the publisher's forwarder is live, so
   `default` means off today.

`effective_forwarder(root)` is cheap (one stat; the file is re-read only when it moved): the
dispatcher calls it at every pass, so a saved change takes effect without a restart, in the serve
process (the PUT also wakes its dispatchers) and in a split runner (its 2 s poll sees the file move).
"""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

# The publisher's relay, once it is hosted (backlog 1049, operator decision). None = not live yet,
# so the packaged default stays `off`.
PUBLISHER_RELAY_URL: Optional[str] = None
PACKAGED_DEFAULT = PUBLISHER_RELAY_URL or "off"

FORWARDER_MODES = ("default", "off", "url")
SETTINGS_FILE = "settings.json"
SETTINGS_SCHEMA = 1
PROBE_TIMEOUT_S = 5.0

FLAG_HELP = (
    "Where this gateway sends Apple push requests for the watch and iPhone apps, for this run: the URL of a push "
    "forwarder (`abstractgateway push-relay`), or off. Without the flag: the console's saved Push forwarder setting, "
    "else the built-in forwarder (none in this release yet, so off). People configure nothing: an app registers itself "
    "at sign-in. The gateway calls the forwarder only once a device has registered, and sends it a device token, the "
    "app, an alert kind and a sequence number, never text. With --no-runner, give it to the `abstractgateway runner` "
    "process too (that process sends). There is no environment variable."
)

_LOCK = threading.Lock()
_relay_url: Optional[str] = None
_source: Optional[str] = None
_flag_given = False
_saved_cache: Dict[str, Tuple[Tuple[int, int], Dict[str, Optional[str]]]] = {}


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
    """Record the `--push-relay` flag of this process (serve/runner main, before the boot); None =
    no flag (the saved setting, then the packaged default, decide). Returns the flag's URL, or the
    packaged default's when no flag was given (None = off)."""
    global _relay_url, _source, _flag_given
    given = value is not None
    url = parse_relay_value(value if given else PACKAGED_DEFAULT)
    with _LOCK:
        _relay_url = url if given else None
        _flag_given = given
        _source = str(source)
    return url


def flag_relay() -> Tuple[bool, Optional[str]]:
    """(the flag was given, its URL or None for `off`)."""
    with _LOCK:
        return _flag_given, _relay_url


def push_relay_url(root_dir: Optional[Path] = None) -> Optional[str]:
    """The forwarder this process sends to, or None (off). With `root_dir` (the gateway's root data
    dir) the saved setting takes part; without it only the flag and the packaged default do."""
    return effective_forwarder(root_dir)[0]


def push_relay_source() -> Optional[str]:
    """The process that recorded the flag (`serve`/`runner`)."""
    with _LOCK:
        return _source


# -- the saved setting ---------------------------------------------------------------------------


def settings_path(root_dir: Path) -> Path:
    return Path(root_dir) / "push" / SETTINGS_FILE


def _is_loopback(host: str) -> bool:
    h = str(host or "").strip().lower()
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def parse_forwarder_url(value: Any) -> str:
    """A saved forwarder address: https (http only for a loopback host such as 127.0.0.1), a host,
    no credentials, query or fragment. Returns it without a trailing slash; raises ValueError."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Give the forwarder's address, e.g. https://push.example.org.")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"The forwarder's address is an https:// URL, not {raw!r}.")
    if parts.username or parts.password:
        raise ValueError("The forwarder's address carries no user name or password.")
    if parts.query or parts.fragment:
        raise ValueError("The forwarder's address is a base URL, without ? or #.")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError(f"The forwarder's address has an invalid port ({exc}).") from exc
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise ValueError(
            "Use https:// for a forwarder on another machine (http:// only on this computer: 127.0.0.1, ::1, localhost)."
        )
    return raw.rstrip("/")


def _stat_token(path: Path) -> Tuple[int, int]:
    try:
        st = os.stat(path)
    except OSError:
        return (0, -1)
    return (int(st.st_mtime_ns), int(st.st_size))


def settings_change_token(root_dir: Optional[Path]) -> Tuple[int, int]:
    """(mtime_ns, size) of the settings file: one stat, moves on every save (any process)."""
    return (0, -1) if root_dir is None else _stat_token(settings_path(Path(root_dir)))


def saved_forwarder(root_dir: Optional[Path]) -> Dict[str, Optional[str]]:
    """`{mode: default|off|url, url}` from the settings file (default when absent or unreadable)."""
    if root_dir is None:
        return {"mode": "default", "url": None}
    path = settings_path(Path(root_dir))
    token = _stat_token(path)
    key = str(path)
    with _LOCK:
        hit = _saved_cache.get(key)
        if hit is not None and hit[0] == token:
            return dict(hit[1])
    out: Dict[str, Optional[str]] = {"mode": "default", "url": None}
    if token != (0, -1):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            fwd = doc.get("forwarder") if isinstance(doc, dict) else None
            mode = fwd.get("mode") if isinstance(fwd, dict) else None
            if mode == "off":
                out = {"mode": "off", "url": None}
            elif mode == "url":
                out = {"mode": "url", "url": parse_forwarder_url(fwd.get("url"))}
        except (OSError, ValueError, AttributeError):
            out = {"mode": "default", "url": None}
    with _LOCK:
        _saved_cache[key] = (token, dict(out))
    return out


def save_forwarder(root_dir: Path, mode: str, url: Optional[str] = None, *, actor: str = "") -> Dict[str, Optional[str]]:
    """Validate and save the setting (atomic write; nothing in it is secret). Raises ValueError."""
    if mode not in FORWARDER_MODES:
        raise ValueError(f"mode is one of {', '.join(FORWARDER_MODES)}.")
    if mode != "url" and url not in (None, ""):
        raise ValueError("url goes only with mode url.")
    clean = parse_forwarder_url(url) if mode == "url" else None
    path = settings_path(Path(root_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": SETTINGS_SCHEMA,
        "forwarder": {"mode": mode, "url": clean, "by": str(actor or ""),
                      "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    }
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"mode": mode, "url": clean}


def effective_forwarder(root_dir: Optional[Path]) -> Tuple[Optional[str], str]:
    """(the address in use or None for off, its source: flag | setting | packaged)."""
    given, flag_url = flag_relay()
    if given:
        return flag_url, "flag"
    saved = saved_forwarder(root_dir)
    if saved["mode"] == "off":
        return None, "setting"
    if saved["mode"] == "url":
        return saved["url"], "setting"
    return PUBLISHER_RELAY_URL, "packaged"


def forwarder_state(root_dir: Optional[Path]) -> Dict[str, Any]:
    """`GET /gateway/push/settings`'s `forwarder`: {mode, url, effective, source, packaged}."""
    saved = saved_forwarder(root_dir)
    effective, source = effective_forwarder(root_dir)
    return {"mode": saved["mode"], "url": saved["url"], "effective": effective, "source": source,
            "packaged": PUBLISHER_RELAY_URL}


def probe_forwarder(url: str, *, transport: Any = None, timeout_s: float = PROBE_TIMEOUT_S) -> Dict[str, Any]:
    """One `GET <url>/v1/info`: `{reachable: true, topics, reason: null}` or `{reachable: false, topics: [], reason}`."""
    try:
        import httpx

        kwargs: Dict[str, Any] = {"timeout": float(timeout_s)}
        if transport is not None:
            kwargs["transport"] = transport
        with httpx.Client(**kwargs) as client:
            resp = client.get(f"{str(url).rstrip('/')}/v1/info")
    except Exception as exc:  # noqa: BLE001 - every transport failure is a reason in words
        return {"reachable": False, "topics": [], "reason": f"Could not reach it ({type(exc).__name__}: {exc})."[:300]}
    if resp.status_code != 200:
        return {"reachable": False, "topics": [], "reason": f"It answered {resp.status_code}: not a push forwarder."}
    try:
        data = resp.json()
    except ValueError:
        data = None
    topics = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(topics, list):
        return {"reachable": False, "topics": [], "reason": "It answered, but not with a push forwarder's list of apps."}
    return {"reachable": True, "topics": [str(t) for t in topics if isinstance(t, str)], "reason": None}


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
    global _relay_url, _source, _flag_given
    with _LOCK:
        _relay_url, _source, _flag_given = None, None, False
        _saved_cache.clear()


__all__ = [
    "FLAG_HELP",
    "FORWARDER_MODES",
    "add_relay_arguments",
    "effective_forwarder",
    "flag_relay",
    "forwarder_state",
    "PACKAGED_DEFAULT",
    "PUBLISHER_RELAY_URL",
    "parse_forwarder_url",
    "parse_relay_value",
    "probe_forwarder",
    "push_relay_source",
    "push_relay_url",
    "save_forwarder",
    "saved_forwarder",
    "set_push_relay",
    "settings_change_token",
]
