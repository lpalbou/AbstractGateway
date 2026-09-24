"""Process-wide host controls: pause/resume execution, restart, shutdown.

One module, one state, consulted by EVERY GatewayRunner in this process (the
multi-user layout builds one runner per principal — a pause that only reached
one of them would be a lie on the tray). The state is deliberately tiny and
lock-guarded; the runner reads it on its hot path, so reads are a boolean
check, never I/O.

Pause persists to ``<data_dir>/gateway_paused.json`` so a restart (or an
update-then-restart) comes back paused: the person who paused a laptop to get
its GPU back must not find the gateway silently executing again after a
bounce. The tray and the console both show the state prominently, with the
resume action one click away. The file is ALSO the channel between processes
in the split layout (`serve --no-runner` + `abstractgateway runner`): the
runner process stats it every couple of seconds (`maybe_reload`) so a pause
written by the API process takes effect there too.

Restart/shutdown: the serving CLI registers the uvicorn ``Server`` here; a
request handler asks for a restart by setting a flag and ``should_exit`` — the
graceful shutdown uvicorn already implements (lifespan → runner drain → entity
close) runs unchanged, and ``cli.main`` re-launches the process after
``server.run()`` returned CLEANLY (never after a Ctrl-C or an exception, which
must mean "stop", not "bounce"). Nothing here ever kills the process directly.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import subprocess
import sys
import threading
import weakref
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

PAUSE_FILE_NAME = "gateway_paused.json"
PAUSE_RELOAD_INTERVAL_S = 2.0
# Tray/console-initiated restarts should feel like seconds, not minutes: open
# SSE tails (console ledger streams) are reconnectable, so the graceful drain
# for a REQUESTED bounce is bounded tighter than the operator's SIGTERM path.
REQUESTED_EXIT_GRACEFUL_S = 10


class HostControlError(RuntimeError):
    """A control request the process cannot honour (reported as HTTP 409)."""


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------

_state_lock = threading.RLock()
_paused = False
_paused_at: Optional[str] = None
_paused_by: Optional[str] = None
_pause_reason: Optional[str] = None
_pause_data_dir: Optional[Path] = None
_resume_listeners: "List[weakref.ref]" = []
_pause_file_seen: Optional[tuple] = None  # (mtime_ns, size) or None = absent
_pause_last_stat = 0.0


def pause_file_path(data_dir: Path) -> Path:
    return Path(data_dir) / PAUSE_FILE_NAME


def _stat_signature(path: Path) -> Optional[tuple]:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return (st.st_mtime_ns, st.st_size)


def _load_from_disk(path: Path) -> None:
    """Replace the in-memory pause state with the file's (caller holds the lock)."""
    global _paused, _paused_at, _paused_by, _pause_reason, _pause_file_seen
    _pause_file_seen = _stat_signature(path)
    raw: Any = None
    if _pause_file_seen is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - a torn file must not block boot
            logger.warning("host_control: ignoring unreadable %s: %s", path, exc)
            raw = None
    if isinstance(raw, dict) and bool(raw.get("paused")):
        _paused = True
        _paused_at = str(raw.get("paused_at") or "") or None
        _paused_by = str(raw.get("paused_by") or "") or None
        _pause_reason = str(raw.get("reason") or "") or None
    else:
        _paused = False
        _paused_at = _paused_by = _pause_reason = None


def configure(data_dir: Path) -> Dict[str, Any]:
    """Bind the pause state to a data dir and load a persisted pause.

    Called at boot (service.start_gateway_runner, so BOTH the serving process
    and a split `abstractgateway runner` process see it). Idempotent. Returns
    the pause snapshot after loading so the boot banner can say "PAUSED".
    """
    global _pause_data_dir, _pause_last_stat
    import time as _time

    with _state_lock:
        _pause_data_dir = Path(data_dir)
        _load_from_disk(pause_file_path(_pause_data_dir))
        _pause_last_stat = _time.monotonic()
        return pause_snapshot()


