"""Browser apps managed by the gateway: Node.js for the user, npm packages, processes.

The five browser apps (Flow, Code, Observer, Continuum, Entity) are npm
packages whose `bin/cli.js` starts a small Node server. That server is not
optional: it holds the app-origin gateway session (HttpOnly cookies, CSRF, the
`/api/connection/gateway` sign-in endpoint, the same-origin `/api/*` proxy that
SSE rides on), and four of the five bundles load their assets from absolute
`/assets/` paths. So the gateway RUNS each app's own server as a child process
(decision A in docs/apps.md) instead of serving `dist/` itself.

What this module does, with every step visible as a job (ADR-0026: progress,
a plain message, and the full log behind `details` on failure):

- Node.js: uses a system Node >= 18 when there is one; otherwise installs the
  `nodejs-wheel-binaries` wheel from PyPI (the same Node build the installer's
  `--with-apps` gets through `uv tool install nodejs-wheel`) into
  `<data_dir>/runtime/node/<version>/`, sha256-checked, no admin rights.
- Apps: downloads `@abstractframework/<name>` from the npm registry into
  `<data_dir>/apps/<id>/<version>/`, checks the tarball against the registry's
  sha512 integrity, and installs its runtime dependencies with npm (a package
  without dependencies is unpacked directly).
- Processes: starts an app on a free port with the gateway URL configured,
  restarts it after a crash (at most 3 times a minute), stops it with the
  gateway, starts the apps marked enabled when the gateway starts (from the
  gateway process itself; never launchd, systemd or a login item), and writes
  each app's output to `<data_dir>/logs/apps/<id>.log`.
- Sign-in handover: a one-time code lets the console open an app already
  signed in to this gateway (see `mint_handover`).
"""

from __future__ import annotations

import base64
import collections
import concurrent.futures
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

NODE_MIN_MAJOR = 18
MANAGED_NODE_MAJOR = 24  # the Node LTS line the gateway installs for the user
NODE_WHEEL_PROJECT = "nodejs-wheel-binaries"
DEFAULT_NPM_REGISTRY = "https://registry.npmjs.org"
DEFAULT_PYPI_URL = "https://pypi.org/pypi"
REGISTRY_CACHE_TTL_S = 600.0
REGISTRY_FAIL_TTL_S = 60.0
REGISTRY_TIMEOUT_S = 4.0
DOWNLOAD_TIMEOUT_S = 60.0
HANDOVER_TTL_S = 120.0
DESKTOP_HANDOVER_SCHEMA = "abstractgateway.desktop_handover.v1"
READY_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 5.0
RESTART_WINDOW_S = 60.0
MAX_RESTARTS = 3
RESTART_BACKOFF_S = 1.0  # 1 s, 2 s, 4 s between crash restarts
APP_LOG_MAX_BYTES = 2_000_000
JOB_LOG_TAIL_LINES = 400
JOB_DETAILS_MAX_BYTES = 256_000

ENV_NODE = "ABSTRACTGATEWAY_APPS_NODE"  # auto (default) | managed | system | /path/to/node
ENV_PORTS = "ABSTRACTGATEWAY_APPS_PORTS"  # e.g. 3100-3199
ENV_HOST = "ABSTRACTGATEWAY_APPS_HOST"  # bind host for the apps (default 127.0.0.1)
ENV_REGISTRY = "ABSTRACTGATEWAY_APPS_NPM_REGISTRY"
ENV_PYPI = "ABSTRACTGATEWAY_APPS_PYPI_URL"
DEFAULT_PORT_RANGE = (3100, 3199)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


# THE usual port of each app: the stack port map of `scripts/start-local.sh`
# (header "Operator port map", same as start.sh), which is what the operator's
# dev stack runs: observer 3001, continuum 3002, code/web 3003, entity 3004,
# flow 3005 (root backlog 0859 aligns the standalone launchers on it). ONE
# table: the managed-launch default port AND the port the external-app probe
# looks at first. Also the order the apps are listed in (console, tray).
STACK_PORTS: Tuple[Tuple[str, int], ...] = (
    ("observer", 3001),
    ("continuum", 3002),
    ("code", 3003),
    ("entity", 3004),
    ("flow", 3005),
)
STACK_PORT: Dict[str, int] = dict(STACK_PORTS)
# Older launcher defaults (root backlog 0859: scripts/lib/apps_common.sh ran
# flow on 3000 and entity on 3007): probed too, after the stack ports, so an
# app started by an older script is still found. The identity check (the
# page title) decides WHICH app answers, never the port.
LEGACY_PROBE_PORTS: Tuple[int, ...] = (3000, 3007)


@dataclass(frozen=True)
class AppSpec:
    id: str
    name: str
    package: str
    description: str
    default_port: int
    cookie_prefix: str  # the app server's `<prefix>_gateway_{url,session,csrf}` cookies
    gateway_url_env: str
    # The start of the app's `<title>` as its server sends it for `/` (how an
    # app started outside the gateway is recognised: none of the five apps
    # has an identity route that answers without a gateway sign-in).
    html_title: str = ""

    @property
    def short_name(self) -> str:
        return self.package.split("/")[-1]


APPS: Tuple[AppSpec, ...] = (
    AppSpec("observer", "Observer", "@abstractframework/observer", "Watch runs, replay/stream ledgers, submit durable commands.", STACK_PORT["observer"], "abstractobserver", "ABSTRACTOBSERVER_GATEWAY_URL", "AbstractObserver"),
    AppSpec("continuum", "Continuum", "@abstractframework/continuum", "Backlog, inbox triage and managed processes against this gateway.", STACK_PORT["continuum"], "abstractcontinuum", "ABSTRACTCONTINUUM_GATEWAY_URL", "AbstractContinuum"),
    AppSpec("code", "Code", "@abstractframework/code", "Browser-based coding assistant with durable agent sessions.", STACK_PORT["code"], "abstractcode", "ABSTRACTCODE_GATEWAY_URL", "AbstractCode"),
    AppSpec("entity", "Entity", "@abstractframework/entity", "Create summoned entities, watch their memory graph, talk with them.", STACK_PORT["entity"], "abstractentity", "ABSTRACTENTITY_GATEWAY_URL", "AbstractEntity"),
    AppSpec("flow", "Flow Editor", "@abstractframework/flow", "Visual workflow editor.", STACK_PORT["flow"], "abstractflow", "ABSTRACTFLOW_GATEWAY_URL", "AbstractFlow"),
)
APP_BY_ID: Dict[str, AppSpec] = {a.id: a for a in APPS}

# Apps whose server takes its address and gateway URL as launch flags
# (`--port`, `--host`, `--gateway-url`). Continuum 0.3.1 reads a settings file
# that beats the environment, so PORT/HOST/<APP>_GATEWAY_URL could lose to a
# user's saved values; flags beat the file. The other apps still read the
# environment.
FLAG_CONFIGURED_APPS = frozenset({"continuum"})


def app_launch_config(app_id: str, *, port: int, host: str, gateway_url: str, gateway_url_env: str) -> Tuple[List[str], Dict[str, str]]:
    """(argv after `bin/cli.js`, environment to set) that give a web app its
    port, bind host and gateway URL."""
    if app_id in FLAG_CONFIGURED_APPS:
        return ["--port", str(port), "--host", str(host), "--gateway-url", str(gateway_url)], {}
    return [], {"PORT": str(port), "HOST": str(host), gateway_url_env: str(gateway_url)}


# ---------------------------------------------------------------------------
# Errors (every one carries a plain message; routes map status_code)
# ---------------------------------------------------------------------------


