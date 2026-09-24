"""No-terminal first run: loopback auth default, one-time claim codes, the
serve record and the per-data-dir first-run state (2026-09-23).

THE PROBLEM. A fresh install used to need three terminal skills before the
console opened: exporting an auth token, knowing where the data dir went, and
pasting a raw admin token into a sign-in form. The pieces here remove all three
for the one case that is safe to make automatic: a gateway bound to LOOPBACK on
the user's own machine.

- **Loopback auth default** (`loopback_auth_default_applies`): a bare
  `abstractgateway serve` on 127.0.0.1/localhost/::1, with no auth configured,
  turns user auth on and bootstraps `default/admin`. Any explicit auth setting
  (token, `ABSTRACTGATEWAY_USER_AUTH`, `ABSTRACTGATEWAY_AUTH_MODE`, security
  off) is respected as before, and a non-loopback bind without auth still
  refuses to start.
- **Claim codes** (`mint_claim` / `redeem_claim`): a single-use, 10-minute
  code minted by the LOCAL CLI (it writes under `<data>/auth/claims/`, so only
  someone who can write the data dir can mint one) and redeemed by the console
  for an admin browser session. Stored as a SHA-256 digest only; redemption
  deletes the file, and whoever deletes it wins, so a code can never be used
  twice. The route that redeems it accepts loopback socket peers only.
- **Serve record** (`<data>/run/gateway-serve.json`): the running gateway
  writes its host/port/URL/auth mode so `abstractgateway-config claim-url`
  can build the right link without being told the port.
- **First-run state** (`<data>/first_run.json`): whether the console's
  first-run wizard has been completed for this data dir.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import secrets
import socket
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, MutableMapping, Optional

CLAIM_TTL_S = 600
CLAIM_PREFIX = "agclaim_"
_CLAIM_RE = re.compile(r"^agclaim_[A-Za-z0-9_-]{20,80}$")

SERVE_RECORD_SCHEMA = "gateway_serve_record_v1"
FIRST_RUN_SCHEMA = "gateway_first_run_v1"
CLAIM_SCHEMA = "gateway_claim_v1"

AUTH_MODE_SOURCE_ENV = "ABSTRACTGATEWAY_AUTH_MODE_SOURCE"
AUTH_SOURCE_LOOPBACK_DEFAULT = "loopback_default"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Every variable through which an operator states an auth posture. Any of
# them set = the operator decided; the loopback default never overrides that.
_USER_AUTH_ENV = (
    "ABSTRACTGATEWAY_USER_AUTH",
    "ABSTRACTGATEWAY_MULTI_USER",
    "ABSTRACTFLOW_GATEWAY_USER_AUTH",
    "ABSTRACTGATEWAY_AUTH_MODE",
)
_TOKEN_ENV = (
    "ABSTRACTGATEWAY_AUTH_TOKEN",
    "ABSTRACTGATEWAY_AUTH_TOKENS",
    "ABSTRACTFLOW_GATEWAY_AUTH_TOKEN",
    "ABSTRACTFLOW_GATEWAY_AUTH_TOKENS",
)
_POSTURE_ENV = (
    "ABSTRACTGATEWAY_SECURITY",
    "ABSTRACTFLOW_GATEWAY_SECURITY",
    "ABSTRACTGATEWAY_PROTECT_WRITE",
    "ABSTRACTFLOW_GATEWAY_PROTECT_WRITE",
    # Read protection off = every anonymous READ runs as the local admin
    # (security middleware): a posture statement too (mission AA finding,
    # closed in mission Z).
    "ABSTRACTGATEWAY_PROTECT_READ",
    "ABSTRACTFLOW_GATEWAY_PROTECT_READ",
)


class ClaimError(Exception):
    """A claim code that cannot be redeemed. `reason_code` is stable."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(raw: Any) -> Optional[datetime.datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)
    except Exception:
        return None


def _set(env: Mapping[str, str], name: str) -> bool:
    return bool(str(env.get(name) or "").strip())


def is_loopback_host(host: str) -> bool:
    h = str(host or "").strip().lower().strip("[]")
    if h in LOOPBACK_HOSTS:
        return True
    try:
        import ipaddress

        return bool(ipaddress.ip_address(h).is_loopback)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Auth posture
