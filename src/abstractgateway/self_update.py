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
  program's files locked, so there Update HANDS OFF: the checked ``scripts/install.ps1`` is
  written to ``<data dir>\\update\\`` with a small runner script, and a PowerShell window that
  is not a descendant of the gateway runs it (``-NoOpen -NoModifyPath -DataDir <this data
  dir>``). The installer stops the gateway and everything running from its environment,
  updates, and starts it again; ``<data dir>\\update\\windows-update.json`` records the
  hand-off and the runner writes the installer's exit code next to it, so the gateway that
  comes back (and its tray and consoles) reports what happened. The newest gateway outside
  a release stays a command-line choice (``--pin latest``).
- **Package installs** (pip / uv venv / pipx / uv tool without the installer): the package
  manager's upgrade, keeping the install profile (the ``apple`` / ``gpu`` / ``embeddings``
  extras), compared with PyPI's newest gateway. A uv tool pinned to an exact version is not
  upgradable in place (uv keeps the pin); the reason says how to reinstall.

Editable checkouts can switch to published PyPI packages without modifying source files.
A Docker image, a conda env or an unknown launcher
answer ``upgradable: false`` with the reason and the command a human would run instead.
Offline is a first-class outcome, not an error. ``update_overview()["update"]`` is the ONE
rendering every client shows (the version line, the hint, the action and its confirmation
text), so the tray and both consoles say the same thing.

