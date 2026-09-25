"""Supervises the desktop tray helper process (`python -m abstractgateway.tray`).

Why a child process and not an in-process icon: on macOS both AppKit (the
tray) and Tk (the Activity window) insist on the MAIN thread, which uvicorn
already owns; a GTK/AppKit crash must never take the HTTP server down; and a
separate process can honestly show "Starting…", "Restarting…" and
"Not responding" because it talks to the gateway only over loopback HTTP.

The decision to start is a small table (see `tray_decision`) evaluated once at
boot. Every entry in it is a FACT -- a runner-only process, `--reload`, no
desktop, no dependencies -- never a preference: while the gateway serves and
the desktop can hold an icon, the icon is there. Every "no" has a reason string
the console shows verbatim, and an install hint when the fix is
`pip install "abstractgateway[tray]"`.

Channels between the two processes (adversarial review 2026-09-05, A8/A10/A21):
- stdin  → the child: ONE JSON handshake line (base URL, the per-process
  ephemeral admin token, pids, data dir, version), then the pipe is KEPT OPEN
  for the child's lifetime. EOF is the child's liveness signal — robust to
  SIGKILL and to `execv` (the fd is close-on-exec) on every OS, and it lets
  the Windows child leave the shell cleanly (`NIM_DELETE`) instead of being
  `TerminateProcess`-ed into a ghost icon. Nothing secret touches argv or
  the environment.
- stdout ← the child: ONE readiness line (`{"ready": true}` or
  `{"ready": false, "reason": ..., "hint": ...}`) within a few seconds, so
  the boot banner never says "started" for a child that died at import.
- stderr → `<data_dir>/logs/tray.log` (the registered, purgeable logs home),
  because a `pythonw`/service parent has no usable stderr to inherit.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

TRAY_EXTRA_HINT = 'pip install "abstractgateway[tray]"'
TRAY_TOKEN_LABEL = "desktop-tray"
READY_TIMEOUT_S = 8.0
CRASH_LOOP_WINDOW_S = 10.0
TRAY_LOG_MAX_BYTES = 1_000_000


@dataclass(frozen=True)
class TrayDecision:
    start: bool
    reason: str
    hint: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"start": bool(self.start), "reason": self.reason, "hint": self.hint}


def tray_dependencies_available() -> tuple[bool, Optional[str]]:
    """(ok, problem). Import probes only — pystray picks a backend at import
    time on Linux (and opens X in the xorg fallback), so the REAL import
    happens in the child, which reports back through the readiness line."""
    try:
        import PIL  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"Pillow is not installed ({type(exc).__name__})"
    try:
        import importlib.util

        if importlib.util.find_spec("pystray") is None:
            return False, "pystray is not installed"
        if sys.platform.startswith("linux") and importlib.util.find_spec("gi") is None and importlib.util.find_spec("Xlib") is None:
            return False, "no tray backend: install the GTK/AppIndicator bindings (python3-gi + gir1.2-ayatanaappindicator3-0.1) or python-xlib"
    except Exception as exc:  # noqa: BLE001
        return False, f"pystray probe failed ({type(exc).__name__}: {exc})"
    return True, None


def _mac_gui_session() -> Optional[bool]:
    """True/False when Quartz can answer (a LaunchDaemon or an SSH login has
    no window-server session), None when Quartz is unavailable."""
    try:
        import Quartz  # type: ignore[import-not-found]

        return Quartz.CGSessionCopyCurrentDictionary() is not None
    except Exception:
        return None


def _windows_session_zero() -> Optional[bool]:
    try:
        import ctypes

        pid = ctypes.windll.kernel32.GetCurrentProcessId()  # type: ignore[attr-defined]
        session = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(session)):  # type: ignore[attr-defined]
            return session.value == 0
    except Exception:
        return None
    return None


def _linux_status_notifier_host() -> Optional[bool]:
    """Best-effort: is there a StatusNotifier host (KDE, GNOME with the
    AppIndicator extension, XFCE, Cinnamon…) on the session bus? None when
    the question cannot be asked (no gdbus)."""
    import shutil

    gdbus = shutil.which("gdbus")
    if not gdbus:
        return None
    try:
        p = subprocess.run(
            [gdbus, "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path", "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.NameHasOwner", "org.kde.StatusNotifierWatcher"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except Exception:
        return None
    if p.returncode != 0:
        return None
    return "true" in (p.stdout or "").lower()


def display_available(*, platform: str = sys.platform, env: Optional[Mapping[str, str]] = None, probes: Optional[Dict[str, Any]] = None) -> tuple[bool, Optional[str]]:
    """Best-effort "is there a desktop to put an icon on" check. `probes`
    lets tests inject the platform answers."""
    environ = os.environ if env is None else env
    pr = probes or {}
    if os.path.exists("/.dockerenv") or environ.get("ABSTRACTGATEWAY_CONTAINER"):
        return False, "running in a container"
    if environ.get("CI") or environ.get("PYTEST_CURRENT_TEST"):
        # Foreign, standard variables (not gateway knobs): a test run or a
        # CI job must never pop a tray on a developer's or a runner's desktop.
        return False, "test or CI environment"
    if platform.startswith("linux") or platform.startswith("freebsd"):
        if not (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")):
            return False, "no DISPLAY / WAYLAND_DISPLAY (headless session)"
        if environ.get("SSH_CONNECTION") or environ.get("SSH_TTY"):
            return False, "SSH session (a forwarded display has no tray)"
        sni = pr.get("sni") if "sni" in pr else _linux_status_notifier_host()
        if sni is False:
            return False, "no system tray on this desktop (GNOME needs the AppIndicator extension)"
        return True, None
    if platform == "darwin":
        if environ.get("SSH_CONNECTION") or environ.get("SSH_TTY"):
            return False, "SSH session (no window server)"
        gui = pr.get("mac_gui") if "mac_gui" in pr else _mac_gui_session()
        if gui is False:
            return False, "no window-server session (background daemon or locked login)"
        return True, None
    if platform.startswith("win"):
        zero = pr.get("win_session_zero") if "win_session_zero" in pr else _windows_session_zero()
        if zero is True or environ.get("ABSTRACTGATEWAY_SERVICE"):
            return False, "running as a Windows service (session 0 has no tray)"
        return True, None
    return False, f"unsupported platform {platform}"


def tray_decision(
    *,
    reload: bool = False,
    runner_only: bool = False,
    no_tray: bool = False,
    platform: str = sys.platform,
    env: Optional[Mapping[str, str]] = None,
    dependencies: Optional[tuple[bool, Optional[str]]] = None,
    probes: Optional[Dict[str, Any]] = None,
) -> TrayDecision:
    """Every reason to say no is a FACT about this process or this desktop --
    never a preference. The icon is the gateway's presence on the desktop:
    while it serves, it is there. No setting hides it, because the icon is the
    one entry point a non-technical user knows (the runtime config refuses the
    retired `desktop_tray` key with a plain message)."""

    if runner_only:
        return TrayDecision(False, "runner_only", "the tray belongs to the process that serves the console")
    if no_tray:
        # The one launch choice (a test or scratch gateway beside the usual
        # one): said at start, never a stored setting.
        return TrayDecision(False, "no_tray_flag", "started with --no-tray")
    if reload:
        return TrayDecision(False, "dev_reload", "`serve --reload` re-imports the app in a child that never runs main(); start without --reload")
    ok, problem = display_available(platform=platform, env=env, probes=probes)
    if not ok:
        return TrayDecision(False, "headless", problem)
    deps_ok, deps_problem = dependencies if dependencies is not None else tray_dependencies_available()
    if not deps_ok:
        return TrayDecision(False, "missing_dependency", f"{deps_problem}; install with {TRAY_EXTRA_HINT}")
    return TrayDecision(True, "ok", None)


def _child_python() -> str:
    """On Windows prefer pythonw.exe (no console window even without
    CREATE_NO_WINDOW quirks); elsewhere the serving interpreter."""
    exe = Path(sys.executable)
    if os.name == "nt":
        cand = exe.with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def _open_tray_log(data_dir: Path) -> Any:
    """Append handle for the child's stderr under the registered logs home;
    truncated when it grows past TRAY_LOG_MAX_BYTES. None = inherit."""
    try:
        path = Path(data_dir) / "logs" / "tray.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > TRAY_LOG_MAX_BYTES:
            path.write_text("", encoding="utf-8")
        return open(path, "ab")
    except Exception:
        return None


class TraySupervisor:
    """Owns at most one tray child. Thread-safe; every method is best-effort
    and reports through `status()` — the gateway never fails because of it."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._token: Optional[str] = None
        self._started_at: Optional[float] = None
        self._last_decision: Optional[TrayDecision] = None
        self._last_error: Optional[str] = None
        self._handshake: Dict[str, Any] = {}
        self._exit_code: Optional[int] = None
        self._ready: Optional[bool] = None
        self._failure: Optional[Dict[str, Any]] = None
        self._log_handle: Any = None
        self._recent_deaths: list[float] = []
        self._crash_looped = False

    # -- state ------------------------------------------------------------

    def _note_exit_locked(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is None:
            return
        if self._exit_code is None:
            self._exit_code = proc.returncode
            now = time.time()
            if self._started_at and (now - self._started_at) < CRASH_LOOP_WINDOW_S:
                self._recent_deaths.append(now)
                self._recent_deaths = [t for t in self._recent_deaths if (now - t) < 120.0]
                if len(self._recent_deaths) >= 2:
                    self._crash_looped = True
            self._revoke_token_locked()
            self._close_log_locked()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            self._note_exit_locked()
            running = self._proc is not None and self._proc.poll() is None
            return {
                "running": running,
                "ready": self._ready if running else None,
                "pid": self._proc.pid if running and self._proc is not None else None,
                "started_at": self._started_at,
                "exit_code": None if running else self._exit_code,
                "decision": self._last_decision.as_dict() if self._last_decision else None,
                "error": self._last_error,
                "failure": self._failure,
                "crash_looped": self._crash_looped,
                "base_url": self._handshake.get("base_url"),
                "log_path": str(Path(self._handshake["data_dir"]) / "logs" / "tray.log") if self._handshake.get("data_dir") else None,
            }

    def _revoke_token_locked(self) -> None:
        token = self._token
        self._token = None
        if token:
            try:
                from .security.gateway_security import revoke_ephemeral_loopback_token

                revoke_ephemeral_loopback_token(token)
            except Exception:
                pass

    def _close_log_locked(self) -> None:
        handle = self._log_handle
        self._log_handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    # -- lifecycle --------------------------------------------------------

    def start(self, *, base_url: str, data_dir: Path, version: str, decision: Optional[TrayDecision] = None, extra: Optional[Dict[str, Any]] = None, manual: bool = False) -> Dict[str, Any]:
        with self._lock:
            if decision is not None:
                self._last_decision = decision
                if not decision.start:
                    return self.status()
            self._note_exit_locked()
            if self._proc is not None and self._proc.poll() is None:
                return self.status()
            if self._crash_looped and not manual:
                self._last_error = "the tray helper crashed twice in a row; not restarting it automatically (see logs/tray.log; use Show in the console, or restart the gateway, to retry)"
                return self.status()
            if manual:
                self._crash_looped = False
                self._recent_deaths = []
            self._last_error = None
            self._exit_code = None
            self._ready = None
            self._failure = None
            self._revoke_token_locked()  # a crashed child's token must not outlive it
            token = secrets.token_urlsafe(32)
            try:
                from .security.gateway_security import register_ephemeral_loopback_token

                register_ephemeral_loopback_token(token, label=TRAY_TOKEN_LABEL)
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"could not register the tray token: {exc}"
                logger.warning("tray: %s", self._last_error)
                return self.status()
            handshake = {
                "base_url": str(base_url),
                "token": token,
                "parent_pid": os.getpid(),
                "data_dir": str(data_dir),
                "version": str(version),
                "console_path": "/console",
                **(extra or {}),
            }
            cmd = [_child_python(), "-m", "abstractgateway.tray", "--parent-pid", str(os.getpid())]
            log_handle = _open_tray_log(Path(data_dir))
            popen_kwargs: Dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": log_handle if log_handle is not None else None,
                "close_fds": True,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                proc = subprocess.Popen(cmd, **popen_kwargs)
                assert proc.stdin is not None
                proc.stdin.write((json.dumps(handshake) + "\n").encode("utf-8"))
                proc.stdin.flush()
                # stdin stays OPEN: EOF is the child's liveness signal.
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"could not start the tray helper: {type(exc).__name__}: {exc}"
                logger.warning("tray: %s", self._last_error)
                try:
                    from .security.gateway_security import revoke_ephemeral_loopback_token

                    revoke_ephemeral_loopback_token(token)
                except Exception:
                    pass
                if log_handle is not None:
                    try:
                        log_handle.close()
                    except Exception:
                        pass
                return self.status()
            self._proc = proc
            self._token = token
            self._log_handle = log_handle
            self._started_at = time.time()
            self._handshake = {k: v for k, v in handshake.items() if k != "token"}
            threading.Thread(target=self._read_ready, args=(proc,), name="gateway-tray-ready", daemon=True).start()
        # Wait (bounded) for the readiness line so the caller's banner is honest.
        deadline = time.monotonic() + READY_TIMEOUT_S
        while time.monotonic() < deadline:
            with self._lock:
                if self._ready is not None or (self._proc is proc and proc.poll() is not None):
                    break
            time.sleep(0.05)
        st = self.status()
        if st.get("running") and st.get("ready"):
            logger.info("tray helper started (pid %s)", proc.pid)
        return st

    def _read_ready(self, proc: subprocess.Popen) -> None:
        """First stdout line = readiness; the rest is drained and ignored."""
        try:
            assert proc.stdout is not None
            line = proc.stdout.readline()
            payload: Any = None
            if line:
                try:
                    payload = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    payload = None
            with self._lock:
                if self._proc is not proc:
                    return
                if isinstance(payload, dict) and payload.get("ready") is True:
                    self._ready = True
                elif isinstance(payload, dict):
                    self._ready = False
                    self._failure = {k: payload.get(k) for k in ("reason", "hint", "error") if payload.get(k) is not None}
                    self._last_error = str(payload.get("reason") or payload.get("error") or "the tray helper could not start")
                else:
                    self._ready = False
                    self._last_error = "the tray helper exited before reporting readiness (see logs/tray.log)"
            try:
                while proc.stdout.readline():
                    pass
            except Exception:
                pass
        except Exception:
            pass

    def stop(self, *, timeout_s: float = 3.0) -> Dict[str, Any]:
        with self._lock:
            proc = self._proc
            self._proc = None
            self._revoke_token_locked()
            self._close_log_locked()
        if proc is None:
            return self.status()
        if proc.poll() is None:
            # Polite first: closing stdin is the quit signal (the child removes
            # its icon and exits); then terminate, then kill.
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=max(0.1, float(timeout_s)))
            except Exception:
                try:
                    proc.terminate()
                    proc.wait(timeout=2.0)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=2.0)
                    except Exception:
                        pass
        with self._lock:
            self._exit_code = proc.returncode
        return self.status()


