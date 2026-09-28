"""Self-update: know how this gateway was installed, check for the newer release, upgrade.

Two paths, one per way of installing:

- **AbstractFramework installer installs** (``kind: installer``: a uv tool whose data dir
  holds the installer's ``bootstrap.env``). The update IS the installer: the same
  ``scripts/install.sh`` the one-line install runs (``curl .../main/scripts/install.sh | sh``),
  so the tray, the web console, the terminal console and a re-run of the line do one thing.
  The check resolves the framework repo's ``main`` to a commit and reads, from that one
  snapshot, the install manifest (``framework.version``, ``bootstrap.gateway_version``) and
  the installer script. It compares AbstractFramework RELEASES: the one ``bootstrap.env``
  records against the manifest's (an install that recorded none, made with ``--pin`` or
  before releases were recorded, compares its gateway with the release's gateway pin). The
  manifest, not ``install.sh --print-versions``: it is data (a check never executes a
  downloaded script) and the root repo's inventory test keeps it equal to the script's pins.
  The admin sees the ``main`` URL, the commit, the script's sha256 and the exact command
  before confirming; the run executes that exact file (``installer_sha256`` guards against a
  newer check in between) with ``--yes --no-start --no-open --no-modify-path --data-dir
  <this data dir>``: nothing is asked, start at login stays as it was, and the running
  gateway is never stopped; the restart is offered afterwards. Windows keeps a running
  program's files locked, so there the update is the PowerShell line, shown, not run. The
  newest gateway outside a release stays a command-line choice (``--pin latest``).
- **Package installs** (pip / uv venv / pipx / uv tool without the installer): the package
  manager's upgrade, keeping the install profile (the ``apple`` / ``gpu`` / ``embeddings``
  extras), compared with PyPI's newest gateway. A uv tool pinned to an exact version is not
  upgradable in place (uv keeps the pin); the reason says how to reinstall.

Honesty first: an editable checkout, a Docker image, a conda env or an unknown launcher
answer ``upgradable: false`` with the reason and the command a human would run instead.
Offline is a first-class outcome, not an error. ``update_overview()["update"]`` is the ONE
rendering every client shows (the version line, the hint, the action and its confirmation
text), so the tray and both consoles say the same thing.

Nothing here restarts the process — a finished upgrade sets ``restart_recommended`` and the
caller (tray / console) offers the restart.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import re
import shlex
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

# AbstractFramework installer installs: the release is what the one-line install runs, the
# framework repo's `main` (resolved to a commit, so the manifest and the script are one snapshot).
FRAMEWORK_NAME = "AbstractFramework"
FRAMEWORK_REPO = "lpalbou/AbstractFramework"
FRAMEWORK_REF = "main"
FRAMEWORK_COMMIT_API = f"https://api.github.com/repos/{FRAMEWORK_REPO}/commits/{FRAMEWORK_REF}"
FRAMEWORK_RAW = f"https://raw.githubusercontent.com/{FRAMEWORK_REPO}"
FRAMEWORK_MANIFEST_PATH = "docs/installers/install-manifest.json"
INSTALLER_SCRIPT_PATH = "scripts/install.sh"
INSTALLER_STATE_FILE = "bootstrap.env"
INSTALLER_UPDATE_FLAGS = ("--yes", "--no-start", "--no-open", "--no-modify-path")
INSTALLER_URL = f"{FRAMEWORK_RAW}/{FRAMEWORK_REF}/{INSTALLER_SCRIPT_PATH}"
INSTALLER_ONE_LINER = f"curl -LsSf {INSTALLER_URL} | sh"
INSTALLER_ONE_LINER_WINDOWS = f'powershell -ExecutionPolicy ByPass -c "irm {FRAMEWORK_RAW}/{FRAMEWORK_REF}/scripts/install.ps1 | iex"'
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


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
    # installer installs only: the data dir holding bootstrap.env, and the AbstractFramework
    # release it records (None: installed before releases were recorded, or with --pin/--from).
    data_dir: Optional[str] = None
    framework_version: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "path": "installer" if self.kind == "installer" else "package",
            "upgradable": bool(self.upgradable),
            "reason": self.reason,
            "command": list(self.command) if self.command else None,
            "display_command": self.display_command,
            "python": self.python,
            "prefix": self.prefix,
            "extras": list(self.extras),
            "version": self.version,
            "data_dir": self.data_dir,
            "framework_version": self.framework_version,
        }


def read_installer_state(data_dir: Path) -> Optional[Dict[str, str]]:
    """The AbstractFramework installer's record in a data dir (`bootstrap.env`, KEY=VALUE
    lines), or None when this data dir was not set up by the installer."""
    path = Path(data_dir) / INSTALLER_STATE_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    out: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key and not key.startswith("#") and key.strip() == key:
            out[key] = value.strip()
    return out


def _data_dir() -> Path:
    from .host_paths import resolve_data_dir

    return resolve_data_dir().path


def _uv_receipt_pin(prefix: str) -> Optional[str]:
    """The exact version a uv tool receipt pins the gateway to (`specifier = "==X"`), if any:
    uv keeps it on `uv tool upgrade`."""
    for candidate in (Path(prefix) / "uv-receipt.toml", Path(prefix).parent / "uv-receipt.toml"):
        try:
            raw = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            import tomllib  # Python 3.11+
        except ImportError:  # pragma: no cover - Python 3.10: no receipt reading, no pin known
            return None
        try:
            reqs = tomllib.loads(raw).get("tool", {}).get("requirements", [])
        except Exception:  # noqa: BLE001 - a malformed receipt pins nothing we can read
            return None
        for req in reqs if isinstance(reqs, list) else []:
            if isinstance(req, dict) and str(req.get("name", "")).lower() == DIST_NAME:
                spec = str(req.get("specifier") or "")
                return spec[2:] if spec.startswith("==") else None
    return None


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


def detect_install(
    *,
    executable: Optional[str] = None,
    prefix: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    data_dir: Optional[Path] = None,
    platform: str = sys.platform,
) -> InstallInfo:
    """Classify the running install. Pure over its inputs where possible so
    tests can pin every branch with fake prefixes, env and data dir."""
    exe = str(executable or sys.executable)
    pfx = str(prefix or sys.prefix)
    environ = os.environ if env is None else env
    extras = installed_extras()
    base_prefix = str(getattr(sys, "base_prefix", pfx)) if prefix is None else pfx + "-not-base"
    version = installed_version()
    spec = DIST_NAME + (f"[{','.join(extras)}]" if extras else "")
    lower = pfx.replace("\\", "/").lower()

    def info(kind: str, *, upgradable: bool, reason: Optional[str], command: Optional[List[str]], display: Optional[str], **more: Any) -> InstallInfo:
        return InstallInfo(kind=kind, upgradable=upgradable, reason=reason, command=command, display_command=display, python=exe, prefix=pfx, extras=extras, version=version, **more)

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
        ddir = Path(data_dir) if data_dir is not None else _data_dir()
        state = read_installer_state(ddir)
        if state is not None:
            # The AbstractFramework installer made this install: its update is the installer.
            framework = state.get("FRAMEWORK_VERSION") or None
            if platform.startswith("win"):
                return info(
                    "installer",
                    upgradable=False,
                    reason=(
                        "Windows keeps the running gateway's files locked, so the update runs in PowerShell: "
                        "the installer stops the gateway, updates everything and starts it again"
                    ),
                    command=None,
                    display=INSTALLER_ONE_LINER_WINDOWS,
                    data_dir=str(ddir),
                    framework_version=framework,
                )
            flags = " ".join(INSTALLER_UPDATE_FLAGS)
            return info(
                "installer",
                upgradable=True,
                reason=None,
                command=None,  # the checked installer, written to the data dir when the update starts
                display=f"{INSTALLER_ONE_LINER} -s -- {flags} --data-dir {shlex.quote(str(ddir))}",
                data_dir=str(ddir),
                framework_version=framework,
            )
        pin = _uv_receipt_pin(pfx)
        if pin:
            reinstall = f"uv tool install --upgrade {shlex.quote(spec)}"
            return info(
                "uv-tool",
                upgradable=False,
                reason=f"installed with an exact version ({DIST_NAME}=={pin}), which `uv tool upgrade` keeps; reinstall without the pin: {reinstall}",
                command=None,
                display=reinstall,
            )
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


HttpGet = Any  # (url, accept, timeout_s) -> bytes; raises urllib errors


def _http_get(url: str, accept: str, timeout_s: float) -> bytes:
    import urllib.request

    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"{DIST_NAME}/{installed_version()}"})
    with urllib.request.urlopen(req, timeout=float(timeout_s)) as resp:  # noqa: S310 - fixed https URLs
        return resp.read()


def _fetch_failure(what: str, exc: BaseException) -> Dict[str, Any]:
    import urllib.error

    if isinstance(exc, urllib.error.HTTPError):
        return {"ok": False, "offline": False, "error": f"{what} answered HTTP {exc.code}"}
    return {"ok": False, "offline": True, "error": f"could not reach {what} ({type(exc).__name__}: {exc})"}


def _fetch_framework_release(*, http_get: HttpGet = None, timeout_s: float = CHECK_TIMEOUT_S) -> Dict[str, Any]:
    """The newest AbstractFramework release, as the one-line install sees it: the framework
    repo's `main` resolved to a commit, that commit's install manifest (release version,
    gateway pin, python packages) and that commit's installer script (one snapshot: a push
    between two downloads cannot pair one release's manifest with another's script). Every
    failure is an in-band {ok: False, offline, error}."""
    get = http_get or _http_get
    try:
        commit = get(FRAMEWORK_COMMIT_API, "application/vnd.github.sha", timeout_s).decode("ascii", errors="replace").strip()
    except Exception as exc:  # noqa: BLE001 - DNS, refused, timeout, TLS, HTTP
        return _fetch_failure("GitHub (the AbstractFramework release)", exc)
    if not _COMMIT_RE.match(commit):
        return {"ok": False, "offline": False, "error": f"GitHub answered {commit[:60]!r} for the latest {FRAMEWORK_NAME} commit"}
    manifest_url = f"{FRAMEWORK_RAW}/{commit}/{FRAMEWORK_MANIFEST_PATH}"
    installer_url = f"{FRAMEWORK_RAW}/{commit}/{INSTALLER_SCRIPT_PATH}"
    try:
        manifest = json.loads(get(manifest_url, "application/json", timeout_s).decode("utf-8"))
        script = get(installer_url, "text/plain", timeout_s)
    except json.JSONDecodeError as exc:
        return {"ok": False, "offline": False, "error": f"the release manifest {manifest_url} is not JSON ({exc})"}
    except Exception as exc:  # noqa: BLE001
        return _fetch_failure("GitHub (the AbstractFramework release files)", exc)
    try:
        version = str(manifest["framework"]["version"])
        gateway_version = str(manifest["bootstrap"]["gateway_version"])
        packages = {str(p["distribution"]).lower().replace("_", "-"): str(p["version"]) for p in manifest.get("python_packages") or []}
    except (KeyError, TypeError) as exc:
        return {"ok": False, "offline": False, "error": f"the release manifest {manifest_url} has no {exc}"}
    if not script.startswith(b"#!/bin/sh"):
        return {"ok": False, "offline": False, "error": f"{installer_url} is not the installer script"}
    return {
        "ok": True,
        "offline": False,
        "commit": commit,
        "version": version,
        "gateway_version": gateway_version,
        "python_packages": packages,
        "manifest_url": manifest_url,
        "installer_url": installer_url,
        "installer_bytes": script,
    }


# The installer the last check downloaded: {"sha256", "url", "bytes", "python_packages"}.
_installer_checked: Optional[Dict[str, Any]] = None


def check_for_update(
    *,
    force: bool = False,
    fetch: Any = None,
    now: Optional[float] = None,
    info: Optional[InstallInfo] = None,
    fetch_release: Any = None,
) -> Dict[str, Any]:
    """Cached (1 h) update check. `fetch` (PyPI) and `fetch_release` (the AbstractFramework
    release) are injectable for tests. Installer installs compare the recorded release with
    the newest one (never PyPI's newest gateway); package installs compare the gateway's
    version with PyPI's."""
    global _last_check, _last_check_at, _installer_checked
    clock = float(now if now is not None else time.time())
    with _check_lock:
        if not force and _last_check is not None and (clock - _last_check_at) < CHECK_CACHE_TTL_S:
            return dict(_last_check)
    inst = info or detect_install()
    current = inst.version or installed_version()
    out: Dict[str, Any] = {"current": current, "checked_at": _utc_now_iso()}
    if inst.kind == "installer":
        rel = (fetch_release or _fetch_framework_release)()
        out.update({"source": "framework-release", "offline": bool(rel.get("offline"))})
        if rel.get("ok"):
            installed = inst.framework_version
            # A recorded release compares with the newest release. None recorded (an install
            # made with --pin/--from, or before releases were recorded): the gateway compares
            # with the release's gateway pin, so a gateway newer than the release is never
            # "updated" back down to it.
            if installed:
                available = is_newer(rel["version"], installed)
            else:
                available = is_newer(rel["gateway_version"], current)
            script = bytes(rel["installer_bytes"])
            digest = hashlib.sha256(script).hexdigest()
            out.update(
                {
                    # `latest` stays the gateway version for clients that read only it (the
                    # terminal console before 0.11.1): the release's gateway pin.
                    "latest": rel["gateway_version"],
                    "update_available": available,
                    "error": None,
                    "release": {
                        "name": FRAMEWORK_NAME,
                        "version": rel["version"],
                        "gateway_version": rel["gateway_version"],
                        "installed": installed,
                        "commit": rel["commit"],
                        "manifest_url": rel["manifest_url"],
                        "installer": {
                            "url": INSTALLER_URL,
                            "commit_url": rel["installer_url"],
                            "sha256": digest,
                            "size": len(script),
                        },
                    },
                }
            )
            with _check_lock:
                _installer_checked = {"sha256": digest, "url": rel["installer_url"], "bytes": script, "python_packages": dict(rel.get("python_packages") or {})}
        else:
            out.update({"latest": None, "update_available": None, "error": str(rel.get("error") or "check failed"), "release": None})
    else:
        result = (fetch or _fetch_pypi_latest)()
        out.update({"source": "pypi", "offline": bool(result.get("offline"))})
        if result.get("ok"):
            latest = str(result.get("latest"))
            out.update({"latest": latest, "update_available": is_newer(latest, current), "error": None})
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
    # installer runs: the script, the release before/after and what moved ({name, from, to}
    # for the release and its packages; a count of the other distributions that moved).
    installer: Optional[Dict[str, Any]] = None
    framework_before: Optional[str] = None
    framework_after: Optional[str] = None
    changes: List[Dict[str, Any]] = field(default_factory=list)
    other_changes: int = 0
    message: Optional[str] = None

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
            "installer": dict(self.installer) if self.installer else None,
            "framework_before": self.framework_before,
            "framework_after": self.framework_after,
            "changes": [dict(c) for c in self.changes],
            "other_changes": int(self.other_changes),
            "message": self.message,
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


_ENV_SNAPSHOT_CODE = (
    "import importlib.metadata as m, json\n"
    "print(json.dumps({(d.metadata['Name'] or '').lower().replace('_', '-'): d.version for d in m.distributions()}))"
)


def _env_versions_fresh(python: str) -> Optional[Dict[str, str]]:
    """Every distribution in the environment of `python`, asked of a FRESH interpreter."""
    try:
        out = subprocess.run([python, "-c", _ENV_SNAPSHOT_CODE], capture_output=True, text=True, timeout=60, check=False)
        data = json.loads(out.stdout or "null")
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else None
    except Exception:
        return None


def _diff_versions(before: Dict[str, str], after: Dict[str, str], named: List[str]) -> tuple:
    """([{name, from, to}] for the named packages that moved, count of the other moved ones)."""
    changes = [{"name": n, "from": before.get(n), "to": after.get(n)} for n in named if before.get(n) != after.get(n)]
    others = sum(1 for n in set(before) | set(after) if n not in named and before.get(n) != after.get(n))
    return changes, others


def installer_command(script: Path, data_dir: str) -> List[str]:
    """The exact command an Update runs: the checked install.sh, asking nothing (--yes keeps
    start at login as it was), never stopping or restarting the running gateway (--no-start),
    opening nothing, leaving shell profiles alone, on this gateway's data dir."""
    return ["/bin/sh", str(script), *INSTALLER_UPDATE_FLAGS, "--data-dir", str(data_dir)]


def _prepare_installer_run(inst: InstallInfo, *, installer_sha256: Optional[str], fetch_release: Any) -> tuple:
    """(command, {url, commit_url, sha256}, release packages): the checked installer, written
    to <data_dir>/update/install.sh, and the exact command that runs it."""
    with _check_lock:
        checked = dict(_installer_checked) if _installer_checked else None
        last = dict(_last_check) if _last_check else None
    if checked is None:
        last = check_for_update(force=True, info=inst, fetch_release=fetch_release)
        with _check_lock:
            checked = dict(_installer_checked) if _installer_checked else None
    if checked is None:
        why = (last or {}).get("error") or "the check did not download it"
        raise UpdateNotPossible(f"the AbstractFramework installer could not be downloaded: {why}")
    if installer_sha256 and installer_sha256.strip().lower() != checked["sha256"]:
        raise UpdateNotPossible(
            f"the installer changed since it was checked (you reviewed sha256 {installer_sha256[:12]}…, the last check "
            f"downloaded {checked['sha256'][:12]}…): check again and review it"
        )
    ddir = Path(str(inst.data_dir))
    script = ddir / "update" / "install.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_bytes(checked["bytes"])
    os.chmod(script, 0o600)
    meta = {"url": INSTALLER_URL, "commit_url": checked["url"], "sha256": checked["sha256"]}
    return installer_command(script, str(ddir)), meta, list(checked.get("python_packages") or {})


def start_update(
    *,
    info: Optional[InstallInfo] = None,
    runner: Any = None,
    installer_sha256: Optional[str] = None,
    fetch_release: Any = None,
    snapshot: Any = None,
) -> Dict[str, Any]:
    """Launch the upgrade in the background. `runner(command, on_line) -> exit_code` and
    `snapshot(python) -> {name: version}` are injectable for tests. `installer_sha256` (the
    confirmation's) refuses to run an installer other than the one the admin reviewed."""
    global _job, _job_thread
    inst = info or detect_install()
    if not inst.upgradable:
        raise UpdateNotPossible(inst.reason or "this install cannot be upgraded automatically")
    installer_meta: Optional[Dict[str, Any]] = None
    named: List[str] = [DIST_NAME]
    if inst.kind == "installer":
        command, installer_meta, packages = _prepare_installer_run(inst, installer_sha256=installer_sha256, fetch_release=fetch_release)
        # The Assistant is a separate app, not in the gateway's environment.
        named += [n for n in packages if n not in named and n != "abstractassistant"]
    else:
        if not inst.command:
            raise UpdateNotPossible(inst.reason or "this install cannot be upgraded automatically")
        command = list(inst.command)
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
        _job = _Job(
            state="running",
            started_at=_utc_now_iso(),
            command=list(command),
            version_before=inst.version,
            installer=installer_meta,
            framework_before=inst.framework_version,
        )
        job = _job
    take_snapshot = snapshot or _env_versions_fresh

    def _default_runner(command: List[str], on_line: Any) -> int:
        env = dict(os.environ)
        # Subprocess-only hygiene (not gateway knobs): never block on a
        # prompt (private index credentials), never spam a progress bar or colors
        # into the log, never let pip nag about itself.
        env.setdefault("PIP_NO_INPUT", "1")
        env.setdefault("PIP_PROGRESS_BAR", "off")
        env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
        env.setdefault("UV_NO_PROGRESS", "1")
        env["NO_COLOR"] = "1"
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

        before = take_snapshot(inst.python) if inst.kind == "installer" else None
        try:
            code = int((runner or _default_runner)(list(command), on_line))
        except Exception as exc:  # noqa: BLE001
            with _job_lock:
                job.state = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished_at = _utc_now_iso()
            return
        if inst.kind == "installer":
            _finish_installer_job(job, inst, code, before, take_snapshot(inst.python) if code == 0 else None, named)
            return
        after = _installed_version_fresh(inst.python) if code == 0 else None
        with _job_lock:
            job.exit_code = code
            job.finished_at = _utc_now_iso()
            job.version_after = after
            job.proc = None
            if code == 0 and after and job.version_before and after == job.version_before:
                # The package manager installed nothing newer. Never offer a restart that
                # would loop back to the same offer.
                job.state = "succeeded_no_change"
                job.error = f"nothing changed: `{inst.display_command or ' '.join(command)}` installed nothing newer than {after}"
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


def _finish_installer_job(job: _Job, inst: InstallInfo, code: int, before: Optional[Dict[str, str]], after: Optional[Dict[str, str]], named: List[str]) -> None:
    global _restart_pending
    state = read_installer_state(Path(str(inst.data_dir))) or {}
    framework_after = state.get("FRAMEWORK_VERSION") or None
    with _job_lock:
        job.exit_code = code
        job.finished_at = _utc_now_iso()
        job.proc = None
        if code != 0:
            job.state = "failed"
            job.error = (
                f"the {FRAMEWORK_NAME} installer exited with code {code}: the last lines of its log say why "
                f"(the full log is the newest install-*.log in {Path(str(inst.data_dir)) / 'logs'}). "
                f"The running gateway was not stopped; fix the cause and update again, or re-run the "
                f"installer in a terminal: {INSTALLER_ONE_LINER}"
            )
            return
        job.framework_after = framework_after
        job.version_after = (after or {}).get(DIST_NAME) or job.version_before
        if before is None or after is None:
            # The environment could not be read back: say so, and recommend the restart anyway
            # (the installer succeeded; a restart is harmless when nothing moved).
            job.state = "succeeded"
            job.restart_recommended = True
            _restart_pending = True
            job.message = "the installer finished; the installed versions could not be read back, so restart to be sure the new code runs"
            return
        job.changes, job.other_changes = _diff_versions(before, after, named)
        if framework_after != inst.framework_version:
            job.changes.insert(0, {"name": FRAMEWORK_NAME, "from": inst.framework_version, "to": framework_after})
        if job.changes or job.other_changes:
            job.state = "succeeded"
            job.restart_recommended = True
            _restart_pending = True
            what = f"{FRAMEWORK_NAME} {framework_after}" if framework_after else f"gateway {job.version_after}"
            job.message = f"{what} is installed ({changes_text(job.changes, job.other_changes)})"
        else:
            job.state = "succeeded_no_change"
            what = f"{FRAMEWORK_NAME} {framework_after}" if framework_after else f"gateway {job.version_after}"
            job.message = f"already up to date: {what}; the installer changed nothing"
            # `error` too: clients before the `message` field show it for this state.
            job.error = job.message


def changes_text(changes: List[Dict[str, Any]], others: int) -> str:
    """"AbstractFramework 0.6.1 -> 0.6.2, abstractgateway 0.7.1 -> 0.7.2, 3 other packages"."""
    parts = [f"{c['name']} {c.get('from') or '(none)'} -> {c.get('to') or '(removed)'}" for c in changes]
    if others:
        parts.append(f"{others} other package{'s' if others != 1 else ''}")
    return ", ".join(parts) or "nothing moved"


def update_view(inst: InstallInfo, check: Optional[Dict[str, Any]], job: Dict[str, Any], restart_pending: bool) -> Dict[str, Any]:
    """The ONE rendering of the update state that the tray, the web console and the terminal
    console show: a status, the version line, a hint, `offer` (what an update would install:
    "AbstractFramework 0.6.2" or "AbstractGateway 0.7.2"), and the action with its label, the
    command it runs, where that comes from and the confirmation text (None when there is
    nothing to start). Clients append "checked <when>" to the line of an `up_to_date` status
    in their own time format (`check.checked_at`)."""
    chk = check or {}
    installer = inst.kind == "installer"
    rel = chk.get("release") if isinstance(chk.get("release"), dict) else None
    if installer:
        installed = f"{FRAMEWORK_NAME} {inst.framework_version}" if inst.framework_version else f"{FRAMEWORK_NAME} (release not recorded)"
        base = f"{installed} · gateway {inst.version}"
        offer = f"{FRAMEWORK_NAME} {rel['version']}" if rel else None
    else:
        base = inst.version
        offer = f"AbstractGateway {chk['latest']}" if chk.get("latest") else None
    state = str(job.get("state") or "idle")
    action: Optional[Dict[str, Any]] = None
    hint = ""
    if state == "running":
        status = "running"
        last = (job.get("log_tail") or [""])[-1]
        line = f"{base} · installing…" + (f" ({last})" if last else "")
    elif state == "succeeded" or restart_pending:
        status = "installed"
        what = job.get("message") or (f"{job.get('version_after')} is installed" if job.get("version_after") else "the update is installed")
        line = f"{base} · {what} — restart to finish"
    elif state == "failed":
        status = "failed"
        line = f"{base} · the update didn't finish ({job.get('error') or 'see the log'})"
    elif state == "succeeded_no_change":
        status = "no_change"
        line = f"{base} · {job.get('message') or job.get('error') or 'nothing changed'}"
    elif not check:
        status = "not_checked"
        line = f"{base} · not checked yet"
    elif chk.get("offline"):
        status = "offline"
        line = f"{base} · couldn't reach the update server (offline?)"
    elif chk.get("error") and chk.get("latest") is None:
        status = "error"
        line = f"{base} · couldn't check for updates ({chk.get('error')})"
    elif chk.get("update_available"):
        status = "available" if inst.upgradable else "not_possible"
        line = f"{base} · {offer} available"
    else:
        status = "up_to_date"
        line = f"{base} · up to date"
    if status in ("available", "not_possible") and not inst.upgradable:
        hint = str(inst.reason or "")
        if inst.display_command:
            hint += f" — run: {inst.display_command}"
    elif installer:
        hint = f"installed with the {FRAMEWORK_NAME} installer; Update runs it again (the same script as the one-line install)"
    elif inst.kind:
        hint = f"installed with {inst.kind}"
    if status == "available":
        if installer and rel:
            ins = rel.get("installer") or {}
            command = " ".join(shlex.quote(a) for a in installer_command(Path("install.sh"), str(inst.data_dir)))
            source = f"{ins.get('url')} (commit {str(rel.get('commit') or '')[:12]}, sha256 {ins.get('sha256')})"
            confirm = (
                f"{FRAMEWORK_NAME} {rel['version']} (gateway {rel['gateway_version']}) is available; you have {installed} "
                f"(gateway {inst.version}). Update runs the {FRAMEWORK_NAME} installer, the script the one-line install "
                f"runs: {source}, as: {command}. It asks nothing and keeps start at login as it is. It installs next to "
                "the running gateway (a few minutes); workflows keep running until you restart."
            )
            action = {
                "label": f"Update to {FRAMEWORK_NAME} {rel['version']}",
                "confirm": confirm,
                "command": command,
                "source": source,
                "installer_sha256": ins.get("sha256"),
            }
        else:
            confirm = f"AbstractGateway {chk.get('latest')} is available (you have {inst.version}). Installing takes a minute or two; workflows keep running until you restart."
            action = {
                "label": f"Update to {chk.get('latest')}",
                "confirm": confirm,
                "command": inst.display_command,
                "source": "PyPI",
                "installer_sha256": None,
            }
    return {"status": status, "line": line, "hint": hint, "offer": offer, "action": action, "checked_at": chk.get("checked_at")}


def update_overview(*, check: bool = False) -> Dict[str, Any]:
    """The one payload the tray/console render: install facts, last (or a
    fresh) check, the job state, and `update`, the rendering every client shows."""
    inst = detect_install()
    checked = check_for_update(info=inst) if check else last_check()
    job = job_status()
    pending = restart_pending()
    return {
        "ok": True,
        "current": inst.version,
        "install": inst.as_dict(),
        "check": checked,
        "job": job,
        "restart_pending": pending,
        "update": update_view(inst, checked, job, pending),
    }


def _reset_for_tests() -> None:
    global _last_check, _last_check_at, _job, _job_thread, _restart_pending, _installer_checked
    with _check_lock:
        _last_check = None
        _last_check_at = 0.0
        _installer_checked = None
    with _job_lock:
        _job = _Job()
    _job_thread = None
    _restart_pending = False
