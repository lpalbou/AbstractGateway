"""The tray's Apps submenu: which of the six AbstractFramework apps are on this
machine, and how to open them (2026-09-24).

Detection is by PRESENCE only — no app is ever imported (the gateway must not
depend on its apps; an import would also run their code in the tray):

- the five browser apps (Observer, Flow, Code, Continuum, Entity): the
  gateway-managed install first (`GET /api/gateway/apps`, mission O: install,
  launch, one-time signed-in handover), else a GLOBAL install the gateway
  does not manage — the app's own command on PATH (`abstractobserver`,
  `abstractflow-editor`, `abstractcode-web`, `abstractcontinuum`,
  `abstractentity`; NOT `abstractcode`, which is the terminal app) or the
  package under `npm root -g`;
- AbstractAssistant (a Python/Qt desktop app): an `AbstractAssistant.app`
  bundle in /Applications or ~/Applications (macOS), the `abstractassistant`
  console script (PATH or this Python's scripts folder), or
  `importlib.util.find_spec("abstractassistant")` in this Python — a spec
  WITHOUT an origin is a namespace package (a folder named like the package
  in the working directory, as in the framework checkout) and does not count.

Launching (a global browser app, or the Assistant) is a detached process whose
environment is scrubbed of tokens, secrets and keys the way mission O scrubs
the apps it runs.

Terminal apps (mission Y): an app row's `interfaces[]` entry of kind "tui"
(Code today) says whether its terminal version is on this machine — the
gateway found it by presence (its own copy, PATH, ~/.cargo/bin). When it is,
the menu offers "Open <app> in Terminal", which goes through the gateway's
`POST /api/gateway/apps/{id}/launch-tui` (a new terminal window, signed in
through a one-time handover) — the same path as the console's button.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..apps_manager import STACK_PORT, TUI_BY_APP

WEB_APPS: Tuple[Tuple[str, str, str, str, int, str], ...] = (
    # id, name, npm package, command on PATH, usual port, gateway-URL env the app reads.
    # Stack order and ports: apps_manager.STACK_PORTS (the one port table,
    # scripts/start-local.sh's port map).
    ("observer", "Observer", "@abstractframework/observer", "abstractobserver", STACK_PORT["observer"], "ABSTRACTOBSERVER_GATEWAY_URL"),
    ("continuum", "Continuum", "@abstractframework/continuum", "abstractcontinuum", STACK_PORT["continuum"], "ABSTRACTCONTINUUM_GATEWAY_URL"),
    ("code", "Code", "@abstractframework/code", "abstractcode-web", STACK_PORT["code"], "ABSTRACTCODE_GATEWAY_URL"),
    ("entity", "Entity", "@abstractframework/entity", "abstractentity", STACK_PORT["entity"], "ABSTRACTENTITY_GATEWAY_URL"),
    ("flow", "Flow", "@abstractframework/flow", "abstractflow-editor", STACK_PORT["flow"], "ABSTRACTFLOW_GATEWAY_URL"),
)
ASSISTANT_ID = "assistant"
ASSISTANT_NAME = "Assistant"
ASSISTANT_PACKAGE = "abstractassistant"
ASSISTANT_BUNDLE = "AbstractAssistant.app"
ASSISTANT_SCRIPT = "abstractassistant"
# Apps whose Install also installs a terminal app (apps_manager.TUI_BY_APP).
TERMINAL_APP_IDS: Tuple[str, ...] = tuple(TUI_BY_APP)


@dataclass(frozen=True)
class AppEntry:
    """One row of the Apps submenu (what the menu model renders)."""

    id: str
    name: str
    status: str  # running | starting | stopped | not_installed | installing | unknown | available (desktop app)
    source: str  # gateway | external (running, started outside the gateway) | path | npm-global | bundle | script | python | ""
    detail: str = ""  # human reason / where it was found
    url: Optional[str] = None
    install_available: bool = False
    install_blocked_reason: Optional[str] = None
    # The gateway said installs are off for THIS caller (payload
    # `install_allowed: false`): the menu says it once, at the bottom.
    installs_off: bool = False
    job_percent: Optional[float] = None
    last_error: Optional[str] = None
    found_by: Tuple[str, ...] = ()  # every presence signal seen (the detection matrix)
    # Terminal version (interfaces[kind=tui]): present on this machine, and
    # whether the gateway would open it here (and why not).
    tui_installed: bool = False
    tui_launch_available: bool = False
    tui_blocked_reason: Optional[str] = None


def tui_interface(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The row's `interfaces[]` entry of kind "tui", or None (no terminal
    version, or an older gateway without `interfaces`)."""
    if not isinstance(row, dict):
        return None
    for i in row.get("interfaces") or []:
        if isinstance(i, dict) and i.get("kind") == "tui":
            return i
    return None


