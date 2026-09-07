"""Self-update: know how this gateway was installed, check PyPI, upgrade.

Honesty first. The tray's "Check for updates" must never guess: an editable
checkout, a Docker image, a conda env or an unknown launcher answer
``upgradable: false`` with the reason and the command a human would run
instead. Only installs this module can reproduce exactly (pip / uv venv /
pipx / uv tool) get the one-click path, and that path keeps the install
profile (the ``apple`` / ``gpu`` / ``embeddings`` extras) the user chose.

Offline is a first-class outcome, not an error: the check answers
``offline: true`` with a plain message and the gateway keeps working.

Nothing here restarts the process — a finished upgrade sets
``restart_recommended`` and the caller (tray / console) offers the restart.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

DIST_NAME = "abstractgateway"
PYPI_JSON_URL = f"https://pypi.org/pypi/{DIST_NAME}/json"
CHECK_CACHE_TTL_S = 3600.0
CHECK_TIMEOUT_S = 5.0
LOG_TAIL_LINES = 200
KNOWN_EXTRAS = ("apple", "gpu", "embeddings")


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------


def installed_version() -> str:
    try:
        from importlib.metadata import version

        return str(version(DIST_NAME))
    except Exception:
        try:
            from . import __version__

            return str(__version__)
        except Exception:
            return "unknown"


_VERSION_RE = re.compile(r"^\s*v?(\d+(?:\.\d+)*)((?:a|b|rc|\.dev|\.post)\d*)?\s*$", re.IGNORECASE)


def _version_key(text: str) -> Optional[tuple]:
    """Fallback ordering when `packaging` is unavailable: numeric tuple plus a
    pre-release rank (final > rc > b > a; dev lowest; post highest)."""
    m = _VERSION_RE.match(str(text or ""))
    if not m:
        return None
    nums = tuple(int(x) for x in m.group(1).split("."))
    tag = (m.group(2) or "").lower()
    rank = 3
    if tag.startswith("a"):
        rank = 0
    elif tag.startswith("b"):
        rank = 1
    elif tag.startswith("rc"):
        rank = 2
    elif tag.startswith(".dev"):
        rank = -1
    elif tag.startswith(".post"):
        rank = 4
    return nums + (99,) * (6 - len(nums)) + (rank,)


def is_newer(candidate: str, current: str) -> Optional[bool]:
    """True when `candidate` is strictly newer than `current`; None when either
    is unparsable (the caller must then say "unknown", never "up to date")."""
    try:
        from packaging.version import Version

        return Version(str(candidate)) > Version(str(current))
    except Exception:
        pass
    a, b = _version_key(candidate), _version_key(current)
    if a is None or b is None:
        return None
    return a > b


# ---------------------------------------------------------------------------
# Install detection
# ---------------------------------------------------------------------------


@dataclass
class InstallInfo:
    kind: str  # pip | uv-venv | pipx | uv-tool | editable | docker | conda | unknown
    upgradable: bool
    reason: Optional[str]
    command: Optional[List[str]]
    display_command: Optional[str]
    python: str
    prefix: str
    extras: List[str] = field(default_factory=list)
    version: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "upgradable": bool(self.upgradable),
            "reason": self.reason,
            "command": list(self.command) if self.command else None,
            "display_command": self.display_command,
            "python": self.python,
            "prefix": self.prefix,
            "extras": list(self.extras),
            "version": self.version,
        }


def _direct_url_info() -> Optional[Dict[str, Any]]:
    try:
        from importlib.metadata import distribution

        raw = distribution(DIST_NAME).read_text("direct_url.json")
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _requirement_parts(text: str) -> tuple[str, set[str], Any]:
    """(name, extras, marker) for one requirement string; marker may be None."""
    try:
        from packaging.requirements import Requirement

        req = Requirement(str(text))
        return req.name.lower().replace("_", "-"), {e.lower() for e in req.extras}, req.marker
    except Exception:
        head = str(text).split(";", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9._-]+)(?:\[([^\]]*)\])?", head)
        name = (m.group(1) if m else head).lower().replace("_", "-")
        extras = {e.strip().lower() for e in (m.group(2) or "").split(",") if e.strip()} if m else set()
        marker_text = str(text).split(";", 1)[1] if ";" in str(text) else ""
        return name, extras, marker_text or None


def _marker_selects_extra(marker: Any, extra: str) -> bool:
    if marker is None:
        return False
    if isinstance(marker, str):
        return re.search(rf"extra\s*==\s*['\"]{re.escape(extra)}['\"]", marker) is not None
    try:
        return bool(marker.evaluate({"extra": extra}))
    except Exception:
        return False


def _dist_installed(name: str) -> bool:
    try:
        from importlib.metadata import distribution

        distribution(name)
        return True
    except Exception:
        return False


def extra_installed(dist_name: str, extra: str, *, _seen: Optional[set] = None) -> bool:
    """True when every leaf distribution the extra pulls in (recursively
    through nested `dep[extra]` requirements) is present. An extra whose
    requirement list is empty or unreadable is reported as NOT installed —
    the upgrade would then omit it, which is the conservative answer."""
    seen = _seen if _seen is not None else set()
    key = (dist_name.lower(), extra.lower())
    if key in seen:
        return True
    seen.add(key)
    try:
        from importlib.metadata import requires

        reqs = requires(dist_name) or []
    except Exception:
        return False
    selected = []
    for text in reqs:
        name, extras, marker = _requirement_parts(text)
        if _marker_selects_extra(marker, extra):
            selected.append((name, extras))
    if not selected:
        return False
    for name, extras in selected:
        if not _dist_installed(name):
            return False
        for nested in extras:
            if not extra_installed(name, nested, _seen=seen):
                return False
    return True


def installed_extras(*, platform: str = sys.platform) -> List[str]:
    """The install profile to carry through an upgrade. Platform-gated: `gpu`
    is a CUDA profile and `apple` an MLX one — CPU torch wheels on a Mac must
    not turn into `[apple,gpu]` and a CUDA-only resolve failure."""
    out: List[str] = []
    for extra in KNOWN_EXTRAS:
        if extra == "gpu" and platform == "darwin":
            continue
        if extra == "apple" and platform != "darwin":
            continue
        if extra_installed(DIST_NAME, extra):
            out.append(extra)
    return out


def _externally_managed(prefix: str, base_prefix: str) -> bool:
    """PEP 668: a distro/Homebrew system Python refuses pip installs."""
    if prefix != base_prefix:
        return False  # a virtual environment is never externally managed
    try:
        import sysconfig

        stdlib = sysconfig.get_path("stdlib")
        return bool(stdlib) and Path(stdlib, "EXTERNALLY-MANAGED").exists()
    except Exception:
        return False


def _site_packages_writable() -> Optional[bool]:
    try:
        import sysconfig

        purelib = sysconfig.get_path("purelib")
        if not purelib:
            return None
        return os.access(purelib, os.W_OK)
    except Exception:
        return None


def _dist_installer() -> Optional[str]:
    """The INSTALLER file pip/uv leave in the dist-info ("pip", "uv", ...)."""
    try:
        from importlib.metadata import distribution

        raw = distribution(DIST_NAME).read_text("INSTALLER")
        return str(raw).strip().lower() or None
    except Exception:
        return None


def _in_docker() -> bool:
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as fh:
            return "docker" in fh.read() or "containerd" in fh.read()
    except Exception:
        return False


def _pyvenv_cfg_text(prefix: Path) -> str:
    for candidate in (prefix / "pyvenv.cfg", prefix.parent / "pyvenv.cfg"):
        try:
            return candidate.read_text(encoding="utf-8")
        except Exception:
            continue
    return ""


def detect_install(*, executable: Optional[str] = None, prefix: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> InstallInfo:
    """Classify the running install. Pure over its inputs where possible so
    tests can pin every branch with fake prefixes and env."""
    exe = str(executable or sys.executable)
    pfx = str(prefix or sys.prefix)
    environ = os.environ if env is None else env
    extras = installed_extras()
    base_prefix = str(getattr(sys, "base_prefix", pfx)) if prefix is None else pfx + "-not-base"
    version = installed_version()
    spec = DIST_NAME + (f"[{','.join(extras)}]" if extras else "")
    lower = pfx.replace("\\", "/").lower()

    def info(kind: str, *, upgradable: bool, reason: Optional[str], command: Optional[List[str]], display: Optional[str]) -> InstallInfo:
        return InstallInfo(kind=kind, upgradable=upgradable, reason=reason, command=command, display_command=display, python=exe, prefix=pfx, extras=extras, version=version)

    direct = _direct_url_info()
    if isinstance(direct, dict) and isinstance(direct.get("dir_info"), dict) and direct["dir_info"].get("editable"):
        return info(
            "editable",
            upgradable=False,
            reason="installed from a source checkout (editable install): update it with `git pull` and reinstall",
            command=None,
            display=None,
        )
    if isinstance(direct, dict) and str(direct.get("url") or "").startswith("file:"):
        return info(
            "local-file",
            upgradable=False,
            reason="installed from a local file, not from PyPI: reinstall from the newer file",
            command=None,
            display=None,
        )
    if _in_docker() or environ.get("ABSTRACTGATEWAY_CONTAINER"):
        return info(
            "docker",
            upgradable=False,
            reason="running in a container: pull the newer image instead of upgrading in place",
            command=None,
            display=None,
        )
    conda_env = bool(environ.get("CONDA_PREFIX")) and lower.startswith(str(environ.get("CONDA_PREFIX") or "").replace("\\", "/").lower())
    if conda_env or Path(pfx, "conda-meta").is_dir():
        cmd = [exe, "-m", "pip", "install", "--upgrade", spec]
        return info("conda", upgradable=_dist_installed("pip"), reason=None if _dist_installed("pip") else "pip is not available in this conda environment", command=cmd, display=" ".join(cmd))
    if _externally_managed(pfx, base_prefix):
        return info(
            "system-python",
            upgradable=False,
            reason="this is the operating system's Python (externally managed): install AbstractGateway with pipx or in a virtual environment to get one-click updates",
            command=None,
            display=f"pipx upgrade {DIST_NAME}",
        )
    pipx_marker = Path(pfx, "pipx_metadata.json").exists()
    if pipx_marker or "/pipx/venvs/" in lower or ("/pipx/" in lower and "/venvs/" in lower):
        pipx = shutil.which("pipx")
        if pipx:
            cmd = [pipx, "upgrade", DIST_NAME]
            return info("pipx", upgradable=True, reason=None, command=cmd, display=" ".join(cmd))
        return info("pipx", upgradable=False, reason="installed with pipx but the `pipx` command is not on PATH", command=None, display=f"pipx upgrade {DIST_NAME}")
    uv_tool_marker = Path(pfx, "uv-receipt.toml").exists() or Path(pfx).parent.joinpath("uv-receipt.toml").exists()
    if uv_tool_marker or "/uv/tools/" in lower:
        uv = shutil.which("uv")
        if uv:
            cmd = [uv, "tool", "upgrade", DIST_NAME]
            return info("uv-tool", upgradable=True, reason=None, command=cmd, display=" ".join(cmd))
        return info("uv-tool", upgradable=False, reason="installed with `uv tool` but the `uv` command is not on PATH", command=None, display=f"uv tool upgrade {DIST_NAME}")
    writable = _site_packages_writable()
    if writable is False:
        return info(
            "read-only",
            upgradable=False,
            reason="the Python environment is not writable by this user; run the upgrade as the user who installed it",
            command=None,
            display=f"pip install --upgrade {spec}",
        )
    cfg = _pyvenv_cfg_text(Path(pfx))
    if (cfg and re.search(r"^uv\s*=", cfg, re.MULTILINE)) or _dist_installer() == "uv":
        uv = shutil.which("uv")
        if uv:
            cmd = [uv, "pip", "install", "--python", exe, "--upgrade", spec]
            return info("uv-venv", upgradable=True, reason=None, command=cmd, display=" ".join(cmd))
        if _dist_installed("pip"):
            cmd = [exe, "-m", "pip", "install", "--upgrade", spec]
            return info("uv-venv", upgradable=True, reason=None, command=cmd, display=" ".join(cmd))
        return info("uv-venv", upgradable=False, reason="a uv-managed environment without `uv` on PATH and without pip", command=None, display=f"uv pip install --upgrade {spec}")
    if _dist_installed("pip"):
        cmd = [exe, "-m", "pip", "install", "--upgrade", spec]
        return info("pip", upgradable=True, reason=None, command=cmd, display=" ".join(cmd))
    return info(
        "unknown",
        upgradable=False,
        reason="could not identify how this gateway was installed (no pip in this environment)",
        command=None,
        display=f"pip install --upgrade {spec}",
    )


# ---------------------------------------------------------------------------
# Update check (PyPI)
# ---------------------------------------------------------------------------

_check_lock = threading.Lock()
_last_check: Optional[Dict[str, Any]] = None
_last_check_at: float = 0.0


def _fetch_pypi_latest(timeout_s: float = CHECK_TIMEOUT_S) -> Dict[str, Any]:
    """One network call; every failure is an in-band {offline|error} payload."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(PYPI_JSON_URL, headers={"Accept": "application/json", "User-Agent": f"{DIST_NAME}/{installed_version()}"})
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:  # noqa: S310 - fixed https URL
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "offline": False, "error": f"PyPI answered HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - DNS, refused, timeout, TLS: all read as offline
        return {"ok": False, "offline": True, "error": f"could not reach PyPI ({type(exc).__name__}: {exc})"}
    latest = None
    if isinstance(payload, dict) and isinstance(payload.get("info"), dict):
        latest = payload["info"].get("version")
    if not isinstance(latest, str) or not latest.strip():
        return {"ok": False, "offline": False, "error": "PyPI answered without a version"}
    return {"ok": True, "offline": False, "latest": latest.strip()}


