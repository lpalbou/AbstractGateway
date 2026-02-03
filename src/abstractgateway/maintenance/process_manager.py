from __future__ import annotations

import datetime
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _ts_compact_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _is_pid_running(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _default_shell() -> str:
    return str(os.environ.get("SHELL") or "/bin/bash")


@dataclass(frozen=True)
class ProcessSpec:
    id: str
    label: str
    kind: str = "service"  # service|task|self
    description: Optional[str] = None
    cwd: str = "."
    command: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    url: Optional[str] = None

    def validate(self) -> None:
        pid = str(self.id or "").strip()
        if not pid or not _SAFE_ID_RE.match(pid):
            raise ValueError(f"Invalid process id: {self.id!r}")
        if self.kind not in {"service", "task", "self"}:
            raise ValueError(f"Invalid process kind: {self.kind!r}")
        if self.kind != "self":
            if not isinstance(self.command, list) or not self.command or not all(isinstance(x, str) and x.strip() for x in self.command):
                raise ValueError(f"Invalid command for process {pid!r}")


def default_process_specs(*, repo_root: Path) -> Dict[str, ProcessSpec]:
    """Default managed processes for the monorepo dev topology."""
    return {
        "gateway": ProcessSpec(
            id="gateway",
            label="AbstractGateway (this process)",
            kind="self",
            description="Gateway API + (optional) runner. Supports restart/redeploy.",
            cwd=".",
            command=[],
            url=None,
        ),
        "gateway_uat": ProcessSpec(
            id="gateway_uat",
            label="AbstractGateway (UAT)",
            kind="service",
            description="Gateway running candidate code from untracked/backlog_exec_uat/current.",
            cwd=".",
            command=[_default_shell(), "-lc", "./agw-uat.sh"],
            url="http://localhost:8082",
        ),
        "build": ProcessSpec(
            id="build",
            label="build.sh (deps install)",
            kind="task",
            description="Runs the repo build script (pip/npm installs).",
            cwd=".",
            command=[_default_shell(), "-lc", "./build.sh"],
        ),
        "abstractobserver": ProcessSpec(
            id="abstractobserver",
            label="AbstractObserver (web)",
            kind="service",
            description="Vite dev server.",
            cwd="abstractobserver",
            command=["npm", "run", "dev"],
            url="http://localhost:3001",
        ),
        "abstractobserver_uat": ProcessSpec(
            id="abstractobserver_uat",
            label="AbstractObserver (web, UAT)",
            kind="service",
            description="Vite dev server from untracked/backlog_exec_uat/current.",
            cwd=".",
            command=[_default_shell(), "-lc", "./aobs-uat.sh"],
            url="http://localhost:3011",
        ),
        "abstractcode_web": ProcessSpec(
            id="abstractcode_web",
            label="AbstractCode Web",
            kind="service",
            description="Vite dev server.",
            cwd="abstractcode/web",
            command=["npm", "run", "dev"],
            url="http://localhost:3002",
        ),
        "abstractcode_web_uat": ProcessSpec(
            id="abstractcode_web_uat",
            label="AbstractCode Web (UAT)",
            kind="service",
            description="Vite dev server from untracked/backlog_exec_uat/current.",
            cwd=".",
            command=[_default_shell(), "-lc", "./acode-web-uat.sh"],
            url="http://localhost:3012",
        ),
        "abstractflow_frontend": ProcessSpec(
            id="abstractflow_frontend",
            label="AbstractFlow Web (frontend)",
            kind="service",
            description="Vite dev server.",
            cwd="abstractflow/web/frontend",
            command=["npm", "run", "dev"],
            url="http://localhost:3003",
        ),
        "abstractflow_backend": ProcessSpec(
            id="abstractflow_backend",
            label="AbstractFlow Web (backend)",
            kind="service",
            description="FastAPI backend (uvicorn).",
            cwd="abstractflow/web",
            command=[sys.executable, "-m", "backend", "--host", "0.0.0.0", "--port", "8080", "--reload"],
            url="http://localhost:8080",
        ),
    }


def _load_specs_from_path(*, repo_root: Path, config_path: Path) -> Dict[str, ProcessSpec]:
    raw = config_path.read_text(encoding="utf-8", errors="replace")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("process manager config must be a JSON object")
    processes = obj.get("processes")
    if not isinstance(processes, list):
        raise ValueError("process manager config must contain 'processes' (list)")

    out: Dict[str, ProcessSpec] = {}
    for p in processes:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip()
        if not pid or not _SAFE_ID_RE.match(pid):
            raise ValueError(f"Invalid process id in config: {pid!r}")
        label = str(p.get("label") or pid).strip() or pid
        kind = str(p.get("kind") or "service").strip().lower() or "service"
        cwd_raw = p.get("cwd")
        cwd = str(cwd_raw if cwd_raw is not None else ".").strip() or "."
        # Security guardrail: treat cwd as repo-relative to avoid arbitrary host path execution.
        if os.path.isabs(cwd):
            raise ValueError(f"Process {pid!r} cwd must be relative to repo_root")

        cmd = p.get("command")
        command: List[str] = []
        if isinstance(cmd, list):
            command = [str(x) for x in cmd if isinstance(x, (str, int, float)) and str(x).strip()]
        elif isinstance(cmd, str) and cmd.strip():
            command = [cmd.strip()]

        env_raw = p.get("env")
        env: Dict[str, str] = {}
        if isinstance(env_raw, dict):
            for k, v in env_raw.items():
                ks = str(k or "").strip()
                if not ks:
                    continue
                env[ks] = str(v if v is not None else "")

        spec = ProcessSpec(
            id=pid,
            label=label,
            kind=kind,
            description=str(p.get("description") or "").strip() or None,
            cwd=cwd,
            command=command,
            env=env,
            url=str(p.get("url") or "").strip() or None,
        )
        spec.validate()
        out[pid] = spec

    # Always include the gateway self entry so UIs can restart/redeploy.
    out.setdefault(
        "gateway",
        ProcessSpec(
            id="gateway",
            label="AbstractGateway (this process)",
            kind="self",
            description="Gateway API + (optional) runner. Supports restart/redeploy.",
            cwd=".",
            command=[],
        ),
    )
    return out


def load_process_specs(*, repo_root: Path) -> Dict[str, ProcessSpec]:
    cfg_path = str(os.getenv("ABSTRACTGATEWAY_PROCESS_MANAGER_CONFIG") or "").strip()
    if not cfg_path:
        specs = default_process_specs(repo_root=repo_root)
        for s in specs.values():
            s.validate()
        return specs
    path = Path(cfg_path).expanduser().resolve()
    return _load_specs_from_path(repo_root=repo_root, config_path=path)


class ProcessManager:
    def __init__(self, *, base_dir: Path, repo_root: Path, specs: Dict[str, ProcessSpec]):
        self._base_dir = Path(base_dir).expanduser().resolve()
        self._repo_root = Path(repo_root).expanduser().resolve()
        self._specs = dict(specs)
        self._lock = threading.Lock()
        self._procs: Dict[str, subprocess.Popen[bytes]] = {}

        self._state_dir = (self._base_dir / "process_manager").resolve()
        self._logs_dir = (self._base_dir / "process_logs").resolve()
        self._state_path = (self._state_dir / "state.json").resolve()

        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._logs_dir.mkdir(parents=True, exist_ok=True)

        self._state: Dict[str, Dict[str, Any]] = self._load_state()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def repo_root(self) -> Path:
        return self._repo_root

    # ----------------------------
    # State I/O
    # ----------------------------

    def _load_state(self) -> Dict[str, Dict[str, Any]]:
        if not self._state_path.exists():
            return {}
        try:
            raw = self._state_path.read_text(encoding="utf-8", errors="replace")
            obj = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(obj, dict):
            return {}
        procs = obj.get("processes")
        if not isinstance(procs, dict):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for k, v in procs.items():
            pid = str(k or "").strip()
            if not pid or not _SAFE_ID_RE.match(pid) or not isinstance(v, dict):
                continue
            out[pid] = dict(v)
        return out

    def _save_state(self) -> None:
        tmp = self._state_path.with_suffix(".tmp")
        obj = {"version": 1, "updated_at": _now_utc_iso(), "processes": self._state}
        data = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(self._state_path)

    # ----------------------------
    # Public API
    # ----------------------------

    def list_processes(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._refresh_states_locked()
            out: List[Dict[str, Any]] = []
            for pid, spec in sorted(self._specs.items(), key=lambda kv: kv[0]):
                st = dict(self._state.get(pid) or {})
                info = {
                    "id": pid,
                    "label": spec.label,
                    "kind": spec.kind,
                    "description": spec.description,
                    "cwd": spec.cwd,
                    "command": list(spec.command) if spec.kind != "self" else [],
                    "url": spec.url,
                    "status": str(st.get("status") or ("running" if spec.kind == "self" else "stopped")),
                    "pid": st.get("pid"),
                    "started_at": st.get("started_at"),
                    "stopped_at": st.get("stopped_at"),
                    "exit_code": st.get("exit_code"),
                    "log_relpath": st.get("log_relpath"),
                    "last_error": st.get("last_error"),
                    "actions": self._actions_for_spec(spec),
                }
                if spec.kind == "self":
                    info["pid"] = os.getpid()
                    info["status"] = "running"
                out.append(info)
            return out

    def start(self, process_id: str) -> Dict[str, Any]:
        pid = str(process_id or "").strip()
        if not pid or pid not in self._specs:
            raise KeyError(f"Unknown process id: {pid}")
        spec = self._specs[pid]
        if spec.kind == "self":
            raise ValueError("Cannot start a self-managed process")

        with self._lock:
            self._refresh_one_locked(pid)
            st = dict(self._state.get(pid) or {})
            if str(st.get("status") or "").strip().lower() == "running" and isinstance(st.get("pid"), int):
                return dict(st)

            ts = _ts_compact_utc()
            log_name = f"{pid}.{ts}.log"
            log_path = (self._logs_dir / log_name).resolve()
            log_relpath = str(log_path.relative_to(self._base_dir))

            cwd_path = (self._repo_root / spec.cwd).resolve()
            try:
                cwd_path.relative_to(self._repo_root)
            except Exception as e:
                raise ValueError(f"Process cwd must be under repo_root: {e}")
            if not cwd_path.exists():
                raise FileNotFoundError(f"cwd does not exist: {cwd_path}")

            env = dict(os.environ)
            for k, v in (spec.env or {}).items():
                env[str(k)] = str(v)

            # Ensure the subprocess is in its own process group so we can stop it cleanly.
            f = open(log_path, "ab", buffering=0)
            try:
                proc = subprocess.Popen(
                    list(spec.command),
                    cwd=str(cwd_path),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except Exception:
                f.close()
                raise
            finally:
                # The child keeps its own fd; close our handle to avoid leaking descriptors.
                try:
                    f.close()
                except Exception:
                    pass

            self._procs[pid] = proc
            st2 = {
                "status": "running",
                "pid": int(proc.pid),
                "started_at": _now_utc_iso(),
                "stopped_at": None,
                "exit_code": None,
                "log_relpath": log_relpath,
                "last_error": None,
            }
            self._state[pid] = st2
            self._save_state()

            t = threading.Thread(target=self._watch_process, args=(pid, proc), daemon=True)
            t.start()

            return dict(st2)

    def stop(self, process_id: str, *, timeout_s: float = 6.0) -> Dict[str, Any]:
        pid = str(process_id or "").strip()
        if not pid or pid not in self._specs:
            raise KeyError(f"Unknown process id: {pid}")
        spec = self._specs[pid]
        if spec.kind == "self":
            raise ValueError("Cannot stop a self-managed process (use restart or an external supervisor)")

        with self._lock:
            self._refresh_one_locked(pid)
            st = dict(self._state.get(pid) or {})
            proc_pid = st.get("pid")
            if not isinstance(proc_pid, int) or proc_pid <= 0 or str(st.get("status") or "").lower() != "running":
                st["status"] = "stopped"
                st["pid"] = None
                self._state[pid] = st
                self._save_state()
                return dict(st)

            # Best-effort: terminate the process group.
            try:
                os.killpg(proc_pid, signal.SIGTERM)
            except Exception:
                try:
                    os.kill(proc_pid, signal.SIGTERM)
                except Exception:
                    pass

        # Wait outside the lock.
        end = time.time() + max(0.25, float(timeout_s))
        while time.time() < end:
            if not _is_pid_running(proc_pid):
                break
            time.sleep(0.05)

        # Escalate if needed.
        if _is_pid_running(proc_pid):
            try:
                os.killpg(proc_pid, signal.SIGKILL)
            except Exception:
                try:
                    os.kill(proc_pid, signal.SIGKILL)
                except Exception:
                    pass

        with self._lock:
            self._refresh_one_locked(pid)
            st2 = dict(self._state.get(pid) or {})
            st2.setdefault("stopped_at", _now_utc_iso())
            st2["status"] = "stopped"
            st2["pid"] = None
            self._state[pid] = st2
            self._save_state()
            return dict(st2)

    def restart(self, process_id: str) -> Dict[str, Any]:
        pid = str(process_id or "").strip()
        spec = self._specs.get(pid)
        if spec is None:
            raise KeyError(f"Unknown process id: {pid}")
        if spec.kind == "self":
            return self.restart_self()
        try:
            self.stop(pid)
        except Exception:
            # Continue with best-effort restart even if stop failed.
            pass
        return self.start(pid)

    def restart_self(self) -> Dict[str, Any]:
        # Reply immediately; the actual exec happens async.
        self._schedule_gateway_execv(delay_s=0.75)
        return {"status": "restarting", "scheduled_at": _now_utc_iso()}

    def redeploy_gateway(self) -> Dict[str, Any]:
        """Run build, then restart the gateway on success (best-effort)."""
        self._schedule_gateway_redeploy()
        return {"status": "redeploy_scheduled", "scheduled_at": _now_utc_iso()}

    def log_tail(self, process_id: str, *, max_bytes: int = 80_000) -> Dict[str, Any]:
        pid = str(process_id or "").strip()
        if not pid or pid not in self._specs:
            raise KeyError(f"Unknown process id: {pid}")

        with self._lock:
            self._refresh_one_locked(pid)
            st = dict(self._state.get(pid) or {})
            rel = st.get("log_relpath")
            # Special case: gateway "self" logs map to the audit log by default.
            if pid == "gateway" and (not isinstance(rel, str) or not rel.strip()):
                rel = "audit_log.jsonl"
            if not isinstance(rel, str) or not rel.strip():
                return {"bytes": 0, "truncated": False, "content": "", "log_relpath": None}
            path = (self._base_dir / rel).resolve()
            try:
                path.relative_to(self._base_dir)
            except Exception:
                return {"bytes": 0, "truncated": False, "content": "", "log_relpath": None}

        if not path.exists():
            return {"bytes": 0, "truncated": False, "content": "", "log_relpath": str(rel)}

        data = b""
        truncated = False
        try:
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = int(f.tell() or 0)
                start = max(0, size - int(max_bytes))
                truncated = start > 0
                f.seek(start, os.SEEK_SET)
                data = f.read(int(max_bytes))
        except Exception:
            return {"bytes": 0, "truncated": False, "content": "", "log_relpath": str(rel)}

        text = ""
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return {"bytes": len(data), "truncated": bool(truncated), "content": text, "log_relpath": str(rel)}

    # ----------------------------
    # Internals
    # ----------------------------

    def _actions_for_spec(self, spec: ProcessSpec) -> List[str]:
        if spec.kind == "self":
            return ["restart", "redeploy", "logs"]
        actions = ["logs"]
        if spec.kind in {"service", "task"}:
            actions = ["start", "stop", "restart", "logs"]
        return actions

    def _refresh_states_locked(self) -> None:
        for pid in list(self._state.keys()):
            self._refresh_one_locked(pid)

    def _refresh_one_locked(self, process_id: str) -> None:
        st = dict(self._state.get(process_id) or {})
        pid = st.get("pid")
        if isinstance(pid, int) and pid > 0:
            if _is_pid_running(pid):
                st["status"] = "running"
            else:
                st["status"] = "stopped"
                st["pid"] = None
                st.setdefault("stopped_at", _now_utc_iso())
        self._state[process_id] = st

    def _watch_process(self, process_id: str, proc: subprocess.Popen[bytes]) -> None:
        rc: Optional[int] = None
        try:
            rc = proc.wait()
        except Exception:
            rc = None
        finally:
            with self._lock:
                st = dict(self._state.get(process_id) or {})
                st["status"] = "stopped"
                st["pid"] = None
                st["exit_code"] = int(rc) if isinstance(rc, int) else st.get("exit_code")
                st["stopped_at"] = _now_utc_iso()
                self._state[process_id] = st
                self._procs.pop(process_id, None)
                try:
                    self._save_state()
                except Exception:
                    pass

    def _schedule_gateway_execv(self, *, delay_s: float) -> None:
        def _do() -> None:
            time.sleep(max(0.0, float(delay_s)))
            argv = list(sys.argv)
            exe = argv[0] if argv else ""
            # Prefer re-exec of the original entrypoint when possible.
            try:
                if exe and os.path.exists(exe) and os.access(exe, os.X_OK):
                    os.execv(exe, argv)
                    return
            except Exception:
                pass
            try:
                # Fallback: execute as a module (keeps compatibility with `python -m`).
                os.execv(sys.executable, [sys.executable, "-m", "abstractgateway.cli", *argv[1:]])
            except Exception:
                # Last resort: exit (requires external supervisor).
                os._exit(0)

        t = threading.Thread(target=_do, daemon=True)
        t.start()

    def _schedule_gateway_redeploy(self) -> None:
        def _do() -> None:
            try:
                st = self.start("build")
            except Exception:
                st = {}

            # Wait for build to finish by polling state (works across the monitor thread).
            for _ in range(60 * 60):  # up to 1h
                time.sleep(1.0)
                with self._lock:
                    cur = dict(self._state.get("build") or {})
                if str(cur.get("status") or "").lower() != "running":
                    exit_code = cur.get("exit_code")
                    if isinstance(exit_code, int) and exit_code == 0:
                        self._schedule_gateway_execv(delay_s=0.75)
                    return

        t = threading.Thread(target=_do, daemon=True)
        t.start()


_PROCESS_MANAGER: Optional[ProcessManager] = None
_PROCESS_MANAGER_LOCK = threading.Lock()


def get_process_manager(*, base_dir: Path, repo_root: Path) -> ProcessManager:
    global _PROCESS_MANAGER
    with _PROCESS_MANAGER_LOCK:
        resolved_base = Path(base_dir).expanduser().resolve()
        resolved_repo = Path(repo_root).expanduser().resolve()
        if _PROCESS_MANAGER is not None:
            if _PROCESS_MANAGER.base_dir == resolved_base and _PROCESS_MANAGER.repo_root == resolved_repo:
                return _PROCESS_MANAGER

        specs = load_process_specs(repo_root=resolved_repo)
        _PROCESS_MANAGER = ProcessManager(base_dir=resolved_base, repo_root=resolved_repo, specs=specs)
        return _PROCESS_MANAGER