def maybe_reload(*, interval_s: float = PAUSE_RELOAD_INTERVAL_S, now: Optional[float] = None) -> bool:
    """Cheap cross-process sync: at most one `stat` per `interval_s`; reloads
    when the pause file appeared, vanished or changed. Returns True when the
    in-memory state changed. Safe to call from the runner loop every poll."""
    global _pause_last_stat
    import time as _time

    if _pause_data_dir is None:
        return False
    clock = float(now if now is not None else _time.monotonic())
    with _state_lock:
        if (clock - _pause_last_stat) < max(0.05, float(interval_s)):
            return False
        _pause_last_stat = clock
        path = pause_file_path(_pause_data_dir)
        sig = _stat_signature(path)
        if sig == _pause_file_seen:
            return False
        before = _paused
        _load_from_disk(path)
        changed = before != _paused
        listeners = [ref() for ref in _resume_listeners] if (changed and not _paused) else []
    if changed:
        logger.warning("gateway execution %s (pause file changed on disk)", "PAUSED" if _paused else "RESUMED")
    for fn in listeners:
        if fn is not None:
            try:
                fn()
            except Exception:  # noqa: BLE001
                logger.debug("host_control: resume listener failed", exc_info=True)
    return changed


def is_paused() -> bool:
    # Unlocked read of a bool: the runner calls this every scheduling pass.
    return _paused


def step_gate() -> bool:
    """The gate handed to ``Runtime.tick(step_gate=...)``: open unless paused."""
    return not _paused


def pause_snapshot() -> Dict[str, Any]:
    with _state_lock:
        return {
            "paused": bool(_paused),
            "paused_at": _paused_at,
            "paused_by": _paused_by,
            "reason": _pause_reason,
        }