def _with_tui(entry: "AppEntry", row: Optional[Dict[str, Any]]) -> "AppEntry":
    from dataclasses import replace

    t = tui_interface(row)
    if not t or not t.get("installed"):
        return entry
    return replace(entry, tui_installed=True, tui_launch_available=bool(t.get("launch_available")), tui_blocked_reason=t.get("launch_blocked_reason"))


@dataclass
class Probes:
    """Every OS touch point, injectable (tests pass fakes)."""

    which: Callable[[str], Optional[str]] = shutil.which
    exists: Callable[[str], bool] = os.path.exists
    find_spec: Callable[[str], Any] = importlib.util.find_spec
    npm_root: Callable[[], Optional[str]] = field(default=None)  # type: ignore[assignment]
    platform: str = sys.platform
    home: Path = field(default_factory=Path.home)
    scripts_dir: Optional[str] = field(default_factory=lambda: sysconfig.get_path("scripts"))
    read_text: Callable[[str], str] = lambda p: Path(p).read_text(encoding="utf-8")
    # (pid, argv) of this user's processes: is the Assistant running
    # (apps_desktop._process_argvs, psutil).
    processes: Callable[[], List[Tuple[int, List[str]]]] = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.npm_root is None:
            self.npm_root = _npm_root_global
        if self.processes is None:
            from ..apps_desktop import _process_argvs

            self.processes = _process_argvs


def _npm_root_global() -> Optional[str]:
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        cp = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=8, check=False)
    except Exception:
        return None
    out = (cp.stdout or "").strip()
    return out if cp.returncode == 0 and out else None


# ---------------------------------------------------------------------------
# Detection (pure over Probes)
# ---------------------------------------------------------------------------


def detect_global_web_app(app_id: str, probes: Probes, *, npm_root: Optional[str]) -> Dict[str, Any]:
    """{found_by: [...], launch: argv | None} for a web app the gateway does not manage."""
    spec = next(a for a in WEB_APPS if a[0] == app_id)
    found: List[str] = []
    launch: Optional[List[str]] = None
    cmd = probes.which(spec[3])
    if cmd:
        found.append(f"path:{cmd}")
        launch = [cmd]
    if npm_root:
        pkg_dir = Path(npm_root) / spec[2]
        pkg_json = pkg_dir / "package.json"
        if probes.exists(str(pkg_json)):
            found.append(f"npm-global:{pkg_dir}")
            if launch is None:
                try:
                    bins = json.loads(probes.read_text(str(pkg_json))).get("bin")
                except Exception:
                    bins = None
                rel = bins.get(spec[3]) if isinstance(bins, dict) else (bins if isinstance(bins, str) else None)
                node = probes.which("node")
                if rel and node:
                    launch = [node, str(pkg_dir / rel)]
    return {"found_by": found, "launch": launch}


def _is_real_spec(spec: Any) -> bool:
    """A regular or single-module install; a namespace portion (no origin) is not."""
    if spec is None:
        return False
    origin = getattr(spec, "origin", None)
    return bool(origin) and origin not in {"namespace", "built-in", "frozen"}


