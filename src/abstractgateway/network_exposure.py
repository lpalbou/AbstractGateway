"""Network exposure: WHO can reach this gateway, and at which addresses.

One setting, three modes (operator ask 2026-09-24: "an option in the systray,
the WUI and the TUI to define if the gateway is accessible only localhost,
local network or open to the internet, and copy buttons to rapidly get the
IP/port"):

    localhost  bind 127.0.0.1           nothing but this machine can connect
    lan        bind 0.0.0.0             every machine on the networks this host
                                        is on; user auth REQUIRED
    internet   bind 0.0.0.0             same bind as `lan` plus an explicit
                                        acknowledgement: the gateway speaks
                                        plain HTTP, so reaching it from the
                                        internet needs a TLS reverse proxy or a
                                        tunnel, and port forwarding is the
                                        router owner's act, never ours

The setting lives in the runtime-config store (`runtime_config.py`, key
`network`); the console, the TUI, the tray and `abstractgateway network`
all edit that one store through `apply_network_change`. It is applied by
`serve` at START (a listening socket cannot move): until the next start the
status reports `restart_required: true` with `configured` vs `effective`.
An explicit `serve --host/--port` wins over the setting and is reported as
`overridden_by_cli` (ADR-0026: explicit, never silent), and a restart then
cannot apply the setting, because a restart replays the same command line.

Addresses are DISCOVERED, never guessed: every up interface's addresses
(psutil when it is importable, else `ifconfig` / `ip`, else the hostname's
own resolution), the Bonjour name when it resolves, and the WAN address only
on an explicit request in `internet` mode (never an outbound call per poll).

The contract (`gateway_network_v1`) is documented in docs/configuration.md
("Network exposure"); the tray and the console render it as is.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, MutableMapping, Optional, Tuple

NETWORK_SCHEMA = "gateway_network_v1"

MODES: Tuple[str, ...] = ("localhost", "lan", "internet")
MODE_LABELS: Dict[str, str] = {
    "localhost": "Localhost only",
    "lan": "Local network",
    "internet": "Internet",
}
MODE_BIND_HOST: Dict[str, str] = {
    "localhost": "127.0.0.1",
    "lan": "0.0.0.0",
    "internet": "0.0.0.0",
}
DEFAULT_PORT = 8080

# Process exports written by `serve` (the bind this process was actually
# given, and where each half came from). BIND_HOST_ENV is runtime_config's.
BIND_PORT_ENV = "ABSTRACTGATEWAY_BIND_PORT"
BIND_SOURCE_ENV = "ABSTRACTGATEWAY_BIND_SOURCE"
# Env names `serve` set ON BEHALF of the network setting (user auth for a
# network mode, the gateway's own origins). A relaunched process inherits
# the environment; listing them lets the next start forget them and derive
# them again from the setting, so they never pose as operator choices.
NETWORK_EXPORTS_ENV = "ABSTRACTGATEWAY_NETWORK_EXPORTS"
AUTH_SOURCE_NETWORK = "network_setting"

RUN_RECORD_SCHEMA = "gateway_network_run_v1"

PUBLIC_IP_URL = "https://api.ipify.org"

_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})


class NetworkSettingError(ValueError):
    """Invalid input (unknown mode, bad port): the route maps it to 400."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_mode(raw: Any) -> str:
    s = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "localhost": "localhost", "local": "localhost", "loopback": "localhost", "localhost_only": "localhost",
        "lan": "lan", "local_network": "lan", "network": "lan",
        "internet": "internet", "public": "internet", "wan": "internet",
    }
    if s not in aliases:
        raise NetworkSettingError(f"unknown network exposure {raw!r}: one of {list(MODES)}")
    return aliases[s]


def validate_port(raw: Any) -> int:
    if isinstance(raw, bool):
        raise NetworkSettingError("port must be an integer 1-65535")
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        raise NetworkSettingError(f"port must be an integer 1-65535 (got {raw!r})") from None
    if not 1 <= port <= 65535:
        raise NetworkSettingError(f"port must be an integer 1-65535 (got {port})")
    return port


def is_loopback_host(host: Optional[str]) -> bool:
    h = str(host or "").strip().strip("[]").lower()
    if not h:
        return False
    if h == "localhost":
        return True
    try:
        return bool(ipaddress.ip_address(h).is_loopback)
    except ValueError:
        return False


def is_wildcard_host(host: Optional[str]) -> bool:
    return str(host or "").strip().strip("[]") in {"0.0.0.0", "::"}


def url_for(host: str, port: int, scheme: str = "http") -> str:
    h = str(host)
    if ":" in h and not h.startswith("["):
        h = f"[{h}]"
    return f"{scheme}://{h}:{int(port)}"


def mode_for_bind(host: Optional[str], configured_mode: Optional[str] = None) -> str:
    """The exposure a bind ACTUALLY gives (effective mode).

    Loopback = `localhost`. Any other bind listens beyond this machine: it
    reads as the configured network mode when one is configured (lan and
    internet share a bind; the difference is the acknowledgement), else as
    `lan`. No bind recorded (not started by `serve`) = `unknown`."""
    if host is None or str(host).strip() == "":
        return "unknown"
    if is_loopback_host(host):
        return "localhost"
    if configured_mode in ("lan", "internet"):
        return str(configured_mode)
    return "lan"


# ---------------------------------------------------------------------------
# Auth posture (what the NEXT start will enforce)
# ---------------------------------------------------------------------------


def _auth_source_env() -> str:
    from .first_run import AUTH_MODE_SOURCE_ENV

    return AUTH_MODE_SOURCE_ENV


def _network_exports(env: Mapping[str, str]) -> List[str]:
    raw = str(env.get(NETWORK_EXPORTS_ENV) or "")
    return [n for n in (x.strip() for x in raw.split(",")) if n]