def _persist_pause_state() -> None:
    """Best-effort atomic write of the pause file (a missing file = not paused)."""
    global _pause_file_seen
    if _pause_data_dir is None:
        return
    path = pause_file_path(_pause_data_dir)
    try:
        if not _paused:
            if path.exists():
                path.unlink()
            _pause_file_seen = None
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(pause_snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        _pause_file_seen = _stat_signature(path)
    except Exception as exc:  # noqa: BLE001 - persistence is a courtesy, the in-memory state rules
        logger.warning("host_control: could not persist pause state to %s: %s", path, exc)


def pause(*, by: str = "operator", reason: Optional[str] = None) -> Dict[str, Any]:
    """Pause execution process-wide. Idempotent (re-pausing keeps the first timestamp)."""
    global _paused, _paused_at, _paused_by, _pause_reason
    with _state_lock:
        if not _paused:
            _paused = True
            _paused_at = _utc_now_iso()
            _paused_by = str(by or "operator")
            _pause_reason = str(reason).strip() if isinstance(reason, str) and reason.strip() else None
            _persist_pause_state()
            logger.warning("gateway execution PAUSED by %s%s", _paused_by, f" ({_pause_reason})" if _pause_reason else "")
        return pause_snapshot()


def resume(*, by: str = "operator") -> Dict[str, Any]:
    """Resume execution process-wide and nudge every registered runner."""
    global _paused, _paused_at, _paused_by, _pause_reason
    with _state_lock:
        was_paused = _paused
        _paused = False
        _paused_at = None
        _paused_by = None
        _pause_reason = None
        _persist_pause_state()
        listeners = [ref() for ref in _resume_listeners]
    if was_paused:
        logger.warning("gateway execution RESUMED by %s", by)
    for fn in listeners:
        if fn is None:
            continue
        try:
            fn()
        except Exception:  # noqa: BLE001 - one broken listener must not stop the others
            logger.debug("host_control: resume listener failed", exc_info=True)
    return pause_snapshot()


def add_resume_listener(fn: Callable[[], None]) -> None:
    """Register a callable invoked after resume (weakly held: a stopped
    runner's bound method disappears with the runner)."""
    try:
        ref: Any = weakref.WeakMethod(fn) if hasattr(fn, "__self__") else weakref.ref(fn)
    except TypeError:
        ref = lambda fn=fn: fn  # noqa: E731 - plain functions/lambdas are not weak-referenceable in every case
    with _state_lock:
        _resume_listeners[:] = [r for r in _resume_listeners if r() is not None]
        _resume_listeners.append(ref)


def paused_warning() -> Optional[str]:
    """Run-start warning text while paused (None when not paused)."""
    if not _paused:
        return None
    return (
        "gateway execution is paused: the run is accepted and queued, and will start "
        "when execution is resumed (system tray, or Console → Resources → Resume)."
    )


# ---------------------------------------------------------------------------
# Restart / shutdown
# ---------------------------------------------------------------------------

_server: Any = None
_server_restartable = False
_server_restart_block_reason: Optional[str] = None
_restart_requested = False
_shutdown_requested = False
_clean_exit = False
_request_by: Optional[str] = None
_request_reason: Optional[str] = None
_relaunch_argv: Optional[List[str]] = None
_update_job_running: Callable[[], bool] = lambda: False  # noqa: E731 - injected by self_update


def set_update_job_probe(fn: Callable[[], bool]) -> None:
    """self_update tells us how to ask "is an upgrade running right now?" —
    a restart in the middle of a pip run orphans pip and imports half of a
    package, so restart/shutdown refuse while it is."""
    global _update_job_running
    _update_job_running = fn


def _relaunch_python(executable: Optional[str] = None) -> str:
    """The interpreter for the NEW server: on Windows a `pythonw.exe` parent
    would give the relaunched server a `sys.stderr` of None (every boot line
    would raise) — prefer the console `python.exe` beside it."""
    exe = Path(str(executable or sys.executable))
    if os.name == "nt" and exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.exists():
            return str(cand)
    return str(exe)


def validate_relaunch(*, executable: Optional[str] = None) -> Optional[str]:
    """None when this process can relaunch itself; else the reason it cannot."""
    exe = _relaunch_python(executable)
    if not exe or not Path(exe).is_file():
        return f"the running interpreter is not a file on disk ({exe!r}); cannot relaunch"
    try:
        import importlib.util

        if importlib.util.find_spec("abstractgateway.__main__") is None:
            return "`python -m abstractgateway` is not importable from this interpreter; cannot relaunch"
    except Exception as exc:  # noqa: BLE001
        return f"could not verify the relaunch entry point: {exc}"
    return None


def register_server(
    server: Any,
    *,
    restartable: bool,
    block_reason: Optional[str] = None,
    relaunch_argv: Optional[List[str]] = None,
    executable: Optional[str] = None,
) -> None:
    """The serving CLI hands over its uvicorn Server (or any object with a
    ``should_exit`` attribute) plus whether this process can re-launch itself.
    `restartable=True` is downgraded (with the reason) when the relaunch
    command cannot possibly work."""
    global _server, _server_restartable, _server_restart_block_reason, _restart_requested, _shutdown_requested
    global _relaunch_argv, _clean_exit
    reason = block_reason
    if restartable:
        problem = validate_relaunch(executable=executable)
        if problem:
            restartable = False
            reason = problem
    with _state_lock:
        _server = server
        _server_restartable = bool(restartable)
        _server_restart_block_reason = reason
        _restart_requested = False
        _shutdown_requested = False
        _clean_exit = False
        _relaunch_argv = [_relaunch_python(executable), "-m", "abstractgateway", *list(relaunch_argv)] if relaunch_argv is not None else None


def unregister_server() -> None:
    global _server, _server_restartable, _server_restart_block_reason
    with _state_lock:
        _server = None
        _server_restartable = False
        _server_restart_block_reason = None


def mark_clean_exit() -> None:
    """The CLI calls this right after `server.run()` RETURNED (no signal
    re-raise, no exception): only then may a requested restart relaunch."""
    global _clean_exit
    with _state_lock:
        _clean_exit = True


def clear_requests() -> None:
    """An unclean exit (Ctrl-C, exception) must never turn into a relaunch."""
    global _restart_requested, _shutdown_requested
    with _state_lock:
        _restart_requested = False
        _shutdown_requested = False


def control_capabilities() -> Dict[str, Any]:
    """What this process can do to itself — the tray greys out what it cannot."""
    with _state_lock:
        if _server is None:
            return {
                "restart": False,
                "shutdown": False,
                "reason": "this process was not started by `abstractgateway serve` (no server handle registered)",
            }
        busy = False
        try:
            busy = bool(_update_job_running())
        except Exception:
            busy = False
        return {
            "restart": bool(_server_restartable) and not busy,
            "shutdown": not busy,
            "reason": (
                "an update is being installed; restart when it finishes"
                if busy
                else (None if _server_restartable else _server_restart_block_reason)
            ),
            "restart_requested": bool(_restart_requested),
            "shutdown_requested": bool(_shutdown_requested),
            "update_job_running": busy,
        }


def _signal_server_exit() -> None:
    server = _server
    if server is None:
        raise HostControlError("no server handle registered: this process cannot stop itself")
    # Bound the requested drain (see REQUESTED_EXIT_GRACEFUL_S); uvicorn reads
    # this at shutdown time, so setting it now takes effect for this exit.
    try:
        cfg = getattr(server, "config", None)
        if cfg is not None and hasattr(cfg, "timeout_graceful_shutdown"):
            cfg.timeout_graceful_shutdown = REQUESTED_EXIT_GRACEFUL_S
    except Exception:
        pass
    try:
        server.should_exit = True
    except Exception as exc:  # noqa: BLE001
        raise HostControlError(f"could not signal the server to exit: {exc}") from exc


def _refuse_if_update_running() -> None:
    try:
        busy = bool(_update_job_running())
    except Exception:
        busy = False
    if busy:
        raise HostControlError("an update is being installed right now; restart when it finishes")


def request_restart(*, by: str = "operator", reason: Optional[str] = None) -> Dict[str, Any]:
    """Schedule a graceful restart. Raises HostControlError when unsupported."""
    global _restart_requested, _request_by, _request_reason
    with _state_lock:
        if _server is None:
            raise HostControlError(str(control_capabilities().get("reason")))
        if not _server_restartable:
            raise HostControlError(_server_restart_block_reason or "restart is not supported in this launch mode")
        if _shutdown_requested:
            raise HostControlError("a shutdown is already in progress")
        _refuse_if_update_running()
        _restart_requested = True
        _request_by = str(by or "operator")
        _request_reason = str(reason).strip() if isinstance(reason, str) and reason.strip() else None
        logger.warning("gateway RESTART requested by %s%s", _request_by, f" ({_request_reason})" if _request_reason else "")
        _signal_server_exit()
        return {"ok": True, "restart": True, "requested_by": _request_by, "reason": _request_reason}


def request_shutdown(*, by: str = "operator", reason: Optional[str] = None) -> Dict[str, Any]:
    global _shutdown_requested, _restart_requested, _request_by, _request_reason
    with _state_lock:
        if _server is None:
            raise HostControlError(str(control_capabilities().get("reason")))
        _refuse_if_update_running()
        _shutdown_requested = True
        _restart_requested = False
        _request_by = str(by or "operator")
        _request_reason = str(reason).strip() if isinstance(reason, str) and reason.strip() else None
        logger.warning("gateway SHUTDOWN requested by %s%s", _request_by, f" ({_request_reason})" if _request_reason else "")
        _signal_server_exit()
        return {"ok": True, "shutdown": True, "requested_by": _request_by, "reason": _request_reason}


def restart_requested() -> bool:
    return bool(_restart_requested)


def shutdown_requested() -> bool:
    return bool(_shutdown_requested)


def should_relaunch() -> bool:
    """True only for a requested restart AFTER a clean server return."""
    with _state_lock:
        return bool(_restart_requested and _clean_exit)


def build_relaunch_command(*, argv: Optional[List[str]] = None, executable: Optional[str] = None) -> List[str]:
    """The command that re-creates THIS process: ``python -m abstractgateway <args>``.

    ``-m`` is used instead of the console script because the script's path
    differs per launcher (pip's ``.exe`` shim on Windows, pipx/uv tool
    venvs) while ``sys.executable`` is always the interpreter that runs the
    package — the same environment, the same installed version.
    """
    if _relaunch_argv and argv is None and executable is None:
        return list(_relaunch_argv)
    args = list(sys.argv[1:] if argv is None else argv)
    return [_relaunch_python(executable), "-m", "abstractgateway", *args]


# The Hugging Face offline trio as found when THIS module was imported. A
# relaunch must hand the new process the environment the operator started the
# gateway with, not flags written in-process since (a library's load-time
# override, the old MLX / HF-provider import writes): the new process records
# its start-up values as the OPERATOR's choice
# (`abstractcore.config.manager.operator_hf_offline_env`), and an
# operator-set HF_HUB_OFFLINE=1 refuses every explicit download by name.
_HF_OFFLINE_ENV_NAMES = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE")
_START_HF_OFFLINE_ENV: Dict[str, Optional[str]] = {name: os.environ.get(name) for name in _HF_OFFLINE_ENV_NAMES}


def _operator_hf_offline_env() -> Dict[str, Optional[str]]:
    """The operator's pre-start values of the offline trio (None = unset).

    Two start-up snapshots can exist: AbstractCore's (taken when
    `abstractcore.config.manager` was imported) and this module's. A value
    counts as the operator's only when every snapshot taken saw it -- a flag
    one of them missed was written in-process before the other was taken.
    AbstractCore's snapshot is read only if that module is ALREADY imported:
    importing it now would snapshot the current, possibly polluted, environment.
    """
    snapshots = [dict(_START_HF_OFFLINE_ENV)]
    core_manager = sys.modules.get("abstractcore.config.manager")
    reader = getattr(core_manager, "operator_hf_offline_env", None) if core_manager is not None else None
    if callable(reader):
        snapshots.append(dict(reader()))
    out: Dict[str, Optional[str]] = {}
    for name in _HF_OFFLINE_ENV_NAMES:
        values = {snap.get(name) for snap in snapshots}
        out[name] = values.pop() if len(values) == 1 else None
    return out


def relaunch_env(base: Optional[Dict[str, str]] = None) -> tuple:
    """`(env, changed)` for the relaunched gateway: `base` (default the live
    `os.environ`) with the HF offline trio reset to the operator's pre-start
    values (`_operator_hf_offline_env`). `changed` maps each variable whose
    live value was dropped or restored to `"<live> -> <relaunch>"`, where
    `<unset>` marks an absent variable; the caller logs it. Every other
    variable passes through unchanged.
    """
    env = dict(os.environ if base is None else base)
    changed: Dict[str, str] = {}
    for name, operator_value in _operator_hf_offline_env().items():
        live = env.pop(name, None)
        if operator_value is not None:
            env[name] = operator_value
        if live != operator_value:
            changed[name] = f"{'<unset>' if live is None else live} -> {'<unset>' if operator_value is None else operator_value}"
    return env, changed


def relaunch_process(command: Optional[List[str]] = None) -> None:
    """Replace this process with a fresh gateway (POSIX ``execve``) or spawn
    one and exit (Windows, where exec is emulated as spawn+terminate anyway
    and would detach the console). Never returns.

    The new process gets `relaunch_env()`: the live environment, except that
    the Hugging Face offline trio is reset to what the operator started this
    gateway with -- an offline flag written in-process during the run must not
    become the next process's "operator-set" environment. What was reset is
    logged by name."""
    cmd = list(command or build_relaunch_command())
    env, changed = relaunch_env()
    logger.warning("gateway relaunching: %s", " ".join(cmd))
    if changed:
        logger.warning(
            "gateway relaunch: Hugging Face offline variables reset to their values at start (set in-process during the run): %s",
            ", ".join(f"{k}: {v}" for k, v in sorted(changed.items())),
        )
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    if os.name == "nt":
        subprocess.Popen(cmd, close_fds=True, env=env)
        os._exit(0)
    os.execve(cmd[0], cmd, env)


def _reset_for_tests() -> None:
    """Test hook: clear every piece of module state."""
    global _paused, _paused_at, _paused_by, _pause_reason, _pause_data_dir, _resume_listeners
    global _server, _server_restartable, _server_restart_block_reason, _restart_requested, _shutdown_requested
    global _request_by, _request_reason, _relaunch_argv, _clean_exit, _pause_file_seen, _pause_last_stat, _update_job_running
    with _state_lock:
        _paused = False
        _paused_at = _paused_by = _pause_reason = None
        _pause_data_dir = None
        _pause_file_seen = None
        _pause_last_stat = 0.0
        _resume_listeners = []
        _server = None
        _server_restartable = False
        _server_restart_block_reason = None
        _restart_requested = _shutdown_requested = False
        _clean_exit = False
        _request_by = _request_reason = None
        _relaunch_argv = None
        _update_job_running = lambda: False  # noqa: E731