_supervisor = TraySupervisor()


def get_tray_supervisor() -> TraySupervisor:
    return _supervisor


# Serve-time facts the routes need to (re)start the helper after a setting
# change: the CLI records them once; a process that never called serve has
# none, and the routes say so.
_serve_context: Dict[str, Any] = {}


def record_serve_context(
    *, base_url: str, data_dir: Path, version: str, reload: bool, runner_only: bool, no_tray: bool = False
) -> None:
    _serve_context.update({"base_url": str(base_url), "data_dir": Path(data_dir), "version": str(version), "reload": bool(reload), "runner_only": bool(runner_only), "no_tray": bool(no_tray)})


def serve_context() -> Dict[str, Any]:
    return dict(_serve_context)


def tray_overview() -> Dict[str, Any]:
    """The console/tray-facing payload: supervisor state + what would happen
    if it were started now. No setting: there is none."""
    ctx = serve_context()
    if ctx:
        decision = tray_decision(reload=bool(ctx.get("reload")), runner_only=bool(ctx.get("runner_only")), no_tray=bool(ctx.get("no_tray")))
    else:
        decision = TrayDecision(False, "not_serving", "this process was not started by `abstractgateway serve`")
    deps_ok, deps_problem = tray_dependencies_available()
    return {
        "ok": True,
        "dependencies_installed": deps_ok,
        "dependencies_problem": deps_problem,
        "install_hint": TRAY_EXTRA_HINT,
        "decision": decision.as_dict(),
        "supervisor": _supervisor.status(),
        "can_control": bool(ctx),
    }