A finished upgrade sets ``restart_recommended``. With ``auto_restart=True``, the
worker requests a graceful relaunch after a successful install; otherwise the caller offers it.
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
from typing import Any, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DIST_NAME = "abstractgateway"
APP_GATEWAY = "AbstractGateway"
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
# `abstractgateway update` (a shell command, not the gateway process): the installer may restart
# the gateway service itself, so no --no-start.
INSTALLER_CLI_FLAGS = ("--yes", "--no-open", "--no-modify-path")
INSTALLER_SCRIPT_PATH_WINDOWS = "scripts/install.ps1"
INSTALLER_URL_WINDOWS = f"{FRAMEWORK_RAW}/{FRAMEWORK_REF}/{INSTALLER_SCRIPT_PATH_WINDOWS}"
# The Windows update (a hand-off, see the module docstring): asks nothing, opens no browser,
# leaves PATH alone; start at login, profile, port and tray stay as bootstrap.env records.
# -AskWait 0: the start-at-login question is answered at once with the recorded choice (in a
# window of its own the installer would otherwise wait 25 s for an answer nobody gives).
WINDOWS_INSTALLER_FLAGS = ("-NoOpen", "-NoModifyPath", "-AskWait", "0")
WINDOWS_HANDOFF_FILE = "windows-update.json"
# How long a hand-off counts as in progress without a result from its runner.
WINDOWS_HANDOFF_TIMEOUT_S = 45 * 60.0
# How long the outcome of a finished hand-off is reported (the gateway that comes back shows it).
WINDOWS_HANDOFF_REPORT_S = 24 * 3600.0
_WINDOWS_INSTALLER_MARK = b"AbstractFramework bootstrap installer for Windows"
ROOT_CHANGELOG_PATH = "CHANGELOG.md"
INSTALLER_URL = f"{FRAMEWORK_RAW}/{FRAMEWORK_REF}/{INSTALLER_SCRIPT_PATH}"
INSTALLER_ONE_LINER = f"curl -LsSf {INSTALLER_URL} | sh"
INSTALLER_ONE_LINER_WINDOWS = f'powershell -ExecutionPolicy ByPass -c "irm {FRAMEWORK_RAW}/{FRAMEWORK_REF}/scripts/install.ps1 | iex"'
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
# The crate pins live in the installer script, not the manifest: AF_CRATE_CONSOLE="name@version".
_CRATE_PIN_RE = re.compile(r'^AF_CRATE_[A-Z0-9_]+="([^@"\s]+)@([^"\s]+)"\s*$', re.MULTILINE)


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
    # Windows installer installs: Update hands the installer to its own PowerShell window.
    windows: bool = False

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
            "windows": bool(self.windows),
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
        # Switch this environment to the published wheel; never pull or modify the checkout.
        supported = not (_in_docker() or environ.get("ABSTRACTGATEWAY_CONTAINER") or _externally_managed(pfx, base_prefix)) and bool(_dist_installed("pip") or shutil.which("uv"))
        cmd = [exe, "-m", "pip", "--isolated", "install", "--index-url", "https://pypi.org/simple", "--no-cache-dir", "--upgrade", spec]
        if not _dist_installed("pip") and shutil.which("uv"):
            cmd = [str(shutil.which("uv")), "--no-config", "--no-cache", "pip", "install", "--python", exe, "--index-url", "https://pypi.org/simple", "--upgrade", spec]
        return info(
            "editable",
            upgradable=supported,
            reason=None if supported else "This environment cannot install releases in place.",
            command=cmd if supported else None,
            display=" ".join(cmd) if supported else None,
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
                # Windows keeps the running gateway's files locked: Update hands the checked
                # install.ps1 to its own PowerShell window, which stops the gateway, updates
                # everything and starts it again (start_update).
                return info(
                    "installer",
                    upgradable=True,
                    reason=None,
                    command=None,  # the checked installer, written to the data dir when the update starts
                    display=" ".join(windows_installer_command(Path("install.ps1"), str(ddir))),
                    data_dir=str(ddir),
                    framework_version=framework,
                    windows=True,
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


def _fetch_framework_release(*, http_get: HttpGet = None, timeout_s: float = CHECK_TIMEOUT_S, windows: bool = False) -> Dict[str, Any]:
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
    windows_extra: Dict[str, Any] = {}
    if windows:
        # The installer a Windows update runs: the same snapshot's install.ps1.
        windows_url = f"{FRAMEWORK_RAW}/{commit}/{INSTALLER_SCRIPT_PATH_WINDOWS}"
        try:
            windows_script = get(windows_url, "text/plain", timeout_s)
        except Exception as exc:  # noqa: BLE001
            return _fetch_failure("GitHub (the AbstractFramework Windows installer)", exc)
        if _WINDOWS_INSTALLER_MARK not in windows_script[:2048]:
            return {"ok": False, "offline": False, "error": f"{windows_url} is not the Windows installer script"}
        windows_extra = {"windows_installer_url": windows_url, "windows_installer_bytes": windows_script}
    # The browser apps the release pins (manifest) and the crates it builds (the script's
    # AF_CRATE_* lines): `abstractgateway update` lists what would change for each of them.
    npm_apps = {str(a["id"]): str(a["version"]) for a in manifest.get("npm_apps") or [] if isinstance(a, dict) and a.get("id") and a.get("version")}
    crates = {name: ver for name, ver in _CRATE_PIN_RE.findall(script.decode("utf-8", errors="replace"))}
    return {
        "ok": True,
        "offline": False,
        "commit": commit,
        "version": version,
        "gateway_version": gateway_version,
        "python_packages": packages,
        "npm_apps": npm_apps,
        "crates": crates,
        "manifest_url": manifest_url,
        "installer_url": installer_url,
        "installer_bytes": script,
        **windows_extra,
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
        rel = fetch_release() if fetch_release else (_fetch_framework_release(windows=True) if inst.windows else _fetch_framework_release())
        out.update({"source": "framework-release", "offline": bool(rel.get("offline"))})
        if rel.get("ok") and inst.windows and not rel.get("windows_installer_bytes"):
            rel = {"ok": False, "offline": False, "error": "the AbstractFramework Windows installer (scripts/install.ps1) was not downloaded"}
        if rel.get("ok"):
            installed = inst.framework_version
            # A recorded release compares with the newest release. None recorded (an install
            # made with --pin/--from, or before releases were recorded): the gateway compares
            # with the release's gateway pin, so a gateway newer than the release is never
            # "updated" back down to it.
            if installed:
                pair = (rel["version"], installed)
                available = is_newer(*pair)
            else:
                pair = (rel["gateway_version"], current)
                available = is_newer(*pair)
            if available is None:
                # Never "up to date" over versions that cannot be compared.
                out.update({"latest": None, "update_available": None, "release": None,
                            "error": f"cannot compare the versions {pair[0]!r} (the {FRAMEWORK_NAME} release) and {pair[1]!r} (installed)"})
                with _check_lock:
                    _last_check = dict(out)
                    _last_check_at = clock
                return out
            # The script an Update runs: install.ps1 on Windows, install.sh elsewhere.
            script = bytes(rel["windows_installer_bytes"] if inst.windows else rel["installer_bytes"])
            script_url = str(rel["windows_installer_url"] if inst.windows else rel["installer_url"])
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
                        # The browser apps the newest release pins: an app installed
                        # older than this is named in the console (apps_manager).
                        "npm_apps": dict(rel.get("npm_apps") or {}),
                        "installed": installed,
                        "commit": rel["commit"],
                        "manifest_url": rel["manifest_url"],
                        "installer": {
                            "url": INSTALLER_URL_WINDOWS if inst.windows else INSTALLER_URL,
                            "commit_url": script_url,
                            "sha256": digest,
                            "size": len(script),
                        },
                    },
                }
            )
            with _check_lock:
                _installer_checked = {"sha256": digest, "url": script_url, "bytes": script, "python_packages": dict(rel.get("python_packages") or {}),
                                      "windows": bool(inst.windows), "release": rel["version"]}
        else:
            out.update({"latest": None, "update_available": None, "error": str(rel.get("error") or "check failed"), "release": None})
    else:
        result = (fetch or _fetch_pypi_latest)()
        out.update({"source": "pypi", "offline": bool(result.get("offline"))})
        if result.get("ok"):
            latest = str(result.get("latest"))
            newer = is_newer(latest, current)
            if newer is None:
                out.update({"latest": None, "update_available": None, "error": f"cannot compare the versions {latest!r} (PyPI) and {current!r} (installed)"})
            else:
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
    state: str = "idle"  # idle | running | completed | succeeded | succeeded_no_change | failed
    proc: Any = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    command: Optional[List[str]] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    log: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_TAIL_LINES))
    restart_recommended: bool = False
    restart_error: Optional[str] = None
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
    # Windows: the hand-off this job is (its id, the installer's log folder, the runner's result).
    handoff: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "handoff": dict(self.handoff) if self.handoff else None,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "command": list(self.command) if self.command else None,
            "exit_code": self.exit_code,
            "error": self.error,
            "log_tail": list(self.log),
            "restart_recommended": bool(self.restart_recommended),
            "restart_error": self.restart_error,
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
_start_lock = threading.Lock()  # serializes start_update (see there)
_job = _Job()
_job_thread: Optional[threading.Thread] = None