def _assistant_entry_point() -> Optional[str]:
    """`module:attr` of the `abstractassistant` console script, read from package
    metadata (no import)."""
    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group="console_scripts"):
            if ep.name == ASSISTANT_SCRIPT:
                return str(ep.value)
    except Exception:
        return None
    return None


def detect_assistant(probes: Probes, *, python: str = sys.executable, entry_point: Callable[[], Optional[str]] = _assistant_entry_point) -> Dict[str, Any]:
    """{found_by: [...], launch: argv | None, source, version, running, pid}.

    The ONE detection the console shares (apps_desktop.detect_assistant,
    mission LL): the tray only maps its own probes onto it. Preference for
    launching: the .app bundle (what a user installed), then the console
    script, then this Python running the console-script entry point."""
    from ..apps_desktop import DesktopProbes, detect_assistant as _detect

    dp = DesktopProbes(
        which=probes.which,
        exists=probes.exists,
        find_spec=probes.find_spec,
        platform=probes.platform,
        home=probes.home,
        script_dirs=[probes.scripts_dir] if probes.scripts_dir else [],
        python=python,
        entry_point=lambda _script: entry_point(),
        processes=probes.processes,
    )
    return _detect(dp)


# ---------------------------------------------------------------------------
# Entries (pure: gateway payload + detection → rows)
# ---------------------------------------------------------------------------