def baseline_env(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment WITHOUT what `serve` exported for the network setting:
    what the operator configured, i.e. what a fresh start sees."""
    env = os.environ if env is None else env
    out = dict(env)
    for name in _network_exports(env):
        out.pop(name, None)
    out.pop(NETWORK_EXPORTS_ENV, None)
    return out


def auth_posture(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The operator's auth posture (no secret values): the input of every
    mode/auth decision. `explicit` = the operator stated a posture through
    the environment (first_run's rule; our own exports are not statements)."""
    from .first_run import auth_mode_summary, explicit_auth_configured

    base = baseline_env(env)
    summary = auth_mode_summary(base)
    return {
        "explicit": bool(explicit_auth_configured(base)),
        "mode": str(summary.get("mode") or ""),
        "source": str(summary.get("source") or ""),
        "user_auth": bool(summary.get("user_auth_enabled")),
        "token_auth": bool(summary.get("token_configured")),
        "security_enabled": bool(summary.get("security_enabled")),
        "read_protected": bool(summary.get("read_protected", True)),
    }


# Operator rule (2026-09-24): user-facing text never tells anyone to set an
# environment variable. Auth topology is a start-time (deployment) choice, so
# these describe the state the gateway was STARTED in and what a plain start
# does, naming the variable only as where that state came from.
_FIX_USER_AUTH = (
    "This gateway was started with accounts (user auth) off: its launch environment sets a shared token only, "
    "or turns user auth off (ABSTRACTGATEWAY_AUTH_TOKEN / ABSTRACTGATEWAY_USER_AUTH=0). Started without that "
    "choice (a plain `abstractgateway serve`, or the login item `abstractgateway service enable` registers), the "
    "gateway turns accounts on for this mode by itself. Then choose this mode again."
)
_FIX_READ_OPEN = (
    "This gateway was started with read protection off (ABSTRACTGATEWAY_PROTECT_READ=0 in its launch environment): "
    "it answers every unauthenticated read as its admin (accounts, settings, files), so on a network anyone could read "
    "them. Started without that choice, reads need a sign-in; then choose this mode again."
)
_FIX_SECURITY_OFF = (
    "This gateway was started with authentication switched off (ABSTRACTGATEWAY_SECURITY=0 or "
    "ABSTRACTGATEWAY_PROTECT_WRITE=0 in its launch environment), so nothing would protect it on a network. "
    "Started without that choice, authentication is on; then choose this mode again."
)


def auth_check(mode: str, posture: Mapping[str, Any]) -> Dict[str, Any]:
    """May `mode` be applied under `posture`? {ok, reason?, fix?, will_enable_user_auth}.

    `localhost` always may. `lan`/`internet` need USER auth (registry
    accounts, the console's sign-in) at the next start:
    - security/write protection switched off -> refused (the bind would be open);
    - the operator stated a posture WITHOUT user auth (token only, or
      USER_AUTH=0) -> refused: we never override an explicit statement;
    - nothing stated (or only the loopback first-run default) -> allowed,
      and `serve` keeps user auth on for that mode, loudly
      (`will_enable_user_auth: true`, auth source `network_setting`).
    """
    if mode == "localhost":
        return {"ok": True, "will_enable_user_auth": False}
    if posture.get("mode") == "open" or not posture.get("security_enabled", True):
        return {
            "ok": False,
            "reason_code": "auth_disabled",
            "reason": f"'{mode}' would expose a gateway whose authentication is switched off",
            "fix": _FIX_SECURITY_OFF,
            "will_enable_user_auth": False,
        }
    if not posture.get("read_protected", True):
        return {
            "ok": False,
            "reason_code": "auth_disabled",
            "reason": f"'{mode}' would expose a gateway that answers unauthenticated reads as its admin (read protection off)",
            "fix": _FIX_READ_OPEN,
            "will_enable_user_auth": False,
        }
    if posture.get("user_auth"):
        if posture.get("explicit"):
            return {"ok": True, "will_enable_user_auth": False}
        # The loopback first-run default: user auth is on today because the
        # bind is loopback; `serve` keeps it on for the network mode.
        return {"ok": True, "will_enable_user_auth": True}
    if posture.get("explicit"):
        return {
            "ok": False,
            "reason_code": "user_auth_required",
            "reason": (
                f"'{mode}' requires user auth (accounts + the console sign-in); this gateway's environment "
                + ("configures a shared token only" if posture.get("token_auth") else "turns user auth off")
            ),
            "fix": _FIX_USER_AUTH,
            "will_enable_user_auth": False,
        }
    return {"ok": True, "will_enable_user_auth": True}


# ---------------------------------------------------------------------------
# Reverse proxy: allowed browser origins + trusted X-Forwarded-For (mission Z)
# ---------------------------------------------------------------------------
#
# Operator 2026-09-24: "i explicitly told you i don't like env vars. most
# should be something one can configure from the consoles (wui+tui)". The two
# knobs a reverse-proxy deployment needs are stored in the network setting
# (`allowed_origins`, `trust_proxy`) and edited through the same door as the
# mode. Their historical env names still work as a DEPLOYMENT override (the
# security carve-out in env_registry): when set in the environment the gateway
# was started from, they decide, and every surface says so
# (`overridden_by_env`). Both are read PER REQUEST by the security middleware
# (`live_reverse_proxy`), so a change applies to the next request: no restart.

ORIGINS_ENV = "ABSTRACTGATEWAY_ALLOWED_ORIGINS"
TRUST_PROXY_ENV = "ABSTRACTGATEWAY_TRUST_PROXY"
_LEGACY_ORIGINS_ENV = "ABSTRACTFLOW_GATEWAY_ALLOWED_ORIGINS"
_LEGACY_TRUST_PROXY_ENV = "ABSTRACTFLOW_GATEWAY_TRUST_PROXY"
# Always allowed (security/gateway_security.py's default): browsers on this
# machine, any port.
BUILTIN_ORIGINS: Tuple[str, ...] = ("http://localhost:*", "http://127.0.0.1:*")

_ORIGIN_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://(.*)$")
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_ORIGIN_HOST_RE = re.compile(rf"^(?:\*\.)?{_HOST_LABEL}(?:\.{_HOST_LABEL})*$")


class OriginsError(NetworkSettingError):
    """One or more origins are invalid; `errors` = [{value, error}] per entry."""

    def __init__(self, errors: List[Dict[str, str]]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(f"{e['value']!r}: {e['error']}" for e in self.errors))


def normalize_origin(raw: Any) -> str:
    """One browser origin, validated and written the way a browser sends it.

    An origin is `scheme://host[:port]`: http or https, no path, no trailing
    slash, no query, no user info. The default port is dropped
    (`https://a.example:443` -> `https://a.example`: a browser never sends it,
    so the long form would never match). `*` alone (every origin), a leading
    `*.` label and a `:*` port are accepted because they are typed on purpose;
    `origin_warnings` flags each of them."""
    s = str(raw if raw is not None else "").strip()
    if not s:
        raise NetworkSettingError("an origin cannot be empty")
    if s == "*":
        return "*"
    if any(ch.isspace() for ch in s):
        raise NetworkSettingError("an origin has no spaces")
    m = _ORIGIN_SCHEME_RE.match(s)
    if not m:
        raise NetworkSettingError("not an origin: write scheme://host[:port], for example https://gateway.example.com")
    scheme, rest = m.group(1).lower(), m.group(2)
    if scheme not in ("http", "https"):
        raise NetworkSettingError(f"only http:// and https:// origins exist for a browser (got {scheme}://)")
    if rest.endswith("/") and "/" not in rest[:-1] and "?" not in rest and "#" not in rest:
        raise NetworkSettingError(f"no trailing slash: an origin is scheme://host[:port] (write {s.rstrip('/')})")
    if any(ch in rest for ch in "/?#"):
        cut = min(i for i in (rest.find("/"), rest.find("?"), rest.find("#")) if i >= 0)
        raise NetworkSettingError(
            f"no path: an origin is scheme://host[:port] only (write {scheme}://{rest[:cut]})"
        )
    if "@" in rest:
        raise NetworkSettingError("no user name or password in an origin")
    if rest.startswith("["):
        end = rest.find("]")
        if end < 0:
            raise NetworkSettingError("unclosed [ in an IPv6 origin")
        try:
            host = "[" + str(ipaddress.IPv6Address(rest[1:end])) + "]"
        except ValueError:
            raise NetworkSettingError(f"{rest[: end + 1]} is not an IPv6 address") from None
        tail = rest[end + 1:]
    else:
        host, sep, port_s = rest.partition(":")
        if ":" in port_s:
            raise NetworkSettingError("write an IPv6 host in brackets, e.g. http://[fd00::1]:8080")
        tail = f":{port_s}" if sep else ""
        host = host.lower()
        if not host:
            raise NetworkSettingError("an origin needs a host")
        if not _ORIGIN_HOST_RE.match(host):
            raise NetworkSettingError(f"{host!r} is not a valid host name")
    port: Any = None
    if tail:
        p = tail[1:]
        if p == "*":
            port = "*"
        elif p.isdigit() and 1 <= int(p) <= 65535:
            port = int(p)
        else:
            raise NetworkSettingError(f"port must be 1-65535 or * (got {p!r})")
    if port == (443 if scheme == "https" else 80):
        port = None
    return f"{scheme}://{host}" + (f":{port}" if port is not None else "")


def validate_origins(values: Any) -> List[str]:
    """A list (or comma-separated text) of origins -> the normalized list,
    duplicates removed, order kept. Raises OriginsError naming EVERY bad entry."""
    if values is None:
        return []
    if isinstance(values, str):
        items: List[Any] = [v for v in values.split(",")]
        items = [v for v in items if v.strip()]
    elif isinstance(values, (list, tuple)):
        items = list(values)
    else:
        raise NetworkSettingError("allowed_origins must be a list of origins")
    out: List[str] = []
    errors: List[Dict[str, str]] = []
    for item in items:
        if not isinstance(item, str):
            errors.append({"value": str(item), "error": "an origin is text, e.g. https://gateway.example.com"})
            continue
        try:
            o = normalize_origin(item)
        except NetworkSettingError as exc:
            errors.append({"value": item, "error": str(exc)})
            continue
        if o not in out:
            out.append(o)
    if errors:
        raise OriginsError(errors)
    return out


def origin_warnings(origins: List[str]) -> List[str]:
    out: List[str] = []
    for o in origins:
        if o == "*":
            out.append(
                "'*' lets ANY website use this gateway from a browser that is signed in to it. "
                "Use it for a short test on a trusted network only; list your real origins instead."
            )
        elif "://*." in o or (o.endswith(":*") and not is_loopback_host(o.split("://", 1)[1].rsplit(":", 1)[0].strip("[]"))):
            # Any port on THIS machine (the built-in http://localhost:*) is not a
            # widening worth a warning; a pattern over other hosts is.
            out.append(f"{o} is a pattern: it matches many origins, not one. Prefer the exact origin.")
        elif o.startswith("http://") and not is_loopback_host(o.split("://", 1)[1].rsplit(":", 1)[0].strip("[]")):
            out.append(f"{o} is plain http: passwords and session cookies cross the network unencrypted.")
    return out


def _env_raw(env: Mapping[str, str], *names: str) -> Optional[Tuple[str, str]]:
    for n in names:
        v = env.get(n)
        if v is not None and str(v).strip():
            return n, str(v).strip()
    return None


def _as_bool_text(raw: str) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def proxy_env_facts(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """What the environment says about the two reverse-proxy knobs, without
    `serve`'s own exports: {origins_env: {name, value[]} | None, trust_proxy_env:
    {name, value} | None, self_origins: [...]} (self_origins = the LAN origins
    `serve` derived for a network mode). Written into the run record so the
    CLI reports the RUNNING gateway's environment, not its own shell's."""
    env = os.environ if env is None else env
    base = baseline_env(env)
    o = _env_raw(base, ORIGINS_ENV, _LEGACY_ORIGINS_ENV)
    t = _env_raw(base, TRUST_PROXY_ENV, _LEGACY_TRUST_PROXY_ENV)
    self_origins: List[str] = []
    if ORIGINS_ENV in _network_exports(env):
        self_origins = [x.strip() for x in str(env.get(ORIGINS_ENV) or "").split(",") if x.strip()]
    return {
        "origins_env": {"name": o[0], "value": [x.strip() for x in o[1].split(",") if x.strip()]} if o else None,
        "trust_proxy_env": {"name": t[0], "value": _as_bool_text(t[1]), "raw": t[1]} if t else None,
        "self_origins": [x for x in self_origins if x not in BUILTIN_ORIGINS],
    }


def reverse_proxy_status(setting: Mapping[str, Any], facts: Mapping[str, Any]) -> Dict[str, Any]:
    """The `reverse_proxy` block of gateway_network_v1: per knob the stored
    value (what the controls edit), the winning `source` (setting | env |
    default), `overridden_by_env`, the `effective` value the middleware
    applies, and `applies: "live"` (read per request, no restart)."""
    stored_origins = list(setting.get("allowed_origins") or [])
    o_src_stored = setting.get("allowed_origins_source") == "stored"
    oenv = facts.get("origins_env")
    self_origins = list(facts.get("self_origins") or [])
    if oenv:
        effective = list(oenv["value"])
        o_source = "env"
    else:
        effective = list(BUILTIN_ORIGINS) + [x for x in self_origins if x not in BUILTIN_ORIGINS]
        effective += [x for x in stored_origins if x not in effective]
        o_source = "setting" if o_src_stored else "default"
    origins: Dict[str, Any] = {
        "value": stored_origins,
        "source": o_source,
        "overridden_by_env": bool(oenv),
        "effective": effective,
        "builtin": list(BUILTIN_ORIGINS),
        "self_origins": self_origins,
        "applies": "live",
        "warnings": origin_warnings(effective if oenv else stored_origins),
    }
    if oenv:
        origins["env_name"] = oenv["name"]
        origins["env_value"] = list(oenv["value"])
        origins["note"] = (
            f"This gateway was started with {oenv['name']} in its environment: that list decides, and the "
            "origins saved here apply once the gateway is started without it."
        )
    if setting.get("invalid_allowed_origins"):
        origins["invalid"] = list(setting["invalid_allowed_origins"])
    tenv = facts.get("trust_proxy_env")
    t_stored = setting.get("trust_proxy_source") == "stored"
    trust: Dict[str, Any] = {
        "value": bool(setting.get("trust_proxy")),
        "source": "env" if tenv else ("setting" if t_stored else "default"),
        "overridden_by_env": bool(tenv),
        "effective": bool(tenv["value"]) if tenv else bool(setting.get("trust_proxy")),
        "applies": "live",
    }
    if tenv:
        trust["env_name"] = tenv["name"]
        trust["env_value"] = bool(tenv["value"])
        trust["note"] = (
            f"This gateway was started with {tenv['name']}={tenv.get('raw', '')} in its environment: it decides "
            f"({'on' if tenv['value'] else 'off'}), and the switch saved here applies once the gateway is started without it."
        )
    if trust["effective"]:
        trust["warning"] = (
            "Trust proxy is on: the gateway takes the client address from X-Forwarded-For. That is right only when "
            "every request comes through your own proxy; otherwise any client can choose the address that sign-in "
            "lockouts and the audit log see."
        )
    return {"allowed_origins": origins, "trust_proxy": trust}


@dataclass(frozen=True)
class LiveReverseProxy:
    """What the security middleware adds to its start-time policy, per request."""

    extra_origins: Tuple[str, ...] = ()
    trust_proxy: Optional[bool] = None  # None = no say (env decides, or nothing stored)


_LIVE_LOCK = threading.Lock()
_LIVE_CACHE: Dict[str, Any] = {"key": None, "value": None}


def live_reverse_proxy(data_dir: Optional[Path] = None, *, env: Optional[Mapping[str, str]] = None) -> LiveReverseProxy:
    """The stored reverse-proxy settings as the middleware applies them NOW.

    One stat() of the settings file per call; the file is parsed again only
    when it changed (mtime/size/inode), so a console or CLI change applies to
    the next request, from this process or another. An env override leaves
    the stored value out (`extra_origins=()` / `trust_proxy=None`)."""
    env = os.environ if env is None else env
    if data_dir is None:
        from .users import gateway_data_dir_from_env

        data_dir = gateway_data_dir_from_env()
    from .runtime_config import _read_store, _store_path

    path = _store_path(Path(data_dir))
    base = baseline_env(env)
    env_key = (_env_raw(base, ORIGINS_ENV, _LEGACY_ORIGINS_ENV), _env_raw(base, TRUST_PROXY_ENV, _LEGACY_TRUST_PROXY_ENV))
    try:
        st = path.stat()
        file_key: Tuple[Any, ...] = (st.st_mtime_ns, st.st_size, st.st_ino)
    except FileNotFoundError:
        file_key = ("absent",)
    key = (str(path), file_key, env_key)
    with _LIVE_LOCK:
        if _LIVE_CACHE["key"] == key and _LIVE_CACHE["value"] is not None:
            return _LIVE_CACHE["value"]
    raw = _read_store(Path(data_dir)).get("network")
    raw = raw if isinstance(raw, dict) else {}
    extra: List[str] = []
    if env_key[0] is None and isinstance(raw.get("allowed_origins"), list):
        for item in raw["allowed_origins"]:
            try:
                o = normalize_origin(item)
            except NetworkSettingError:
                continue  # reported as `invalid` by the status payload; never applied
            if o not in extra:
                extra.append(o)
    trust = bool(raw["trust_proxy"]) if (env_key[1] is None and isinstance(raw.get("trust_proxy"), bool)) else None
    value = LiveReverseProxy(extra_origins=tuple(extra), trust_proxy=trust)
    with _LIVE_LOCK:
        _LIVE_CACHE.update({"key": key, "value": value})
    return value


# ---------------------------------------------------------------------------
# Address discovery
# ---------------------------------------------------------------------------


@dataclass
class IfaceAddr:
    interface: str
    address: str
    family: str  # "ipv4" | "ipv6"
    up: bool = True


_IFCONFIG_HEAD = re.compile(r"^([A-Za-z0-9_.:-]+): flags=\d+<([^>]*)>")
_IFCONFIG_INET = re.compile(r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)")
_IFCONFIG_INET6 = re.compile(r"^\s+inet6\s+([0-9a-fA-F:]+)(?:%\S+)?")
_IP_ADDR_LINE = re.compile(r"^\d+:\s+(\S+)\s+(inet6?)\s+([0-9a-fA-F:.]+)/\d+")


def parse_ifconfig(text: str) -> List[IfaceAddr]:
    """`ifconfig -a` (macOS/BSD): interfaces flagged UP with their inet/inet6."""
    out: List[IfaceAddr] = []
    cur: Optional[str] = None
    up = False
    for ln in str(text or "").splitlines():
        m = _IFCONFIG_HEAD.match(ln)
        if m:
            cur = m.group(1).rstrip(":")
            flags = {f.strip().upper() for f in m.group(2).split(",")}
            up = "UP" in flags and "RUNNING" in flags
            continue
        if cur is None:
            continue
        m4 = _IFCONFIG_INET.match(ln)
        if m4:
            out.append(IfaceAddr(cur, m4.group(1), "ipv4", up))
            continue
        m6 = _IFCONFIG_INET6.match(ln)
        if m6:
            out.append(IfaceAddr(cur, m6.group(1), "ipv6", up))
    return out


def parse_ip_addr(text: str) -> List[IfaceAddr]:
    """`ip -o addr show up` (Linux): one address per line."""
    out: List[IfaceAddr] = []
    for ln in str(text or "").splitlines():
        m = _IP_ADDR_LINE.match(ln.strip())
        if m:
            out.append(IfaceAddr(m.group(1).split("@", 1)[0], m.group(3), "ipv6" if m.group(2) == "inet6" else "ipv4", True))
    return out


def _run(cmd: List[str], timeout: float = 3.0) -> Optional[str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except Exception:
        return None
    return p.stdout if p.returncode == 0 else None


def discover_interfaces() -> Tuple[List[IfaceAddr], str]:
    """Every address of every interface, with the method that found them."""
    try:
        import psutil  # optional: present with AbstractCore's local extras

        stats = psutil.net_if_stats()
        out: List[IfaceAddr] = []
        for name, addrs in psutil.net_if_addrs().items():
            st = stats.get(name)
            up = bool(getattr(st, "isup", True)) if st is not None else True
            for a in addrs:
                if a.family == socket.AF_INET:
                    out.append(IfaceAddr(name, str(a.address), "ipv4", up))
                elif a.family == socket.AF_INET6:
                    out.append(IfaceAddr(name, str(a.address).split("%", 1)[0], "ipv6", up))
        return out, "psutil"
    except Exception:
        pass
    if sys.platform != "win32":
        text = _run(["ip", "-o", "addr", "show", "up"]) if sys.platform.startswith("linux") else None
        if text:
            return parse_ip_addr(text), "ip"
        for exe in ("/sbin/ifconfig", "ifconfig"):
            text = _run([exe, "-a"])
            if text:
                return parse_ifconfig(text), "ifconfig"
    out = []
    try:
        for fam, _t, _p, _c, sa in socket.getaddrinfo(socket.gethostname(), None):
            if fam == socket.AF_INET:
                out.append(IfaceAddr("", str(sa[0]), "ipv4", True))
            elif fam == socket.AF_INET6:
                out.append(IfaceAddr("", str(sa[0]).split("%", 1)[0], "ipv6", True))
    except Exception:
        pass
    return out, "getaddrinfo"


_LABEL_CACHE: Dict[str, Any] = {"at": 0.0, "labels": None}
_LABEL_TTL_S = 300.0


def parse_hardware_ports(text: str) -> Dict[str, str]:
    """`networksetup -listallhardwareports` -> {device: "Wi-Fi", ...}."""
    labels: Dict[str, str] = {}
    port: Optional[str] = None
    for ln in str(text or "").splitlines():
        if ln.startswith("Hardware Port:"):
            port = ln.split(":", 1)[1].strip()
        elif ln.startswith("Device:") and port:
            labels[ln.split(":", 1)[1].strip()] = port
            port = None
    return labels


def interface_labels() -> Dict[str, str]:
    """Human names for interfaces (macOS: "Wi-Fi", "Ethernet", ...). Cached."""
    now = time.monotonic()
    cached = _LABEL_CACHE.get("labels")
    if cached is not None and now - float(_LABEL_CACHE.get("at") or 0.0) < _LABEL_TTL_S:
        return dict(cached)
    labels: Dict[str, str] = {}
    if sys.platform == "darwin":
        labels = parse_hardware_ports(_run(["networksetup", "-listallhardwareports"]) or "")
    _LABEL_CACHE.update({"at": now, "labels": dict(labels)})
    return labels


def _addr_scope(ip: ipaddress._BaseAddress) -> Optional[str]:
    """None = never listed (loopback, link-local, multicast, unspecified)."""
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return None
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return "cgnat"  # carrier-grade NAT space: Tailscale and friends
    if ip.is_private:
        return "private"
    return "global"


def _iface_label(name: str, labels: Mapping[str, str], scope: str) -> str:
    if name in labels:
        return labels[name]
    if name.startswith(("utun", "tun", "wg", "tailscale", "zt")):
        return "VPN"
    return ""


_HOSTNAME_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}
_HOSTNAME_TTL_S = 60.0


def local_hostname_candidate() -> Optional[str]:
    """This machine's Bonjour name (`<LocalHostName>.local`)."""
    name = None
    if sys.platform == "darwin":
        name = (_run(["scutil", "--get", "LocalHostName"], timeout=2.0) or "").strip() or None
    if not name:
        try:
            name = socket.gethostname().strip() or None
        except Exception:
            name = None
    if not name:
        return None
    name = name.split(".", 1)[0] if not name.endswith(".local") else name[: -len(".local")]
    return f"{name}.local" if name else None


def _resolves(name: str, timeout_s: float = 1.5) -> bool:
    box: Dict[str, bool] = {}

    def _go() -> None:
        try:
            box["ok"] = bool(socket.getaddrinfo(name, None))
        except Exception:
            box["ok"] = False

    t = threading.Thread(target=_go, daemon=True, name="network-mdns-probe")
    t.start()
    t.join(timeout_s)
    return bool(box.get("ok"))


def bonjour_hostname() -> Optional[str]:
    """`<name>.local` when it resolves on this machine, else None. Cached 60 s."""
    now = time.monotonic()
    if _HOSTNAME_CACHE.get("at") and now - float(_HOSTNAME_CACHE["at"]) < _HOSTNAME_TTL_S:
        return _HOSTNAME_CACHE.get("value")
    cand = local_hostname_candidate()
    value = cand if (cand and _resolves(cand)) else None
    _HOSTNAME_CACHE.update({"at": now, "value": value})
    return value


def lookup_public_ip(timeout_s: float = 4.0) -> Dict[str, Any]:
    """The WAN address as the internet sees it (one HTTPS GET). Only ever
    called on an explicit request."""
    import urllib.request

    try:
        with urllib.request.urlopen(PUBLIC_IP_URL, timeout=timeout_s) as resp:  # noqa: S310 - fixed https URL
            text = resp.read(64).decode("ascii", "replace").strip()
        ip = ipaddress.ip_address(text)
        return {"ok": True, "address": str(ip), "service": PUBLIC_IP_URL}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "service": PUBLIC_IP_URL}


def _reachable(bind_host: Optional[str], family: str, address: str) -> bool:
    b = str(bind_host or "").strip().strip("[]")
    if not b:
        return False
    if b == "0.0.0.0":
        return family == "ipv4"
    if b == "::":
        return True  # dual-stack by default on macOS/Linux/Windows
    if b == "localhost":
        return is_loopback_host(address)
    return b == address


def build_addresses(
    *,
    bind_host: Optional[str],
    port: int,
    interfaces: List[IfaceAddr],
    labels: Mapping[str, str],
    hostname: Optional[str],
    public: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """The address list: loopback first, then interface addresses (private
    IPv4 of labelled hardware first), the Bonjour name, the WAN address."""
    loop_host = "::1" if str(bind_host or "").strip("[]") == "::1" else "127.0.0.1"
    out: List[Dict[str, Any]] = [
        {
            "kind": "loopback",
            "host": loop_host,
            "port": int(port),
            "url": url_for(loop_host, port),
            "family": "ipv6" if loop_host == "::1" else "ipv4",
            "reachable": bool(bind_host) and (is_loopback_host(bind_host) or is_wildcard_host(bind_host)),
            "note": "this machine only",
        }
    ]
    rows: List[Tuple[Tuple[int, int, int, str], Dict[str, Any]]] = []
    seen: set = set()
    for ia in interfaces:
        if not ia.up:
            continue
        try:
            ip = ipaddress.ip_address(ia.address)
        except ValueError:
            continue
        scope = _addr_scope(ip)
        if scope is None or (ia.family, ia.address) in seen:
            continue
        seen.add((ia.family, ia.address))
        label = _iface_label(ia.interface, labels, scope)
        reachable = _reachable(bind_host, ia.family, ia.address)
        row: Dict[str, Any] = {
            "kind": "lan",
            "host": ia.address,
            "port": int(port),
            "url": url_for(ia.address, port),
            "family": ia.family,
            "interface": ia.interface or None,
            "interface_label": label or None,
            "scope": scope,
            "reachable": reachable,
        }
        if scope == "global":
            row["note"] = "globally routable address on this interface: reachable beyond the local network unless a firewall blocks it"
        elif scope == "cgnat":
            row["note"] = "VPN / carrier-grade NAT address (e.g. Tailscale): reachable by peers of that network"
        if not reachable:
            row["note"] = (
                "not listening here yet: the gateway is bound to "
                + (str(bind_host) if bind_host else "an unknown host")
                + ("" if ia.family == "ipv4" or not is_wildcard_host(bind_host) else " (IPv4 only)")
            )
        order = (
            0 if ia.family == "ipv4" else 1,
            0 if label and label not in ("VPN",) else 1,
            {"private": 0, "cgnat": 1, "global": 2}.get(scope, 3),
            ia.interface,
        )
        rows.append((order, row))
    out.extend(r for _o, r in sorted(rows, key=lambda x: x[0]))
    if hostname:
        out.append(
            {
                "kind": "hostname",
                "host": hostname,
                "port": int(port),
                "url": url_for(hostname, port),
                "family": "name",
                "reachable": is_wildcard_host(bind_host),
                "note": "Bonjour/mDNS name: resolves on macOS, iOS, Windows 10+ and most Linux desktops of the same network",
            }
        )
    if public is not None:
        if public.get("ok"):
            out.append(
                {
                    "kind": "public",
                    "host": public["address"],
                    "port": int(port),
                    "url": url_for(public["address"], port),
                    "family": "ipv6" if ":" in str(public["address"]) else "ipv4",
                    "reachable": None,
                    "note": (
                        f"your network's WAN address (as seen by {public.get('service')}). Reachable only if YOUR router "
                        f"forwards TCP {int(port)} to this machine and no firewall blocks it; plain HTTP (use a TLS proxy or tunnel)"
                    ),
                }
            )
        else:
            out.append(
                {
                    "kind": "public",
                    "host": None,
                    "port": int(port),
                    "url": None,
                    "reachable": None,
                    "note": f"WAN address lookup failed: {public.get('error')}",
                }
            )
    return out


# ---------------------------------------------------------------------------
# Run record (what THIS serve process was given; the CLI reads it too)
# ---------------------------------------------------------------------------


def run_record_path(data_dir: Path) -> Path:
    return Path(data_dir) / "run" / "gateway-network.json"


def write_run_record(data_dir: Path, payload: Dict[str, Any]) -> None:
    path = run_record_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_run_record(data_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        rec = json.loads(run_record_path(data_dir).read_text(encoding="utf-8"))
    except Exception:
        return None
    return rec if isinstance(rec, dict) else None


# ---------------------------------------------------------------------------
# `serve`: resolve the bind from the setting
# ---------------------------------------------------------------------------


@dataclass
class ServeBind:
    host: str
    port: int
    host_source: str  # cli | setting | default
    port_source: str  # cli | setting | default
    mode: str  # configured mode (stored or default)
    mode_source: str  # stored | default
    messages: List[str] = field(default_factory=list)
    exports: Dict[str, str] = field(default_factory=dict)
    blocked_reason: Optional[str] = None
    posture: Dict[str, Any] = field(default_factory=dict)


def _self_origins(port: int, interfaces: List[IfaceAddr], hostname: Optional[str]) -> List[str]:
    origins: List[str] = []
    for ia in interfaces:
        if not ia.up or ia.family != "ipv4":
            continue
        try:
            ip = ipaddress.ip_address(ia.address)
        except ValueError:
            continue
        if _addr_scope(ip) is None:
            continue
        origins.append(url_for(ia.address, port))
    if hostname:
        origins.append(url_for(hostname, port))
    return sorted(set(origins))


def prepare_serve_bind(
    *,
    cli_host: Optional[str],
    cli_port: Optional[int],
    data_dir: Path,
    env: Optional[MutableMapping[str, str]] = None,
    discover: Optional[Callable[[], Tuple[List[IfaceAddr], str]]] = None,
    hostname_fn: Optional[Callable[[], Optional[str]]] = None,
) -> ServeBind:
    """Decide host/port for `serve` and export what the app reads.

    Precedence per half: `--host`/`--port` > the stored network setting >
    the historical default (`first_run.default_bind_host()`, port 8080).
    MUTATES `env` (os.environ by default): forgets the previous start's
    network exports, then writes BIND_PORT/BIND_SOURCE and, for a network
    mode from the setting, user auth (only when the operator stated no auth
    posture) and ALLOWED_ORIGINS (only when unset) — each listed in
    NETWORK_EXPORTS_ENV and each announced in `messages`.
    """
    from .first_run import AUTH_MODE_SOURCE_ENV, default_bind_host
    from .runtime_config import resolve_network_setting

    env = os.environ if env is None else env
    for name in _network_exports(env):
        env.pop(name, None)
    env.pop(NETWORK_EXPORTS_ENV, None)
    if str(env.get(AUTH_MODE_SOURCE_ENV) or "") == AUTH_SOURCE_NETWORK:
        env.pop(AUTH_MODE_SOURCE_ENV, None)

    setting = resolve_network_setting(data_dir, env=env)
    posture = auth_posture(env)
    mode = str(setting["mode"])
    messages: List[str] = []
    blocked: Optional[str] = None

    if cli_host:
        host, host_source = str(cli_host), "cli"
        if setting["source"] == "stored" and host != MODE_BIND_HOST[mode]:
            messages.append(
                f"Network exposure: the setting says '{mode}' ({MODE_BIND_HOST[mode]}) but --host {host} on the "
                "command line overrides it (reported as overridden_by_cli)."
            )
    elif setting["source"] == "stored":
        host, host_source = MODE_BIND_HOST[mode], "setting"
        check = auth_check(mode, posture)
        if not check["ok"]:
            blocked = str(check.get("reason"))
            host = MODE_BIND_HOST["localhost"]
            messages.append(
                f"[ERROR] Network exposure '{mode}' cannot be applied: {blocked}. Serving on {host} (this machine only) "
                f"until it is fixed. Fix: {check.get('fix')}"
            )
    else:
        host, host_source = default_bind_host(env), "default"

    if cli_port is not None:
        port, port_source = int(cli_port), "cli"
        if setting["port_source"] == "stored" and int(cli_port) != int(setting["port"]):
            messages.append(
                f"Network exposure: the setting says port {setting['port']} but --port {cli_port} on the command line overrides it."
            )
    elif setting["port_source"] == "stored":
        port, port_source = int(setting["port"]), "setting"
    else:
        port, port_source = DEFAULT_PORT, "default"

    exports: Dict[str, str] = {}
    if host_source == "setting" and mode in ("lan", "internet") and not is_loopback_host(host):
        messages.append(
            f"Network exposure: '{mode}' from the settings store: listening on {host}:{port} "
            "(every network this machine is on" + (", and the internet if your router forwards the port" if mode == "internet" else "") + ")."
        )
        if auth_check(mode, posture).get("will_enable_user_auth"):
            exports["ABSTRACTGATEWAY_USER_AUTH"] = "1"
            exports[AUTH_MODE_SOURCE_ENV] = AUTH_SOURCE_NETWORK
            messages.append(
                f"Gateway auth: user auth enabled because network exposure '{mode}' requires it "
                "(no auth posture is configured in the environment)."
            )
        if not str(env.get("ABSTRACTGATEWAY_ALLOWED_ORIGINS") or "").strip():
            ifaces, _method = (discover or discover_interfaces)()
            hn = (hostname_fn or bonjour_hostname)()
            origins = ["http://localhost:*", "http://127.0.0.1:*", *_self_origins(port, ifaces, hn)]
            exports["ABSTRACTGATEWAY_ALLOWED_ORIGINS"] = ",".join(origins)
            messages.append(
                "Browser origins: this gateway's own LAN addresses are allowed so the console signs in from another "
                f"machine ({', '.join(origins[2:]) or 'none found'}); an address that appears later needs a restart."
            )
    for k, v in exports.items():
        env[k] = v
    if exports:
        env[NETWORK_EXPORTS_ENV] = ",".join(exports)
    env[BIND_PORT_ENV] = str(int(port))
    env[BIND_SOURCE_ENV] = f"host={host_source};port={port_source}"
    return ServeBind(
        host=host,
        port=int(port),
        host_source=host_source,
        port_source=port_source,
        mode=mode,
        mode_source=str(setting["source"]),
        messages=messages,
        exports=exports,
        blocked_reason=blocked,
        posture=posture,
    )


def record_serve_bind(data_dir: Path, bind: ServeBind) -> None:
    """Persist what this process was given (the CLI's `effective`)."""
    try:
        write_run_record(
            data_dir,
            {
                "schema": RUN_RECORD_SCHEMA,
                "pid": os.getpid(),
                "host": bind.host,
                "port": int(bind.port),
                "host_source": bind.host_source,
                "port_source": bind.port_source,
                "blocked_reason": bind.blocked_reason,
                "posture": dict(bind.posture),
                "exports": sorted(bind.exports),
                # The RUNNING process's reverse-proxy environment (the CLI
                # reports it instead of its own shell's).
                "proxy_env": proxy_env_facts(),
                "started_at": _now_iso(),
            },
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Status + change (the route, the CLI, the tray and the TUI all land here)
# ---------------------------------------------------------------------------


def _parse_bind_source(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in str(raw or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def effective_bind(data_dir: Path, *, env: Optional[Mapping[str, str]] = None, in_process: bool = True) -> Dict[str, Any]:
    """{known, bind_host, port, host_source, port_source, overridden_by_cli, pid?, blocked_reason?}.

    In the serving process the environment is authoritative; from another
    process (the CLI) the run record is, when its pid is alive."""
    from .runtime_config import BIND_HOST_ENV

    env = os.environ if env is None else env
    rec = read_run_record(data_dir)
    if in_process and str(env.get(BIND_HOST_ENV) or "").strip():
        src = _parse_bind_source(str(env.get(BIND_SOURCE_ENV) or ""))
        host = str(env.get(BIND_HOST_ENV)).strip()
        try:
            port = int(str(env.get(BIND_PORT_ENV) or "").strip())
        except ValueError:
            port = None
        blocked = rec.get("blocked_reason") if (rec and int(rec.get("pid") or -1) == os.getpid()) else None
        out = {"known": True, "bind_host": host, "port": port,
               "host_source": src.get("host", "unknown"), "port_source": src.get("port", "unknown"), "pid": os.getpid()}
    elif rec:
        from .first_run import pid_alive

        alive = pid_alive(rec.get("pid"))
        if alive is False:
            return {"known": False, "running": False, "bind_host": None, "port": None, "overridden_by_cli": False,
                    "note": f"no gateway is running for this data dir (last one: pid {rec.get('pid')}, {rec.get('host')}:{rec.get('port')})"}
        blocked = rec.get("blocked_reason")
        out = {"known": True, "bind_host": rec.get("host"), "port": rec.get("port"),
               "host_source": rec.get("host_source", "unknown"), "port_source": rec.get("port_source", "unknown"),
               "pid": rec.get("pid")}
    else:
        return {"known": False, "running": None, "bind_host": None, "port": None, "overridden_by_cli": False,
                "note": ("this process was not started by `abstractgateway serve` (no bind recorded)" if in_process
                         else "no running gateway recorded for this data dir")}
    out["running"] = True
    out["overridden_by_cli"] = out["host_source"] == "cli" or out["port_source"] == "cli"
    if blocked:
        out["blocked_reason"] = blocked
    return out


def _restart_story(effective: Dict[str, Any], restart_required: bool, auth_ok: bool, in_process: bool) -> Dict[str, Any]:
    story: Dict[str, Any] = {
        "route": "POST /api/gateway/network/restart",
        "cli": "abstractgateway network restart --url <gateway URL>",
        "tray": "Restart AbstractGateway…",
    }
    capability: Dict[str, Any] = {"restart": False, "reason": "not the serving process"}
    if in_process:
        try:
            from . import host_control

            capability = host_control.control_capabilities()
        except Exception as exc:  # noqa: BLE001
            capability = {"restart": False, "reason": str(exc)}
    applies = True
    reason = None
    if effective.get("overridden_by_cli"):
        applies = False
        reason = (
            "this gateway was started with --host/--port on its command line and a restart replays that command "
            "line. Start it without --host/--port for the setting to apply. A login item registered with "
            "`--pin-command-line` does this on purpose; `abstractgateway service enable` rewrites a pinned "
            "registration so the setting applies"
        )
    elif not auth_ok:
        applies = False
        reason = "the configured mode's auth requirement is not met (see auth.fix); a restart would fall back to localhost"
    story["available"] = bool(capability.get("restart"))
    story["applies"] = bool(applies)
    if not capability.get("restart"):
        story["unavailable_reason"] = capability.get("reason")
        story["how"] = (
            "restart the gateway yourself: tray → Restart AbstractGateway…, or stop `abstractgateway serve` and start it again"
        )
    else:
        story["how"] = "POST /api/gateway/network/restart (admin), the tray's Restart AbstractGateway…, or stop and start `abstractgateway serve`"
    if reason:
        story["reason"] = reason
    story["needed"] = bool(restart_required)
    return story


def mode_warnings(
    mode: str, port: int, *, lan_urls: List[str], proxy: Optional[Mapping[str, Any]] = None
) -> List[str]:
    """What to know about `mode`. Never an instruction to set an environment
    variable (operator rule 2026-09-24): every knob named here is a control in
    the console, the TUI and the CLI."""
    if mode == "localhost":
        return []
    first = lan_urls[0] if lan_urls else f"http://<this machine's LAN IP>:{port}"
    common = [
        "Traffic is plain HTTP: sign-in passwords, tokens and session cookies cross the network unencrypted. "
        "Use it on networks you trust.",
        "Anyone who can reach the port sees the sign-in page; accounts are the gate. Give every person their own "
        "account (Users) and never share the admin token.",
        "Browser apps (Apps page) listen on this computer only by default: other machines cannot open them unless "
        "you change where they listen too (Apps → Advanced: apps settings → Where apps listen).",
        "Engine and app installs default to OFF for other computers on a non-loopback bind; someone at this computer "
        "can still install (Settings → allow_engine_install).",
    ]
    if mode == "lan":
        return common
    origins = (proxy or {}).get("allowed_origins") or {}
    trust = (proxy or {}).get("trust_proxy") or {}
    https_origin = next((o for o in (origins.get("effective") or []) if str(o).startswith("https://")), None)
    if origins.get("overridden_by_env"):
        origin_line = (
            f"Browser origins come from the environment this gateway was started with ({origins.get('env_name')}): "
            f"{', '.join(origins.get('env_value') or []) or 'none'}. Your public https origin must be in that list."
        )
    elif https_origin:
        origin_line = f"Browsers may call this gateway from {', '.join(o for o in origins.get('value') or [])} (Reverse proxy below)."
    else:
        origin_line = (
            "Add your public https origin (for example https://gateway.example.com) under Reverse proxy below, "
            "or the console will refuse to sign in through your proxy."
        )
    trust_line = (
        "Turn on \"Trust the proxy\" under Reverse proxy only when your own proxy sits in front of every request "
        "(it makes sign-in lockouts and the audit log use the address the proxy reports)."
        if not trust.get("effective")
        else "Trust the proxy is on: correct only when every request comes through your own proxy."
    )
    return [
        "The gateway does not terminate TLS. Put it behind a TLS reverse proxy (Caddy, nginx, Traefik) or a tunnel "
        "(Cloudflare Tunnel, Tailscale Funnel, ngrok) and expose THAT, never the raw port.",
        f"If you forward a port on your router anyway: forward TCP {int(port)} to {first.split('//', 1)[-1]}; the "
        "router, its firewall and this machine's firewall are yours to configure (the gateway changes none of them).",
        "Failed sign-ins are locked out per client address with a growing wait; add rate limits at the proxy for "
        "anything public.",
        origin_line,
        trust_line,
        *common,
    ]


def network_status(
    data_dir: Path,
    *,
    env: Optional[Mapping[str, str]] = None,
    in_process: bool = True,
    lookup_public: bool = False,
    is_admin: bool = True,
    discover: Optional[Callable[[], Tuple[List[IfaceAddr], str]]] = None,
    hostname_fn: Optional[Callable[[], Optional[str]]] = None,
    public_fn: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """The whole `gateway_network_v1` payload (GET /api/gateway/network)."""
    from .runtime_config import resolve_network_setting

    env = os.environ if env is None else env
    setting = resolve_network_setting(data_dir, env=baseline_env(env))
    effective = effective_bind(data_dir, env=env, in_process=in_process)
    rec = read_run_record(data_dir)
    proxy_facts_known = True
    if in_process or not rec or not effective.get("running"):
        posture = auth_posture(env)
        posture_source = "this process" if in_process else "this shell (no running gateway recorded)"
        from .first_run import auth_mode_summary

        live = auth_mode_summary(env)
        live_user, live_token = bool(live.get("user_auth_enabled")), bool(live.get("token_configured"))
        proxy_facts = proxy_env_facts(env)
    else:
        posture = dict(rec.get("posture") or auth_posture(env))
        posture_source = f"the running gateway (pid {rec.get('pid')})"
        live_user = bool(posture.get("user_auth")) or "ABSTRACTGATEWAY_USER_AUTH" in (rec.get("exports") or [])
        live_token = bool(posture.get("token_auth"))
        proxy_facts = rec.get("proxy_env") if isinstance(rec.get("proxy_env"), dict) else None
        if proxy_facts is None:
            # A gateway started before the run record carried it: its
            # environment is unknown from here (never guessed from this shell).
            proxy_facts_known = False
            proxy_facts = {"origins_env": None, "trust_proxy_env": None, "self_origins": []}
    reverse_proxy = reverse_proxy_status(setting, proxy_facts)
    reverse_proxy["evaluated_from"] = posture_source
    if not proxy_facts_known:
        reverse_proxy["env_unknown"] = True
        reverse_proxy["note"] = (
            "The running gateway was started by an older version that did not record its environment: whether it "
            "was started with an origins or proxy override is unknown from here (the console shows it)."
        )

    mode = str(setting["mode"])
    port = int(setting["port"])
    configured = {
        "mode": mode,
        "label": MODE_LABELS[mode],
        "port": port,
        "bind_host": MODE_BIND_HOST[mode],
        "source": setting["source"],
        "port_source": setting["port_source"],
        "internet_acknowledged": setting.get("internet_acknowledged"),
    }
    if setting["source"] != "stored":
        configured["note"] = (
            "no network setting stored: `serve` keeps its historical default (0.0.0.0 when auth is configured in "
            "the environment, else 127.0.0.1)"
        )
        configured["bind_host"] = setting.get("default_bind_host") or configured["bind_host"]

    eff_host = effective.get("bind_host")
    eff_port = effective.get("port") or port
    eff_mode = mode_for_bind(eff_host, mode if mode != "localhost" else None) if effective.get("known") else "unknown"
    effective_out = {
        "mode": eff_mode,
        "label": MODE_LABELS.get(eff_mode, "Unknown"),
        "bind_host": eff_host,
        "port": effective.get("port"),
        "overridden_by_cli": bool(effective.get("overridden_by_cli")),
        "host_source": effective.get("host_source"),
        "port_source": effective.get("port_source"),
        "running": effective.get("running"),
    }
    for k in ("note", "blocked_reason", "pid"):
        if effective.get(k) is not None:
            effective_out[k] = effective[k]

    if effective.get("known"):
        same_bind = (
            (is_loopback_host(eff_host) and is_loopback_host(configured["bind_host"]))
            or str(eff_host) == str(configured["bind_host"])
            or (is_wildcard_host(eff_host) and is_wildcard_host(configured["bind_host"]))
        )
        in_sync = same_bind and int(eff_port) == int(port)
        restart_required = not in_sync
    else:
        restart_required = False

    checks = {m: auth_check(m, posture) for m in MODES}
    cur = checks[mode]
    auth = {
        # What the running gateway enforces NOW (user_auth includes a user auth
        # `serve` turned on for the loopback first run or a network mode)...
        "user_auth": live_user,
        "token_auth": live_token,
        # ...and what the operator configured (a fresh start's input).
        "configured_user_auth": bool(posture.get("user_auth")),
        "posture": posture.get("mode"),
        "source": posture.get("source"),
        "explicit": bool(posture.get("explicit")),
        "evaluated_from": posture_source,
        "ok_for_mode": bool(cur["ok"]),
        "will_enable_user_auth": bool(cur.get("will_enable_user_auth")),
    }
    if str(env.get(_auth_source_env()) or "") == AUTH_SOURCE_NETWORK and in_process:
        auth["source"] = AUTH_SOURCE_NETWORK
    if not cur["ok"]:
        auth["reason"] = cur.get("reason")
        auth["fix"] = cur.get("fix")

    ifaces, method = (discover or discover_interfaces)()
    labels = interface_labels() if discover is None else {}
    hostname = (hostname_fn or bonjour_hostname)()
    public = None
    public_note = None
    if lookup_public:
        if not is_admin:
            public_note = "the WAN address lookup is admin-only"
        elif mode != "internet" and eff_mode != "internet":
            public_note = "the WAN address is looked up only in 'internet' mode"
        else:
            public = (public_fn or lookup_public_ip)()
    addr_host = eff_host if effective.get("known") else None
    addresses = build_addresses(
        bind_host=addr_host, port=int(eff_port), interfaces=ifaces, labels=labels, hostname=hostname, public=public
    )
    lan_urls = [a["url"] for a in addresses if a["kind"] == "lan" and a.get("family") == "ipv4"]
    reachable = [a for a in addresses if a.get("reachable")]
    primary = next((a for a in reachable if a["kind"] == "lan" and a.get("family") == "ipv4"), None) or (
        reachable[0] if reachable else addresses[0]
    )

    warnings = mode_warnings(mode, port, lan_urls=lan_urls, proxy=reverse_proxy)
    if reverse_proxy["trust_proxy"].get("warning") and mode != "internet":
        warnings.append(reverse_proxy["trust_proxy"]["warning"])
    for w in reverse_proxy["allowed_origins"].get("warnings") or []:
        warnings.append(w)
    if effective_out["overridden_by_cli"]:
        warnings.insert(0, "The command line (--host/--port) overrides this setting; see restart.reason.")
    if effective.get("blocked_reason"):
        warnings.insert(0, f"The configured mode could not be applied at start: {effective['blocked_reason']}")
    if restart_required and not effective_out["overridden_by_cli"]:
        warnings.insert(0, f"Restart the gateway to apply '{mode}' on port {port} (running: {eff_host}:{eff_port}).")

    modes = []
    for m in MODES:
        c = checks[m]
        row = {"id": m, "label": MODE_LABELS[m], "bind_host": MODE_BIND_HOST[m], "selected": m == mode,
               "allowed": bool(c["ok"]), "requires_acknowledgement": m == "internet"}
        if not c["ok"]:
            row["reason"] = c.get("reason")
            row["fix"] = c.get("fix")
        modes.append(row)

    out = {
        "schema": NETWORK_SCHEMA,
        "writable": bool(is_admin),
        "configured": configured,
        "effective": effective_out,
        "restart_required": bool(restart_required),
        "restart": _restart_story(effective, restart_required, bool(cur["ok"]), in_process),
        "auth": auth,
        "reverse_proxy": reverse_proxy,
        "modes": modes,
        "addresses": addresses,
        "copy_hint": primary.get("url"),
        "warnings": warnings,
        "discovery": {"method": method, "interfaces": len({i.interface for i in ifaces}), "hostname": hostname,
                      "public_lookup": bool(public is not None)},
        "checked_at": _now_iso(),
    }
    if public_note:
        out["discovery"]["public_note"] = public_note
    return out


def parse_trust_proxy(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    t = str(raw if raw is not None else "").strip().lower()
    if t in {"on", "true", "1", "yes"}:
        return True
    if t in {"off", "false", "0", "no"}:
        return False
    raise NetworkSettingError(f"trust_proxy is on or off (got {raw!r})")


def apply_network_change(
    data_dir: Path,
    *,
    mode: Any = None,
    port: Any = None,
    acknowledge_internet: bool = False,
    allowed_origins: Any = None,
    trust_proxy: Any = None,
    actor: str,
    env: Optional[Mapping[str, str]] = None,
    in_process: bool = True,
    status_kwargs: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Validate and persist a network change. Returns (http_status, body).

    Any subset of {mode, port, allowed_origins, trust_proxy} (mode/port are
    applied at the next start; the two reverse-proxy fields apply to the next
    request). 400 invalid input (`errors[]` names every bad origin); 409
    refused (auth, acknowledgement) with `refused_reason` + `fix`; NOTHING is
    written on a 400/409. 200 stored: the new posture, `restart_required`,
    `reverse_proxy`, and `changed{field: {from, to, applies}}` (applies =
    live | restart | overridden_by_env) — the route writes it to the audit log."""
    from .runtime_config import resolve_network_setting, write_network_setting

    env = os.environ if env is None else env
    try:
        m = normalize_mode(mode) if mode not in (None, "") else None
        p = validate_port(port) if port not in (None, "") else None
        origins = validate_origins(allowed_origins) if allowed_origins is not None else None
        tp = parse_trust_proxy(trust_proxy) if trust_proxy is not None else None
    except OriginsError as exc:
        return 400, {
            "ok": False,
            "reason_code": "invalid_origins",
            "field": "allowed_origins",
            "refused_reason": (
                f"{len(exc.errors)} origin{'s are' if len(exc.errors) != 1 else ' is'} not valid (nothing was saved): "
                + "; ".join(f"{e['value']}: {e['error']}" for e in exc.errors)
            ),
            "errors": exc.errors,
        }
    except NetworkSettingError as exc:
        return 400, {"ok": False, "reason_code": "invalid", "refused_reason": str(exc)}
    if m is None and p is None and origins is None and tp is None:
        return 400, {
            "ok": False,
            "reason_code": "invalid",
            "refused_reason": "nothing to change: send mode, port, allowed_origins or trust_proxy",
        }
    current = resolve_network_setting(data_dir, env=baseline_env(env))
    target_port = p if p is not None else int(current["port"])

    warnings: List[str] = []
    if m is not None:
        rec = read_run_record(data_dir)
        if not in_process and rec and effective_bind(data_dir, env=env, in_process=False).get("running"):
            posture = dict(rec.get("posture") or auth_posture(env))
        else:
            posture = auth_posture(env)
        check = auth_check(m, posture)
        warnings = mode_warnings(m, target_port, lan_urls=[])
        if not check["ok"]:
            return 409, {
                "ok": False,
                "reason_code": check.get("reason_code"),
                "refused_reason": check.get("reason"),
                "fix": check.get("fix"),
                "configured": {"mode": current["mode"], "port": current["port"], "source": current["source"]},
                "restart_required": None,
                "warnings": warnings,
            }
        if m == "internet" and not bool(acknowledge_internet):
            return 409, {
                "ok": False,
                "reason_code": "acknowledgement_required",
                "refused_reason": (
                    "'internet' needs an explicit acknowledgement: read the warnings, then send "
                    "acknowledge_internet: true (CLI: --acknowledge-internet)"
                ),
                "configured": {"mode": current["mode"], "port": current["port"], "source": current["source"]},
                "restart_required": None,
                "warnings": warnings,
            }
    extra: List[str] = []
    if p is not None and p < 1024:
        extra.append(f"Port {p} is privileged: binding it needs administrator rights on most systems.")
    eff = effective_bind(data_dir, env=env, in_process=in_process)
    if p is not None and p != eff.get("port"):
        try:
            from .first_run import port_is_free

            if not port_is_free("127.0.0.1", p):
                extra.append(f"Port {p} is in use by another process right now; the restart will fail unless it is freed.")
        except Exception:
            pass
    write_network_setting(
        data_dir,
        mode=m,
        port=p,
        internet_acknowledged={"at": _now_iso(), "by": str(actor)} if m == "internet" else None,
        actor=actor,
        allowed_origins=origins,
        trust_proxy=tp,
    )
    status = network_status(data_dir, env=env, in_process=in_process, **(status_kwargs or {}))
    rp = status["reverse_proxy"]
    changed: Dict[str, Any] = {}
    if m is not None and (m != current["mode"] or current["source"] != "stored"):
        changed["mode"] = {"from": current["mode"], "to": m, "applies": "restart" if status["restart_required"] else "live"}
    if p is not None and p != current["port"]:
        changed["port"] = {"from": current["port"], "to": p, "applies": "restart" if status["restart_required"] else "live"}
    if origins is not None and origins != list(current.get("allowed_origins") or []):
        changed["allowed_origins"] = {
            "from": list(current.get("allowed_origins") or []),
            "to": origins,
            "applies": "overridden_by_env" if rp["allowed_origins"]["overridden_by_env"] else "live",
        }
    if tp is not None and (tp != bool(current.get("trust_proxy")) or current.get("trust_proxy_source") != "stored"):
        changed["trust_proxy"] = {
            "from": bool(current.get("trust_proxy")),
            "to": tp,
            "applies": "overridden_by_env" if rp["trust_proxy"]["overridden_by_env"] else "live",
        }
    for field_name in ("allowed_origins", "trust_proxy"):
        if field_name in changed and changed[field_name]["applies"] == "overridden_by_env":
            extra.append(
                f"Saved, but not in effect: {rp[field_name].get('note') or 'the environment this gateway was started with decides.'}"
            )
    body = {
        "ok": True,
        "configured": status["configured"],
        "effective": status["effective"],
        "restart_required": status["restart_required"],
        "restart": status["restart"],
        "auth": status["auth"],
        "reverse_proxy": rp,
        "changed": changed,
        "warnings": extra + status["warnings"],
        "copy_hint": status["copy_hint"],
    }
    return 200, body


# ---------------------------------------------------------------------------
# CLI: `abstractgateway network status|set|addresses|restart`
# ---------------------------------------------------------------------------


def add_network_subparser(sub: Any) -> None:
    net = sub.add_parser(
        "network",
        help="Network exposure (localhost / lan / internet) and the addresses to reach this gateway",
    )
    nsub = net.add_subparsers(dest="network_cmd", required=True)
    for name, text in (("status", "Configured vs running exposure, auth, reverse proxy, addresses, warnings"),
                       ("show", "Same as `status`: every network setting and where each value comes from")):
        st = nsub.add_parser(name, help=text)
        st.add_argument("--json", action="store_true", help="Emit the gateway_network_v1 payload")
        st.add_argument("--data-dir", default=None, help="Gateway data dir (default: same resolution as `serve`)")
    se = nsub.add_parser(
        "set",
        help="Store a mode/port (applied at the next start) and/or the reverse-proxy settings (applied to the next request)",
    )
    se.add_argument("mode", nargs="?", choices=list(MODES), default=None,
                    help="localhost | lan | internet (omit to keep the stored mode)")
    se.add_argument("--port", type=int, default=None, help="Listening port (default: keep the stored one, else 8080)")
    se.add_argument("--acknowledge-internet", action="store_true", dest="acknowledge_internet",
                    help="Required for `internet`: you read the warnings (no TLS here; port forwarding is yours)")
    se.add_argument("--allowed-origins", default=None, dest="allowed_origins", metavar="ORIGIN[,ORIGIN...]",
                    help="Browser origins allowed besides this machine's own, comma-separated, each scheme://host[:port] "
                    "(e.g. https://gateway.example.com). Replaces the stored list; an empty string clears it")
    se.add_argument("--trust-proxy", default=None, dest="trust_proxy", choices=["on", "off"],
                    help="on = take the client address from X-Forwarded-For (only when your own proxy sits in front of "
                    "every request)")
    se.add_argument("--json", action="store_true")
    se.add_argument("--data-dir", default=None)
    ad = nsub.add_parser("addresses", help="Every URL a client can use; --copy puts the primary one on the clipboard")
    ad.add_argument("--copy", action="store_true", help="Copy the primary reachable URL to the clipboard")
    ad.add_argument("--public", action="store_true", help="Also look up the WAN address (one HTTPS call; internet mode only)")
    ad.add_argument("--json", action="store_true")
    ad.add_argument("--data-dir", default=None)
    rs = nsub.add_parser("restart", help="Ask the RUNNING gateway to restart and apply the setting (admin)")
    rs.add_argument("--url", default=os.environ.get("ABSTRACTGATEWAY_URL") or None,
                    help="Gateway base URL (default: $ABSTRACTGATEWAY_URL, else the running gateway's recorded URL)")
    rs.add_argument("--token", default=None, help="Bearer token (default: $ABSTRACTGATEWAY_AUTH_TOKEN)")
    rs.add_argument("--force", action="store_true", help="Restart even when nothing is pending")
    rs.add_argument("--data-dir", default=None)


def _print_status(payload: Dict[str, Any]) -> None:
    c, e = payload["configured"], payload["effective"]
    print(f"configured: {c['label']} ({c['mode']}) on port {c['port']}  [{c['source']}]")
    if e.get("running"):
        cli = "  (overridden by the command line)" if e.get("overridden_by_cli") else ""
        print(f"running:    {e['label']} — bound {e['bind_host']}:{e['port']}{cli}")
    else:
        print(f"running:    {e.get('note') or 'unknown'}")
    if payload.get("restart_required"):
        r = payload["restart"]
        print(f"RESTART REQUIRED to apply — {r.get('reason') or r.get('how')}")
    a = payload["auth"]
    print(
        f"auth:       user auth {'on' if a['user_auth'] else 'off'}, token {'set' if a['token_auth'] else 'unset'}"
        f" ({a.get('evaluated_from')}); ok for '{c['mode']}': {'yes' if a['ok_for_mode'] else 'NO'}"
    )
    if a.get("fix"):
        print(f"            fix: {a['fix']}")
    _print_reverse_proxy(payload.get("reverse_proxy") or {})


def _print_reverse_proxy(rp: Mapping[str, Any]) -> None:
    o, t = rp.get("allowed_origins") or {}, rp.get("trust_proxy") or {}
    if not o:
        return
    mine = ", ".join(o.get("value") or []) or "none"
    print(f"origins:    {mine}  [{o.get('source')}]" + ("  (OVERRIDDEN by the environment)" if o.get("overridden_by_env") else ""))
    print(f"            in effect: {', '.join(o.get('effective') or [])}")
    print(f"proxy:      trust X-Forwarded-For {'on' if t.get('value') else 'off'}  [{t.get('source')}]"
          + (f"  (OVERRIDDEN by the environment: {'on' if t.get('effective') else 'off'})" if t.get("overridden_by_env") else ""))
    for note in (o.get("note"), t.get("note"), rp.get("note")):
        if note:
            print(f"            {note}")


def _print_addresses(payload: Dict[str, Any]) -> None:
    for a in payload["addresses"]:
        if not a.get("url"):
            print(f"  {a['kind']:9} {a.get('note')}")
            continue
        where = a.get("interface_label") or a.get("interface") or ""
        mark = "●" if a.get("reachable") else ("?" if a.get("reachable") is None else "○")
        print(f"  {mark} {a['kind']:9} {a['url']:<42} {('(' + where + ')') if where else ''}")
    print(f"primary: {payload.get('copy_hint')}")


def _login_name() -> str:
    try:
        import getpass

        return getpass.getuser() or "operator"
    except Exception:
        return "operator"


def run_network_command(args: Any) -> int:
    from .host_paths import resolve_data_dir

    # `--data-dir` wins (it was accepted but ignored before mission Z).
    explicit = getattr(args, "data_dir", None)
    data_dir = Path(str(explicit)).expanduser().resolve() if explicit else resolve_data_dir().path
    cmd = getattr(args, "network_cmd", "")
    if cmd == "restart":
        return _run_network_restart(args, data_dir)
    if cmd == "set":
        origins_arg = getattr(args, "allowed_origins", None)
        status, body = apply_network_change(
            data_dir,
            mode=args.mode,
            port=args.port,
            acknowledge_internet=bool(args.acknowledge_internet),
            allowed_origins=None if origins_arg is None else [o for o in str(origins_arg).split(",") if o.strip()],
            trust_proxy=getattr(args, "trust_proxy", None),
            actor=f"cli/{_login_name()}",
            in_process=False,
        )
        if args.json:
            print(json.dumps(body, indent=2, default=str))
        elif status != 200:
            print(f"refused: {body.get('refused_reason')}", file=sys.stderr)
            if body.get("fix"):
                print(f"fix: {body['fix']}", file=sys.stderr)
            for w in body.get("warnings") or []:
                print(f"  - {w}", file=sys.stderr)
        else:
            c = body["configured"]
            print(f"stored: {c['label']} ({c['mode']}) on port {c['port']}")
            for name, ch in (body.get("changed") or {}).items():
                when = {"live": "applies now", "restart": "applies at the next start",
                        "overridden_by_env": "saved, NOT in effect (environment override)"}.get(ch.get("applies"), "")
                print(f"changed: {name} {ch.get('from')!r} -> {ch.get('to')!r} ({when})")
            _print_reverse_proxy(body.get("reverse_proxy") or {})
            if body.get("restart_required"):
                r = body["restart"]
                print("restart required: " + (r.get("reason") if not r.get("applies") else
                      "`abstractgateway network restart`, the tray's Restart AbstractGateway…, or stop and start `serve`"))
            for w in body.get("warnings") or []:
                print(f"  - {w}")
        return 0 if status == 200 else 1
    payload = network_status(data_dir, in_process=False, lookup_public=bool(getattr(args, "public", False)))
    if cmd in ("status", "show"):
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            _print_status(payload)
            _print_addresses(payload)
            for w in payload.get("warnings") or []:
                print(f"  - {w}")
        return 0
    if cmd == "addresses":
        if args.json:
            print(json.dumps({"addresses": payload["addresses"], "copy_hint": payload["copy_hint"],
                              "discovery": payload["discovery"]}, indent=2, default=str))
        else:
            _print_addresses(payload)
        if getattr(args, "copy", False):
            from .tray.platform import copy_to_clipboard

            url = str(payload.get("copy_hint") or "")
            if url and copy_to_clipboard(url):
                print(f"copied: {url}", file=sys.stderr)
            else:
                print("could not copy to the clipboard (pbcopy / clip / wl-copy / xclip / xsel not available)", file=sys.stderr)
                return 1
        return 0
    print(f"unknown network command {cmd!r}", file=sys.stderr)
    return 2


def _run_network_restart(args: Any, data_dir: Path) -> int:
    from .tray.client import GatewayClient

    url = getattr(args, "url", None)
    if not url:
        rec = read_run_record(data_dir)
        if not rec:
            print("no running gateway recorded for this data dir; pass --url", file=sys.stderr)
            return 1
        url = url_for("127.0.0.1" if is_wildcard_host(rec.get("host")) else str(rec.get("host")), int(rec.get("port") or DEFAULT_PORT))
    token = args.token if args.token is not None else os.environ.get("ABSTRACTGATEWAY_AUTH_TOKEN", "")
    client = GatewayClient(str(url), token)
    res = client._request("POST", "/network/restart", body={"force": bool(getattr(args, "force", False))}, timeout=30.0)
    print(json.dumps(res.data if res.data is not None else {"error": res.error, "status": res.status}, indent=2, default=str))
    if not res.ok:
        print(f"abstractgateway network restart: {res.detail}", file=sys.stderr)
        return 1
    return 0