def job_status(*, data_dir: Optional[str] = None) -> Dict[str, Any]:
    """This process's update job. On Windows a gateway that has run no job reports the last
    hand-off recorded in `data_dir` instead: the installer stopped the gateway that started
    it, so its outcome belongs to the gateway that came back."""
    with _job_lock:
        current = _job.as_dict()
    ddir = data_dir or ((current.get("handoff") or {}).get("data_dir"))
    if ddir and (current["state"] == "idle" or current.get("handoff")):
        # A gateway that ran no job reports the last recorded hand-off; the gateway that
        # started one reports its outcome once the installer wrote it (the installer failed
        # before it stopped this gateway: a lock, the network, a closed window).
        recorded = windows_handoff_job(Path(ddir))
        if recorded is not None and (current["state"] == "idle" or (recorded.get("handoff") or {}).get("id") == (current.get("handoff") or {}).get("id")):
            return recorded
    return current


def _job_running_locked() -> bool:
    """`_job` runs (call with _job_lock held). A Windows hand-off runs until its installer
    reported (the result file) or its record says it no longer runs."""
    if _job.state != "running":
        return False
    if not _job.handoff:
        return True
    ddir = _job.handoff.get("data_dir")
    if not ddir:
        return True
    recorded = windows_handoff_job(Path(str(ddir)))
    return recorded is None or recorded.get("state") == "running" or (recorded.get("handoff") or {}).get("id") != _job.handoff.get("id")


def job_running() -> bool:
    with _job_lock:
        return _job_running_locked()


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


def installer_command(script: Path, data_dir: str, *, flags: tuple = INSTALLER_UPDATE_FLAGS) -> List[str]:
    """The exact command an Update runs: the checked install.sh, asking nothing (--yes keeps
    start at login as it was), never stopping or restarting the running gateway (--no-start),
    opening nothing, leaving shell profiles alone, on this gateway's data dir. `flags`:
    `abstractgateway update` passes INSTALLER_CLI_FLAGS (no --no-start: it is not the gateway
    process, so the installer restarts the service itself)."""
    return ["/bin/sh", str(script), *flags, "--data-dir", str(data_dir)]


def windows_installer_command(script: Path, data_dir: str) -> List[str]:
    """The exact command a Windows update runs (in its own PowerShell window, see
    start_update): the checked install.ps1 as a file, asking nothing, opening no browser,
    leaving PATH alone, on this gateway's data dir. Without -NoStart: the installer stops the
    running gateway (Windows locks its files) and starts it again."""
    return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            *WINDOWS_INSTALLER_FLAGS, "-DataDir", str(data_dir)]