class AppsError(Exception):
    status_code = 400
    reason = "apps_error"

    def __init__(self, message: str, *, hint: Optional[str] = None, details: Optional[str] = None, extra: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details
        self.extra = dict(extra or {})  # e.g. {"command": ...}: what the user can copy instead

    def payload(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ok": False, "reason": self.reason, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.details:
            out["details"] = self.details
        for k, v in self.extra.items():
            out.setdefault(k, v)
        return out


class UnknownApp(AppsError):
    status_code = 404
    reason = "unknown_app"


class NodeMissing(AppsError):
    status_code = 409
    reason = "node_missing"


class NotInstalled(AppsError):
    status_code = 409
    reason = "not_installed"


INSTALLS_OFF_MESSAGE = "Installing software on the gateway host is turned off for this gateway."


class InstallsNotAllowed(AppsError):
    status_code = 403
    reason = "installs_not_allowed"


class NetworkUnavailable(AppsError):
    status_code = 503
    reason = "network_unavailable"


class IntegrityMismatch(AppsError):
    status_code = 502
    reason = "integrity_mismatch"


class LaunchFailed(AppsError):
    status_code = 500
    reason = "launch_failed"


class NoFreePort(AppsError):
    status_code = 503
    reason = "no_free_port"


class ToolchainRequired(AppsError):
    """No prebuilt terminal app for this computer: only a source build works."""

    status_code = 409
    reason = "toolchain_required"


class StartedOutsideGateway(AppsError):
    status_code = 409
    reason = "started_outside_gateway"


class NotOnGatewayMachine(AppsError):
    """A terminal can only be opened on the gateway's own screen."""

    status_code = 409
    reason = "not_on_gateway_machine"


class NoTerminal(AppsError):
    status_code = 409
    reason = "no_terminal"


class JobCancelled(Exception):
    pass


def spec_for(app_id: str) -> AppSpec:
    spec = APP_BY_ID.get(str(app_id or "").strip().lower())
    if spec is None:
        raise UnknownApp(f"Unknown app '{app_id}'. Known apps: {', '.join(APP_BY_ID)}.")
    return spec


# ---------------------------------------------------------------------------
# What an app holds (mission JJ, 2026-09-24): the console's Entity card offers
# "Create your first entity" on a gateway that hosts none, and opens the app
# on its creation deep link through the sign-in handover (`handover_path`).
# ---------------------------------------------------------------------------


class InvalidAppPath(AppsError):
    status_code = 400
    reason = "invalid_app_path"


# Characters a same-app path never needs: control characters, whitespace and
# the backslash (browsers read "/\\host" as "//host", another origin).
_APP_PATH_FORBIDDEN = re.compile(r"[\x00-\x20\x7f\\]")


def handover_path(path: Optional[str]) -> str:
    """The path (with optional ?query and #fragment) the handover redirects
    to inside the app, e.g. "/#new". It must stay on the app's own origin:
    an absolute path ("/..."), never "//host" (a network-path reference),
    never a scheme, a backslash, whitespace or a control character. Empty
    means the app's root. Raises InvalidAppPath."""
    p = "" if path is None else str(path)
    if p == "":
        return "/"
    if len(p) > 2048:
        raise InvalidAppPath("The app path is too long.", hint="Use a path inside the app, such as /#new.")
    if not p.startswith("/") or p.startswith("//") or _APP_PATH_FORBIDDEN.search(p):
        raise InvalidAppPath(
            "The app path must be a path inside the app, such as /#new.",
            hint="A full address, another host (//host) or a backslash is not accepted.",
        )
    parts = urllib.parse.urlsplit(p)
    if parts.scheme or parts.netloc:
        raise InvalidAppPath("The app path must be a path inside the app, such as /#new.")
    return p


def gateway_entities_count() -> Optional[int]:
    """How many entities this gateway hosts: the number of entries
    `GET /api/gateway/entities` lists (routes/entities.py `_registry()`,
    `EntityRegistry.list_entities`: every directory under the registry's
    entities dir that holds a manifest, unreadable ones included), counted
    without reading any manifest. None when it cannot be known."""
    try:
        from .entities import MANIFEST_FILENAME
        from .routes.entities import _registry

        root = Path(_registry().entities_dir)
        if not root.exists():
            return 0
        return sum(1 for child in root.iterdir() if child.is_dir() and (child / MANIFEST_FILENAME).exists())
    except Exception:  # noqa: BLE001 - a count is a hint, never an error
        logger.debug("entities count unavailable", exc_info=True)
        return None


def _gateway_key(url: Optional[str]) -> Optional[Tuple[str, str, int]]:
    """(scheme, loopback-normalised host, port) of a gateway URL, to tell
    whether an app started outside the gateway talks to THIS gateway."""
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
        if not parts.scheme or not parts.hostname:
            return None
        host = parts.hostname.lower()
        if host in {"localhost", "::1"} or host.startswith("127."):
            host = "loopback"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return parts.scheme.lower(), host, int(port)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?")


def parse_version(value: Any) -> Optional[Tuple[int, int, int, int, str]]:
    """Semver key: (major, minor, patch, is_release, prerelease). A release
    sorts after its prereleases."""
    m = _SEMVER.match(str(value or "").strip())
    if not m:
        return None
    pre = m.group(4) or ""
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if pre else 1, pre)


def version_newer(candidate: Any, current: Any) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    return a > b


def verify_sri(data_sha512: bytes, integrity: str) -> bool:
    """True when `integrity` (npm Subresource Integrity, e.g. "sha512-<b64>")
    names this sha512 digest. Only sha512 entries count (npm publishes them
    for every tarball since 2017); a string without one is a mismatch."""
    for entry in str(integrity or "").split():
        algo, _, b64 = entry.partition("-")
        if algo.lower() != "sha512" or not b64:
            continue
        try:
            expected = base64.b64decode(b64.split("?")[0])
        except Exception:
            continue
        if expected == data_sha512:
            return True
    return False


def _safe_join(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    root_r = root.resolve()
    if target != root_r and root_r not in target.parents:
        raise AppsError(f"Refusing an archive entry outside the install folder: {rel}")
    return target


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Nothing listens on host:port and we could bind it now."""
    try:
        with socket.create_connection((host if host not in {"0.0.0.0", "::"} else "127.0.0.1", int(port)), timeout=0.2):
            return False
    except OSError:
        pass
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        if not sys.platform.startswith("win"):
            # Like Node's and Python's servers: a port in TIME_WAIT after a
            # stop is free for a new listener (a live listener still is not).
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


def parse_port_range(raw: Optional[str]) -> Optional[Tuple[int, int]]:
    text = str(raw or "").strip()
    if not text:
        return None
    m = re.match(r"^(\d{2,5})\s*-\s*(\d{2,5})$", text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
    elif text.isdigit():
        lo = hi = int(text)
    else:
        raise AppsError(f"Ports for apps: {text!r} is not a port or a range like 3100-3199.")
    if not (1 <= lo <= hi <= 65535):
        raise AppsError(f"Ports for apps: {text!r} is not a valid port range.")
    return lo, hi


def allocate_port(
    *,
    preferred: Sequence[Optional[int]],
    port_range: Tuple[int, int],
    taken: Sequence[int] = (),
    host: str = "127.0.0.1",
    is_free: Callable[[int, str], bool] = port_is_free,
    restrict_to_range: bool = False,
) -> int:
    """First free port among `preferred` (in order), then the range. With
    `restrict_to_range`, preferred ports outside the range are skipped."""
    lo, hi = port_range
    seen = set(int(p) for p in taken)
    candidates: List[int] = []
    for p in preferred:
        if p is None:
            continue
        p = int(p)
        if restrict_to_range and not (lo <= p <= hi):
            continue
        candidates.append(p)
    candidates.extend(range(lo, hi + 1))
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        if is_free(p, host):
            return p
    raise NoFreePort(
        f"No free port for the app in {lo}-{hi}.",
        hint="Stop what uses these ports, or choose another range for the apps (Ports for apps in the settings, or "
        "`abstractgateway apps config set ports 3200-3299`), then try again.",
    )


def _scrubbed_child_env(base: Dict[str, str]) -> Dict[str, str]:
    """The app servers need no gateway secret: they sign in with a browser
    session. Drop tokens, secrets and provider keys from their environment."""
    out: Dict[str, str] = {}
    for k, v in base.items():
        ku = k.upper()
        if ku.startswith("ABSTRACTGATEWAY_") or ku.startswith("ABSTRACTCORE_"):
            continue
        if any(ku.endswith(s) for s in ("_TOKEN", "_SECRET", "_API_KEY", "_PASSWORD", "_KEY")):
            continue
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

JOB_STATES = ("queued", "running", "succeeded", "failed", "cancelled")
_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "queued": ("running", "failed", "cancelled"),
    "running": ("succeeded", "failed", "cancelled"),
    "succeeded": (),
    "failed": (),
    "cancelled": (),
}


class InvalidTransition(RuntimeError):
    pass


class Job:
    """One visible unit of work (install Node, install/update/launch an app)."""

    def __init__(self, *, kind: str, target: str, app_id: Optional[str], log_dir: Path, title: str) -> None:
        self.id = "appjob_" + secrets.token_hex(8)
        self.kind = kind
        # The caller that started it was on the gateway machine (the install
        # rule's same-machine default, re-checked inside the job).
        self.same_machine = False
        self.target = target
        self.app_id = app_id
        self.title = title
        self.state = "queued"
        self.percent: float = 0.0
        self.bytes_done: int = 0
        self.bytes_total: Optional[int] = None
        self.indeterminate = False
        self.message = "Waiting to start…"
        self.details: Optional[str] = None
        self.error: Optional[Dict[str, Any]] = None
        self.result: Dict[str, Any] = {}
        self.steps: List[Dict[str, Any]] = []
        # The job's child rows (mission LL): one Install that installs the
        # browser app AND its terminal app shows both, e.g.
        # [{"id": "install", "label": "Code in the browser", "state": "done"},
        #  {"id": "install-tui", "label": "Code in the terminal", "state": "running"}].
        # state: waiting | running | done | failed | cancelled | skipped.
        self.parts: List[Dict[str, Any]] = []
        self.created_at = _now()
        self.updated_at = self.created_at
        self.finished_at: Optional[float] = None
        self.cancel_event = threading.Event()
        self._lock = threading.RLock()
        self._tail: collections.deque = collections.deque(maxlen=JOB_LOG_TAIL_LINES)
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"{self.id}.log"

    # -- state machine -----------------------------------------------------
    @property
    def terminal(self) -> bool:
        return self.state in ("succeeded", "failed", "cancelled")

    def transition(self, new_state: str) -> None:
        with self._lock:
            if new_state not in _TRANSITIONS.get(self.state, ()):
                raise InvalidTransition(f"job {self.id}: {self.state} -> {new_state} is not allowed")
            self.state = new_state
            self.updated_at = _now()
            if self.terminal:
                self.finished_at = self.updated_at

    # -- progress ----------------------------------------------------------
    def log(self, line: str) -> None:
        text = str(line).rstrip("\n")
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            for part in text.splitlines() or [""]:
                entry = f"[{stamp}] {part}"
                self._tail.append(entry)
                try:
                    with self.log_path.open("a", encoding="utf-8") as f:
                        f.write(entry + "\n")
                except Exception:
                    pass
            self.updated_at = _now()

    def say(self, message: str, *, percent: Optional[float] = None, log: bool = True) -> None:
        with self._lock:
            self.message = str(message)
            if percent is not None:
                self.percent = max(self.percent, min(100.0, float(percent)))
            self.updated_at = _now()
        if log:
            self.log(message)

    def step(self, name: str, label: str) -> None:
        with self._lock:
            for s in self.steps:
                if s["state"] == "running":
                    s["state"] = "done"
            self.steps.append({"name": name, "label": label, "state": "running"})
        self.say(label)

    def finish_steps(self, state: str) -> None:
        with self._lock:
            for s in self.steps:
                if s["state"] == "running":
                    s["state"] = state
            for part in self.parts:
                if part["state"] == "running":
                    part["state"] = state
                elif part["state"] == "waiting" and state in ("failed", "cancelled"):
                    part["state"] = "skipped"

    def part(self, part_id: str, state: str) -> None:
        with self._lock:
            for part in self.parts:
                if part["id"] == part_id:
                    part["state"] = state
            self.updated_at = _now()

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled()

    def full_log(self) -> str:
        try:
            data = self.log_path.read_bytes()
        except Exception:
            return "\n".join(self._tail)
        if len(data) > JOB_DETAILS_MAX_BYTES:
            data = b"[... earlier lines in " + str(self.log_path).encode() + b"]\n" + data[-JOB_DETAILS_MAX_BYTES:]
        return data.decode("utf-8", "replace")

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "kind": self.kind,
                "app_id": self.app_id,
                "title": self.title,
                "state": self.state,
                "percent": round(self.percent, 1),
                "indeterminate": bool(self.indeterminate and not self.terminal),
                "bytes_done": int(self.bytes_done),
                "bytes_total": self.bytes_total,
                "message": self.message,
                "details": self.details,
                "error": self.error,
                "steps": [dict(s) for s in self.steps],
                "parts": [dict(p) for p in self.parts],
                "result": dict(self.result),
                "log_path": str(self.log_path),
                "log_tail": list(self._tail)[-20:],
                "created_at": _iso(self.created_at),
                "updated_at": _iso(self.updated_at),
                "finished_at": _iso(self.finished_at),
            }


class JobRegistry:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self._jobs: "collections.OrderedDict[str, Job]" = collections.OrderedDict()
        self._lock = threading.Lock()

    def active_for(self, target: str) -> Optional[Job]:
        with self._lock:
            for job in reversed(self._jobs.values()):
                if job.target == target and not job.terminal:
                    return job
        return None

    def start(self, *, kind: str, target: str, app_id: Optional[str], title: str, work: Callable[[Job], Dict[str, Any]], run_inline: bool = False) -> Tuple[Job, bool]:
        """(job, created). An active job for the same target is returned
        instead of starting a second one."""
        with self._lock:
            for job in reversed(self._jobs.values()):
                if job.target == target and not job.terminal:
                    return job, False
            job = Job(kind=kind, target=target, app_id=app_id, log_dir=self.log_dir, title=title)
            self._jobs[job.id] = job
            while len(self._jobs) > 200:
                oldest_id, oldest = next(iter(self._jobs.items()))
                if not oldest.terminal:
                    break
                self._jobs.pop(oldest_id)
        if run_inline:
            _run_job(job, work)
        else:
            threading.Thread(target=_run_job, args=(job, work), name=f"apps-{job.kind}-{job.id}", daemon=True).start()
        return job, True

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(str(job_id))

    def list(self) -> List[Job]:
        with self._lock:
            return list(self._jobs.values())


def _run_job(job: Job, work: Callable[[Job], Dict[str, Any]]) -> None:
    job.transition("running")
    job.log(f"{job.title}: started")
    try:
        result = work(job) or {}
        job.result = dict(result)
        job.bytes_done = job.bytes_total or job.bytes_done
        job.finish_steps("done")
        job.indeterminate = False
        job.percent = 100.0
        job.transition("succeeded")
        job.say(str(result.get("message") or f"{job.title}: done"))
    except JobCancelled:
        job.finish_steps("cancelled")
        job.transition("cancelled")
        job.say("Cancelled.")
    except AppsError as exc:
        job.finish_steps("failed")
        job.log(f"ERROR: {exc.message}")
        if exc.details:
            job.log(exc.details)
        if exc.hint:
            job.log(f"HINT: {exc.hint}")
        job.error = {"reason": exc.reason, "message": exc.message, "hint": exc.hint}
        job.details = job.full_log()
        job.transition("failed")
        job.say(exc.message, log=False)
    except Exception as exc:  # noqa: BLE001 - a job must always end visibly
        logger.exception("apps job %s failed", job.id)
        job.finish_steps("failed")
        job.log(f"ERROR: {type(exc).__name__}: {exc}")
        job.error = {"reason": "internal_error", "message": f"{type(exc).__name__}: {exc}", "hint": None}
        job.details = job.full_log()
        job.transition("failed")
        job.say(f"{job.title} failed: {type(exc).__name__}: {exc}", log=False)


class _Phase:
    """Maps a sub-step's 0..1 progress onto a slice of the job's percent."""

    def __init__(self, job: Job, lo: float, hi: float) -> None:
        self.job, self.lo, self.hi = job, lo, hi

    def at(self, fraction: float) -> float:
        f = max(0.0, min(1.0, float(fraction)))
        return self.lo + (self.hi - self.lo) * f


# ---------------------------------------------------------------------------
# Node runtime
# ---------------------------------------------------------------------------


def _run_version(cmd: Sequence[str], timeout: float = 15.0) -> Optional[str]:
    try:
        out = subprocess.run(list(cmd) + ["--version"], capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    text = (out.stdout or "").strip().splitlines()
    return text[0].strip().lstrip("v") if text else None


def _npm_command_for(node_path: Path) -> Optional[List[str]]:
    """How to run npm with THIS node: `node npm-cli.js` when npm ships next to
    it (never a `#!/usr/bin/env node` shim that might pick another node), else
    an `npm` executable in the same folder."""
    node_path = Path(node_path)
    real = node_path.resolve()
    for base in (real.parent, node_path.parent):
        for cand in (
            base.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
            base / "node_modules" / "npm" / "bin" / "npm-cli.js",
            base / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
        ):
            if cand.is_file():
                return [str(node_path), str(cand)]
    for name in ("npm.cmd", "npm") if sys.platform.startswith("win") else ("npm",):
        cand = node_path.parent / name
        if cand.is_file() and os.access(cand, os.X_OK):
            return [str(cand)]
    return None


def _system_node_candidates() -> List[Path]:
    """PATH first, then the places Node lands without admin rights — a
    gateway started as a login service has a minimal PATH."""
    out: List[Path] = []
    found = shutil.which("node")
    if found:
        out.append(Path(found))
    home = Path.home()
    if sys.platform.startswith("win"):
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                out.append(Path(base) / "nodejs" / "node.exe")
        out.append(home / ".local" / "bin" / "node.exe")
    else:
        out.extend(
            [
                home / ".local" / "bin" / "node",  # `uv tool install nodejs-wheel` (install.sh --with-apps)
                Path("/opt/homebrew/bin/node"),
                Path("/usr/local/bin/node"),
                Path("/usr/bin/node"),
                home / ".volta" / "bin" / "node",
            ]
        )
        nvm = home / ".nvm" / "versions" / "node"
        try:
            versions = sorted(nvm.iterdir(), key=lambda p: parse_version(p.name) or (0, 0, 0, 0, ""), reverse=True)
            out.extend(v / "bin" / "node" for v in versions)
        except Exception:
            pass
    uniq: List[Path] = []
    for p in out:
        if p not in uniq:
            uniq.append(p)
    return uniq


def _wheel_platform_match(filename: str, *, system: Optional[str] = None, machine: Optional[str] = None, libc: Optional[str] = None) -> bool:
    system = (system or sys.platform).lower()
    machine = (machine or platform.machine()).lower()
    fn = filename.lower()
    arm = machine in {"arm64", "aarch64"}
    if system.startswith("darwin"):
        return "macosx_" in fn and fn.endswith(("_arm64.whl" if arm else "_x86_64.whl"))
    if system.startswith("linux"):
        if libc is None:
            libc = "glibc" if (platform.libc_ver()[0] or "").lower() == "glibc" else "musl"
        tag = "manylinux" if libc == "glibc" else "musllinux"
        return tag in fn and fn.endswith(("_aarch64.whl" if arm else "_x86_64.whl"))
    if system.startswith("win"):
        return fn.endswith("win_arm64.whl" if arm else "win_amd64.whl")
    return False


def pick_node_wheel(pypi_json: Dict[str, Any], *, major: int = MANAGED_NODE_MAJOR, pin: Optional[str] = None, **plat: Any) -> Tuple[str, Dict[str, Any]]:
    """(version, file) — the newest `major.x` release with a wheel for this
    platform (or the pinned version)."""
    releases = pypi_json.get("releases") or {}
    versions = [pin] if pin else sorted(
        # final releases only: PEP 440 pre-releases look like "24.20.0rc1"
        (v for v in releases if re.fullmatch(r"\d+\.\d+\.\d+", str(v)) and int(str(v).split(".")[0]) == major),
        key=lambda v: parse_version(v) or (0, 0, 0, 0, ""),
        reverse=True,
    )
    for v in versions:
        for f in releases.get(v) or []:
            if f.get("packagetype") == "bdist_wheel" and not f.get("yanked") and _wheel_platform_match(str(f.get("filename") or ""), **plat):
                return v, f
    raise AppsError(
        f"No Node.js {pin or f'{major}.x'} build for this platform ({sys.platform}/{platform.machine()}) on PyPI ({NODE_WHEEL_PROJECT}).",
        hint="Install Node.js 18 or newer yourself (https://nodejs.org), then refresh this page.",
    )


# ---------------------------------------------------------------------------
# Supervised processes
# ---------------------------------------------------------------------------

_PARENT_WATCH_JS = """// Written by the AbstractGateway apps manager (apps_manager.py).
// The gateway keeps this process's stdin open; when the gateway goes away for
// any reason (even a hard kill) the pipe closes and the app exits with it.
try {
  process.stdin.on('end', () => process.exit(0));
  process.stdin.on('error', () => process.exit(0));
  process.stdin.resume();
} catch (e) { /* no stdin: nothing to watch */ }
"""


class AppProcess:
    """One running app server, supervised from a thread of the gateway."""

    def __init__(self, manager: "AppsManager", spec: AppSpec) -> None:
        self.manager = manager
        self.spec = spec
        self.proc: Optional[subprocess.Popen] = None
        self.status = "stopped"  # stopped | starting | running | crashed | crash_loop | stopping
        self.port: Optional[int] = None
        self.version: Optional[str] = None
        self.started_at: Optional[float] = None
        self.last_exit_code: Optional[int] = None
        self.last_error: Optional[str] = None
        self.restarts: collections.deque = collections.deque(maxlen=10)
        self._stop_requested = False
        self._initial_pending = False
        self._lock = threading.RLock()
        self._log_fh: Any = None
        self._launch_args: Dict[str, Any] = {}

    @property
    def pid(self) -> Optional[int]:
        return self.proc.pid if self.proc is not None else None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "running": self.status == "running" and self.alive(),
                "pid": self.pid if self.alive() else None,
                "port": self.port,
                "version": self.version,
                "started_at": _iso(self.started_at),
                "restarts_last_minute": sum(1 for t in self.restarts if _now() - t < RESTART_WINDOW_S),
                "last_exit_code": self.last_exit_code,
                "last_error": self.last_error,
            }

    def start(self, *, node: str, bin_js: Path, port: int, host: str, gateway_url: str, version: str, ready_timeout_s: float = READY_TIMEOUT_S) -> None:
        with self._lock:
            if self.alive():
                return
            self._stop_requested = False
            self._initial_pending = True
            self._launch_args = dict(node=node, bin_js=bin_js, port=port, host=host, gateway_url=gateway_url, version=version, ready_timeout_s=ready_timeout_s)
            self._spawn()
        self._wait_ready(ready_timeout_s)

    def _spawn(self) -> None:
        a = self._launch_args
        m = self.manager
        log_path = m.app_log_path(self.spec.id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if log_path.exists() and log_path.stat().st_size > APP_LOG_MAX_BYTES:
                log_path.replace(log_path.with_suffix(".log.1"))
        except Exception:
            pass
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:
                pass
        self._log_fh = log_path.open("ab")
        self._log_fh.write(
            f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} starting {self.spec.package}@{a['version']} on {a['host']}:{a['port']} "
            f"(gateway {a['gateway_url']}) ===\n".encode()
        )
        self._log_fh.flush()
        env = _scrubbed_child_env(dict(os.environ))
        node_dir = str(Path(a["node"]).parent)
        env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
        flags, app_env = app_launch_config(
            self.spec.id, port=a["port"], host=a["host"], gateway_url=a["gateway_url"], gateway_url_env=self.spec.gateway_url_env
        )
        env.update({"NODE_ENV": "production", "ABSTRACTGATEWAY_URL": a["gateway_url"], **app_env})
        watch = m.parent_watch_script()
        cmd = [str(a["node"]), "-r", str(watch), str(a["bin_js"]), *flags]
        kwargs: Dict[str, Any] = dict(
            cwd=str(Path(a["bin_js"]).parent.parent),
            env=env,
            stdin=subprocess.PIPE,
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
        )
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True  # a terminal's Ctrl-C reaches the gateway, which stops the apps itself
        self.proc = subprocess.Popen(cmd, **kwargs)
        self.port = int(a["port"])
        self.version = str(a["version"])
        self.started_at = _now()
        self.status = "starting"
        self.last_error = None
        m.write_pid_file(self.spec.id, pid=self.proc.pid, port=self.port, cmd=cmd, version=self.version, bin_js=str(a["bin_js"]))
        threading.Thread(target=self._watch, args=(self.proc,), name=f"app-{self.spec.id}-watch", daemon=True).start()

    def _wait_ready(self, timeout_s: float) -> None:
        deadline = _now() + float(timeout_s)
        url = f"http://{_connect_host(self._launch_args['host'])}:{self.port}/"
        while _now() < deadline:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                code = proc.returncode if proc is not None else None
                tail = self.manager.app_log_tail(self.spec.id, 60)
                with self._lock:
                    self.status = "crashed"
                    self.last_exit_code = code
                    self.last_error = f"{self.spec.name} exited during start (exit code {code})."
                raise LaunchFailed(self.last_error, hint="The app's own output is in details.", details="\n".join(tail))
            try:
                with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=1.0) as resp:  # noqa: S310 - loopback
                    if resp.status < 500:
                        with self._lock:
                            if self.status == "starting":
                                self.status = "running"
                            self._initial_pending = False
                        return
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    with self._lock:
                        if self.status == "starting":
                            self.status = "running"
                        self._initial_pending = False
                    return
            except Exception:
                pass
            time.sleep(0.25)
        tail = self.manager.app_log_tail(self.spec.id, 60)
        self.stop()
        with self._lock:
            self.status = "crashed"
            self.last_error = f"{self.spec.name} did not answer on port {self.port} within {int(timeout_s)} s."
        raise LaunchFailed(self.last_error, details="\n".join(tail))

    def _watch(self, proc: subprocess.Popen) -> None:
        code = proc.wait()
        with self._lock:
            if proc is not self.proc:
                return  # an old process of a restarted app
            self.last_exit_code = code
            if self._stop_requested:
                self.status = "stopped"
                self.manager.remove_pid_file(self.spec.id)
                return
            if self._initial_pending:
                # The first start failed: `_wait_ready` reports it to the
                # caller with the app's output; it is not a crash to retry.
                self.status = "crashed"
                self.manager.remove_pid_file(self.spec.id)
                return
            now = _now()
            self.restarts.append(now)
            recent = sum(1 for t in self.restarts if now - t < RESTART_WINDOW_S)
            self.last_error = f"{self.spec.name} exited unexpectedly (exit code {code})."
            logger.warning("app %s exited unexpectedly (code %s); restarts in the last minute: %s", self.spec.id, code, recent)
            if recent > MAX_RESTARTS or not self.manager.is_enabled(self.spec.id):
                self.status = "crash_loop" if recent > MAX_RESTARTS else "crashed"
                if recent > MAX_RESTARTS:
                    self.last_error += f" It crashed {recent} times in a minute; not restarting. See the app log."
                self.manager.remove_pid_file(self.spec.id)
                return
            self.status = "crashed"
            delay = min(8.0, RESTART_BACKOFF_S * 2.0 ** (recent - 1))
        time.sleep(delay)
        with self._lock:
            if self._stop_requested or self.proc is not proc:
                return
            try:
                self._spawn()
            except Exception as exc:  # noqa: BLE001
                self.status = "crashed"
                self.last_error = f"Restart failed: {type(exc).__name__}: {exc}"
                return
        try:
            self._wait_ready(float(self._launch_args.get("ready_timeout_s") or READY_TIMEOUT_S))
        except AppsError as exc:
            logger.warning("app %s restart failed: %s", self.spec.id, exc.message)

    def stop(self, timeout_s: float = STOP_TIMEOUT_S) -> None:
        with self._lock:
            self._stop_requested = True
            proc = self.proc
            if proc is None or proc.poll() is not None:
                self.status = "stopped"
                self.manager.remove_pid_file(self.spec.id)
                return
            self.status = "stopping"
        _terminate(proc, timeout_s)
        with self._lock:
            self.status = "stopped"
            self.last_exit_code = proc.returncode
            self.manager.remove_pid_file(self.spec.id)
            if self._log_fh is not None:
                try:
                    self._log_fh.close()
                except Exception:
                    pass
                self._log_fh = None


def _connect_host(host: str) -> str:
    h = str(host or "").strip()
    return "127.0.0.1" if h in {"", "0.0.0.0", "::"} else h


def _terminate(proc: subprocess.Popen, timeout_s: float) -> None:
    try:
        if proc.stdin:
            proc.stdin.close()  # the parent-watch preload exits on EOF
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=timeout_s)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
        proc.wait(timeout=timeout_s)
    except Exception:
        pass


def _open_terminal(argv: Sequence[str]) -> None:
    """Start the terminal program detached (scrubbed environment: the new
    window must not inherit the gateway's token or keys). `open` and `start`
    return at once; their exit code says whether the window opened."""
    kwargs: Dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=_scrubbed_child_env(dict(os.environ)), close_fds=True)
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(list(argv), **kwargs)
    if argv and (Path(str(argv[0])).name in {"open", "cmd"}):
        out, _ = proc.communicate(timeout=20)
        if proc.returncode != 0:
            raise LaunchFailed(f"{argv[0]} could not open a terminal (exit code {proc.returncode}).", details=(out or b"").decode("utf-8", "replace"))


def _pid_command(pid: int) -> Optional[str]:
    if sys.platform.startswith("win"):
        return None
    try:
        # `-ww`: unlimited width. Linux procps cuts a non-tty listing at 80
        # columns, so the reaper's "is this still that app" test (the app's
        # bin path in the command line) failed for every real install path
        # and a leftover app kept its port.
        out = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    return out.stdout.strip() or None


# ---------------------------------------------------------------------------
# Apps started OUTSIDE the gateway — mission HH, 2026-09-24
#
# The gateway manages what it installed under `<data>/apps/`, but the same
# apps also run started some other way: the framework's dev stack
# (`scripts/start-local.sh`), `npx @abstractframework/observer`, a global npm
# install, a service unit. Such an app is found by asking the usual ports on
# loopback (STACK_PORTS first, then LEGACY_PROBE_PORTS) for `/` and reading
# the page title: none of the five app servers has an identity route that
# answers without a gateway sign-in (their `/api/*` proxies to the gateway,
# so it would also make the gateway call itself), while `/` is static and
# names the app (`<title>AbstractObserver</title>`, "AbstractContinuum — …",
# "AbstractCode", "AbstractEntity — …", "AbstractFlow Visual Editor").
# The version comes from the process that listens on the port (its
# command line -> the app's package.json), when this machine lets us see it.
# Probes are cheap: loopback only, EXTERNAL_PROBE_TIMEOUT_S each, all ports in
# parallel, the result cached EXTERNAL_CACHE_TTL_S.
# ---------------------------------------------------------------------------

EXTERNAL_PROBE_TIMEOUT_S = 0.5
EXTERNAL_CACHE_TTL_S = 5.0
EXTERNAL_HTML_MAX_BYTES = 65_536
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_UI_CONFIG_GATEWAY_RE = re.compile(r'__ABSTRACT_UI_CONFIG__[^<]*?"gateway_url"\s*:\s*"([^"]+)"')


@dataclass(frozen=True)
class ExternalApp:
    """A running app the gateway did not start."""

    app_id: str
    port: int
    url: str
    version: Optional[str] = None
    pid: Optional[int] = None
    # The gateway the app's page is configured for, when its page says so
    # (Observer and Entity put it in `window.__ABSTRACT_UI_CONFIG__`).
    gateway_url: Optional[str] = None


def external_probe_ports() -> List[int]:
    """Every port the probe asks, in priority order (stack map first)."""
    out: List[int] = []
    for port in [p for _, p in STACK_PORTS] + list(LEGACY_PROBE_PORTS):
        if port not in out:
            out.append(port)
    return out


def identify_app_page(html: str) -> Optional[str]:
    """The app id whose server sent this page (by its `<title>`), or None."""
    import html as _html

    m = _TITLE_RE.search(html or "")
    if not m:
        return None
    title = _html.unescape(m.group(1)).strip()
    for spec in APPS:
        if spec.html_title and re.match(re.escape(spec.html_title) + r"(?![A-Za-z0-9])", title):
            return spec.id
    return None


def probe_app_port(port: int, *, host: str = "127.0.0.1", timeout: float = EXTERNAL_PROBE_TIMEOUT_S) -> Optional[Tuple[str, str]]:
    """GET http://host:port/ -> (app id, page) when one of the apps answers."""
    import http.client

    conn = http.client.HTTPConnection(host, int(port), timeout=float(timeout))
    try:
        conn.request("GET", "/", headers={"Accept": "text/html", "User-Agent": "abstractgateway-apps-probe"})
        resp = conn.getresponse()
        if resp.status != 200 or "html" not in str(resp.getheader("Content-Type") or "").lower():
            return None
        page = resp.read(EXTERNAL_HTML_MAX_BYTES).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - nothing there, or not an HTTP server
        return None
    finally:
        conn.close()
    app_id = identify_app_page(page)
    return (app_id, page) if app_id else None


def _listener_pids(ports: Sequence[int]) -> Dict[int, int]:
    """port -> pid of the process listening on it (this user's processes;
    empty when the OS does not tell)."""
    wanted = {int(p) for p in ports}
    out: Dict[int, int] = {}
    if not wanted:
        return out
    try:
        import psutil  # optional (AbstractCore's local extras)

        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and int(c.laddr.port) in wanted and c.pid:
                out.setdefault(int(c.laddr.port), int(c.pid))
        return out
    except Exception:  # noqa: BLE001 - macOS refuses the system-wide list to non-root
        pass
    lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if Path("/usr/sbin/lsof").is_file() else None)
    if not lsof:
        return out
    argv = [lsof, "-nP", "-sTCP:LISTEN", "-Fpn"]
    for port in sorted(wanted):
        argv.append(f"-iTCP:{port}")
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=3)
    except Exception:  # noqa: BLE001
        return out
    pid: Optional[int] = None
    for line in (cp.stdout or "").splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid is not None:
            port_txt = line.rsplit(":", 1)[-1]
            if port_txt.isdigit() and int(port_txt) in wanted:
                out.setdefault(int(port_txt), pid)
    return out


def _process_argv(pid: int) -> List[str]:
    try:
        import psutil

        return [str(a) for a in psutil.Process(int(pid)).cmdline()]
    except Exception:  # noqa: BLE001
        pass
    cmd = _pid_command(pid)
    return cmd.split() if cmd else []


def app_version_from_process(pid: int, spec: AppSpec, *, argv: Optional[Sequence[str]] = None) -> Optional[str]:
    """The version of `spec`'s package the process runs: the package.json
    named `spec.package` above the script on its command line."""
    for arg in list(argv if argv is not None else _process_argv(pid))[1:]:
        if not arg.startswith("/") and not re.match(r"^[A-Za-z]:[\\/]", arg):
            continue
        try:
            path = Path(arg).resolve()
        except (OSError, RuntimeError):
            continue
        if not path.exists():
            continue
        for parent in [path] + list(path.parents)[:8]:
            pkg = parent / "package.json"
            if not pkg.is_file():
                continue
            try:
                meta = json.loads(pkg.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(meta, dict) and meta.get("name") == spec.package and meta.get("version"):
                return str(meta["version"])
    return None


def detect_external_apps(
    *,
    ports: Optional[Sequence[int]] = None,
    exclude_ports: Sequence[int] = (),
    host: str = "127.0.0.1",
    timeout: float = EXTERNAL_PROBE_TIMEOUT_S,
    probe: Callable[..., Optional[Tuple[str, str]]] = probe_app_port,
    pids: Callable[[Sequence[int]], Dict[int, int]] = _listener_pids,
    version_of: Callable[[int, AppSpec], Optional[str]] = app_version_from_process,
) -> Dict[str, ExternalApp]:
    """app id -> ExternalApp for every app answering on a usual port (the
    first port in priority order wins for an app). Never raises."""
    order = [int(p) for p in (ports if ports is not None else external_probe_ports()) if int(p) not in set(exclude_ports)]
    if not order:
        return {}
    found: Dict[int, Tuple[str, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(order)) as ex:
        futs = {port: ex.submit(probe, port, host=host, timeout=timeout) for port in order}
        for port, fut in futs.items():
            try:
                hit = fut.result(timeout=timeout + 2.0)
            except Exception:  # noqa: BLE001
                hit = None
            if hit:
                found[port] = hit
    chosen: Dict[str, int] = {}
    for port in order:
        if port in found and found[port][0] not in chosen:
            chosen[found[port][0]] = port
    if not chosen:
        return {}
    try:
        pid_by_port = pids(list(chosen.values()))
    except Exception:  # noqa: BLE001
        pid_by_port = {}
    out: Dict[str, ExternalApp] = {}
    for app_id, port in chosen.items():
        spec = APP_BY_ID[app_id]
        pid = pid_by_port.get(port)
        version = None
        if pid:
            try:
                version = version_of(pid, spec)
            except Exception:  # noqa: BLE001
                version = None
        gw = _UI_CONFIG_GATEWAY_RE.search(found[port][1])
        out[app_id] = ExternalApp(app_id, port, f"http://{_connect_host(host)}:{port}/", version=version, pid=pid, gateway_url=gw.group(1) if gw else None)
    return out


# ---------------------------------------------------------------------------
# Terminal apps (TUIs) — mission Y, 2026-09-24
#
# Some apps also exist as a terminal app. Today that is Code only
# (`abstractcode`, a Rust TUI in the abstractcode repository); Flow, Observer,
# Continuum and Entity are browser apps only. The gateway's own console has a
# terminal twin too (`abstractgateway-console`), shown on the console's Done
# step, not as an app card.
#
# Distribution decides what the gateway can do (docs/apps.md, "Terminal apps"):
# - a GitHub release with prebuilt binaries AND a SHA256SUMS file (Code): the
#   gateway downloads the archive for this computer, checks it against the
#   release's SHA256SUMS (and the per-asset sha256 digest GitHub reports, when
#   present), unpacks the one binary into `<data>/apps/bin/`, sets the
#   executable bit and runs `--version` before it replaces anything;
# - crates.io source only (the gateway console), or no binary for this
#   platform: the gateway says so ("needs the Rust toolchain") and gives the
#   exact `cargo install` command; there is no install button.
# Presence only: the gateway never imports an app, it looks for the binary in
# `<data>/apps/bin/`, on PATH and in ~/.cargo/bin, and checks its `--help`
# names the program (PyPI's unrelated `abstractcode` package installs a
# console script with the same name).
#
# Opening one in a terminal (`launch_tui`): only for a caller on the gateway
# machine. The terminal gets a small launcher script holding a ONE-TIME code
# (2 minutes, single use; the script deletes itself), never a token. The
# script runs `tui_signin.py`, which trades the code on loopback
# (`POST /apps/tui-handover`) for a bearer token that works from this machine
# only, acts as the caller, lives in the gateway's memory only, and reaches the
# terminal app through its environment — never argv, never a file.
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"
TUI_HANDOVER_ENV = "ABSTRACTGATEWAY_TUI_HANDOVER"  # internal: launcher script -> tui_signin.py
TUI_PROBE_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class TuiSpec:
    id: str  # the app it belongs to ("code") or "gateway-console"
    name: str
    binary: str
    crate: str
    repo: Optional[str]  # GitHub owner/name that attaches prebuilt binaries + SHA256SUMS; None = crates.io only
    help_marker: str  # a phrase its --help prints (tells it apart from a same-named command)
    gateway_flag: str  # how it is pointed at a gateway
    token_env: str  # where it reads a bearer token from (the handover puts it there)
    url_env: str = ""

    @property
    def exe_name(self) -> str:
        return self.binary + (".exe" if sys.platform.startswith("win") else "")

    @property
    def install_command(self) -> str:
        return f"cargo install {self.crate}"

    @property
    def releases_page(self) -> Optional[str]:
        return f"https://github.com/{self.repo}/releases" if self.repo else None


CODE_TUI = TuiSpec(
    id="code",
    name="Code",
    binary="abstractcode",
    crate="abstractcode",
    repo="lpalbou/abstractcode",
    help_marker="AbstractTUI",
    gateway_flag="--gateway",
    token_env="ABSTRACTCODE_GATEWAY_TOKEN",
    url_env="ABSTRACTCODE_GATEWAY_URL",
)
GATEWAY_CONSOLE_TUI = TuiSpec(
    id="gateway-console",
    name="Gateway console",
    binary="abstractgateway-console",
    crate="abstractgateway-console",
    repo=None,  # published to crates.io only (abstractgateway release.yml, publish-console-crate)
    help_marker="AbstractGateway",
    gateway_flag="--url",
    token_env="ABSTRACTGATEWAY_AUTH_TOKEN",
)
TUI_BY_APP: Dict[str, TuiSpec] = {CODE_TUI.id: CODE_TUI}


def tui_for(app_id: str) -> TuiSpec:
    spec = spec_for(app_id)
    tui = TUI_BY_APP.get(spec.id)
    if tui is None:
        raise UnknownApp(f"{spec.name} has no terminal version; it runs in the browser only.")
    return tui


def release_target(*, system: Optional[str] = None, machine: Optional[str] = None, libc: Optional[str] = None) -> Optional[str]:
    """The Rust target triple the release workflows build for this computer,
    or None when no prebuilt binary is published for it."""
    system = (system or sys.platform).lower()
    machine = (machine or platform.machine()).lower()
    arm = machine in {"arm64", "aarch64"}
    x64 = machine in {"x86_64", "amd64", "x64"}
    if system.startswith("darwin"):
        return "aarch64-apple-darwin" if arm else ("x86_64-apple-darwin" if x64 else None)
    if system.startswith("linux"):
        if libc is None:
            libc = "glibc" if (platform.libc_ver()[0] or "").lower() == "glibc" else "musl"
        if libc != "glibc":
            return None
        return "aarch64-unknown-linux-gnu" if arm else ("x86_64-unknown-linux-gnu" if x64 else None)
    if system.startswith("win"):
        return "x86_64-pc-windows-msvc" if x64 else None
    return None


def release_asset_name(tui: TuiSpec, tag: str, target: str) -> str:
    return f"{tui.binary}-{tag}-{target}" + (".zip" if "windows" in target else ".tar.gz")


def parse_sha256sums(text: str) -> Dict[str, str]:
    """`<hex>  <name>` (or `<hex> *<name>`) lines -> {name: hex}."""
    out: Dict[str, str] = {}
    for line in str(text or "").splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(\S.*)$", line.strip())
        if m:
            out[m.group(2).strip()] = m.group(1).lower()
    return out


def pick_tui_release(releases: Any, tui: TuiSpec) -> Optional[Dict[str, Any]]:
    """Newest final `v<semver>` release (web-v tags and prereleases are not
    the terminal app). {version, tag, html_url, assets{name: {url, size, digest}}}."""
    best: Optional[Tuple[Tuple[int, int, int, int, str], Dict[str, Any]]] = None
    for rel in releases if isinstance(releases, list) else []:
        if not isinstance(rel, dict) or rel.get("draft") or rel.get("prerelease"):
            continue
        tag = str(rel.get("tag_name") or "")
        if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            continue
        key = parse_version(tag)
        if key is None or (best is not None and key <= best[0]):
            continue
        assets = {}
        for a in rel.get("assets") or []:
            if isinstance(a, dict) and a.get("name") and a.get("browser_download_url"):
                assets[str(a["name"])] = {"url": str(a["browser_download_url"]), "size": a.get("size"), "digest": a.get("digest")}
        best = (key, {"version": tag[1:], "tag": tag, "html_url": rel.get("html_url") or (tui.releases_page or ""), "assets": assets})
    return best[1] if best else None


def _extract_single_binary(archive: Path, member: str, dest: Path) -> None:
    """Copy exactly one regular file named `member` (at any depth) out of a
    .tar.gz or .zip into `dest`; links, devices and everything else refused."""
    found = False
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if info.is_dir() or Path(info.filename).name != member:
                    continue
                with zf.open(info) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
                found = True
                break
    else:
        with tarfile.open(archive, "r:gz") as tf:
            for m in tf.getmembers():
                if Path(m.name).name != member:
                    continue
                if not m.isfile():
                    raise AppsError(f"Refusing {m.name} in {archive.name}: not a regular file.")
                src = tf.extractfile(m)
                if src is None:
                    continue
                with src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
                found = True
                break
    if not found:
        raise AppsError(f"{archive.name} does not contain {member}.")


def _is_script(path: Path) -> bool:
    """A `#!` script (e.g. the console script of PyPI's unrelated Python
    `abstractcode` package), not a native terminal app."""
    if sys.platform.startswith("win"):
        return path.suffix.lower() not in {".exe", ""}
    try:
        with path.open("rb") as f:
            return f.read(2) == b"#!"
    except OSError:
        return True


def _quote_posix(value: str) -> str:
    import shlex

    return shlex.quote(str(value))


def _quote_cmd(value: str) -> str:
    return '"' + str(value).replace('"', '') + '"'


def tui_command(tui: TuiSpec, gateway_url: str, *, binary: Optional[str] = None, windows: Optional[bool] = None) -> str:
    """The exact command a user types to open it against this gateway."""
    win = sys.platform.startswith("win") if windows is None else windows
    exe = binary or tui.binary
    if binary and (" " in exe or "'" in exe or '"' in exe):
        exe = _quote_cmd(exe) if win else _quote_posix(exe)
    return f"{exe} {tui.gateway_flag} {gateway_url}"


def tui_launch_script_text(*, python: str, helper: str, binary: str, gateway_url: str, app_id: str, code: str, windows: bool) -> str:
    """The launcher a terminal runs: holds the ONE-TIME code only (never a
    token), deletes itself first, then hands over to tui_signin.py."""
    if windows:
        return (
            "@echo off\r\n"
            "rem AbstractGateway: opens a terminal app here, signed in. One use; this file deletes itself.\r\n"
            f'set "{TUI_HANDOVER_ENV}={code}"\r\n'
            f"{_quote_cmd(python)} -I {_quote_cmd(helper)} --gateway {_quote_cmd(gateway_url)} --app {app_id} -- {_quote_cmd(binary)}\r\n"
            '(goto) 2>nul & del "%~f0"\r\n'
        )
    return (
        "#!/bin/sh\n"
        "# AbstractGateway: opens a terminal app here, signed in. One use; this file deletes itself.\n"
        'rm -f -- "$0"\n'
        f"{TUI_HANDOVER_ENV}={_quote_posix(code)}\n"
        f"export {TUI_HANDOVER_ENV}\n"
        f"exec {_quote_posix(python)} -I {_quote_posix(helper)} --gateway {_quote_posix(gateway_url)} --app {_quote_posix(app_id)} -- {_quote_posix(binary)}\n"
    )


def terminal_argv(script: Path, *, system: Optional[str] = None, which: Callable[[str], Optional[str]] = shutil.which, environ: Optional[Dict[str, str]] = None) -> Tuple[str, List[str]]:
    """(terminal name, argv) that opens a NEW terminal window running `script`."""
    system = (system or sys.platform).lower()
    env = os.environ if environ is None else environ
    if system.startswith("darwin"):
        return "Terminal", ["open", "-a", "Terminal", str(script)]
    if system.startswith("win"):
        return "Command Prompt", ["cmd", "/c", "start", "", "cmd", "/k", str(script)]
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        raise NoTerminal(
            "This gateway has no desktop session to open a terminal window in.",
            hint="Copy the command and run it in a terminal on this computer.",
        )
    for name, args in (
        ("x-terminal-emulator", ["-e"]),
        ("gnome-terminal", ["--"]),
        ("konsole", ["-e"]),
        ("xfce4-terminal", ["-x"]),
        ("kitty", []),
        ("alacritty", ["-e"]),
        ("xterm", ["-e"]),
    ):
        found = which(name)
        if found:
            return name, [found, *args, str(script)]
    raise NoTerminal(
        "No terminal program was found on this computer (looked for x-terminal-emulator, gnome-terminal, konsole, xfce4-terminal, kitty, alacritty, xterm).",
        hint="Copy the command and run it in the terminal you use.",
    )


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


class AppsManager:
    def __init__(
        self,
        data_dir: Path,
        *,
        urlopen: Optional[Callable[..., Any]] = None,
        install_allowed: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.urlopen = urlopen or urllib.request.urlopen
        self._install_allowed_fn = install_allowed
        try:
            import inspect

            self._install_fn_takes_caller = install_allowed is not None and "same_machine" in inspect.signature(install_allowed).parameters
        except (TypeError, ValueError):
            self._install_fn_takes_caller = False
        # Apps started OUTSIDE the gateway (the dev stack, npx, a global
        # install, a service unit): found by probing the usual ports on
        # loopback (`detect_external_apps`); tests replace the probe.
        self.external_probe: Callable[..., Dict[str, "ExternalApp"]] = detect_external_apps
        self._external_cache: Optional[Tuple[float, Dict[str, "ExternalApp"], List[int]]] = None
        self._external_lock = threading.Lock()
        self.jobs = JobRegistry(self.apps_root / "jobs")
        self._procs: Dict[str, AppProcess] = {}
        self._lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._registry_cache: Dict[str, Tuple[float, Any]] = {}
        self._node_cache: Optional[Tuple[float, Dict[str, Any]]] = None
        self._handover: Dict[str, Tuple[float, str, Any, str, str]] = {}  # code -> (expires, app_id, principal, host, path)
        self._tui_handover: Dict[str, Tuple[float, str, Any, str]] = {}  # code -> (expires, app_id, principal, gateway_url)
        self._desktop_handover: Dict[str, Tuple[float, Any, str, str]] = {}  # code -> (expires, principal, base_url, file)
        self._tui_probe_cache: Dict[Tuple[str, int, int], Tuple[Optional[str]]] = {}
        # How a terminal window is opened (argv from `terminal_argv`); tests
        # replace it so no test ever opens a real window.
        self.terminal_opener: Callable[[Sequence[str]], None] = _open_terminal
        self.gateway_url: Optional[str] = None
        # How many entities this gateway hosts (the Entity card's first-run
        # button); tests replace it.
        self.entities_counter: Callable[[], Optional[int]] = gateway_entities_count
        # Desktop apps (mission LL, apps_desktop.py): every OS touch point is
        # replaceable, so no test finds, installs or launches the real one.
        from . import apps_desktop as _desk

        self.desktop_probes: Callable[[], Any] = lambda: _desk.system_probes()
        self.desktop_pip_runner: Callable[..., int] = _desk.stream_command
        self.desktop_pins: Callable[[str], Dict[str, str]] = _desk.abstract_pins
        self.desktop_find_uv: Callable[[], Optional[str]] = _desk.find_uv
        self.desktop_has_pip: Callable[[], bool] = lambda: importlib.util.find_spec("pip") is not None
        self.desktop_spawner: Callable[..., Any] = _desk.spawn_detached
        self.desktop_wait: Callable[[Any], Optional[int]] = _desk.wait_launch
        self.desktop_python: str = sys.executable
        self._desktop_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

    # -- paths ---------------------------------------------------------------
    @property
    def apps_root(self) -> Path:
        return self.data_dir / "apps"

    @property
    def node_root(self) -> Path:
        return self.data_dir / "runtime" / "node"

    def app_log_path(self, app_id: str) -> Path:
        return self.data_dir / "logs" / "apps" / f"{app_id}.log"

    def pid_path(self, app_id: str) -> Path:
        return self.data_dir / "run" / "apps" / f"{app_id}.json"

    def state_path(self) -> Path:
        return self.apps_root / "state.json"

    def parent_watch_script(self) -> Path:
        p = self.apps_root / "_support" / "parent_watch.cjs"
        if not p.exists() or p.read_text(encoding="utf-8") != _PARENT_WATCH_JS:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_PARENT_WATCH_JS, encoding="utf-8")
        return p

    # -- config ---------------------------------------------------------------
    # Mission Z seam: every knob is the runtime-config setting `apps.<name>`
    # (stored > env > default, validated there; the ENV_* names above are its
    # labeled fallback rung). Read at each use, so a console/TUI/CLI change
    # applies to the next app start or download.
    def _setting(self, name: str) -> str:
        """runtime_config.resolve_apps_setting(...)["value"]; an unknown name
        raises KeyError (a typo fails loudly, never reads as a default)."""
        from .runtime_config import resolve_apps_setting

        return str(resolve_apps_setting(self.data_dir, name)["value"] or "")

    @property
    def registry_url(self) -> str:
        return (self._setting("npm_registry") or DEFAULT_NPM_REGISTRY).rstrip("/")

    @property
    def pypi_url(self) -> str:
        return (self._setting("pypi_url") or DEFAULT_PYPI_URL).rstrip("/")

    @property
    def bind_host(self) -> str:
        return self._setting("host").strip() or "127.0.0.1"

    def port_range(self) -> Tuple[Tuple[int, int], bool]:
        explicit = parse_port_range(self._setting("ports"))
        return (explicit or DEFAULT_PORT_RANGE), explicit is not None

    def install_allowed(self, *, same_machine: bool = False) -> bool:
        """The `allow_engine_install` rule for one caller: `same_machine` is
        True when the request comes from the gateway machine itself
        (security/same_machine.py), which is allowed by default whatever the
        bind (runtime_config.allow_engine_install_for_caller)."""
        if self._install_allowed_fn is None:
            return True
        try:
            if self._install_fn_takes_caller:
                return bool(self._install_allowed_fn(same_machine=bool(same_machine)))
            return bool(self._install_allowed_fn())
        except Exception:
            return False

    def _require_install_allowed(self, *, same_machine: bool = False) -> None:
        if not self.install_allowed(same_machine=same_machine):
            raise InstallsNotAllowed(
                INSTALLS_OFF_MESSAGE,
                hint="By default only someone at the gateway machine itself can install (or anyone, when the gateway listens on this machine alone). "
                "An admin can turn on 'allow engine install' in Settings, or install from the machine itself with `abstractgateway apps install`.",
            )

    # -- persisted state -------------------------------------------------------
    def _read_state(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.state_path().read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("apps"), dict):
                return data
        except Exception:
            pass
        return {"schema": 1, "apps": {}}

    def _update_app_state(self, app_id: str, **fields: Any) -> Dict[str, Any]:
        with self._state_lock:
            state = self._read_state()
            row = dict(state["apps"].get(app_id) or {})
            row.update(fields)
            state["apps"][app_id] = row
            path = self.state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(path)
            return row

    def app_state(self, app_id: str) -> Dict[str, Any]:
        return dict(self._read_state()["apps"].get(app_id) or {})

    def is_enabled(self, app_id: str) -> bool:
        return bool(self.app_state(app_id).get("enabled"))

    # -- pid files -------------------------------------------------------------
    def write_pid_file(self, app_id: str, *, pid: int, port: int, cmd: Sequence[str], version: str, bin_js: Optional[str] = None) -> None:
        p = self.pid_path(app_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec: Dict[str, Any] = {"pid": int(pid), "port": int(port), "cmd": list(cmd), "version": version, "gateway_pid": os.getpid(), "started_at": _iso(_now())}
        if bin_js:
            rec["bin_js"] = str(bin_js)  # what reap_orphans matches: the argv may end with launch flags
        p.write_text(
            json.dumps(rec, indent=2) + "\n",
            encoding="utf-8",
        )

    def remove_pid_file(self, app_id: str) -> None:
        try:
            self.pid_path(app_id).unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def reap_orphans(self) -> List[Dict[str, Any]]:
        """A previous gateway that died hard may have left an app running
        (its stdin watch normally makes it exit). Stop any process a pid
        file names whose command line still is that app — never anything else."""
        reaped: List[Dict[str, Any]] = []
        d = self.data_dir / "run" / "apps"
        if not d.is_dir():
            return reaped
        for f in d.glob("*.json"):
            app_id = f.stem
            with self._lock:
                live = self._procs.get(app_id)
                if live is not None and live.alive():
                    continue
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                pid = int(rec.get("pid") or 0)
            except Exception:
                f.unlink(missing_ok=True)
                continue
            cmdline = _pid_command(pid) if pid > 0 else None
            # `bin_js` since 0.4.3 (Continuum's argv ends with flags); older
            # pid files end their `cmd` with the app's bin/cli.js.
            bin_js = str(rec.get("bin_js") or (rec.get("cmd") or [""])[-1])
            if cmdline and bin_js and bin_js in cmdline:
                try:
                    os.kill(pid, signal.SIGTERM)
                    for _ in range(30):
                        time.sleep(0.1)
                        if _pid_command(pid) is None:
                            break
                    reaped.append({"app_id": app_id, "pid": pid})
                    logger.warning("stopped a leftover %s app process (pid %s) from a previous gateway run", app_id, pid)
                except Exception:
                    pass
            f.unlink(missing_ok=True)
        return reaped

    # -- network ---------------------------------------------------------------
    def _get_json(self, url: str, *, timeout: float, accept: str = "application/json") -> Any:
        req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "abstractgateway-apps"})
        try:
            with self.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise AppsError(f"Not found: {url}") from exc
            raise NetworkUnavailable(f"{urllib.parse.urlparse(url).netloc} answered HTTP {exc.code} for {url}.") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NetworkUnavailable(
                f"Cannot reach {urllib.parse.urlparse(url).netloc} ({reason}).",
                hint="Check this machine's internet connection (or proxy), then try again.",
            ) from exc

    def _download(self, job: Job, url: str, dest: Path, *, expected_size: Optional[int], phase: _Phase, what: str) -> Tuple[bytes, bytes]:
        """Stream `url` to `dest`; returns (sha256, sha512) digests."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        h256, h512 = hashlib.sha256(), hashlib.sha512()
        req = urllib.request.Request(url, headers={"User-Agent": "abstractgateway-apps"})
        job.indeterminate = False
        try:
            with self.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as resp, part.open("wb") as out:
                total = expected_size
                try:
                    cl = int(resp.headers.get("Content-Length") or 0)
                    if cl > 0:
                        total = cl
                except Exception:
                    pass
                job.bytes_total = total
                job.bytes_done = 0
                last_log = 0.0
                while True:
                    job.check_cancel()
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    h256.update(chunk)
                    h512.update(chunk)
                    job.bytes_done += len(chunk)
                    frac = (job.bytes_done / total) if total else 0.0
                    msg = f"Downloading {what}: {_mb(job.bytes_done)}" + (f" of {_mb(total)} ({frac * 100:.0f}%)" if total else "")
                    now = _now()
                    log_it = now - last_log > 2.0
                    if log_it:
                        last_log = now
                    job.say(msg, percent=phase.at(frac), log=log_it)
        except JobCancelled:
            part.unlink(missing_ok=True)
            raise
        except urllib.error.HTTPError as exc:
            part.unlink(missing_ok=True)
            raise NetworkUnavailable(f"Download of {what} failed: HTTP {exc.code} from {urllib.parse.urlparse(url).netloc}.") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            part.unlink(missing_ok=True)
            raise NetworkUnavailable(
                f"Download of {what} failed: cannot reach {urllib.parse.urlparse(url).netloc} ({getattr(exc, 'reason', exc)}).",
                hint="Check this machine's internet connection (or proxy), then try again.",
            ) from exc
        job.log(f"Downloaded {what}: {job.bytes_done} bytes from {url}")
        part.replace(dest)
        return h256.digest(), h512.digest()

    # -- npm registry ------------------------------------------------------------
    def registry_metadata(self, spec: AppSpec, *, timeout: float = REGISTRY_TIMEOUT_S, use_cache: bool = True) -> Dict[str, Any]:
        key = spec.package
        cached = self._registry_cache.get(key)
        if use_cache and cached is not None:
            age = _now() - cached[0]
            if isinstance(cached[1], Exception):
                if age < REGISTRY_FAIL_TTL_S:
                    raise cached[1]
            elif age < REGISTRY_CACHE_TTL_S:
                return cached[1]
        url = f"{self.registry_url}/{urllib.parse.quote(spec.package, safe='@')}"
        try:
            meta = self._get_json(url, timeout=timeout, accept="application/vnd.npm.install-v1+json; q=1.0, application/json; q=0.8")
        except AppsError as exc:
            self._registry_cache[key] = (_now(), exc)
            raise
        self._registry_cache[key] = (_now(), meta)
        return meta

    def resolve_package(self, spec: AppSpec, version: Optional[str] = None, *, use_cache: bool = True) -> Dict[str, Any]:
        meta = self.registry_metadata(spec, timeout=DOWNLOAD_TIMEOUT_S if not use_cache else REGISTRY_TIMEOUT_S, use_cache=use_cache)
        return resolve_from_metadata(meta, spec, version)

    def latest_version(self, spec: AppSpec) -> Tuple[Optional[str], Optional[str]]:
        try:
            meta = self.registry_metadata(spec)
            return str((meta.get("dist-tags") or {}).get("latest") or "") or None, None
        except AppsError as exc:
            return None, exc.message

    # -- Node runtime ------------------------------------------------------------
    def _managed_node(self) -> Optional[Dict[str, Any]]:
        try:
            cur = json.loads((self.node_root / "current.json").read_text(encoding="utf-8"))
        except Exception:
            return None
        node = self.node_root / str(cur.get("node") or "")
        if not node.is_file():
            return None
        npm_cli = self.node_root / str(cur.get("npm_cli") or "")
        return {
            "path": str(node),
            "version": str(cur.get("version") or ""),
            "npm": [str(node), str(npm_cli)] if npm_cli.is_file() else None,
        }

    def node_status(self, *, refresh: bool = False) -> Dict[str, Any]:
        cached = self._node_cache
        if cached is not None and not refresh and _now() - cached[0] < 30.0:
            return dict(cached[1])
        mode = (self._setting("node") or "auto").strip()
        found: Optional[Dict[str, Any]] = None
        problems: List[str] = []
        managed = self._managed_node()

        def _check(path: Path, source: str) -> Optional[Dict[str, Any]]:
            if not path.is_file():
                return None
            v = _run_version([str(path)])
            if not v:
                problems.append(f"{path} did not run")
                return None
            major = (parse_version(v) or (0,))[0]
            if major < NODE_MIN_MAJOR:
                problems.append(f"{path} is Node {v}; the apps need {NODE_MIN_MAJOR} or newer")
                return None
            return {"path": str(path), "version": v, "source": source, "npm": _npm_command_for(path)}

        if mode not in {"auto", "managed", "system", ""}:
            found = _check(Path(mode).expanduser(), "system")
            if found is None:
                problems.append(f"Node.js for apps ({mode}) is not a usable Node.js {NODE_MIN_MAJOR}+")
        else:
            if mode in {"auto", "system", ""}:
                for cand in _system_node_candidates():
                    found = _check(cand, "system")
                    if found:
                        break
            if found is None and mode in {"auto", "managed", ""} and managed:
                v = _run_version([managed["path"]])
                if v:
                    found = {"path": managed["path"], "version": v, "source": "managed", "npm": managed["npm"]}
                else:
                    problems.append(f"the gateway's own Node ({managed['path']}) did not run")
        status: Dict[str, Any] = {
            "available": bool(found and found.get("npm")),
            "version": (found or {}).get("version"),
            "source": (found or {}).get("source") or "none",
            "path": (found or {}).get("path"),
            "npm": bool((found or {}).get("npm")),
            "npm_command": (found or {}).get("npm"),
            "mode": mode or "auto",
            "managed_version": (managed or {}).get("version"),
            "managed_dir": str(self.node_root),
            "problems": problems,
        }
        if found and not found.get("npm"):
            problems.append(f"npm was not found next to {found['path']}")
        status["install_available"] = (not status["available"]) and mode != "system" and self.install_allowed()
        if status["available"]:
            status["message"] = f"Node.js {status['version']} ({'installed by the gateway' if status['source'] == 'managed' else 'on this machine'})."
        else:
            status["message"] = "Node.js is not installed. The gateway can install it for you (about 56 MB, no admin rights needed)."
        self._node_cache = (_now(), status)
        return dict(status)

    def install_node(self, job: Job, phase: _Phase) -> Dict[str, Any]:
        self._require_install_allowed(same_machine=job.same_machine)
        job.step("node", "Installing Node.js for the apps…")
        meta = self._get_json(f"{self.pypi_url}/{NODE_WHEEL_PROJECT}/json", timeout=DOWNLOAD_TIMEOUT_S)
        version, file = pick_node_wheel(meta)
        size = int(file.get("size") or 0) or None
        sha256 = str((file.get("digests") or {}).get("sha256") or "")
        job.log(f"Node.js {version}: {file.get('filename')} ({_mb(size)}), sha256 {sha256}")
        target = self.node_root / version
        cache = self.node_root / "downloads" / str(file.get("filename"))
        dl = _Phase(job, phase.lo, phase.at(0.85))
        got256, _ = self._download(job, str(file["url"]), cache, expected_size=size, phase=dl, what=f"Node.js {version}")
        if not sha256 or got256.hex() != sha256.lower():
            cache.unlink(missing_ok=True)
            raise IntegrityMismatch(
                f"The downloaded Node.js {version} does not match PyPI's checksum; it was deleted.",
                details=f"expected sha256 {sha256 or '(none published)'}\nreceived sha256 {got256.hex()}",
                hint="Try again; if it keeps failing, a proxy may be altering downloads.",
            )
        job.log("sha256 verified against PyPI")
        job.check_cancel()
        job.say(f"Unpacking Node.js {version}…", percent=phase.at(0.9))
        staging = self.node_root / f".staging-{version}-{secrets.token_hex(4)}"
        try:
            node_rel, npm_rel = _extract_node_wheel(cache, staging)
            v = _run_version([str(staging / node_rel)])
            if not v:
                raise AppsError(f"The unpacked Node.js {version} does not run on this machine.")
            job.log(f"node --version -> v{v}")
            if target.exists():
                shutil.rmtree(target)
            staging.replace(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        cur = {
            "version": version,
            "node": f"{version}/{node_rel}",
            "npm_cli": f"{version}/{npm_rel}" if npm_rel else "",
            "source": f"{NODE_WHEEL_PROJECT} {version} ({file.get('filename')})",
            "sha256": sha256,
            "installed_at": _iso(_now()),
        }
        (self.node_root / "current.json").write_text(json.dumps(cur, indent=2) + "\n", encoding="utf-8")
        cache.unlink(missing_ok=True)
        self._node_cache = None
        job.say(f"Node.js {version} installed in {target}", percent=phase.hi)
        return {"node_version": version, "node_path": str(target / node_rel)}

    def start_node_install(self, *, run_inline: bool = False, same_machine: bool = False) -> Tuple[Job, bool]:
        self._require_install_allowed(same_machine=same_machine)

        def work(job: Job) -> Dict[str, Any]:
            job.same_machine = bool(same_machine)
            res = self.install_node(job, _Phase(job, 0.0, 100.0))
            res["message"] = f"Node.js {res['node_version']} is ready for the apps."
            return res

        return self.jobs.start(kind="runtime_install", target="runtime:node", app_id=None, title="Install Node.js", work=work, run_inline=run_inline)

    def _ensure_node(self, job: Job, phase: _Phase) -> Dict[str, Any]:
        st = self.node_status(refresh=True)
        if st["available"]:
            job.log(f"Node.js {st['version']} ({st['source']}): {st['path']}")
            return st
        if (self._setting("node") or "auto").strip() == "system":
            raise NodeMissing("No Node.js 18+ with npm found on this machine, and this gateway is set to use the system Node only.", hint="Install Node.js, or set Node.js for apps to auto (`abstractgateway apps config set node auto`).")
        self.install_node(job, phase)
        st = self.node_status(refresh=True)
        if not st["available"]:
            raise NodeMissing("Node.js was installed but cannot be used: " + "; ".join(st.get("problems") or []))
        return st

    # -- app install ------------------------------------------------------------
    def installed_version(self, app_id: str) -> Optional[str]:
        v = self.app_state(app_id).get("version")
        if v and self.package_dir(app_id, str(v)).joinpath("package.json").is_file():
            return str(v)
        return None

    def package_dir(self, app_id: str, version: str) -> Path:
        spec = spec_for(app_id)
        return self.apps_root / app_id / version / "node_modules" / Path(*spec.package.split("/"))

    def bin_path(self, app_id: str, version: str) -> Path:
        pkg_dir = self.package_dir(app_id, version)
        try:
            pj = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
        except Exception as exc:
            raise NotInstalled(f"{spec_for(app_id).name} {version} is not installed correctly (package.json unreadable: {exc}).") from exc
        b = pj.get("bin")
        rel = b if isinstance(b, str) else (next(iter(b.values())) if isinstance(b, dict) and b else "bin/cli.js")
        path = (pkg_dir / str(rel)).resolve()
        if not path.is_file():
            raise NotInstalled(f"{spec_for(app_id).name} {version}: its start script {rel} is missing.")
        return path

    def _install_app(self, job: Job, spec: AppSpec, version: Optional[str], phase: _Phase) -> Dict[str, Any]:
        self._require_install_allowed(same_machine=job.same_machine)
        job.step("resolve", f"Looking up {spec.package} on the npm registry…")
        try:
            info = self.resolve_package(spec, version, use_cache=False)
        except NetworkUnavailable as exc:
            raise NetworkUnavailable(
                f"The npm registry ({urllib.parse.urlparse(self.registry_url).netloc}) is not reachable, so {spec.name} cannot be downloaded: {exc.message}",
                hint="Connect this machine to the internet and try again. Apps already installed keep working offline.",
            ) from exc
        ver = info["version"]
        size_note = _mb(info["size"]) if info.get("size") else (f"{_mb(info['unpacked_size'])} unpacked" if info.get("unpacked_size") else "size not published")
        job.log(f"{spec.package}@{ver}: {info['tarball']} ({size_note}); integrity {info['integrity'][:24]}…; dependencies: {', '.join(info['dependencies']) or 'none'}")
        job.result.update({"version": ver})
        needs_npm = bool(info["dependencies"])
        job.step("download", f"Downloading {spec.name} {ver}…")
        cache = self.apps_root / "downloads" / f"{spec.short_name}-{ver}.tgz"
        dl = _Phase(job, phase.lo, phase.at(0.6 if needs_npm else 0.85))
        _, got512 = self._download(job, info["tarball"], cache, expected_size=info.get("size"), phase=dl, what=f"{spec.name} {ver}")
        job.step("verify", "Checking the download against the registry's sha512 integrity…")
        if not verify_sri(got512, info["integrity"]):
            cache.unlink(missing_ok=True)
            raise IntegrityMismatch(
                f"The downloaded {spec.name} {ver} does not match the npm registry's integrity hash; it was deleted.",
                details=f"expected {info['integrity']}\nreceived sha512-{base64.b64encode(got512).decode()}",
                hint="Try again; if it keeps failing, a proxy may be altering downloads.",
            )
        job.log("sha512 integrity verified")
        job.check_cancel()
        staging = self.apps_root / spec.id / f".staging-{ver}-{secrets.token_hex(4)}"
        target = self.apps_root / spec.id / ver
        try:
            staging.mkdir(parents=True, exist_ok=True)
            if needs_npm:
                node = self._ensure_node(job, _Phase(job, phase.at(0.6), phase.at(0.62)))
                job.step("dependencies", f"Installing {spec.name}'s dependencies with npm ({', '.join(info['dependencies'])})…")
                job.indeterminate = True
                self._npm_install(job, node, staging, cache, spec)
                job.indeterminate = False
            else:
                job.step("unpack", f"Unpacking {spec.name} {ver} (no dependencies to install)…")
                _extract_npm_tarball(cache, staging / "node_modules" / Path(*spec.package.split("/")))
            job.say("Checking the installed files…", percent=phase.at(0.95))
            pkg = staging / "node_modules" / Path(*spec.package.split("/"))
            pj = json.loads((pkg / "package.json").read_text(encoding="utf-8"))
            if str(pj.get("version")) != ver or str(pj.get("name")) != spec.package:
                raise AppsError(f"Installed package is {pj.get('name')}@{pj.get('version')}, expected {spec.package}@{ver}.")
            (staging / ".install.json").write_text(
                json.dumps({"package": spec.package, "version": ver, "integrity": info["integrity"], "tarball": info["tarball"], "installed_at": _iso(_now())}, indent=2) + "\n",
                encoding="utf-8",
            )
            running_here = self._procs.get(spec.id)
            if target.exists():
                if running_here is not None and running_here.alive() and running_here.version == ver:
                    running_here.stop()
                shutil.rmtree(target)
            staging.replace(target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        previous = self.app_state(spec.id).get("version")
        self._update_app_state(spec.id, version=ver, installed_at=_iso(_now()), integrity=info["integrity"])
        self._prune_versions(spec.id, keep={ver, str(previous or "")})
        job.say(f"{spec.name} {ver} installed.", percent=phase.hi)
        return {"version": ver, "previous_version": previous, "path": str(target)}

    def _npm_install(self, job: Job, node: Dict[str, Any], staging: Path, tarball: Path, spec: AppSpec) -> None:
        (staging / "package.json").write_text(json.dumps({"name": f"abstractgateway-app-{spec.id}", "private": True}, indent=2) + "\n", encoding="utf-8")
        npm = list(node.get("npm_command") or node.get("npm") or [])
        if not npm:
            raise NodeMissing(f"npm was not found next to Node.js at {node.get('path')}.")
        cmd = npm + ["install", "--omit=dev", "--no-audit", "--no-fund", "--no-package-lock", "--loglevel=http", str(tarball)]
        env = _scrubbed_child_env(dict(os.environ))
        env["PATH"] = str(Path(node["path"]).parent) + os.pathsep + env.get("PATH", "")
        env["npm_config_cache"] = str(self.apps_root / "npm-cache")
        env["npm_config_update_notifier"] = "false"
        env["npm_config_registry"] = self.registry_url + "/"
        job.log("$ " + " ".join(cmd))
        proc = subprocess.Popen(cmd, cwd=str(staging), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        fetched = 0
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            job.log("npm: " + line)
            if " fetch GET 200 " in line or "http fetch GET 200" in line:
                fetched += 1
                job.say(f"Installing dependencies: {fetched} packages fetched…", log=False)
            if job.cancel_event.is_set():
                proc.terminate()
        code = proc.wait()
        job.check_cancel()
        if code != 0:
            offline = any(k in job.full_log() for k in ("ENOTFOUND", "EAI_AGAIN", "ECONNREFUSED", "ETIMEDOUT", "network"))
            if offline:
                raise NetworkUnavailable(
                    f"npm could not download {spec.name}'s dependencies: the npm registry is not reachable.",
                    hint="Connect this machine to the internet and try again.",
                )
            raise AppsError(f"npm could not install {spec.name}'s dependencies (exit code {code}).", hint="The npm output is in details.")
        job.log(f"npm finished ({fetched} packages fetched)")

    def _prune_versions(self, app_id: str, *, keep: set) -> None:
        root = self.apps_root / app_id
        running = self._procs.get(app_id)
        for d in root.iterdir() if root.is_dir() else []:
            if not d.is_dir() or d.name.startswith(".") or d.name in keep:
                continue
            if running is not None and running.alive() and running.version == d.name:
                continue
            shutil.rmtree(d, ignore_errors=True)

    def install_includes_terminal(self, app_id: str) -> Optional[TuiSpec]:
        """The terminal app that an Install of this app also installs (mission
        LL, operator 2026-09-24: "it should always download both wui and tui
        if both are present"): the app has one (TUI_BY_APP), it is not on this
        computer yet, and a prebuilt binary exists for this computer. None
        otherwise: then Install installs the browser app only and the terminal
        app's command stays under Technical details."""
        tui = TUI_BY_APP.get(str(app_id or "").strip().lower())
        if tui is None:
            return None
        if self.tui_status(tui)["installed"]:
            return None
        if self.tui_install_plan(tui)["method"] != "release_binary":
            return None
        return tui

    def start_install(
        self,
        app_id: str,
        *,
        version: Optional[str] = None,
        launch: bool = False,
        update: bool = False,
        gateway_url: Optional[str] = None,
        run_inline: bool = False,
        same_machine: bool = False,
        with_terminal: bool = True,
    ) -> Tuple[Job, bool]:
        """Install (or update) an app as ONE job. A fresh install of an app with
        a terminal version (Code) installs the browser app AND the terminal app
        (`install_includes_terminal`), shown as the job's two `parts`; Cancel
        stops both. Nothing is started unless `launch` (CLI `--launch`)."""
        from .apps_desktop import is_desktop_app

        if is_desktop_app(app_id) and not update:
            return self.start_desktop_install(app_id, run_inline=run_inline, same_machine=same_machine)
        spec = spec_for(app_id)
        self._require_install_allowed(same_machine=same_machine)
        tui = self.install_includes_terminal(spec.id) if (with_terminal and not update) else None

        def work(job: Job) -> Dict[str, Any]:
            job.same_machine = bool(same_machine)
            web_hi = 100.0 if tui is None else 65.0
            if tui is not None:
                job.parts = [
                    {"id": "install", "label": f"{spec.name} in the browser", "state": "running"},
                    {"id": "install-tui", "label": f"{spec.name} in the terminal", "state": "waiting"},
                ]
            node_needed = not self.node_status(refresh=True)["available"]
            lo = 0.0
            if node_needed:
                self._ensure_node(job, _Phase(job, 0.0, web_hi * 0.45))
                lo = web_hi * 0.45
            was_running = self._procs.get(spec.id) is not None and self._procs[spec.id].alive()
            res = self._install_app(job, spec, version, _Phase(job, lo, web_hi * 0.9 if (launch or was_running) else web_hi))
            if launch or (update and was_running):
                job.step("launch", f"Starting {spec.name}…")
                if was_running:
                    self._procs[spec.id].stop()
                row = self.launch(spec.id, gateway_url=gateway_url)
                res.update({"url": row.get("url"), "port": row.get("port"), "running": True})
                res["message"] = f"{spec.name} {res['version']} is running at {row.get('url')}"
            else:
                res["message"] = f"{spec.name} {res['version']} installed."
            if tui is not None:
                job.part("install", "done")
                job.part("install-tui", "running")
                job.check_cancel()
                try:
                    tres = self._install_tui(job, tui, _Phase(job, web_hi, 100.0))
                except AppsError as exc:
                    raise type(exc)(
                        f"{spec.name} is installed for the browser, but its terminal app did not install: {exc.message}",
                        hint=exc.hint,
                        details=exc.details,
                        extra=exc.extra,
                    ) from exc
                job.part("install-tui", "done")
                res["terminal"] = {"version": tres.get("version"), "path": tres.get("path")}
                res["message"] = f"{spec.name} {res['version']} is installed, with its terminal app {tres.get('version')}."
            return res

        kind = "app_update" if update else "app_install"
        title = f"{'Update' if update else 'Install'} {spec.name}"
        return self.jobs.start(kind=kind, target=f"app:{spec.id}", app_id=spec.id, title=title, work=work, run_inline=run_inline)

    # -- processes ----------------------------------------------------------------
    def _proc(self, spec: AppSpec) -> AppProcess:
        with self._lock:
            p = self._procs.get(spec.id)
            if p is None:
                p = AppProcess(self, spec)
                self._procs[spec.id] = p
            return p

    def resolve_gateway_url(self, explicit: Optional[str] = None) -> str:
        if explicit:
            return str(explicit).rstrip("/")
        if self.gateway_url:
            return self.gateway_url
        try:
            from .tray_supervisor import serve_context

            base = serve_context().get("base_url")
            if base:
                return str(base).rstrip("/")
        except Exception:
            pass
        try:
            from .first_run import read_serve_record

            rec = read_serve_record(self.data_dir) or {}
            if rec.get("alive") and rec.get("url"):
                return str(rec["url"]).rstrip("/")
        except Exception:
            pass
        return (os.environ.get("ABSTRACTGATEWAY_URL") or "http://127.0.0.1:8080").rstrip("/")

    def external_apps(self, *, refresh: bool = False) -> Dict[str, ExternalApp]:
        """Apps running without the gateway having started them (probed on
        the usual loopback ports, cached EXTERNAL_CACHE_TTL_S). Ports held by
        an app this gateway runs are never probed."""
        with self._lock:
            exclude = sorted(p.port for p in self._procs.values() if p.alive() and p.port)
        with self._external_lock:
            cached = self._external_cache
            if not refresh and cached is not None and _now() - cached[0] < EXTERNAL_CACHE_TTL_S and cached[2] == exclude:
                return dict(cached[1])
            try:
                found = dict(self.external_probe(exclude_ports=exclude) or {})
            except Exception:  # noqa: BLE001 - a probe failure is "nothing found", never an error page
                logger.warning("probing for apps started outside the gateway failed", exc_info=True)
                found = {}
            self._external_cache = (_now(), found, exclude)
            return dict(found)

    def _external_running(self, spec: AppSpec) -> Optional[ExternalApp]:
        p = self._procs.get(spec.id)
        if p is not None and p.alive():
            return None
        return self.external_apps().get(spec.id)

    def launch(self, app_id: str, *, gateway_url: Optional[str] = None, enable: bool = True, ready_timeout_s: float = READY_TIMEOUT_S) -> Dict[str, Any]:
        spec = spec_for(app_id)
        if self._external_running(spec) is not None:
            # Already running, started outside the gateway: nothing to start
            # (a second copy would only take another port).
            return self.app_row(spec)
        version = self.installed_version(spec.id)
        if not version:
            raise NotInstalled(f"{spec.name} is not installed yet.", hint="Install it first (the Install button, or `abstractgateway apps install " + spec.id + "`).")
        node = self.node_status(refresh=True)
        if not node.get("path"):
            raise NodeMissing("Node.js is not installed, so the app cannot start.", hint="Install Node.js from the Apps page (one click) and try again.")
        bin_js = self.bin_path(spec.id, version)
        proc = self._proc(spec)
        if proc.alive():
            if enable:
                self._update_app_state(spec.id, enabled=True)
            return self.app_row(spec)
        (lo, hi), explicit = self.port_range()
        state = self.app_state(spec.id)
        taken = [p.port for s, p in self._procs.items() if s != spec.id and p.alive() and p.port]
        port = allocate_port(
            preferred=[state.get("port"), None if explicit else spec.default_port],
            port_range=(lo, hi),
            taken=taken,
            host=self.bind_host,
            restrict_to_range=explicit,
        )
        gw = self.resolve_gateway_url(gateway_url)
        self._update_app_state(spec.id, port=port, gateway_url=gw, **({"enabled": True} if enable else {}))
        proc.start(node=str(node["path"]), bin_js=bin_js, port=port, host=self.bind_host, gateway_url=gw, version=version, ready_timeout_s=ready_timeout_s)
        return self.app_row(spec)

    def stop(self, app_id: str, *, disable: bool = True) -> Dict[str, Any]:
        spec = spec_for(app_id)
        ext = self._external_running(spec)
        if ext is not None:
            raise StartedOutsideGateway(
                f"{spec.name} was started outside the gateway (port {ext.port}), so the gateway cannot stop it.",
                hint="Stop it where it was started (its terminal, or the script that started it).",
            )
        if disable:
            self._update_app_state(spec.id, enabled=False)
        p = self._procs.get(spec.id)
        if p is not None:
            p.stop()
        return self.app_row(spec)

    def stop_all(self) -> None:
        """Gateway shutdown: stop every app, keep their enabled flags."""
        for p in list(self._procs.values()):
            try:
                p.stop()
            except Exception:
                logger.warning("stopping app %s failed", p.spec.id, exc_info=True)

    def autostart(self, *, gateway_url: Optional[str] = None) -> List[Dict[str, Any]]:
        """Gateway boot: stop leftovers of a previous run, then start every
        installed app marked enabled. Returns one outcome row per app."""
        if gateway_url:
            self.gateway_url = str(gateway_url).rstrip("/")
        outcomes: List[Dict[str, Any]] = []
        self.reap_orphans()
        for spec in APPS:
            st = self.app_state(spec.id)
            if not st.get("enabled"):
                continue
            try:
                row = self.launch(spec.id, gateway_url=gateway_url)
                outcomes.append({"app_id": spec.id, "ok": True, "url": row.get("url")})
                logger.info("app %s started at %s (enabled)", spec.id, row.get("url"))
            except AppsError as exc:
                outcomes.append({"app_id": spec.id, "ok": False, "message": exc.message})
                logger.warning("app %s is enabled but did not start: %s", spec.id, exc.message)
        return outcomes

    def app_log_tail(self, app_id: str, lines: int = 200) -> List[str]:
        path = self.app_log_path(app_id)
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 256_000))
                data = f.read().decode("utf-8", "replace")
        except Exception:
            return []
        return data.splitlines()[-max(1, int(lines)):]

    # -- sign-in handover ------------------------------------------------------------
    def mint_handover(self, app_id: str, principal: Any, *, host: str, path: Optional[str] = None) -> str:
        """A one-time code (2 minutes) the browser trades at
        `/apps/handover/<code>` for this app's sign-in cookies. `path` is
        where inside the app the browser lands (`handover_path`: "/#new"
        opens Entity's creation form); it is bound to the code, never read
        from the handover URL."""
        spec_for(app_id)
        target = handover_path(path)
        now = _now()
        for code, rec in list(self._handover.items()):
            if rec[0] < now:
                self._handover.pop(code, None)
        code = secrets.token_urlsafe(32)
        self._handover[code] = (now + HANDOVER_TTL_S, app_id, principal, host, target)
        return code

    def redeem_handover_target(self, code: str) -> Optional[Tuple[str, Any, str, str]]:
        """(app_id, principal, host, path) for a live code, once."""
        rec = self._handover.pop(str(code or ""), None)
        if rec is None or rec[0] < _now():
            return None
        return rec[1], rec[2], rec[3], rec[4]

    def redeem_handover(self, code: str) -> Optional[Tuple[str, Any, str]]:
        rec = self.redeem_handover_target(code)
        return None if rec is None else (rec[0], rec[1], rec[2])

    # -- what an app holds -------------------------------------------------------------
    def content_summary(self, spec: AppSpec, *, app_gateway_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """A small fact about what the app holds ON THIS GATEWAY, for the
        card's first-run action; None for apps that have none. Entity:
        {"entities_count": n}, n None when unknown (the count failed, or an
        app started outside the gateway talks to another gateway)."""
        if spec.id != "entity":
            return None
        if app_gateway_url:
            mine = _gateway_key(self.resolve_gateway_url())
            theirs = _gateway_key(app_gateway_url)
            if mine is None or theirs is None or mine != theirs:
                return {"entities_count": None}
        try:
            n = self.entities_counter()
        except Exception:  # noqa: BLE001
            n = None
        return {"entities_count": n if isinstance(n, int) and n >= 0 else None}

    # -- terminal apps (TUIs) ----------------------------------------------------------
    @property
    def bin_dir(self) -> Path:
        return self.apps_root / "bin"

    def managed_tui_path(self, tui: TuiSpec) -> Path:
        return self.bin_dir / tui.exe_name

    def _tui_candidates(self, tui: TuiSpec) -> List[Tuple[str, Path]]:
        """(source, path) in preference order: the gateway's own copy, PATH,
        then ~/.cargo/bin (a gateway started at login has a minimal PATH)."""
        out: List[Tuple[str, Path]] = [("gateway", self.managed_tui_path(tui))]
        found = shutil.which(tui.binary)
        if found:
            out.append(("path", Path(found)))
        out.append(("path", Path.home() / ".cargo" / "bin" / tui.exe_name))
        uniq: List[Tuple[str, Path]] = []
        seen = set()
        for src, p in out:
            key = str(p)
            if key not in seen:
                seen.add(key)
                uniq.append((src, p))
        return uniq

    def probe_tui_binary(self, path: Path, tui: TuiSpec) -> Optional[str]:
        """The version of the terminal app at `path`, or None when `path` is
        not it (missing, not executable, a script, or a same-named program
        whose --help does not name it). Cached per (path, mtime, size)."""
        try:
            st = path.stat()
        except OSError:
            return None
        if not path.is_file() or not os.access(path, os.X_OK):
            return None
        key = (str(path), st.st_mtime_ns, st.st_size)
        cached = self._tui_probe_cache.get(key)
        if cached is not None:
            return cached[0]
        version: Optional[str] = None
        try:
            if _is_script(path):
                raise ValueError("a script, not the terminal app")
            env = _scrubbed_child_env(dict(os.environ))
            h = subprocess.run([str(path), "--help"], capture_output=True, text=True, timeout=TUI_PROBE_TIMEOUT_S, env=env, stdin=subprocess.DEVNULL)
            if tui.help_marker in (h.stdout or "") + (h.stderr or ""):
                v = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=TUI_PROBE_TIMEOUT_S, env=env, stdin=subprocess.DEVNULL)
                m = re.search(rf"{re.escape(tui.binary)}\s+v?(\d+\.\d+\.\d+[0-9A-Za-z.+-]*)", v.stdout or "")
                version = m.group(1) if m else None
        except Exception:
            version = None
        self._tui_probe_cache[key] = (version,)
        return version

    def tui_status(self, tui: TuiSpec) -> Dict[str, Any]:
        """{installed, version, source: gateway|path|None, path}: presence only."""
        for source, path in self._tui_candidates(tui):
            v = self.probe_tui_binary(path, tui)
            if v:
                return {"installed": True, "version": v, "source": source, "path": str(path)}
        return {"installed": False, "version": None, "source": None, "path": None}

    def _get_text(self, url: str, *, timeout: float) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": "abstractgateway-apps"})
        try:
            with self.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise NetworkUnavailable(f"{urllib.parse.urlparse(url).netloc} answered HTTP {exc.code} for {url}.") from exc
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise NetworkUnavailable(
                f"Cannot reach {urllib.parse.urlparse(url).netloc} ({getattr(exc, 'reason', exc)}).",
                hint="Check this machine's internet connection (or proxy), then try again.",
            ) from exc

    def tui_release(self, tui: TuiSpec, *, use_cache: bool = True) -> Optional[Dict[str, Any]]:
        """The newest release of a terminal app that publishes binaries
        (GitHub API), cached like the npm metadata. None: no release at all."""
        if not tui.repo:
            return None
        key = "tui:" + tui.id
        cached = self._registry_cache.get(key)
        if use_cache and cached is not None:
            age = _now() - cached[0]
            if isinstance(cached[1], Exception):
                if age < REGISTRY_FAIL_TTL_S:
                    raise cached[1]
            elif age < REGISTRY_CACHE_TTL_S:
                return cached[1]
        url = f"{GITHUB_API}/repos/{tui.repo}/releases?per_page=30"
        try:
            data = self._get_json(url, timeout=REGISTRY_TIMEOUT_S if use_cache else DOWNLOAD_TIMEOUT_S, accept="application/vnd.github+json")
        except AppsError as exc:
            self._registry_cache[key] = (_now(), exc)
            raise
        rel = pick_tui_release(data, tui)
        self._registry_cache[key] = (_now(), rel)
        return rel

    def tui_install_plan(self, tui: TuiSpec, release: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """How this terminal app can be installed HERE: {method: release_binary|cargo,
        target, asset?, reason?}. `release` None = not looked up (plan by platform)."""
        if not tui.repo:
            return {"method": "cargo", "target": release_target(), "reason": f"Needs the Rust toolchain: the {tui.name.lower()}'s terminal version is published as source code only (crates.io), with no ready-made download."}
        target = release_target()
        if target is None:
            return {"method": "cargo", "target": None, "reason": f"Needs the Rust toolchain: there is no prebuilt {tui.name} terminal app for this computer ({sys.platform}/{platform.machine()})."}
        if release is None:
            return {"method": "release_binary", "target": target}
        name = release_asset_name(tui, release["tag"], target)
        if name not in release["assets"] or "SHA256SUMS" not in release["assets"]:
            missing = name if name not in release["assets"] else "SHA256SUMS"
            return {"method": "cargo", "target": target, "reason": f"Needs the Rust toolchain: release {release['tag']} has no {missing} for this computer."}
        return {"method": "release_binary", "target": target, "asset": name}

    def _install_tui(self, job: Job, tui: TuiSpec, phase: _Phase) -> Dict[str, Any]:
        self._require_install_allowed(same_machine=job.same_machine)
        job.step("resolve", f"Looking up the newest {tui.name} terminal app on GitHub…")
        plan = self.tui_install_plan(tui)
        if plan["method"] != "release_binary":
            raise ToolchainRequired(plan["reason"], hint=f"Install it with `{tui.install_command}` (needs Rust 1.87 or newer).", extra={"command": tui.install_command})
        try:
            rel = self.tui_release(tui, use_cache=False)
        except NetworkUnavailable as exc:
            raise NetworkUnavailable(
                f"GitHub is not reachable, so the {tui.name} terminal app cannot be downloaded: {exc.message}",
                hint="Connect this machine to the internet and try again.",
            ) from exc
        if rel is None:
            raise ToolchainRequired(f"{tui.repo} has no release with a prebuilt terminal app yet.", hint=f"Install it with `{tui.install_command}`.", extra={"command": tui.install_command})
        plan = self.tui_install_plan(tui, rel)
        if plan["method"] != "release_binary":
            raise ToolchainRequired(plan["reason"], hint=f"Install it with `{tui.install_command}`.", extra={"command": tui.install_command})
        ver, name = rel["version"], plan["asset"]
        asset = rel["assets"][name]
        job.result.update({"version": ver})
        job.log(f"{tui.binary} {ver}: {name} ({_mb(asset.get('size'))}) from {asset['url']}")
        job.step("checksums", "Reading the release's checksums…")
        sums = parse_sha256sums(self._get_text(rel["assets"]["SHA256SUMS"]["url"], timeout=DOWNLOAD_TIMEOUT_S))
        expected = sums.get(name)
        if not expected:
            raise IntegrityMismatch(f"Release {rel['tag']} publishes no checksum for {name}; nothing was installed.")
        digest = str(asset.get("digest") or "")
        if digest.startswith("sha256:") and digest[7:].lower() != expected:
            raise IntegrityMismatch(
                f"GitHub's digest for {name} disagrees with the release's SHA256SUMS; nothing was installed.",
                details=f"SHA256SUMS {expected}\nGitHub     {digest[7:].lower()}",
            )
        job.log(f"expected sha256 {expected} (SHA256SUMS" + (", GitHub digest agrees)" if digest else ")"))
        job.step("download", f"Downloading {tui.name}'s terminal app {ver}…")
        cache = self.apps_root / "downloads" / name
        got256, _ = self._download(job, asset["url"], cache, expected_size=asset.get("size"), phase=_Phase(job, phase.lo, phase.at(0.85)), what=f"{tui.name} terminal app {ver}")
        job.step("verify", "Checking the download against the release's sha256…")
        if got256.hex() != expected:
            cache.unlink(missing_ok=True)
            raise IntegrityMismatch(
                f"The downloaded {name} does not match the release's checksum; it was deleted.",
                details=f"expected sha256 {expected}\nreceived sha256 {got256.hex()}",
                hint="Try again; if it keeps failing, a proxy may be altering downloads.",
            )
        job.log("sha256 verified")
        job.check_cancel()
        job.step("unpack", "Unpacking…")
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        staging = self.bin_dir / f".{tui.exe_name}.{secrets.token_hex(4)}"
        try:
            _extract_single_binary(cache, tui.exe_name, staging)
            os.chmod(staging, 0o755)
            job.step("check", f"Checking that {tui.binary} runs…")
            env = _scrubbed_child_env(dict(os.environ))
            try:
                out = subprocess.run([str(staging), "--version"], capture_output=True, text=True, timeout=TUI_PROBE_TIMEOUT_S, env=env, stdin=subprocess.DEVNULL)
            except Exception as exc:  # noqa: BLE001
                raise AppsError(f"The downloaded {tui.binary} does not run on this computer: {type(exc).__name__}: {exc}") from exc
            said = (out.stdout or "").strip()
            job.log(f"{tui.binary} --version -> {said or '(nothing)'} (exit {out.returncode})")
            if out.returncode != 0 or said != f"{tui.binary} {ver}":
                raise AppsError(
                    f"The downloaded {tui.binary} did not answer `--version` with {ver}.",
                    details=f"exit code {out.returncode}\nstdout: {out.stdout}\nstderr: {out.stderr}",
                )
            target = self.managed_tui_path(tui)
            os.replace(staging, target)
        finally:
            staging.unlink(missing_ok=True)
        cache.unlink(missing_ok=True)
        previous = (self.app_state(tui.id).get("tui") or {}).get("version")
        self._update_app_state(tui.id, tui={"version": ver, "sha256": expected, "asset": name, "tag": rel["tag"], "installed_at": _iso(_now()), "path": str(self.managed_tui_path(tui))})
        self._tui_probe_cache.clear()
        job.say(f"{tui.name}'s terminal app {ver} is installed.", percent=phase.hi, log=False)
        return {"version": ver, "previous_version": previous, "path": str(self.managed_tui_path(tui)), "interface": "tui"}

    def start_tui_install(self, app_id: str, *, run_inline: bool = False, same_machine: bool = False) -> Tuple[Job, bool]:
        tui = tui_for(app_id)
        self._require_install_allowed(same_machine=same_machine)
        plan = self.tui_install_plan(tui)
        if plan["method"] != "release_binary":
            raise ToolchainRequired(plan["reason"], hint=f"Install it with `{tui.install_command}` (needs Rust 1.87 or newer).", extra={"command": tui.install_command})

        def work(job: Job) -> Dict[str, Any]:
            job.same_machine = bool(same_machine)
            res = self._install_tui(job, tui, _Phase(job, 0.0, 100.0))
            res["message"] = f"{tui.name}'s terminal app {res['version']} is installed."
            return res

        return self.jobs.start(kind="tui_install", target=f"tui:{tui.id}", app_id=tui.id, title=f"Install {tui.name} for the terminal", work=work, run_inline=run_inline)

    # -- desktop sign-in handover (the Assistant, CONTRACTS A1 / A-3) ------------------
    def handover_dir(self) -> Path:
        return self.data_dir / "handover"

    def mint_desktop_handover(self, principal: Any, *, base_url: str) -> Tuple[str, Path]:
        """A one-time code (2 minutes) for the desktop Assistant, written into
        a 0600 file this gateway owns: <data dir>/handover/<random>.json =
        {schema, code, base_url, expires_at, user_id}. The Assistant reads the file,
        deletes it, and trades the code on loopback at
        POST /api/gateway/apps/desktop-handover. The code is never on argv
        nor in the environment."""
        import datetime as _dt

        now = _now()
        for code, rec in list(self._desktop_handover.items()):
            if rec[0] < now:
                self._desktop_handover.pop(code, None)
                try:
                    Path(rec[3]).unlink()
                except OSError:
                    pass
        code = secrets.token_urlsafe(32)
        expires = now + HANDOVER_TTL_S
        folder = self.handover_dir()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
        path = folder / f"{secrets.token_hex(16)}.json"
        body = {
            "schema": DESKTOP_HANDOVER_SCHEMA,
            "code": code,
            "base_url": str(base_url).rstrip("/"),
            "expires_at": _dt.datetime.fromtimestamp(expires, tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            # Who clicked Open: the session the code redeems is this user's
            # (the gateway has no display names, so no user_name).
            "user_id": str(getattr(principal, "user_id", "") or ""),
        }
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        self._desktop_handover[code] = (expires, principal, str(base_url).rstrip("/"), str(path))
        return code, path

    def redeem_desktop_handover(self, code: str) -> Optional[Tuple[Any, str]]:
        """(principal, base_url) for a live code, once; the file is removed
        if the Assistant did not already."""
        rec = self._desktop_handover.pop(str(code or ""), None)
        if rec is None:
            return None
        try:
            Path(rec[3]).unlink()
        except OSError:
            pass
        if rec[0] < _now():
            return None
        return rec[1], rec[2]

    # -- terminal sign-in handover ------------------------------------------------------
    def mint_tui_handover(self, app_id: str, principal: Any, *, gateway_url: str) -> str:
        """A one-time code (2 minutes) the launcher script trades on loopback
        at `POST /apps/tui-handover` for a terminal sign-in. Separate from the
        browser handover: a browser code never opens a terminal and back."""
        tui_for(app_id)
        now = _now()
        for code, rec in list(self._tui_handover.items()):
            if rec[0] < now:
                self._tui_handover.pop(code, None)
        code = secrets.token_urlsafe(32)
        self._tui_handover[code] = (now + HANDOVER_TTL_S, app_id, principal, gateway_url)
        return code

    def redeem_tui_handover(self, code: str) -> Optional[Tuple[str, Any, str]]:
        rec = self._tui_handover.pop(str(code or ""), None)
        if rec is None or rec[0] < _now():
            return None
        return rec[1], rec[2], rec[3]

    @staticmethod
    def issue_tui_token(app_id: str, principal: Any) -> str:
        """A bearer token for a terminal app: accepted from loopback peers
        only, acts as `principal`, lives in this gateway process's memory only."""
        from .security.gateway_security import register_ephemeral_loopback_token

        token = "agtui_" + secrets.token_urlsafe(32)
        register_ephemeral_loopback_token(token, label=f"terminal-app:{app_id}", principal=principal)
        return token

    def write_tui_launcher(self, *, tui: TuiSpec, binary: str, gateway_url: str, code: str, windows: Optional[bool] = None) -> Path:
        win = sys.platform.startswith("win") if windows is None else windows
        d = self.apps_root / "terminal"
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        suffix = ".cmd" if win else (".command" if sys.platform == "darwin" else ".sh")
        path = d / f"open-{tui.id}-{secrets.token_hex(6)}{suffix}"
        helper = str(Path(__file__).with_name("tui_signin.py"))
        text = tui_launch_script_text(python=sys.executable, helper=helper, binary=binary, gateway_url=gateway_url, app_id=tui.id, code=code, windows=win)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return path

    def _prepare_tui_launcher(self, tui: TuiSpec, st: Dict[str, Any], *, principal: Any, gateway_url: str) -> Tuple[Path, str]:
        """A one-use launcher script (0700, deletes itself) holding a fresh
        single-use handover code for `principal`: what a terminal runs."""
        code = self.mint_tui_handover(tui.id, principal, gateway_url=gateway_url)
        return self.write_tui_launcher(tui=tui, binary=str(st["path"]), gateway_url=gateway_url, code=code), code

    def tui_signin_command(self, app_id: str, *, principal: Any, gateway_url: str) -> Dict[str, Any]:
        """Terminal parity for "Open in Terminal" (`abstractgateway apps
        tui-command`): the same one-use launcher, WITHOUT opening a window.
        Returns the line to run in a terminal on this machine (it signs the
        app in once, within 2 minutes) and the plain launch command. Never a
        token: the launcher holds only the single-use code."""
        tui = tui_for(app_id)
        st = self.tui_status(tui)
        if not st["installed"]:
            raise NotInstalled(
                f"{tui.name}'s terminal app is not installed on this computer.",
                hint=f"Install it with `abstractgateway apps install-tui {tui.id}`.",
                extra={"command": tui_command(tui, gateway_url), "install_command": tui.install_command},
            )
        script, _code = self._prepare_tui_launcher(tui, st, principal=principal, gateway_url=gateway_url)
        win = sys.platform.startswith("win")
        return {
            "ok": True,
            "app_id": tui.id,
            "interface": "tui",
            "version": st["version"],
            "signin_command": _quote_cmd(str(script)) if win else _quote_posix(str(script)),
            "command": tui_command(tui, gateway_url, binary=str(st["path"]) if (st["source"] == "gateway" or shutil.which(tui.binary) is None) else None),
            "expires_in_s": int(HANDOVER_TTL_S),
        }

    def launch_tui(
        self,
        app_id: str,
        *,
        principal: Any,
        gateway_url: str,
        opener: Optional[Callable[[Sequence[str]], None]] = None,
    ) -> Dict[str, Any]:
        """Open the app's terminal version in a NEW terminal window on this
        machine, signed in as `principal` through a one-time handover. The
        caller (route) has already checked the request comes from this machine."""
        tui = tui_for(app_id)
        st = self.tui_status(tui)
        command = tui_command(tui, gateway_url)
        if not st["installed"]:
            raise NotInstalled(
                f"{tui.name}'s terminal app is not installed on this computer.",
                hint="Install it from its app card first.",
                extra={"command": command, "install_command": tui.install_command},
            )
        script, code = self._prepare_tui_launcher(tui, st, principal=principal, gateway_url=gateway_url)
        try:
            terminal, argv = terminal_argv(script)
            (opener or self.terminal_opener)(argv)
        except AppsError as exc:
            self._tui_handover.pop(code, None)
            script.unlink(missing_ok=True)
            exc.extra.setdefault("command", tui_command(tui, gateway_url, binary=str(st["path"])))
            raise
        except Exception as exc:  # noqa: BLE001
            self._tui_handover.pop(code, None)
            script.unlink(missing_ok=True)
            raise LaunchFailed(
                f"The terminal did not open: {type(exc).__name__}: {exc}",
                hint="Copy the command and run it in a terminal on this computer.",
                extra={"command": tui_command(tui, gateway_url, binary=str(st["path"]))},
            ) from exc
        return {
            "ok": True,
            "app_id": tui.id,
            "interface": "tui",
            "terminal": terminal,
            "version": st["version"],
            "message": f"{tui.name} opened in a new {terminal} window, signed in to this gateway.",
            "expires_in_s": int(HANDOVER_TTL_S),
        }

    def tui_interface(self, tui: TuiSpec, *, release: Optional[Dict[str, Any]] = None, release_error: Optional[str] = None, caller: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The `interfaces[]` entry of kind "tui" for an app row (docs/apps.md)."""
        caller = caller or {}
        gw = str(caller.get("gateway_url") or self.resolve_gateway_url())
        local = bool(caller.get("local", True))
        admin = bool(caller.get("admin", True))
        st = self.tui_status(tui)
        plan = self.tui_install_plan(tui, release)
        allowed = self.install_allowed(same_machine=bool(caller.get("same_machine")))
        installed = bool(st["installed"])
        job = self.jobs.active_for(f"tui:{tui.id}")
        latest = (release or {}).get("version")
        update_available = bool(installed and st["source"] == "gateway" and latest and version_newer(latest, st["version"]))
        install_available = (not installed or update_available) and plan["method"] == "release_binary" and allowed and not release_error
        if installed and not update_available:
            install_blocked = None
        elif plan["method"] != "release_binary":
            install_blocked = plan["reason"]
        elif not allowed:
            install_blocked = INSTALLS_OFF_MESSAGE
        elif release_error:
            install_blocked = f"GitHub (where the prebuilt terminal app is published) is not reachable: {release_error}"
        else:
            install_blocked = None
        if not installed:
            launch_available, launch_blocked = False, f"{tui.name}'s terminal app is not installed on this computer."
        elif not local:
            launch_available, launch_blocked = False, "Your browser is on another computer: a terminal can only open on the gateway's own screen. Copy the command instead."
        elif not admin:
            launch_available, launch_blocked = False, "Only an admin can open a terminal on the gateway computer. Copy the command instead."
        else:
            launch_available, launch_blocked = True, None
        # The full path when this computer's copy is not reachable by name
        # (the gateway's own copy, or ~/.cargo/bin missing from PATH).
        here = local and installed and (st["source"] == "gateway" or shutil.which(tui.binary) is None)
        return {
            "kind": "tui",
            "name": f"{tui.name} in the terminal",
            "binary": tui.binary,
            "installed": installed,
            "version": st["version"],
            "source": st["source"],
            "path": st["path"],
            "latest_version": latest,
            "update_available": update_available,
            "install_available": bool(install_available),
            "install_method": plan["method"],
            "install_blocked_reason": install_blocked,
            "install_command": tui.install_command,
            "download_page": tui.releases_page,
            "launch_available": launch_available,
            "launch_blocked_reason": launch_blocked,
            "launch_mode": "terminal" if launch_available else "copy",
            "command": tui_command(tui, gw, binary=st["path"] if here else None),
            "signin_command": None if launch_available else f"{tui.binary} login {tui.gateway_flag} {gw} --token <your token>",
            "active_job": job.to_dict() if job is not None else None,
        }

    def console_tui_interface(self, *, caller: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The gateway console's own terminal app (crates.io only): what the
        console's Done step says about it. Never installable here."""
        tui = GATEWAY_CONSOLE_TUI
        gw = str((caller or {}).get("gateway_url") or self.resolve_gateway_url())
        st = self.tui_status(tui)
        plan = self.tui_install_plan(tui)
        return {
            "kind": "tui",
            "name": "Gateway console in the terminal",
            "binary": tui.binary,
            "installed": bool(st["installed"]),
            "version": st["version"],
            "source": st["source"],
            "path": st["path"],
            "install_available": False,
            "install_method": plan["method"],
            "install_blocked_reason": plan["reason"],
            "install_command": tui.install_command,
            "launch_available": False,
            "launch_blocked_reason": "It signs in with an admin token typed into its Connection screen.",
            "launch_mode": "copy",
            "command": tui_command(tui, gw),
        }

    def web_interface(self, row: Dict[str, Any], spec: AppSpec) -> Dict[str, Any]:
        """The `interfaces[]` entry of kind "web": today's row fields, mirrored."""
        installed = bool(row["installed"])
        actions = row.get("actions") or []
        launch_available = installed and ("launch" in actions or "open" in actions)
        if not installed:
            blocked = f"{spec.name} is not installed yet."
        elif not launch_available:
            blocked = f"{spec.name} is {str(row.get('status') or 'busy').replace('_', ' ')}."
        else:
            blocked = None
        return {
            "kind": "web",
            "name": f"{spec.name} in the browser",
            "installed": installed,
            "version": row.get("version"),
            "latest_version": row.get("latest_version"),
            "update_available": bool(row.get("update_available")),
            "install_available": bool(row.get("install_available")),
            "install_method": "npm",
            "install_blocked_reason": row.get("install_blocked_reason"),
            "launch_available": launch_available,
            "launch_blocked_reason": blocked,
            "running": bool(row.get("running")),
            "url": row.get("url"),
            "command": f"npx {spec.package}",
        }

    # -- desktop apps (mission LL, 2026-09-24): the Assistant ---------------------------
    def desktop_presence(self, app_id: str, *, refresh: bool = False) -> Dict[str, Any]:
        """apps_desktop.detect_assistant on this machine (cached a few seconds:
        the running check lists processes)."""
        from .apps_desktop import DESKTOP_BY_ID, RUNNING_CACHE_TTL_S, detect_assistant

        spec = DESKTOP_BY_ID[str(app_id)]
        hit = self._desktop_cache.get(spec.id)
        if not refresh and hit is not None and _now() - hit[0] < RUNNING_CACHE_TTL_S:
            return dict(hit[1])
        try:
            found = detect_assistant(self.desktop_probes(), spec=spec)
        except Exception as exc:  # noqa: BLE001 - detection never breaks the apps page
            logger.warning("detecting %s failed", spec.name, exc_info=True)
            found = {"installed": False, "found_by": [f"detection failed: {type(exc).__name__}: {exc}"], "source": "", "launch": None, "launches": [], "bundle": None, "script": None, "package_origin": None, "version": None, "location": None, "running": False, "pid": None, "running_argv": None}
        self._desktop_cache[spec.id] = (_now(), found)
        return dict(found)

    def desktop_install_argv(self, app_id: str) -> Optional[List[str]]:
        """`uv pip install --python <gateway python> abstractassistant` (or pip),
        None when this Python has neither."""
        from .apps_desktop import DESKTOP_BY_ID, pip_install_argv

        spec = DESKTOP_BY_ID[str(app_id)]
        try:
            uv = self.desktop_find_uv()
        except Exception:
            uv = None
        try:
            has_pip = bool(self.desktop_has_pip())
        except Exception:
            has_pip = False
        return pip_install_argv(spec.package, python=self.desktop_python, uv=uv, has_pip=has_pip)

    def desktop_row(self, app_id: str, *, caller: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The console's card for a desktop app (`kind: "desktop"`): the same
        keys as a browser app's row, no port or address; `desktop` says where
        it is found, how it launches, and whether it can open for THIS caller
        (it opens on the gateway computer's screen)."""
        from .apps_desktop import DESKTOP_BY_ID, launch_command_text

        spec = DESKTOP_BY_ID[str(app_id)]
        caller = caller or {}
        pres = self.desktop_presence(spec.id)
        installed = bool(pres.get("installed"))
        running = bool(pres.get("running"))
        same_machine = bool(caller.get("same_machine", True))
        admin = bool(caller.get("admin", True))
        allowed = self.install_allowed(same_machine=bool(caller.get("same_machine")))
        argv = self.desktop_install_argv(spec.id)
        job = self.jobs.active_for(f"app:{spec.id}")
        blocked = None
        if not installed and not allowed:
            blocked = INSTALLS_OFF_MESSAGE
        elif not installed and argv is None:
            blocked = f"The gateway's Python has neither uv nor pip, so the {spec.name} cannot be installed from here."
        # launch_blocked: not_installed | other_computer | admin | None.
        if not installed:
            launch_available, code, launch_blocked = False, "not_installed", f"The {spec.name} is not installed on the gateway's computer."
        elif not same_machine:
            launch_available, code, launch_blocked = False, "other_computer", f"The {spec.name} runs on the gateway's computer: open it there."
        elif not admin:
            launch_available, code, launch_blocked = False, "admin", "Only an admin can start apps."
        else:
            launch_available, code, launch_blocked = True, None, None
        actions: List[str] = []
        if not installed:
            if allowed and argv is not None:
                actions.append("install")
        else:
            actions.append("open")
        return {
            "id": spec.id,
            "name": spec.name,
            "kind": "desktop",
            "description": spec.description,
            "package": spec.package,
            "installed": installed,
            "version": pres.get("version"),
            "latest_version": None,
            "update_available": False,
            "running": running,
            "status": ("running" if running else "stopped") if installed else "not_installed",
            "managed": False,
            "source": pres.get("source") or None,
            "external": None,
            "enabled": False,
            "url": None,
            "port": None,
            "pid": pres.get("pid"),
            "restarts_last_minute": 0,
            "last_exit_code": None,
            "last_error": None,
            "needs_node_install": False,
            "install_available": (not installed) and allowed and argv is not None,
            "install_blocked_reason": blocked,
            "install_parts": ["desktop"],
            "actions": actions,
            "active_job": job.to_dict() if job is not None else None,
            "log_path": str(self.app_log_path(spec.id)),
            "content_summary": None,
            "interfaces": [],
            "desktop": {
                "location": pres.get("location"),
                "found_by": list(pres.get("found_by") or []),
                "launch_command": launch_command_text(pres.get("launch")),
                "install_command": launch_command_text(argv) if argv else f"pip install {spec.package}",
                "launch_available": launch_available,
                "launch_blocked": code,
                "launch_blocked_reason": launch_blocked,
            },
        }

    def start_desktop_install(self, app_id: str, *, run_inline: bool = False, same_machine: bool = False) -> Tuple[Job, bool]:
        """Install a desktop app's package into the gateway's own Python, as a
        job; every `abstract*` package already there is pinned to its
        installed version as a requirement of the same command, so the
        gateway itself never changes."""
        from .apps_desktop import DESKTOP_BY_ID, launch_command_text, pin_requirements

        spec = DESKTOP_BY_ID[str(app_id)]
        self._require_install_allowed(same_machine=same_machine)
        argv = self.desktop_install_argv(spec.id)
        if argv is None:
            raise AppsError(
                f"The gateway's Python has neither uv nor pip, so the {spec.name} cannot be installed from here.",
                hint=f"Install it with `pip install {spec.package}` in the gateway's Python environment.",
                extra={"command": f"pip install {spec.package}"},
            )

        def work(job: Job) -> Dict[str, Any]:
            job.same_machine = bool(same_machine)
            self._require_install_allowed(same_machine=job.same_machine)
            job.step("pins", "Keeping the gateway's own packages as they are…")
            pins = self.desktop_pins(self.desktop_python) or {}
            full = list(argv)
            if pins:
                job.log("keeping the gateway's own packages as they are: " + ", ".join(pin_requirements(pins)))
                full += pin_requirements(pins)
            job.step("install", f"Downloading and installing {spec.name}…")
            job.log("$ " + (launch_command_text(full) or ""))
            job.percent = max(job.percent, 5.0)
            tail: collections.deque = collections.deque(maxlen=60)

            def on_line(text: str) -> None:
                tail.append(text)
                job.log(text)
                t = text.strip()
                if t.startswith("Resolved"):
                    job.say("Resolving packages…", percent=20, log=False)
                elif t.startswith(("Downloading", "Collecting", "Downloaded")):
                    job.say("Downloading packages…", percent=45, log=False)
                elif t.startswith(("Prepared", "Installing collected")):
                    job.say("Installing packages…", percent=75, log=False)
                elif t.startswith(("Installed", "Successfully installed")):
                    job.say("Packages installed.", percent=92, log=False)

            env = dict(os.environ)
            env.setdefault("UV_NO_PROGRESS", "1")
            code = self.desktop_pip_runner(full, on_line=on_line, cancelled=job.cancel_event.is_set, env=env)
            job.check_cancel()
            if code != 0:
                last = next((ln for ln in reversed(tail) if ln.strip()), "")
                raise AppsError(
                    f"{spec.name} did not install" + (f": {last.strip()}" if last else "."),
                    hint="Show details has the whole installer output.",
                    details="\n".join(tail),
                )
            importlib.invalidate_caches()
            self._desktop_cache.pop(spec.id, None)
            job.step("check", f"Checking that the {spec.name} is there…")
            pres = self.desktop_presence(spec.id, refresh=True)
            if not pres.get("installed"):
                raise AppsError(
                    f"The installer finished, but the {spec.name} is not found next to the gateway's Python.",
                    details="\n".join(pres.get("found_by") or []) or None,
                )
            ver = pres.get("version") or ""
            return {"version": ver, "location": pres.get("location"), "message": f"{spec.name} {ver} is installed.".replace("  ", " ")}

        return self.jobs.start(kind="app_install", target=f"app:{spec.id}", app_id=spec.id, title=f"Install {spec.name}", work=work, run_inline=run_inline)

    def launch_desktop(self, app_id: str, *, same_machine: bool, principal: Any = None, gateway_url: Optional[str] = None) -> Dict[str, Any]:
        """Open the desktop app on the gateway computer's screen (a person at
        that computer only). A running Assistant is not started twice: the
        bundle is brought to the front (`open -a`), a script-started one is
        left as it is (its icon is in the menu bar).

        A NEW Assistant is started signed in: with `principal` it gets
        `--gateway-url <url> --gateway-handover-file <file>` (a one-time code
        in a 0600 file, mint_desktop_handover). A running one gets no code
        (it cannot receive one); the message says how to sign it in."""
        from .apps_desktop import DESKTOP_BY_ID, assistant_argv_with_handover, tail_text

        spec = DESKTOP_BY_ID[str(app_id)]
        if not same_machine:
            raise NotOnGatewayMachine(
                f"The {spec.name} runs on the gateway's computer: open it there.",
                hint=f"It is a desktop app for that computer's screen. On this computer, install {spec.package} and connect it to this gateway in its Settings.",
            )
        pres = self.desktop_presence(spec.id, refresh=True)
        if not pres.get("installed"):
            raise NotInstalled(f"The {spec.name} is not installed on the gateway's computer.", hint="Install it first (the Install button).")
        log = self.app_log_path(spec.id)
        running_argv = [str(a) for a in (pres.get("running_argv") or [])]
        bundle = pres.get("bundle")
        running_note = (
            f" If it is not signed in to this gateway, quit the {spec.name} and open it again from here: "
            "a newly opened one is signed in for you."
        )
        handover_file: Optional[Path] = None
        handover_code: Optional[str] = None
        if pres.get("running"):
            if not (bundle and running_argv and f"{bundle}/Contents/MacOS/" in running_argv[0]):
                return {"ok": True, "app": self.desktop_row(spec.id), "already_running": True, "signed_in_by_gateway": False,
                        "message": f"The {spec.name} is already running: its icon is in the menu bar." + running_note}
            argv: List[str] = ["open", "-a", str(bundle)]
        else:
            argv = list(pres.get("launch") or [])
            if principal is not None:
                url = self.resolve_gateway_url(gateway_url)
                handover_code, handover_file = self.mint_desktop_handover(principal, base_url=url)
                argv = assistant_argv_with_handover(argv, gateway_url=url, handover_file=str(handover_file))
        try:
            proc = self.desktop_spawner(argv, env=_scrubbed_child_env(dict(os.environ)), log_path=log)
        except OSError as exc:
            if handover_code:
                self.redeem_desktop_handover(handover_code)  # voids the code, removes the file
            raise LaunchFailed(f"The {spec.name} could not start: {argv[0]}: {exc}") from exc
        code = self.desktop_wait(proc)
        if code is not None:
            if handover_code:
                self.redeem_desktop_handover(handover_code)
            raise LaunchFailed(f"The {spec.name} did not start (exit code {code}).", hint=f"Its log: {log}", details=tail_text(log) or None)
        self._desktop_cache.pop(spec.id, None)
        if pres.get("running"):
            message = f"The {spec.name} is in front." + running_note
        elif handover_file is not None:
            message = f"The {spec.name} is starting, signed in to this gateway: its icon appears in the menu bar."
        else:
            message = f"The {spec.name} is starting: its icon appears in the menu bar."
        return {"ok": True, "app": self.desktop_row(spec.id), "already_running": bool(pres.get("running")),
                "signed_in_by_gateway": handover_file is not None, "message": message}

    # -- views ---------------------------------------------------------------------------
    def _external_row(
        self,
        spec: AppSpec,
        ext: ExternalApp,
        *,
        managed_version: Optional[str],
        latest: Optional[str],
        caller: Optional[Dict[str, Any]],
        tui_release: Optional[Dict[str, Any]],
        tui_release_error: Optional[str],
    ) -> Dict[str, Any]:
        """A running app the gateway did not start: open it, nothing else (no
        stop, update or log: its process belongs to whoever started it)."""
        job = self.jobs.active_for(f"app:{spec.id}")
        row: Dict[str, Any] = {
            "id": spec.id,
            "name": spec.name,
            "kind": "web",
            "description": spec.description,
            "package": spec.package,
            "installed": True,
            "version": ext.version,
            "latest_version": latest,
            "update_available": False,
            "running": True,
            "status": "running",
            "managed": False,
            "source": "external",
            "external": {
                "port": ext.port,
                "pid": ext.pid,
                "version": ext.version,
                "gateway_url": ext.gateway_url,
                "detail": f"Started outside the gateway on port {ext.port}",
            },
            "managed_version": managed_version,
            "enabled": bool(self.app_state(spec.id).get("enabled")),
            "url": ext.url,
            "port": ext.port,
            "pid": ext.pid,
            "restarts_last_minute": 0,
            "last_exit_code": None,
            "last_error": None,
            "needs_node_install": False,
            "install_available": False,
            "install_blocked_reason": None,
            "install_parts": ["web"],
            "actions": ["open"],
            "active_job": job.to_dict() if job is not None else None,
            "log_path": None,
            # Counted on THIS gateway: unknown when the app's page says it
            # talks to another one.
            "content_summary": self.content_summary(spec, app_gateway_url=ext.gateway_url),
        }
        interfaces = [self.web_interface(row, spec)]
        tui = TUI_BY_APP.get(spec.id)
        if tui is not None:
            interfaces.append(self.tui_interface(tui, release=tui_release, release_error=tui_release_error, caller=caller))
        row["interfaces"] = interfaces
        return row

    def app_row(
        self,
        spec: AppSpec,
        *,
        latest: Optional[str] = None,
        registry_error: Optional[str] = None,
        node: Optional[Dict[str, Any]] = None,
        caller: Optional[Dict[str, Any]] = None,
        tui_release: Optional[Dict[str, Any]] = None,
        tui_release_error: Optional[str] = None,
    ) -> Dict[str, Any]:
        st = self.app_state(spec.id)
        installed = self.installed_version(spec.id)
        p = self._procs.get(spec.id)
        snap = p.snapshot() if p is not None else {"status": "stopped", "running": False, "pid": None, "port": None, "version": None, "started_at": None, "restarts_last_minute": 0, "last_exit_code": None, "last_error": None}
        running = bool(snap["running"])
        if not running:
            ext = self.external_apps().get(spec.id)
            if ext is not None:
                return self._external_row(spec, ext, managed_version=installed, latest=latest, caller=caller, tui_release=tui_release, tui_release_error=tui_release_error)
        port = snap["port"] if running else st.get("port")
        url = f"http://{_connect_host(self.bind_host)}:{port}/" if running and port else None
        allowed = self.install_allowed(same_machine=bool((caller or {}).get("same_machine")))
        node = node if node is not None else self.node_status()
        update_available = bool(installed and latest and version_newer(latest, installed))
        actions: List[str] = []
        if not installed:
            if allowed and not registry_error:
                actions.append("install")
        else:
            if running:
                actions += ["open", "stop"]
            elif snap["status"] != "starting":
                actions.append("launch")
            if update_available and allowed:
                actions.append("update")
            actions.append("logs")
        blocked = None
        if not installed and not allowed:
            blocked = INSTALLS_OFF_MESSAGE
        elif not installed and registry_error:
            blocked = f"The npm registry is not reachable: {registry_error}"
        job = self.jobs.active_for(f"app:{spec.id}")
        row = {
            "id": spec.id,
            "name": spec.name,
            "kind": "web",
            "description": spec.description,
            "package": spec.package,
            "installed": bool(installed),
            "version": installed,
            "latest_version": latest,
            "update_available": update_available,
            "running": running,
            "status": snap["status"] if (installed or running) else "not_installed",
            "managed": bool(installed),
            "source": "gateway" if installed else None,
            "external": None,
            "enabled": bool(st.get("enabled")),
            "url": url,
            "port": port,
            "pid": snap["pid"],
            "restarts_last_minute": snap["restarts_last_minute"],
            "last_exit_code": snap["last_exit_code"],
            "last_error": snap["last_error"],
            "needs_node_install": not bool(node.get("available")),
            "install_available": (not installed) and allowed and not registry_error,
            "install_blocked_reason": blocked,
            # What the Install button installs (mission LL): "web", plus "tui"
            # when the app's terminal version comes with it on this computer.
            "install_parts": ["web", "tui"] if (not installed and self.install_includes_terminal(spec.id) is not None) else ["web"],
            "actions": actions,
            "active_job": job.to_dict() if job is not None else None,
            "log_path": str(self.app_log_path(spec.id)),
            # Entity: {"entities_count": n | None}; None for the other apps.
            "content_summary": self.content_summary(spec) if installed else None,
        }
        # Every way this app runs (mission Y): the browser app above, and a
        # terminal app for the apps that have one (Code). docs/apps.md.
        interfaces = [self.web_interface(row, spec)]
        tui = TUI_BY_APP.get(spec.id)
        if tui is not None:
            interfaces.append(self.tui_interface(tui, release=tui_release, release_error=tui_release_error, caller=caller))
        row["interfaces"] = interfaces
        return row

    def overview(self, *, check_latest: bool = True, caller: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        node = self.node_status()
        latest: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        releases: Dict[str, Tuple[Optional[Dict[str, Any]], Optional[str]]] = {}

        def _release(tui: TuiSpec) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
            try:
                return self.tui_release(tui), None
            except AppsError as exc:
                return None, exc.message

        if check_latest:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(APPS) + len(TUI_BY_APP)) as ex:
                futs = {spec.id: ex.submit(self.latest_version, spec) for spec in APPS}
                rfuts = {app_id: ex.submit(_release, tui) for app_id, tui in TUI_BY_APP.items()}
                for app_id, fut in futs.items():
                    try:
                        latest[app_id] = fut.result(timeout=REGISTRY_TIMEOUT_S + 2)
                    except Exception as exc:  # noqa: BLE001
                        latest[app_id] = (None, f"{type(exc).__name__}: {exc}")
                for app_id, fut in rfuts.items():
                    try:
                        releases[app_id] = fut.result(timeout=REGISTRY_TIMEOUT_S + 2)
                    except Exception as exc:  # noqa: BLE001
                        releases[app_id] = (None, f"{type(exc).__name__}: {exc}")
        errors = [e for (_, e) in latest.values() if e]
        reachable: Optional[bool] = None if not check_latest else (not errors or len(errors) < len(APPS))
        rows = [
            self.app_row(
                spec,
                latest=(latest.get(spec.id) or (None, None))[0],
                registry_error=(latest.get(spec.id) or (None, None))[1],
                node=node,
                caller=caller,
                tui_release=(releases.get(spec.id) or (None, None))[0],
                tui_release_error=(releases.get(spec.id) or (None, None))[1],
            )
            for spec in APPS
        ]
        # Desktop apps (mission LL): after the five browser apps, stack order.
        from .apps_desktop import DESKTOP_APPS

        rows += [self.desktop_row(d.id, caller=caller) for d in DESKTOP_APPS]
        runtime_job = self.jobs.active_for("runtime:node")
        node_public = {k: node.get(k) for k in ("available", "version", "source", "install_available", "message", "managed_version", "problems", "path")}
        node_public["active_job"] = runtime_job.to_dict() if runtime_job else None
        same_machine = bool((caller or {}).get("same_machine"))
        node_public["install_available"] = (not node.get("available")) and str(node.get("mode") or "auto") != "system" and self.install_allowed(same_machine=same_machine)
        return {
            "ok": True,
            "runtime": {"node": node_public},
            "apps": rows,
            "install_allowed": self.install_allowed(same_machine=same_machine),
            "registry": {"url": self.registry_url, "reachable": reachable, "error": errors[0] if errors else None},
            "gateway_url": self.resolve_gateway_url(),
            "console_tui": self.console_tui_interface(caller=caller),
            "apps_host": self.bind_host,
            "data": {"apps_dir": str(self.apps_root), "node_dir": str(self.node_root), "logs_dir": str(self.data_dir / "logs" / "apps")},
        }


def resolve_from_metadata(meta: Dict[str, Any], spec: AppSpec, version: Optional[str] = None) -> Dict[str, Any]:
    versions = meta.get("versions") or {}
    ver = str(version or (meta.get("dist-tags") or {}).get("latest") or "")
    if not ver or ver not in versions:
        known = ", ".join(sorted(versions, key=lambda v: parse_version(v) or (0,))[-5:])
        raise AppsError(f"{spec.package} has no version '{ver or '(latest)'}' on the npm registry (recent: {known or 'none'}).")
    v = versions[ver] or {}
    dist = v.get("dist") or {}
    tarball = str(dist.get("tarball") or "")
    integrity = str(dist.get("integrity") or "")
    if not tarball or not integrity:
        raise AppsError(f"{spec.package}@{ver} has no tarball or integrity hash in the registry metadata.")
    expected_prefix = f"/{spec.package}/-/{spec.short_name}-{ver}.tgz"
    if not urllib.parse.urlparse(tarball).path.endswith(expected_prefix):
        raise AppsError(f"{spec.package}@{ver}: unexpected tarball URL {tarball}.")
    return {
        "version": ver,
        "tarball": tarball,
        "integrity": integrity,
        "size": dist.get("size") or None,
        "unpacked_size": dist.get("unpackedSize"),
        "dependencies": sorted((v.get("dependencies") or {}).keys()),
    }


def _mb(n: Optional[int]) -> str:
    if not n:
        return "? MB"
    return f"{n / 1_000_000:.1f} MB"


def _extract_npm_tarball(tgz: Path, dest: Path) -> None:
    """Unpack an npm tarball's `package/` folder into `dest` (no links, no
    paths outside dest)."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tgz, "r:gz") as tf:
        for m in tf.getmembers():
            name = m.name
            if not name.startswith("package/"):
                continue
            rel = name[len("package/"):]
            if not rel:
                continue
            if m.issym() or m.islnk() or m.isdev():
                raise AppsError(f"Refusing a link or device entry in the package: {name}")
            target = _safe_join(dest, rel)
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not m.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            mode = (m.mode or 0o644) & 0o755
            os.chmod(target, mode | 0o600)


def _extract_node_wheel(wheel: Path, dest: Path) -> Tuple[str, Optional[str]]:
    """Unpack the Node.js runtime from a nodejs-wheel-binaries wheel.
    Returns (node binary, npm-cli.js) relative to dest."""
    dest.mkdir(parents=True, exist_ok=True)
    node_rel: Optional[str] = None
    npm_rel: Optional[str] = None
    with zipfile.ZipFile(wheel) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name.startswith("nodejs_wheel/") or name.endswith("/"):
                continue
            rel = name[len("nodejs_wheel/"):]
            if rel.startswith(("include/", "share/")) or rel.endswith(".py") or rel == "py.typed":
                continue
            target = _safe_join(dest, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            mode = (info.external_attr >> 16) & 0o777
            if mode:
                os.chmod(target, mode)
            if rel in ("bin/node", "node.exe", "bin/node.exe"):
                node_rel = rel
                if not sys.platform.startswith("win"):
                    os.chmod(target, 0o755)
            if rel.endswith("node_modules/npm/bin/npm-cli.js"):
                npm_rel = rel
    if node_rel is None:
        raise AppsError(f"The Node.js wheel {wheel.name} contains no node binary.")
    return node_rel, npm_rel


# ---------------------------------------------------------------------------
# Process-wide manager
# ---------------------------------------------------------------------------

_MANAGERS: Dict[str, AppsManager] = {}
_MANAGERS_LOCK = threading.Lock()


def _default_install_allowed(data_dir: Path) -> Callable[..., bool]:
    def _fn(*, same_machine: bool = False) -> bool:
        from .runtime_config import resolve_allow_engine_install

        return bool(resolve_allow_engine_install(data_dir, caller_on_this_machine=same_machine))

    return _fn


def get_apps_manager(data_dir: Optional[Path] = None) -> AppsManager:
    if data_dir is None:
        from .users import gateway_data_dir_from_env

        data_dir = gateway_data_dir_from_env()
    key = str(Path(data_dir).resolve())
    with _MANAGERS_LOCK:
        m = _MANAGERS.get(key)
        if m is None:
            m = AppsManager(Path(key), install_allowed=_default_install_allowed(Path(key)))
            _MANAGERS[key] = m
        return m


def start_apps_on_boot() -> None:
    """Lifespan hook: start the enabled apps on a background thread (never
    raises, never delays the listener)."""
    def _run() -> None:
        try:
            outcomes = get_apps_manager().autostart()
            for o in outcomes:
                line = f"Browser app {o['app_id']}: " + (f"started at {o['url']}" if o["ok"] else f"did not start: {o['message']}")
                print(line, file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001
            logger.warning("starting the enabled browser apps failed", exc_info=True)

    threading.Thread(target=_run, name="apps-autostart", daemon=True).start()


def stop_apps_on_shutdown() -> None:
    with _MANAGERS_LOCK:
        managers = list(_MANAGERS.values())
    for m in managers:
        try:
            m.stop_all()
        except Exception:  # noqa: BLE001
            logger.warning("stopping browser apps failed", exc_info=True)