def check_for_update(*, force: bool = False, fetch: Any = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Cached (1 h) update check. `fetch` is injectable for tests."""
    global _last_check, _last_check_at
    clock = float(now if now is not None else time.time())
    with _check_lock:
        if not force and _last_check is not None and (clock - _last_check_at) < CHECK_CACHE_TTL_S:
            return dict(_last_check)
    current = installed_version()
    result = (fetch or _fetch_pypi_latest)()
    out: Dict[str, Any] = {
        "current": current,
        "checked_at": _utc_now_iso(),
        "source": "pypi",
        "offline": bool(result.get("offline")),
    }
    if result.get("ok"):
        latest = str(result.get("latest"))
        newer = is_newer(latest, current)
        out.update({"latest": latest, "update_available": newer, "error": None})
    else:
        out.update({"latest": None, "update_available": None, "error": str(result.get("error") or "check failed")})
    with _check_lock:
        _last_check = dict(out)
        _last_check_at = clock
    return out


def last_check() -> Optional[Dict[str, Any]]:
    with _check_lock:
        return dict(_last_check) if _last_check is not None else None


# ---------------------------------------------------------------------------
# Upgrade job (one at a time, background thread, bounded log)
# ---------------------------------------------------------------------------


class UpdateJobBusy(RuntimeError):
    pass


class UpdateNotPossible(RuntimeError):
    pass


JOB_TIMEOUT_S = 30 * 60.0
LOG_LINE_MAX_CHARS = 500
# True after an in-place upgrade succeeded in THIS process: the code on disk
# is newer than the code running, and every not-yet-imported module now
# resolves to the new version. Surfaced on /host/update and /api/health.
_restart_pending = False


@dataclass
class _Job:
    state: str = "idle"  # idle | running | succeeded | succeeded_no_change | failed
    proc: Any = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    command: Optional[List[str]] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    log: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_TAIL_LINES))
    restart_recommended: bool = False
    version_before: Optional[str] = None
    version_after: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "command": list(self.command) if self.command else None,
            "exit_code": self.exit_code,
            "error": self.error,
            "log_tail": list(self.log),
            "restart_recommended": bool(self.restart_recommended),
            "version_before": self.version_before,
            "version_after": self.version_after,
        }


_job_lock = threading.Lock()
_job = _Job()
_job_thread: Optional[threading.Thread] = None


def job_status() -> Dict[str, Any]:
    with _job_lock:
        return _job.as_dict()


def job_running() -> bool:
    with _job_lock:
        return _job.state == "running"


def restart_pending() -> bool:
    return bool(_restart_pending)


def _install_host_control_probe() -> None:
    try:
        from . import host_control

        host_control.set_update_job_probe(job_running)
    except Exception:
        pass


_install_host_control_probe()


def _installed_version_fresh(python: str) -> Optional[str]:
    """Ask a FRESH interpreter (this process's importlib cache is stale after
    an in-place upgrade) which version is now on disk."""
    try:
        out = subprocess.run(
            [python, "-c", f"import importlib.metadata as m; print(m.version('{DIST_NAME}'))"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        text = (out.stdout or "").strip()
        return text or None
    except Exception:
        return None


def start_update(*, info: Optional[InstallInfo] = None, runner: Any = None) -> Dict[str, Any]:
    """Launch the upgrade command in the background. `runner(command, on_line)
    -> exit_code` is injectable for tests."""
    global _job, _job_thread
    inst = info or detect_install()
    if not inst.upgradable or not inst.command:
        raise UpdateNotPossible(inst.reason or "this install cannot be upgraded automatically")
    try:
        from . import host_control

        if host_control.restart_requested() or host_control.shutdown_requested():
            raise UpdateJobBusy("the gateway is restarting; try again once it is back")
    except UpdateJobBusy:
        raise
    except Exception:
        pass
    with _job_lock:
        if _job.state == "running":
            raise UpdateJobBusy("an update is already running")
        _job = _Job(state="running", started_at=_utc_now_iso(), command=list(inst.command), version_before=inst.version)
        job = _job

    def _default_runner(command: List[str], on_line: Any) -> int:
        env = dict(os.environ)
        # Subprocess-only hygiene (not gateway knobs): never block on a
        # prompt (private index credentials), never spam a progress bar into
        # the log, never let pip nag about itself.
        env.setdefault("PIP_NO_INPUT", "1")
        env.setdefault("PIP_PROGRESS_BAR", "off")
        env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
        env.setdefault("UV_NO_PROGRESS", "1")
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        with _job_lock:
            job.proc = proc
        assert proc.stdout is not None
        deadline = time.monotonic() + JOB_TIMEOUT_S
        for line in proc.stdout:
            on_line(line.rstrip("\n")[:LOG_LINE_MAX_CHARS])
            if time.monotonic() > deadline:
                try:
                    proc.kill()
                except Exception:
                    pass
                on_line(f"[gateway] update timed out after {int(JOB_TIMEOUT_S // 60)} minutes; command killed")
                break
        return int(proc.wait())

    def _work() -> None:
        def on_line(line: str) -> None:
            with _job_lock:
                job.log.append(str(line))

        try:
            code = int((runner or _default_runner)(list(inst.command or []), on_line))
        except Exception as exc:  # noqa: BLE001
            with _job_lock:
                job.state = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished_at = _utc_now_iso()
            return
        after = _installed_version_fresh(inst.python) if code == 0 else None
        with _job_lock:
            job.exit_code = code
            job.finished_at = _utc_now_iso()
            job.version_after = after
            job.proc = None
            if code == 0 and after and job.version_before and after == job.version_before:
                # pip said "already satisfied": the index this environment
                # uses does not carry the newer release (yet). Never offer a
                # restart that would loop back to the same offer.
                job.state = "succeeded_no_change"
                job.error = f"nothing changed: the package index used by this environment still serves {after}"
            elif code == 0:
                job.state = "succeeded"
                job.restart_recommended = True
                global _restart_pending
                _restart_pending = True
            else:
                job.state = "failed"
                job.error = f"the update command exited with code {code}"

    _job_thread = threading.Thread(target=_work, name="gateway-self-update", daemon=True)
    _job_thread.start()
    return job_status()


def update_overview(*, check: bool = False) -> Dict[str, Any]:
    """The one payload the tray/console render: install facts, last (or a
    fresh) check, and the job state."""
    inst = detect_install()
    checked = check_for_update() if check else last_check()
    return {
        "ok": True,
        "current": inst.version,
        "install": inst.as_dict(),
        "check": checked,
        "job": job_status(),
        "restart_pending": restart_pending(),
    }


def _reset_for_tests() -> None:
    global _last_check, _last_check_at, _job, _job_thread, _restart_pending
    with _check_lock:
        _last_check = None
        _last_check_at = 0.0
    with _job_lock:
        _job = _Job()
    _job_thread = None
    _restart_pending = False