def _write_installer_script(folder: Path, data: bytes, sha256: str, *, suffix: str = ".sh") -> Path:
    """A NEW file per run (written to a temporary name in `folder`, then renamed to a name no
    other run uses): /bin/sh reads its script while it runs, so a running script's file is
    never written again. Only called while no update runs (start_update holds _start_lock and
    checked), so the older scripts of finished runs are removed first."""
    import tempfile
    import uuid

    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"install-*{suffix}"):
        try:
            old.unlink()
        except OSError:
            pass
    # A temporary file a crashed write left behind (older than an hour: never one being written).
    stale_before = time.time() - 3600.0
    for tmp_old in folder.glob(".install-*.tmp"):
        try:
            if tmp_old.stat().st_mtime < stale_before:
                tmp_old.unlink()
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=str(folder), prefix=".install-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        final = folder / f"install-{sha256[:12]}-{uuid.uuid4().hex[:8]}{suffix}"
        os.rename(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return final


def _prepare_installer_run(inst: InstallInfo, *, installer_sha256: Optional[str], fetch_release: Any) -> tuple:
    """(command, {url, commit_url, sha256}, release packages): the checked installer, written
    to its own new file in <data_dir>/update/, and the exact command that runs it."""
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
    if not (installer_sha256 or "").strip():
        raise UpdateNotPossible(
            "check again first: an installer update runs only the installer whose sha256 the confirmation showed "
            f"(the last check downloaded {checked['sha256'][:12]}…), and this request named none"
        )
    if installer_sha256.strip().lower() != checked["sha256"]:
        raise UpdateNotPossible(
            f"the installer changed since it was checked (you reviewed sha256 {installer_sha256[:12]}…, the last check "
            f"downloaded {checked['sha256'][:12]}…): check again and review it"
        )
    if bool(checked.get("windows")) != bool(inst.windows):
        raise UpdateNotPossible("check again first: the last check downloaded the installer for another system")
    ddir = Path(str(inst.data_dir))
    if inst.windows:
        script = _write_installer_script(ddir / "update", checked["bytes"], checked["sha256"], suffix=".ps1")
        meta = {"url": INSTALLER_URL_WINDOWS, "commit_url": checked["url"], "sha256": checked["sha256"], "release": checked.get("release")}
        return windows_installer_command(script, str(ddir)), meta, list(checked.get("python_packages") or {})
    script = _write_installer_script(ddir / "update", checked["bytes"], checked["sha256"])
    meta = {"url": INSTALLER_URL, "commit_url": checked["url"], "sha256": checked["sha256"]}
    return installer_command(script, str(ddir)), meta, list(checked.get("python_packages") or {})


def start_update(
    *,
    info: Optional[InstallInfo] = None,
    runner: Any = None,
    installer_sha256: Optional[str] = None,
    fetch_release: Any = None,
    snapshot: Any = None,
    auto_restart: bool = False,
) -> Dict[str, Any]:
    """Launch the upgrade in the background. `runner(command, on_line) -> exit_code` and
    `snapshot(python) -> {name: version}` are injectable for tests. `installer_sha256` (the
    confirmation's) refuses to run an installer other than the one the admin reviewed."""
    if auto_restart and not (info is not None and info.windows and info.kind == "installer"):
        # (A Windows installer update restarts the gateway itself: nothing to check then.)
        from . import host_control
        capabilities = host_control.control_capabilities()
        if not capabilities.get("restart"):
            raise UpdateNotPossible(capabilities.get("reason") or "Automatic restart is unavailable.")
    # One start at a time, and the busy check BEFORE any preparation: preparing an installer
    # run writes a script, which must never happen while another run executes one.
    if not _start_lock.acquire(blocking=False):
        raise UpdateJobBusy("an update is being started")
    try:
        with _job_lock:
            # (A Windows hand-off whose installer already reported is no longer running:
            # job_running() reads its result.)
            if _job.state == "running" and _job_running_locked():
                raise UpdateJobBusy("an update is already running")
        return _start_update_locked(info=info, runner=runner, installer_sha256=installer_sha256, fetch_release=fetch_release, snapshot=snapshot, auto_restart=auto_restart)
    finally:
        _start_lock.release()


def _start_update_locked(*, info: Optional[InstallInfo], runner: Any, installer_sha256: Optional[str], fetch_release: Any, snapshot: Any, auto_restart: bool = False) -> Dict[str, Any]:
    global _job, _job_thread
    inst = info or detect_install()
    if not inst.upgradable:
        raise UpdateNotPossible(inst.reason or "this install cannot be upgraded automatically")
    if inst.kind == "installer" and inst.windows:
        return _start_windows_handoff(inst, installer_sha256=installer_sha256, fetch_release=fetch_release, launcher=runner)
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
        if inst.kind == "editable":
            # The published index is the sole source, even when the shell configures
            # a private index, local wheel directory or offline package-manager mode.
            env = {key: value for key, value in env.items() if not key.startswith(("PIP_", "UV_"))}
            env["PIP_CONFIG_FILE"] = os.devnull
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
            # Any byte sequence the command prints is logged (a byte that is not UTF-8 becomes
            # U+FFFD), never a decode error that would end the job while the command runs on.
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            # Its own process group, so the watchdog stops the whole tree (the installer's uv,
            # cargo, …), not only the shell: a child left holding the pipe would hang the job.
            start_new_session=(os.name != "nt"),
        )
        with _job_lock:
            job.proc = proc
        assert proc.stdout is not None
        # A watchdog, not a check between log lines: a command that hangs silently is killed too.
        timeout_s = float(JOB_TIMEOUT_S)
        timed_out = threading.Event()

        def _kill_group() -> None:
            try:
                if os.name != "nt":
                    import signal

                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except Exception:
                pass

        def _kill() -> None:
            timed_out.set()
            _kill_group()

        watchdog = threading.Timer(timeout_s, _kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            for line in proc.stdout:
                on_line(line.rstrip("\n")[:LOG_LINE_MAX_CHARS])
            code = int(proc.wait())
        except BaseException:
            # Whatever went wrong here, the job must not leave "running" while the command
            # still runs (a second Update would start a second installer next to it): stop
            # the whole group and reap it, then report the error.
            _kill_group()
            proc.wait()
            raise
        finally:
            watchdog.cancel()
        if timed_out.is_set():
            on_line(f"[gateway] update timed out after {timeout_s / 60:.0f} minutes ({timeout_s:.0f} s); command killed")
            return code if code != 0 else 124
        return code

    def _work() -> None:
        def on_line(line: str) -> None:
            with _job_lock:
                job.log.append(str(line))

        before = take_snapshot(inst.python) if inst.kind == "installer" else None
        try:
            code = int((runner or _default_runner)(list(command), on_line))
        except Exception as exc:  # noqa: BLE001 - the runner stopped and reaped the command first
            with _job_lock:
                job.state = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished_at = _utc_now_iso()
                job.proc = None
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

    def _work_and_restart() -> None:
        _work()
        if auto_restart and job.state == "succeeded" and job.restart_recommended:
            try:
                from . import host_control
                host_control.request_restart(by="online-update", reason="Update installed; automatic relaunch")
            except Exception as exc:
                with _job_lock:
                    job.restart_error = str(exc)
                    job.log.append(f"Installed successfully, but automatic restart failed: {exc}")

    _job_thread = threading.Thread(target=_work_and_restart, name="gateway-self-update", daemon=True)
    _job_thread.start()
    return job_status()


# ---------------------------------------------------------------------------
# Windows: the update hands off to its own PowerShell window
# ---------------------------------------------------------------------------
#
# The installer stops the gateway (Windows keeps a running program's files locked) and then
# ends, with `taskkill /T`, every process still running from the gateway's environment and
# its children. So the installer must run in a process that is neither the gateway nor a
# descendant of it: the gateway starts a short-lived launcher (Windows PowerShell, hidden),
# which starts the runner in a new window and exits at once. The runner's parent is then a
# process that is gone, and nothing the installer ends reaches it. The runner runs the checked
# install.ps1 in its window (its progress is visible), writes the exit code to a result file,
# and leaves the window open on failure. The gateway the installer starts again reads the
# record and the result: `windows_handoff_job`.


def _ps_literal(value: str) -> str:
    """A PowerShell single-quoted string: nothing inside is expanded."""
    return "'" + str(value).replace("'", "''") + "'"


def _windows_runner_script(*, handoff_id: str, command: List[str], result_file: Path, log_dir: Path,
                           before: str, after: str, close_after_s: int = 15, start_delay_s: int = 3,
                           health_url: Optional[str] = None, recover: Optional[List[str]] = None,
                           installer_sha256: Optional[str] = None) -> str:
    """The runner the update window executes. Written as UTF-8 WITH a byte-order mark (see
    _start_windows_handoff): Windows PowerShell 5.1 reads a script without one in the system
    code page, which would garble a data dir whose path is not ASCII (an account named José)."""
    exe, args = command[0], command[1:]
    arg_list = ", ".join(_ps_literal(a) for a in args)
    # After a failed run: the installer may have stopped the gateway before it failed (it stops
    # it before files change). Then the gateway is started again the way its Restart does, so a
    # failed update never leaves the person without it.
    recover_lines: List[str] = []
    if health_url and recover:
        health = health_url.rstrip("/") + "/api/health"
        recover_lines = [
            "        $up = $false",
            f"        try {{ $null = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri {_ps_literal(health)}; $up = $true }} catch {{ }}",
            "        if (-not $up) {",
            "            Write-Host 'AbstractGateway is not running: starting it again as it was.' -ForegroundColor Yellow",
            f"            try {{ Start-Process -FilePath {_ps_literal(recover[0])} -ArgumentList {_ps_literal(subprocess.list2cmdline(recover[1:]))} -WindowStyle Hidden }}"
            " catch { Write-Host ('Could not start it: ' + $_.Exception.Message) -ForegroundColor Red }",
            "        }",
        ]
    # The script that runs is the one whose sha256 the admin confirmed (checked again here,
    # just before it runs: the file sat on disk since the gateway wrote it).
    script_path = args[args.index("-File") + 1] if "-File" in args else None
    verify_lines: List[str] = []
    if installer_sha256 and script_path:
        verify_lines = [
            # .NET, not Get-FileHash: a Windows PowerShell started from PowerShell 7 can inherit a
            # module path where Get-FileHash fails to load.
            f"    $sha = [System.Security.Cryptography.SHA256]::Create()",
            f"    $hash = (($sha.ComputeHash([System.IO.File]::ReadAllBytes({_ps_literal(script_path)})) | ForEach-Object {{ $_.ToString('x2') }}) -join '')",
            f"    if ($hash -ne {_ps_literal(installer_sha256.lower())}) {{",
            "        Write-Host 'The installer file changed after it was checked: nothing was run. Check for updates again.' -ForegroundColor Red",
            "        $code = 3",
            "    } else {",
        ]
    return "\r\n".join([
        "# Written by AbstractGateway: runs the AbstractFramework installer for an update requested",
        "# from the tray or a console, then records its exit code for the gateway that comes back.",
        "$ErrorActionPreference = 'Continue'",
        f"try {{ $Host.UI.RawUI.WindowTitle = {_ps_literal('Updating AbstractFramework')} }} catch {{ }}",
        f"$resultFile = {_ps_literal(str(result_file))}",
        f"$logDir = {_ps_literal(str(log_dir))}",
        "Write-Host ''",
        f"Write-Host {_ps_literal(f'Updating {before} to {after}.')} -ForegroundColor White",
        "Write-Host 'AbstractGateway stops, is updated, and starts again by itself. Leave this window open; it closes when the update is done.'",
        "Write-Host ''",
        # Give the gateway time to answer the request that started this window.
        # Ctrl+C would stop the installer half way and skip the result: it is ignored here (the
        # window can still be closed; the gateway then reports that it did not hear back).
        "try { [Console]::TreatControlCAsInput = $true } catch { }",
        f"Start-Sleep -Seconds {int(start_delay_s)}",
        "$code = 1",
        "try {",
        *verify_lines,
        f"    & {_ps_literal(exe)} @({arg_list})",
        "    $code = [int]$LASTEXITCODE",
        *(["    }"] if verify_lines else []),
        "} catch {",
        "    Write-Host ('The installer could not be started: ' + $_.Exception.Message) -ForegroundColor Red",
        "    $code = 1",
        "} finally {",
        "    $result = [ordered]@{",
        f"        id = {_ps_literal(handoff_id)}",
        "        exit_code = $code",
        "        finished_at = [DateTime]::UtcNow.ToString('o')",
        "    }",
        "    try {",
        "        $json = $result | ConvertTo-Json -Compress",
        "        [System.IO.File]::WriteAllText($resultFile + '.tmp', $json, (New-Object System.Text.UTF8Encoding $false))",
        "        Move-Item -LiteralPath ($resultFile + '.tmp') -Destination $resultFile -Force",
        "    } catch {",
        "        Write-Host ('Could not record the result: ' + $_.Exception.Message) -ForegroundColor Yellow",
        "    }",
        "    if ($code -ne 0) {",
        *recover_lines,
        "    }",
        "}",
        "Write-Host ''",
        "if ($code -eq 0) {",
        f"    Write-Host {_ps_literal(f'Done: {after} is installed and AbstractGateway is running again. This window closes in {int(close_after_s)} seconds.')} -ForegroundColor Green",
        f"    Start-Sleep -Seconds {int(close_after_s)}",
        "} else {",
        "    Write-Host ('The update did not finish (installer exit code ' + $code + '). Its log is the newest install-*.log in ' + $logDir) -ForegroundColor Red",
        "    Write-Host 'AbstractGateway shows this in its tray and console once it runs again.'",
        "    try { Read-Host 'Press Enter to close this window' | Out-Null } catch { Start-Sleep -Seconds 600 }",
        "}",
        "exit $code",
        "",
    ])


def _default_windows_launcher(runner_script: Path, *, window_style: str = "Normal", env: Optional[Dict[str, str]] = None) -> None:
    """Start the runner in its own window, outside this process's tree (see above). Raises
    UpdateNotPossible when Windows did not start it."""
    import base64

    inner = (
        "Start-Process -FilePath 'powershell.exe' -ArgumentList @("
        "'-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "
        + _ps_literal('"' + str(runner_script) + '"')
        + ") -WindowStyle " + ("Hidden" if window_style == "Hidden" else "Normal")
    )
    encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded]
    no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    breakaway = int(getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000))
    group = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    last: Optional[BaseException] = None
    # Leave a job the gateway may run in (a job that ends its processes would end the window
    # too); a job that does not allow it refuses the flag, and the launch is tried without it.
    for flags in (no_window | group | breakaway, no_window | group):
        try:
            proc = subprocess.Popen(argv, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
        except OSError as exc:
            last = exc
            continue
        try:
            _, err = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise UpdateNotPossible("Windows PowerShell did not start the update window within a minute")
        if proc.returncode != 0:
            why = (err or b"").decode("utf-8", errors="replace").strip().splitlines()[-1:] or ["no error text"]
            raise UpdateNotPossible(f"Windows PowerShell could not open the update window (exit code {proc.returncode}: {why[0]})")
        return
    raise UpdateNotPossible(f"Windows PowerShell could not be started ({type(last).__name__}: {last})")


def _windows_handoff_env() -> Optional[Dict[str, str]]:
    """The environment the installer window gets: the gateway's, with the Hugging Face offline
    flags as the operator started it (host_control.relaunch_env, the same as a Restart)."""
    try:
        from . import host_control

        env = dict(host_control.relaunch_env()[0])
        # A gateway started from PowerShell 7 carries its module path; Windows PowerShell 5.1
        # (the update window, the installer) then loads PowerShell 7 modules and fails. Without
        # it, powershell.exe builds its own default.
        env.pop("PSModulePath", None)
        return env
    except Exception:
        return None


def _windows_recovery(data_dir: Path) -> Tuple[Optional[str], Optional[List[str]]]:
    """(the gateway's local address, the command that starts it again): a failed installer that
    had already stopped the gateway must not leave it stopped. In the gateway, its own Restart
    command (the arguments it was started with, so a login start keeps its tray); elsewhere
    (`abstractgateway update`, whose own arguments are not a gateway's), `serve` on this data
    dir and the port the gateway last served."""
    try:
        from . import host_control
        from .first_run import local_gateway, read_serve_record

        url = str(local_gateway(Path(data_dir))["url"])
        command = list(host_control.build_relaunch_command())
        if "serve" not in command[1:]:
            command = [host_control._relaunch_python(), "-m", "abstractgateway", "serve", "--data-dir", str(data_dir)]
            rec = read_serve_record(Path(data_dir)) or {}
            if rec.get("port"):
                command += ["--port", str(rec["port"])]
        return url, command
    except Exception:
        return None, None


def _protect_update_dir(folder: Path) -> None:
    """Windows: the update folder becomes this account's alone (and SYSTEM's), so no other
    local user can replace the scripts between writing and running (a data dir under C:\
    inherits write access for every authenticated user). Best effort: the runner still checks
    the installer's sha256 before it runs it."""
    if os.name != "nt":
        return
    try:
        out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=15, check=False)
        sid = (out.stdout or "").strip().split(",")[-1].strip().strip('"')
        if not sid.startswith("S-1-"):
            return
        subprocess.run(["icacls", str(folder), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F", "/grant:r", "*S-1-5-18:(OI)(CI)F"],
                       capture_output=True, text=True, timeout=30, check=False)
    except Exception:  # noqa: BLE001
        logger.warning("update: could not restrict %s to this account", folder, exc_info=True)


def _handoff_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "update"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _parse_iso(text: Any) -> Optional[float]:
    try:
        moment = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.timestamp()


def _start_windows_handoff(inst: InstallInfo, *, installer_sha256: Optional[str], fetch_release: Any, launcher: Any = None) -> Dict[str, Any]:
    """Windows: write the checked install.ps1 and its runner, record the hand-off, open the
    update window. The in-process job is `running` with `handoff` set (every client already
    follows a running job); nothing here waits for it: the installer stops this gateway."""
    global _job
    import uuid

    ddir = Path(str(inst.data_dir))
    in_progress = windows_handoff_job(ddir)
    if in_progress is not None and in_progress.get("state") == "running":
        raise UpdateJobBusy("an update is already running in its own window")
    folder = _handoff_dir(ddir)
    folder.mkdir(parents=True, exist_ok=True)
    if launcher is None:
        _protect_update_dir(folder)
    command, installer_meta, packages = _prepare_installer_run(inst, installer_sha256=installer_sha256, fetch_release=None)
    handoff_id = uuid.uuid4().hex[:12]
    result_file = folder / f"windows-update-{handoff_id}.result.json"
    log_dir = ddir / "logs"
    before = f"{FRAMEWORK_NAME} {inst.framework_version}" if inst.framework_version else f"AbstractGateway {inst.version}"
    target = installer_meta.get("release") if installer_meta else None
    after = f"{FRAMEWORK_NAME} {target}" if target else f"the newest {FRAMEWORK_NAME} release"
    for old in folder.glob("windows-update-*"):
        try:
            old.unlink()
        except OSError:
            pass
    runner = folder / f"windows-update-{handoff_id}.ps1"
    health_url, recover = _windows_recovery(ddir)
    runner.write_bytes(_windows_runner_script(handoff_id=handoff_id, command=command, result_file=result_file,
                                              log_dir=log_dir, before=before, after=after,
                                              health_url=health_url, recover=recover,
                                              installer_sha256=(installer_meta or {}).get("sha256")).encode("utf-8-sig"))
    started = _utc_now_iso()
    record = {
        "id": handoff_id,
        "started_at": started,
        "framework_before": inst.framework_version,
        "gateway_before": inst.version,
        "target": target,
        "installer": installer_meta,
        "command": list(command),
        "runner": str(runner),
        "result_file": str(result_file),
        "log_dir": str(log_dir),
    }
    _write_json_atomic(folder / WINDOWS_HANDOFF_FILE, record)
    try:
        if launcher is not None:
            launcher(runner)
        else:
            _default_windows_launcher(runner, env=_windows_handoff_env())
    except Exception as exc:
        # Nothing started: the record must not say an update runs.
        try:
            (folder / WINDOWS_HANDOFF_FILE).unlink()
        except OSError:
            pass
        if isinstance(exc, UpdateNotPossible):
            raise
        raise UpdateNotPossible(f"the update window could not be opened ({type(exc).__name__}: {exc})") from exc
    with _job_lock:
        _job = _Job(
            state="running",
            started_at=started,
            command=list(command),
            version_before=inst.version,
            installer=installer_meta,
            framework_before=inst.framework_version,
            handoff={"id": handoff_id, "log_dir": str(log_dir), "result_file": str(result_file), "data_dir": str(ddir)},
            message=(f"{after} is being installed in its own window: AbstractGateway stops, updates and starts "
                     "again by itself in a few minutes"),
        )
        _job.log.append(f"[gateway] the installer runs in its own PowerShell window: {' '.join(command)}")
        return _job.as_dict()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _installer_lock_held(data_dir: Path) -> bool:
    """The installer holds `<data dir>/update/install.lock/pid` while it runs (Lock-Install): a
    slow install (a GPU profile's downloads) is still running after the hand-off time limit."""
    try:
        text = (Path(data_dir) / "update" / "install.lock" / "pid").read_text(encoding="utf-8-sig").strip()
        return _pid_alive(int(re.sub(r"[^0-9]", "", text) or "0"))
    except (OSError, ValueError):
        return False


def windows_handoff_job(data_dir: Path, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The job view of the last Windows hand-off recorded in `data_dir` (None: no recent one).

    `running` while the installer has not reported (until WINDOWS_HANDOFF_TIMEOUT_S), then
    `completed` (exit 0; AbstractGateway already runs the new code: the installer started it)
    or `failed` (its exit code, or no result in time). Reported for WINDOWS_HANDOFF_REPORT_S."""
    folder = _handoff_dir(Path(data_dir))
    record = _read_json(folder / WINDOWS_HANDOFF_FILE)
    if not record or not record.get("id"):
        return None
    clock = float(now if now is not None else time.time())
    started = _parse_iso(record.get("started_at"))
    if started is None:
        return None
    result_path = Path(str(record.get("result_file") or folder / f"windows-update-{record['id']}.result.json"))
    result = _read_json(result_path)
    if result is not None and str(result.get("id") or "") != str(record["id"]):
        result = None
    job = _Job(
        started_at=str(record.get("started_at")),
        command=list(record.get("command") or []) or None,
        version_before=record.get("gateway_before"),
        installer=record.get("installer") if isinstance(record.get("installer"), dict) else None,
        framework_before=record.get("framework_before"),
        handoff={"id": record["id"], "log_dir": record.get("log_dir"), "result_file": str(result_path), "data_dir": str(data_dir)},
    )
    log_dir = str(record.get("log_dir") or Path(data_dir) / "logs")
    if result is None:
        if clock - started < WINDOWS_HANDOFF_TIMEOUT_S or _installer_lock_held(Path(data_dir)):
            job.state = "running"
            job.message = "the AbstractFramework installer is running in its own window"
            job.log.append("[gateway] the installer runs in its own PowerShell window")
            return job.as_dict()
        if clock - started > WINDOWS_HANDOFF_REPORT_S:
            return None
        job.state = "failed"
        job.error = (f"the update started at {record.get('started_at')} did not report back (its window was closed, or the "
                     f"computer restarted): the newest install-*.log in {log_dir} says how far it got. Check for updates again to retry.")
        return job.as_dict()
    finished = _parse_iso(result.get("finished_at")) or started
    if clock - finished > WINDOWS_HANDOFF_REPORT_S:
        return None
    job.finished_at = str(result.get("finished_at") or "")
    try:
        job.exit_code = int(result.get("exit_code"))
    except (TypeError, ValueError):
        job.exit_code = None
    state = read_installer_state(Path(data_dir)) or {}
    job.framework_after = state.get("FRAMEWORK_VERSION") or None
    job.version_after = installed_version()
    if job.exit_code == 0:
        job.state = "completed"
        if job.framework_after and job.framework_after != job.framework_before:
            job.changes.append({"name": FRAMEWORK_NAME, "from": job.framework_before, "to": job.framework_after})
        if job.version_after and job.version_after != job.version_before:
            job.changes.append({"name": DIST_NAME, "from": job.version_before, "to": job.version_after})
        what = f"{FRAMEWORK_NAME} {job.framework_after}" if job.framework_after else f"AbstractGateway {job.version_after}"
        job.message = (f"{what} is installed and running ({changes_text(job.changes, 0)})" if job.changes
                       else f"the installer finished; {what} was already installed")
        return job.as_dict()
    job.state = "failed"
    job.error = (f"the {FRAMEWORK_NAME} installer exited with code {job.exit_code}: the newest install-*.log in "
                 f"{log_dir} says why. Check for updates again to retry.")
    return job.as_dict()


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
    note = ""
    if state == "failed" and job.get("handoff") and chk.get("update_available"):
        # The last Windows update did not finish, and a check offers it again: offer it, say so.
        note = f" (the last update didn't finish: {job.get('error') or 'see the log'})"
        state = "idle"
    if state == "running" and job.get("handoff"):
        # Windows: the installer runs in its own window, stops this gateway and starts it again.
        status = "running"
        line = f"{base} · installing in its own window: {APP_GATEWAY} stops, updates and starts again by itself (a few minutes)"
    elif state == "running":
        status = "running"
        last = (job.get("log_tail") or [""])[-1]
        line = f"{base} · installing…" + (f" ({last})" if last else "")
    elif state == "completed" and not (check and check.get("update_available")):
        # Windows: the update this gateway was started by finished; nothing to restart.
        status = "up_to_date"
        line = f"{base} · updated: {job.get('message') or 'the installer finished'}"
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
        line = f"{base} · {offer} available{note}"
    else:
        status = "up_to_date"
        line = f"{base} · up to date"
    if status in ("available", "not_possible") and not inst.upgradable:
        hint = str(inst.reason or "")
        if inst.display_command:
            hint += f" — run: {inst.display_command}"
    elif installer and inst.windows:
        hint = (f"installed with the {FRAMEWORK_NAME} installer; Update runs it again in its own PowerShell window "
                "(the same script as the one-line install), which stops the gateway, updates everything and starts it again")
    elif installer:
        hint = f"installed with the {FRAMEWORK_NAME} installer; Update runs it again (the same script as the one-line install)"
    elif inst.kind == "editable":
        hint = "Updates install the published PyPI release in this environment. Your source checkout is left untouched."
    elif inst.kind:
        hint = f"installed with {inst.kind}"
    if status == "available":
        if installer and rel and inst.windows:
            ins = rel.get("installer") or {}
            command = " ".join(windows_installer_command(Path("install.ps1"), str(inst.data_dir)))
            source = f"{ins.get('url')} (commit {str(rel.get('commit') or '')[:12]}, sha256 {ins.get('sha256')})"
            confirm = (
                f"{FRAMEWORK_NAME} {rel['version']} (gateway {rel['gateway_version']}) is available; you have {installed} "
                f"(gateway {inst.version}). Update runs the {FRAMEWORK_NAME} installer, the script the one-line install "
                f"runs: {source}, as: {command}. It opens in its own PowerShell window and asks nothing. Windows keeps "
                f"a running program's files locked, so it stops {APP_GATEWAY} (running workflows pause and resume "
                f"afterwards), updates everything, and starts it again with start at login as it is; this takes a few "
                f"minutes, and the tray icon and the console are away meanwhile. Close the terminal console "
                f"(abstractgateway-console) first if it is open: the installer replaces it."
            )
            action = {
                "label": f"Update to {FRAMEWORK_NAME} {rel['version']}",
                "confirm": confirm,
                "command": command,
                "source": source,
                "installer_sha256": ins.get("sha256"),
            }
        elif installer and rel:
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
            if inst.kind == "editable":
                confirm += " This replaces the editable installation with the published PyPI package; your checkout files are not changed."
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
    job = job_status(data_dir=inst.data_dir if inst.windows else None)
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


def newest_release_app_pins() -> Tuple[Optional[str], Dict[str, str]]:
    """(release version, {app id: pinned version}) of the newest AbstractFramework release
    from the last update check (no request; empty until a check has run)."""
    with _check_lock:
        rel = (_last_check or {}).get("release") if isinstance(_last_check, dict) else None
    if not isinstance(rel, dict):
        return None, {}
    pins = rel.get("npm_apps") if isinstance(rel.get("npm_apps"), dict) else {}
    return (str(rel.get("version") or "") or None), {str(k): str(v) for k, v in pins.items()}