def build_app_entries(
    payload: Optional[Dict[str, Any]],
    payload_error: Optional[str],
    *,
    globals_found: Mapping[str, Dict[str, Any]],
    assistant: Optional[Dict[str, Any]],
    local_running: Mapping[str, str],
) -> Tuple[AppEntry, ...]:
    rows = {}
    installs_off = isinstance(payload, dict) and payload.get("install_allowed") is False
    if isinstance(payload, dict):
        for r in payload.get("apps") or []:
            if isinstance(r, dict) and r.get("id"):
                rows[str(r["id"])] = r
    out: List[AppEntry] = []
    for app_id, name, _pkg, _cmd, _port, _env in WEB_APPS:
        g = globals_found.get(app_id) or {}
        found_by = tuple(g.get("found_by") or ())
        row = rows.get(app_id)
        job = row.get("active_job") if row and isinstance(row.get("active_job"), dict) else None
        if row and row.get("installed"):
            found_by = ("gateway:" + str(row.get("version") or "?"),) + found_by
            st = str(row.get("status") or "")
            if job and str(job.get("state") or "") in {"queued", "running"}:
                out.append(AppEntry(app_id, name, "installing", "gateway", str(job.get("message") or "updating"), job_percent=job.get("percent"), found_by=found_by))
            elif row.get("running") and row.get("source") == "external":
                port = (row.get("external") or {}).get("port") or row.get("port")
                out.append(AppEntry(app_id, name, "running", "external", f"started outside the gateway on port {port}", url=row.get("url"), found_by=found_by))
            elif row.get("running"):
                out.append(AppEntry(app_id, name, "running", "gateway", f"running at {row.get('url')}", url=row.get("url"), found_by=found_by))
            elif st == "starting":
                out.append(AppEntry(app_id, name, "starting", "gateway", "starting", found_by=found_by))
            else:
                err = row.get("last_error")
                detail = "stopped" if st == "stopped" else st.replace("_", " ")
                out.append(AppEntry(app_id, name, "stopped", "gateway", detail, last_error=str(err) if err else None, found_by=found_by))
            continue
        if app_id in local_running:
            out.append(AppEntry(app_id, name, "running", "npm-global" if any(f.startswith("npm") for f in found_by) else "path", "started from this menu (global install)", url=local_running[app_id], found_by=found_by))
            continue
        if g.get("launch"):
            src = "path" if any(f.startswith("path:") for f in found_by) else "npm-global"
            out.append(AppEntry(app_id, name, "stopped", src, "installed globally, not managed by the gateway", found_by=found_by))
            continue
        if job and str(job.get("state") or "") in {"queued", "running"}:
            out.append(AppEntry(app_id, name, "installing", "gateway", str(job.get("message") or "installing"), job_percent=job.get("percent"), found_by=found_by))
            continue
        if row is None:
            reason = payload_error or "the gateway did not list it"
            out.append(AppEntry(app_id, name, "unknown", "", reason, found_by=found_by))
            continue
        out.append(
            AppEntry(
                app_id,
                name,
                "not_installed",
                "",
                "not installed",
                install_available=bool(row.get("install_available")),
                install_blocked_reason=row.get("install_blocked_reason"),
                installs_off=installs_off,
                found_by=found_by,
            )
        )
    # One entry per web app so far; each also carries its terminal version.
    out = [_with_tui(e, rows.get(e.id)) for e in out]
    # The Assistant (a desktop app): found on this machine by the detection
    # the console shares (apps_desktop.detect_assistant); installed through
    # the gateway's row (`kind: "desktop"`) when the gateway lists it.
    a = assistant or {}
    arow = rows.get(ASSISTANT_ID)
    ajob = arow.get("active_job") if arow and isinstance(arow.get("active_job"), dict) else None
    afound = tuple(a.get("found_by") or ())
    if ajob and str(ajob.get("state") or "") in {"queued", "running"}:
        out.append(AppEntry(ASSISTANT_ID, ASSISTANT_NAME, "installing", "gateway", str(ajob.get("message") or "installing"), job_percent=ajob.get("percent"), found_by=afound))
    elif a.get("launch"):
        out.append(AppEntry(ASSISTANT_ID, ASSISTANT_NAME, "running" if a.get("running") else "available", str(a.get("source") or ""), "desktop app", found_by=afound))
    else:
        out.append(
            AppEntry(
                ASSISTANT_ID,
                ASSISTANT_NAME,
                "not_installed",
                "",
                "not installed",
                install_available=bool(arow and arow.get("install_available")),
                install_blocked_reason=(arow or {}).get("install_blocked_reason"),
                installs_off=installs_off,
                found_by=afound,
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Launching (detached, scrubbed environment)
# ---------------------------------------------------------------------------


def scrubbed_env(base: Mapping[str, str]) -> Dict[str, str]:
    """Same rule as mission O's app servers: no gateway/core settings, no
    token, secret, password or key reaches a launched app."""
    out: Dict[str, str] = {}
    for k, v in base.items():
        ku = k.upper()
        if ku.startswith("ABSTRACTGATEWAY_") or ku.startswith("ABSTRACTCORE_"):
            continue
        if any(ku.endswith(s) for s in ("_TOKEN", "_SECRET", "_API_KEY", "_PASSWORD", "_KEY")):
            continue
        out[k] = v
    return out


def free_port(start: int, *, span: int = 100, host: str = "127.0.0.1") -> Optional[int]:
    for p in range(int(start), int(start) + span):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return None


def spawn_detached(argv: Sequence[str], *, env: Mapping[str, str], log_path: Optional[Path] = None) -> subprocess.Popen:
    """Start argv in its own session (no console window on Windows), output to
    `log_path` when given. Raises OSError when it cannot start at all."""
    out: Any = subprocess.DEVNULL
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        out = open(log_path, "ab")  # noqa: SIM115 - handed to the child
    kwargs: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT if log_path is not None else subprocess.DEVNULL, "env": dict(env), "close_fds": True}
    if os.name == "nt":
        flags = 0
        for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            flags |= int(getattr(subprocess, name, 0))
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(list(argv), **kwargs)
    finally:
        if log_path is not None:
            try:
                out.close()
            except Exception:
                pass


def web_app_spec(app_id: str) -> Tuple[str, str, str, str, int, str]:
    return next(a for a in WEB_APPS if a[0] == app_id)