# ---------------------------------------------------------------------------


def explicit_auth_configured(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when the operator stated ANY auth posture through the environment."""
    env = os.environ if env is None else env
    if str(env.get(AUTH_MODE_SOURCE_ENV) or "").strip() == AUTH_SOURCE_LOOPBACK_DEFAULT:
        # Our own export (inherited by a relaunched/child process) is not an
        # operator statement.
        return any(_set(env, n) for n in (*_TOKEN_ENV, *_POSTURE_ENV))
    return any(_set(env, n) for n in (*_TOKEN_ENV, *_USER_AUTH_ENV, *_POSTURE_ENV))


def default_bind_host(env: Optional[Mapping[str, str]] = None) -> str:
    """`serve`'s bind when `--host` is omitted.

    Unconfigured (a first run): loopback, which is what makes a bare `serve`
    start. Configured deployments keep the historical `0.0.0.0` default, so
    every existing setup that relied on it binds exactly as before."""
    return "0.0.0.0" if explicit_auth_configured(env) else "127.0.0.1"


def loopback_auth_default_applies(host: str, env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    if not is_loopback_host(host):
        return False
    return not explicit_auth_configured(env)


def apply_loopback_auth_default(host: str, env: Optional[MutableMapping[str, str]] = None) -> bool:
    """Turn user auth on for an unconfigured loopback serve. Returns True when applied."""
    env = os.environ if env is None else env
    if str(env.get(AUTH_MODE_SOURCE_ENV) or "").strip() == AUTH_SOURCE_LOOPBACK_DEFAULT and is_loopback_host(host):
        env["ABSTRACTGATEWAY_USER_AUTH"] = "1"
        return True
    if not loopback_auth_default_applies(host, env):
        return False
    env["ABSTRACTGATEWAY_USER_AUTH"] = "1"
    env[AUTH_MODE_SOURCE_ENV] = AUTH_SOURCE_LOOPBACK_DEFAULT
    return True


def auth_mode_summary(env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """The auth posture as data (stable keys; `abstractframework doctor` reads it).

    `mode`: `users` | `token` | `users+token` | `open` (security or write
    protection off) | `loopback_auto` (nothing configured: `serve` on loopback
    enables user auth automatically, a non-loopback bind refuses)."""
    env = os.environ if env is None else env
    from .users import _as_bool  # local import: users imports nothing from here

    security_on = _as_bool(env.get("ABSTRACTGATEWAY_SECURITY") or env.get("ABSTRACTFLOW_GATEWAY_SECURITY") or "1", True)
    protect_write = _as_bool(
        env.get("ABSTRACTGATEWAY_PROTECT_WRITE") or env.get("ABSTRACTFLOW_GATEWAY_PROTECT_WRITE") or "1", True
    )
    protect_read = _as_bool(
        env.get("ABSTRACTGATEWAY_PROTECT_READ") or env.get("ABSTRACTFLOW_GATEWAY_PROTECT_READ") or "1", True
    )
    token = any(_set(env, n) for n in _TOKEN_ENV)
    user_raw = env.get("ABSTRACTGATEWAY_USER_AUTH") or env.get("ABSTRACTGATEWAY_MULTI_USER") or env.get("ABSTRACTFLOW_GATEWAY_USER_AUTH")
    mode_raw = str(env.get("ABSTRACTGATEWAY_AUTH_MODE") or "").strip().lower()
    if user_raw is not None and str(user_raw).strip():
        users = _as_bool(user_raw, False)
    else:
        users = mode_raw in {"user", "users", "multi-user", "multi_user", "hosted"}
    source = "loopback_default" if str(env.get(AUTH_MODE_SOURCE_ENV) or "").strip() == AUTH_SOURCE_LOOPBACK_DEFAULT else "env"
    if not security_on or not protect_write:
        mode = "open"
    elif users and token:
        mode = "users+token"
    elif users:
        mode = "users"
    elif token:
        mode = "token"
    else:
        mode = "loopback_auto"
        source = "default"
    return {
        "mode": mode,
        "source": source,
        "user_auth_enabled": bool(users),
        "token_configured": bool(token),
        "security_enabled": bool(security_on),
        # False = anonymous reads are answered as the local admin (the
        # middleware's PROTECT_READ=0 branch): never acceptable off loopback.
        "read_protected": bool(protect_read),
    }


# ---------------------------------------------------------------------------
# Claim codes
# ---------------------------------------------------------------------------


def claims_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "auth" / "claims"


def _digest(code: str) -> str:
    return hashlib.sha256(str(code).encode("utf-8")).hexdigest()


def _write_private_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except Exception:
        pass
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)


def prune_claims(data_dir: Path) -> int:
    """Delete expired (or unreadable) claim files. Returns how many remain live."""
    d = claims_dir(data_dir)
    if not d.is_dir():
        return 0
    now = _now()
    live = 0
    for f in d.glob("*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            exp = _parse_iso(rec.get("expires_at")) if isinstance(rec, dict) else None
        except Exception:
            exp = None
        if exp is None or exp <= now:
            try:
                f.unlink()
            except Exception:
                pass
            continue
        live += 1
    return live


def pending_claims(data_dir: Path) -> Dict[str, Any]:
    """Unexpired, unredeemed claim codes for this data dir (count + soonest expiry)."""
    d = claims_dir(data_dir)
    out: Dict[str, Any] = {"pending": 0, "next_expires_at": None}
    if not d.is_dir():
        return out
    now = _now()
    soonest: Optional[datetime.datetime] = None
    for f in d.glob("*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            exp = _parse_iso(rec.get("expires_at"))
        except Exception:
            continue
        if exp is None or exp <= now:
            continue
        out["pending"] += 1
        if soonest is None or exp < soonest:
            soonest = exp
    out["next_expires_at"] = _iso(soonest) if soonest else None
    return out


def mint_claim(
    *,
    data_dir: Path,
    tenant_id: str = "default",
    user_id: str = "admin",
    ttl_s: int = CLAIM_TTL_S,
    created_by: str = "cli",
) -> Dict[str, Any]:
    """Mint one single-use claim code. The RAW code is returned once and never stored."""
    ttl = max(30, min(int(ttl_s), 3600))
    prune_claims(data_dir)
    code = CLAIM_PREFIX + secrets.token_urlsafe(32)
    digest = _digest(code)
    now = _now()
    expires = now + datetime.timedelta(seconds=ttl)
    record = {
        "schema": CLAIM_SCHEMA,
        "digest": digest,
        "tenant_id": str(tenant_id or "default"),
        "user_id": str(user_id or "admin"),
        "created_at": _iso(now),
        "expires_at": _iso(expires),
        "created_by": str(created_by or "cli"),
        "pid": os.getpid(),
    }
    path = claims_dir(data_dir) / f"{digest}.json"
    _write_private_json(path, record)
    return {"code": code, "expires_at": record["expires_at"], "ttl_s": ttl, "tenant_id": record["tenant_id"], "user_id": record["user_id"]}


def redeem_claim(code: str, *, data_dir: Path) -> Dict[str, Any]:
    """Consume a claim code. Raises ClaimError; success deletes the code for good."""
    raw = str(code or "").strip()
    if not _CLAIM_RE.match(raw):
        raise ClaimError("claim_invalid", "This first-run link is not valid. Mint a new one with `abstractgateway-config claim-url`.")
    path = claims_dir(data_dir) / f"{_digest(raw)}.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ClaimError(
            "claim_unknown",
            "This first-run link was already used or never existed. Mint a new one with `abstractgateway-config claim-url`.",
        ) from None
    # Single use: the redeemer that deletes the file wins; a concurrent second
    # redeemer finds it gone.
    try:
        path.unlink()
    except FileNotFoundError:
        raise ClaimError("claim_unknown", "This first-run link was already used.") from None
    try:
        rec = json.loads(text)
    except Exception:
        raise ClaimError("claim_invalid", "This first-run link's record is unreadable; mint a new one.") from None
    if not isinstance(rec, dict) or rec.get("digest") != _digest(raw):
        raise ClaimError("claim_invalid", "This first-run link's record does not match; mint a new one.")
    exp = _parse_iso(rec.get("expires_at"))
    if exp is None or exp <= _now():
        raise ClaimError(
            "claim_expired",
            "This first-run link expired (links last 10 minutes). Mint a new one with `abstractgateway-config claim-url`.",
        )
    return rec


# ---------------------------------------------------------------------------
# Serve record
# ---------------------------------------------------------------------------


def serve_record_path(data_dir: Path) -> Path:
    return Path(data_dir) / "run" / "gateway-serve.json"


def browser_base_url(host: str, port: int) -> str:
    """The URL a browser ON THIS MACHINE uses to reach a gateway bound to `host`."""
    h = str(host or "").strip()
    if h in {"", "0.0.0.0", "127.0.0.1", "localhost"}:
        return f"http://127.0.0.1:{int(port)}"
    if h in {"::", "::1"}:
        return f"http://[::1]:{int(port)}"
    if ":" in h and not h.startswith("["):
        return f"http://[{h}]:{int(port)}"
    return f"http://{h}:{int(port)}"


def write_serve_record(
    *,
    data_dir: Path,
    host: str,
    port: int,
    auth: Dict[str, Any],
    data_dir_source: str,
    version: str = "",
) -> Path:
    path = serve_record_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    base = browser_base_url(host, port)
    payload = {
        "schema": SERVE_RECORD_SCHEMA,
        "pid": os.getpid(),
        "host": str(host),
        "port": int(port),
        "url": base,
        "console_url": base + "/console",
        "loopback": bool(is_loopback_host(host)),
        "auth": dict(auth or {}),
        "data_dir": str(Path(data_dir)),
        "data_dir_source": str(data_dir_source or ""),
        "version": str(version or ""),
        "started_at": _iso(_now()),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def clear_serve_record(data_dir: Path) -> None:
    path = serve_record_path(data_dir)
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(rec, dict) and int(rec.get("pid") or -1) == os.getpid():
        try:
            path.unlink()
        except Exception:
            pass


def pid_alive(pid: Any) -> Optional[bool]:
    """Is `pid` a live process? None when this platform cannot say safely.

    NEVER `os.kill(pid, 0)` on Windows: there it is TerminateProcess."""
    try:
        p = int(pid)
    except Exception:
        return False
    if p <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, p)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return None
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        os.kill(p, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None
    return True


def read_serve_record(data_dir: Path) -> Optional[Dict[str, Any]]:
    """The last serve record for this data dir, with `alive` (pid check)."""
    path = serve_record_path(data_dir)
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(rec, dict):
        return None
    rec["alive"] = pid_alive(rec.get("pid"))
    return rec


def port_is_free(host: str, port: int) -> bool:
    """Can a server bind `host:port` right now? (A bind probe: nothing is contacted.)"""
    family = socket.AF_INET6 if ":" in str(host) else socket.AF_INET
    bind_host = "127.0.0.1" if str(host) in {"", "localhost"} else str(host).strip("[]")
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        if not sys.platform.startswith("win"):
            # Match uvicorn: it sets SO_REUSEADDR, so a port in TIME_WAIT is
            # bindable for it and must read as free here too.
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((bind_host, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------------------------------------------------------------------------
# First-run state
# ---------------------------------------------------------------------------


def first_run_path(data_dir: Path) -> Path:
    return Path(data_dir) / "first_run.json"


def first_run_state(data_dir: Path) -> Dict[str, Any]:
    path = first_run_path(data_dir)
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        rec = None
    completed = isinstance(rec, dict) and bool(rec.get("completed_at"))
    return {
        "schema": FIRST_RUN_SCHEMA,
        "completed": bool(completed),
        "completed_at": rec.get("completed_at") if completed else None,
        "completed_by": rec.get("completed_by") if completed else None,
        "outcome": rec.get("outcome") if completed else None,
    }


def mark_first_run_complete(data_dir: Path, *, by: str, outcome: str = "finished") -> Dict[str, Any]:
    path = first_run_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": FIRST_RUN_SCHEMA,
        "completed_at": _iso(_now()),
        "completed_by": str(by or ""),
        "outcome": "skipped" if str(outcome) == "skipped" else "finished",
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return first_run_state(data_dir)


def claim_url(base_url: str, code: str) -> str:
    return f"{str(base_url).rstrip('/')}/console#claim={code}"
