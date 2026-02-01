from __future__ import annotations

import datetime
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .notifier import send_email_notification, send_telegram_notification


def _as_bool(raw: Any, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if not s:
        return default
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat()


def normalize_codex_model_id(raw: Any) -> str:
    """Normalize Codex model ids for `codex exec --model <MODEL>`.

    Codex CLI shows "effort" (e.g. reasoning xhigh) separately from the model id.
    Some configs historically used names like `gpt-5.2-xhigh` which are rejected by Codex.
    """

    m = str(raw or "").strip()
    if not m:
        return "gpt-5.2"
    # Drop anything after whitespace (e.g. "gpt-5.2 (reasoning xhigh, ...)" from copy/paste).
    if " " in m:
        m = m.split(" ", 1)[0].strip()
    low = m.lower()
    if low.endswith("-xhigh"):
        m = m[: -len("-xhigh")]
    return m.strip() or "gpt-5.2"

def _triage_repo_root_from_env() -> Optional[Path]:
    raw = str(os.getenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT") or os.getenv("ABSTRACT_TRIAGE_REPO_ROOT") or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except Exception:
        return None


@dataclass(frozen=True)
class BacklogExecRunnerConfig:
    enabled: bool = False
    poll_interval_s: float = 2.0
    executor: str = "none"  # none|codex_cli|workflow_bundle
    notify: bool = False

    # Codex executor defaults.
    codex_bin: str = "codex"
    codex_model: str = "gpt-5.2"
    codex_sandbox: str = "workspace-write"
    codex_approvals: str = "never"

    @staticmethod
    def from_env() -> "BacklogExecRunnerConfig":
        enabled = _as_bool(os.getenv("ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER"), False) or _as_bool(
            os.getenv("ABSTRACT_BACKLOG_EXEC_RUNNER"), False
        )
        poll_s_raw = os.getenv("ABSTRACTGATEWAY_BACKLOG_EXEC_POLL_S") or os.getenv("ABSTRACT_BACKLOG_EXEC_POLL_S") or ""
        try:
            poll_s = float(str(poll_s_raw).strip()) if str(poll_s_raw).strip() else 2.0
        except Exception:
            poll_s = 2.0

        executor = str(os.getenv("ABSTRACTGATEWAY_BACKLOG_EXECUTOR") or os.getenv("ABSTRACT_BACKLOG_EXECUTOR") or "none").strip().lower()
        notify = _as_bool(os.getenv("ABSTRACTGATEWAY_BACKLOG_EXEC_NOTIFY"), False) or _as_bool(
            os.getenv("ABSTRACT_BACKLOG_EXEC_NOTIFY"), False
        )

        codex_bin = str(os.getenv("ABSTRACTGATEWAY_BACKLOG_CODEX_BIN") or os.getenv("ABSTRACT_BACKLOG_CODEX_BIN") or "codex").strip() or "codex"
        codex_model = (
            str(os.getenv("ABSTRACTGATEWAY_BACKLOG_CODEX_MODEL") or os.getenv("ABSTRACT_BACKLOG_CODEX_MODEL") or "gpt-5.2").strip()
            or "gpt-5.2"
        )
        codex_sandbox = (
            str(os.getenv("ABSTRACTGATEWAY_BACKLOG_CODEX_SANDBOX") or os.getenv("ABSTRACT_BACKLOG_CODEX_SANDBOX") or "workspace-write").strip()
            or "workspace-write"
        )
        codex_approvals = (
            str(os.getenv("ABSTRACTGATEWAY_BACKLOG_CODEX_APPROVALS") or os.getenv("ABSTRACT_BACKLOG_CODEX_APPROVALS") or "never").strip()
            or "never"
        )

        return BacklogExecRunnerConfig(
            enabled=bool(enabled),
            poll_interval_s=max(0.25, float(poll_s)),
            executor=executor,
            notify=bool(notify),
            codex_bin=codex_bin,
            codex_model=codex_model,
            codex_sandbox=codex_sandbox,
            codex_approvals=codex_approvals,
        )


def exec_queue_dir(gateway_data_dir: Path) -> Path:
    return (Path(gateway_data_dir).expanduser().resolve() / "backlog_exec_queue").resolve()


def exec_runs_dir(gateway_data_dir: Path) -> Path:
    return (Path(gateway_data_dir).expanduser().resolve() / "backlog_exec_runs").resolve()


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def _claim_lock(queue_dir: Path, request_id: str) -> Optional[Path]:
    lock_path = (queue_dir / f"{request_id}.lock").resolve()
    try:
        lock_path.relative_to(queue_dir.resolve())
    except Exception:
        return None
    try:
        with open(lock_path, "x", encoding="utf-8") as f:
            f.write(_now_iso() + "\n")
            f.write(f"pid={os.getpid()}\n")
        return lock_path
    except FileExistsError:
        return None
    except Exception:
        return None


def _release_lock(lock_path: Optional[Path]) -> None:
    if lock_path is None:
        return
    try:
        lock_path.unlink()
    except Exception:
        pass


class BacklogExecutor:
    name: str = "executor"

    def execute(self, *, prompt: str, repo_root: Path, run_dir: Path) -> Dict[str, Any]:
        raise NotImplementedError


class CodexCliExecutor(BacklogExecutor):
    name = "codex_cli"

    def __init__(self, *, bin_path: str, model: str, sandbox: str, approvals: str):
        self.bin_path = str(bin_path or "codex").strip() or "codex"
        self.model = str(model or "").strip() or "gpt-5.2"
        self.sandbox = str(sandbox or "").strip() or "workspace-write"
        self.approvals = str(approvals or "").strip() or "never"

    def execute(self, *, prompt: str, repo_root: Path, run_dir: Path) -> Dict[str, Any]:
        run_dir.mkdir(parents=True, exist_ok=True)
        events_path = (run_dir / "codex_events.jsonl").resolve()
        last_msg_path = (run_dir / "codex_last_message.txt").resolve()
        stderr_path = (run_dir / "codex_stderr.log").resolve()
        rel_base = Path("backlog_exec_runs") / str(run_dir.name)
        model_id = normalize_codex_model_id(self.model)

        # Keep this invocation compatible with the installed Codex CLI.
        # As of 2026-01, `codex exec --help` shows no `--ask-for-approval` flag; passing it causes hard failure.
        # We also force `--` before the prompt to prevent prompt text from being parsed as flags.
        cmd = [
            self.bin_path,
            "exec",
            "--json",
            "--color",
            "never",
            "--output-last-message",
            str(last_msg_path),
            "--skip-git-repo-check",
            "--cd",
            str(repo_root),
            "--model",
            model_id,
            "--sandbox",
            self.sandbox,
            "--",
            str(prompt or ""),
        ]

        started_at = _now_iso()
        exit_code = -1
        err: Optional[str] = None
        try:
            with open(events_path, "wb") as out, open(stderr_path, "wb") as errf:
                proc = subprocess.run(
                    cmd,
                    stdout=out,
                    stderr=errf,
                    cwd=str(repo_root),
                    check=False,
                    timeout=None,
                    stdin=subprocess.DEVNULL,
                )
                exit_code = int(proc.returncode)
        except FileNotFoundError:
            err = f"codex binary not found: {self.bin_path}"
        except Exception as e:
            err = str(e)

        finished_at = _now_iso()
        last_msg = ""
        try:
            if last_msg_path.exists():
                last_msg = last_msg_path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            last_msg = ""

        ok = err is None and exit_code == 0
        return {
            "ok": bool(ok),
            "executor": self.name,
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "error": err,
            "logs": {
                "events_relpath": str(rel_base / "codex_events.jsonl").replace("\\", "/"),
                "stderr_relpath": str(rel_base / "codex_stderr.log").replace("\\", "/"),
                "last_message_relpath": str(rel_base / "codex_last_message.txt").replace("\\", "/"),
            },
            "last_message": last_msg[:20000] if last_msg else "",
        }


def _resolve_executor(cfg: BacklogExecRunnerConfig) -> Optional[BacklogExecutor]:
    ex = str(cfg.executor or "").strip().lower()
    if not ex or ex == "none":
        return None
    if ex in {"codex", "codex_cli", "codex-cli"}:
        return CodexCliExecutor(bin_path=cfg.codex_bin, model=cfg.codex_model, sandbox=cfg.codex_sandbox, approvals=cfg.codex_approvals)
    # Future: execute via a bundle workflow run (durable).
    if ex in {"workflow", "workflow_bundle", "workflow-bundle"}:
        return None
    return None


def process_next_backlog_exec_request(
    *,
    gateway_data_dir: Path,
    repo_root: Path,
    cfg: BacklogExecRunnerConfig,
) -> Tuple[bool, Optional[str]]:
    """Process a single queued request (best-effort).

    Returns: (processed, request_id).
    """
    queue_dir = exec_queue_dir(gateway_data_dir)
    if not queue_dir.exists():
        return False, None

    executor = _resolve_executor(cfg)
    if executor is None:
        return False, None

    items = sorted([p for p in queue_dir.glob("*.json")], key=lambda p: p.name)
    for p in items:
        req = _load_json(p)
        request_id = str(req.get("request_id") or p.stem).strip()
        status = str(req.get("status") or "").strip().lower()
        if not request_id or status != "queued":
            continue

        lock = _claim_lock(queue_dir, request_id)
        if lock is None:
            continue

        try:
            run_root = exec_runs_dir(gateway_data_dir)
            run_dir = (run_root / request_id).resolve()
            run_root.mkdir(parents=True, exist_ok=True)

            # Update to running.
            req["status"] = "running"
            req["started_at"] = _now_iso()
            req.setdefault("executor", {})["type"] = executor.name
            req.setdefault("executor", {})["version"] = "v0"
            req.setdefault("run_dir_relpath", str(Path("backlog_exec_runs") / request_id).replace("\\", "/"))
            _atomic_write_json(p, req)

            prompt = str(req.get("prompt") or "").strip()
            if not prompt:
                req["status"] = "failed"
                req["finished_at"] = _now_iso()
                req["result"] = {"ok": False, "error": "Missing prompt"}
                _atomic_write_json(p, req)
                return True, request_id

            result = executor.execute(prompt=prompt, repo_root=repo_root, run_dir=run_dir)
            req["result"] = result
            req["finished_at"] = str(result.get("finished_at") or _now_iso())
            req["status"] = "completed" if bool(result.get("ok") is True) else "failed"
            _atomic_write_json(p, req)

            if cfg.notify:
                _notify_backlog_exec_done(req=req)

            return True, request_id
        finally:
            _release_lock(lock)

    return False, None


def _notify_backlog_exec_done(*, req: Dict[str, Any]) -> None:
    status = str(req.get("status") or "").strip() or "unknown"
    request_id = str(req.get("request_id") or "").strip() or "unknown"
    backlog = req.get("backlog") if isinstance(req.get("backlog"), dict) else {}
    rel = str(backlog.get("relpath") or backlog.get("filename") or "").strip()
    result = req.get("result") if isinstance(req.get("result"), dict) else {}
    exit_code = result.get("exit_code")
    last_msg = str(result.get("last_message") or "").strip()
    run_dir = str(req.get("run_dir_relpath") or "").strip()

    subject = f"[AbstractFramework] Backlog exec {status}: {request_id}"
    body_lines = [
        f"status: {status}",
        f"request_id: {request_id}",
        f"backlog: {rel}" if rel else "backlog: (unknown)",
        f"run_dir: {run_dir}" if run_dir else "",
        f"exit_code: {exit_code}" if exit_code is not None else "",
        "",
    ]
    if last_msg:
        body_lines.append("last_message:")
        body_lines.append(last_msg[:3500])
    body = "\n".join([l for l in body_lines if l is not None])

    try:
        send_telegram_notification(text=body[:3500])
    except Exception:
        pass
    try:
        send_email_notification(subject=subject, body_text=body)
    except Exception:
        pass


class BacklogExecRunner:
    def __init__(self, *, gateway_data_dir: Path, cfg: BacklogExecRunnerConfig):
        self.gateway_data_dir = Path(gateway_data_dir).expanduser().resolve()
        self.cfg = cfg
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_error: Optional[str] = None

    def last_error(self) -> Optional[str]:
        return self._last_error

    def start(self) -> None:
        if not self.cfg.enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        t = threading.Thread(target=self._run, name="BacklogExecRunner", daemon=True)
        self._thread = t
        t.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is None:
            return
        try:
            t.join(timeout=5.0)
        except Exception:
            pass

    def is_running(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive() and not self._stop.is_set())

    def _run(self) -> None:
        repo_root = _triage_repo_root_from_env()
        if repo_root is None:
            # Can't run without repo root; stay idle.
            while not self._stop.wait(self.cfg.poll_interval_s):
                continue
            return

        while not self._stop.is_set():
            try:
                processed, _rid = process_next_backlog_exec_request(
                    gateway_data_dir=self.gateway_data_dir, repo_root=repo_root, cfg=self.cfg
                )
                if processed:
                    # Drain quickly when there is work.
                    continue
            except Exception as e:
                # Best-effort; do not crash the gateway.
                try:
                    self._last_error = str(e)
                except Exception:
                    pass
            self._stop.wait(self.cfg.poll_interval_s)
