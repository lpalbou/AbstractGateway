from __future__ import annotations

import datetime
import json
import os
import re
import socket
import subprocess
import threading
import time
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


def _as_int(raw: Any, default: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    s = str(raw).strip()
    if not s:
        return default
    try:
        return int(s)
    except Exception:
        return default


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat()


def normalize_codex_model_id(raw: Any) -> str:
    """Normalize Codex model ids for `codex exec --model <MODEL>`."""

    return parse_codex_model_spec(raw)[0]


def normalize_codex_reasoning_effort(raw: Any) -> Optional[str]:
    s = str(raw or "").strip().lower()
    if not s:
        return None
    if s in {"low", "medium", "high", "xhigh"}:
        return s
    return None


def parse_codex_model_spec(raw: Any) -> Tuple[str, Optional[str]]:
    """Parse a "model spec" into (model_id, reasoning_effort).

    Supported forms:
    - `gpt-5.2-xhigh` (historic effort suffix; Codex CLI rejects this as `--model`, so we split it)
    - `gpt-5.2 (reasoning xhigh, ...)` (copy/paste from UI)
    - `gpt-5.2` (no effort specified)
    """

    s = str(raw or "").strip()
    if not s:
        return "gpt-5.2", None

    # Codex CLI expects model ids without spaces, so use the first token by default.
    token = s.split(" ", 1)[0].strip()
    model_id = token or "gpt-5.2"

    low = model_id.lower()
    effort: Optional[str] = None
    for cand in ("xhigh", "high", "medium", "low"):
        suffix = f"-{cand}"
        if low.endswith(suffix):
            model_id = model_id[: -len(suffix)].strip() or "gpt-5.2"
            effort = cand
            break

    if effort is None:
        # Try to recover effort from a descriptive string (best-effort).
        m = re.search(r"(?:reasoning|effort)\s*[:=]?\s*(xhigh|high|medium|low)\b", s, flags=re.IGNORECASE)
        if m:
            effort = normalize_codex_reasoning_effort(m.group(1))

    return (model_id.strip() or "gpt-5.2"), effort


def _triage_repo_root_from_env() -> Optional[Path]:
    raw = str(os.getenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT") or os.getenv("ABSTRACT_TRIAGE_REPO_ROOT") or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except Exception:
        return None


def _backlog_exec_run_id(request_id: str) -> str:
    rid = str(request_id or "").strip()
    return f"backlog_exec_{rid}" if rid else "backlog_exec"


def _safe_relpath_from_repo_root(*, repo_root: Path, path: Path) -> Optional[str]:
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except Exception:
        return None
    return str(rel).replace("\\", "/")


def _tail_text(path: Path, *, max_bytes: int = 12_000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = int(f.tell() or 0)
            start = max(0, size - int(max_bytes))
            f.seek(start, os.SEEK_SET)
            data = f.read(int(max_bytes))
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _probe_service_url(url: str, *, timeout_s: float = 0.9) -> Dict[str, Any]:
    s = str(url or "").strip()
    if not s:
        return {"ok": False, "error": "missing_url"}
    try:
        u = urlparse(s)
    except Exception:
        return {"ok": False, "error": "invalid_url"}
    host = str(u.hostname or "localhost").strip() or "localhost"
    port = int(u.port or (443 if (u.scheme or "").lower() == "https" else 80))
    try:
        with socket.create_connection((host, port), timeout=float(timeout_s)):
            return {"ok": True, "host": host, "port": port}
    except Exception as e:
        return {"ok": False, "host": host, "port": port, "error": str(e)}


def _repo_pythonpath_entries(repo_root: Path) -> List[str]:
    """Best-effort PYTHONPATH entries to prefer repo code over editable installs.

    Many packages use a `src/` layout; some (e.g., abstractcode) do not.
    We include:
    - <pkg>/src when present
    - otherwise <pkg> when it contains a pyproject.toml
    """
    root = Path(repo_root).resolve()
    out: List[str] = []
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            if not (child / "pyproject.toml").exists():
                continue
            src = child / "src"
            if src.is_dir():
                out.append(str(src))
            else:
                out.append(str(child))
    except Exception:
        return []
    return out


def _build_pythonpath_for_repo(*, repo_root: Path, base_env: Optional[Dict[str, str]] = None) -> str:
    env = base_env or os.environ
    existing = str(env.get("PYTHONPATH") or "").strip()
    parts = [p for p in _repo_pythonpath_entries(repo_root) if p]
    if existing:
        parts.append(existing)
    # De-dupe preserving order.
    seen: set[str] = set()
    deduped: List[str] = []
    for p in parts:
        ps = str(p).strip()
        if not ps or ps in seen:
            continue
        seen.add(ps)
        deduped.append(ps)
    return os.pathsep.join(deduped)


def _try_symlink(*, src: Path, dst: Path) -> None:
    try:
        if not src.exists():
            return
        if dst.exists() or dst.is_symlink():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src)
    except Exception:
        pass


def _default_uat_root(repo_root: Path) -> Path:
    return (Path(repo_root).resolve() / "untracked" / "backlog_exec_uat").resolve()


def _uat_root_from_env(*, repo_root: Path) -> Path:
    raw = str(os.getenv("ABSTRACTGATEWAY_BACKLOG_EXEC_UAT_DIR") or os.getenv("ABSTRACT_BACKLOG_EXEC_UAT_DIR") or "").strip()
    if not raw:
        return _default_uat_root(repo_root)
    try:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (Path(repo_root).resolve() / p).resolve()
        else:
            p = p.resolve()
        return p
    except Exception:
        return _default_uat_root(repo_root)


def _ensure_uat_workspace(*, repo_root: Path, request_id: str) -> Optional[Path]:
    """Create (or reuse) a candidate workspace for this request.

    The AbstractFramework root is a *workspace of repos* (no root `.git/`), so we
    cannot create a single worktree at repo_root.

    v0 approach (multi-repo):
    - Create <repo_root>/untracked/backlog_exec_uat/workspaces/<request_id>/
    - For each top-level repo dir with a `.git/`, create a detached worktree:
        git -C <repo> worktree add --detach <candidate>/<repo_name> HEAD
    - Best-effort symlink shared caches (node_modules, .venv) to avoid re-install overhead
    """
    rid = str(request_id or "").strip()
    if not rid:
        return None

    uat_root = _uat_root_from_env(repo_root=repo_root)
    ws_root = (uat_root / "workspaces").resolve()
    ws_root.mkdir(parents=True, exist_ok=True)

    ws = (ws_root / rid).resolve()
    if ws.exists():
        return ws

    try:
        ws.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None

    rr = Path(repo_root).resolve()

    def _is_git_repo_dir(p: Path) -> bool:
        try:
            g = (p / ".git").resolve()
        except Exception:
            g = p / ".git"
        return g.exists()

    repos: List[Path] = []
    try:
        for child in sorted(rr.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            if not _is_git_repo_dir(child):
                continue
            repos.append(child)
    except Exception:
        return None

    required = {"abstractgateway", "abstractobserver", "abstractcode", "abstractflow", "docs"}
    required_present = {p.name for p in repos if p.name in required}

    # Create required repos first (we need them to render a usable UAT stack).
    def _worktree_add(repo: Path) -> bool:
        dst = (ws / repo.name).resolve()
        if dst.exists():
            return True
        try:
            subprocess.run(
                ["git", "-C", str(repo), "worktree", "add", "--detach", str(dst), "HEAD"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90.0,
            )
            return True
        except Exception:
            return False

    for repo in repos:
        if repo.name in required_present:
            if not _worktree_add(repo):
                return None

    # Best-effort: remaining repos (non-fatal).
    for repo in repos:
        if repo.name in required_present:
            continue
        _worktree_add(repo)

    # Best-effort: reuse repo-local caches to make UAT worktrees runnable without reinstall.
    _try_symlink(src=(rr / ".venv").resolve(), dst=(ws / ".venv").resolve())
    _try_symlink(src=(rr / "abstractobserver" / "node_modules").resolve(), dst=(ws / "abstractobserver" / "node_modules").resolve())
    _try_symlink(src=(rr / "abstractcode" / "web" / "node_modules").resolve(), dst=(ws / "abstractcode" / "web" / "node_modules").resolve())
    _try_symlink(src=(rr / "abstractflow" / "web" / "frontend" / "node_modules").resolve(), dst=(ws / "abstractflow" / "web" / "frontend" / "node_modules").resolve())

    return ws


_VALID_EXEC_MODE_RE = re.compile(r"^(uat|candidate|inplace)$", re.IGNORECASE)


def _default_exec_mode() -> str:
    raw = str(os.getenv("ABSTRACTGATEWAY_BACKLOG_EXEC_MODE") or os.getenv("ABSTRACT_BACKLOG_EXEC_MODE") or "").strip().lower()
    if raw and _VALID_EXEC_MODE_RE.match(raw):
        return "uat" if raw in {"uat", "candidate"} else "inplace"
    # Safer default: do not mutate prod repo unless explicitly requested.
    return "uat"


def _is_uat_mode(mode: str) -> bool:
    m = str(mode or "").strip().lower()
    return m in {"uat", "candidate"}


def _is_safe_repo_relpath(path: str) -> bool:
    p = str(path or "").replace("\\", "/").strip()
    if not p:
        return False
    if p.startswith("/") or p.startswith("\\"):
        return False
    # Windows drive/path style.
    if re.match(r"^[a-zA-Z]:[/\\\\]", p):
        return False
    parts = [seg for seg in p.split("/") if seg]
    if not parts:
        return False
    for seg in parts:
        if seg in {".", ".."}:
            return False
    return True


def _write_candidate_git_diff_patch(*, candidate_root: Path, out_path: Path) -> bool:
    """Write a best-effort git patch for a candidate workspace.

    - If candidate_root is a git repo, we `git diff` directly.
    - If candidate_root is a multi-repo workspace, we concatenate per-repo diffs and
      prefix paths with the repo folder (so the patch is readable at the workspace root).
    """
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False
    try:
        root = Path(candidate_root).resolve()
        if (root / ".git").exists():
            with open(out_path, "wb") as f:
                subprocess.run(
                    ["git", "-C", str(root), "diff", "--binary", "--no-color"],
                    check=False,
                    stdout=f,
                    stderr=subprocess.DEVNULL,
                    timeout=30.0,
                )
            return out_path.exists()

        repos: List[Path] = []
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            if not (child / ".git").exists():
                continue
            repos.append(child)

        wrote_any = False
        with open(out_path, "wb") as f:
            for repo in repos:
                # Prefix the diff paths so the patch is meaningful at the workspace root.
                src_prefix = f"a/{repo.name}/"
                dst_prefix = f"b/{repo.name}/"
                proc = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo),
                        "diff",
                        "--binary",
                        "--no-color",
                        f"--src-prefix={src_prefix}",
                        f"--dst-prefix={dst_prefix}",
                    ],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=30.0,
                )
                if proc.stdout:
                    f.write(proc.stdout)
                    if not proc.stdout.endswith(b"\n"):
                        f.write(b"\n")
                    wrote_any = True
        return out_path.exists() and wrote_any
    except Exception:
        return False


_BACKLOG_ID_FROM_FILENAME_RE = re.compile(r"^(?P<id>\d+)-")


def _maybe_fix_backlog_move_in_candidate(*, candidate_root: Path, req: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort enforce the 'planned -> completed' move inside a candidate workspace.

    Some agent runs create the completed backlog file but forget to delete the planned one.
    That results in duplicated backlog items (both Planned and Completed) after promotion.

    Safety constraints:
    - Only touches a planned backlog item under the `docs/` repo.
    - Only deletes the planned file when a completed file for the same numeric id exists.
    """
    out: Dict[str, Any] = {"ok": False, "action": "none"}
    backlog = req.get("backlog") if isinstance(req.get("backlog"), dict) else {}
    relpath = str(backlog.get("relpath") or "").replace("\\\\", "/").strip()
    if not relpath:
        out["reason"] = "missing_relpath"
        return out
    if not relpath.startswith("docs/backlog/planned/"):
        out["reason"] = "not_planned_docs_backlog"
        return out

    planned_name = relpath.split("/")[-1]
    m = _BACKLOG_ID_FROM_FILENAME_RE.match(planned_name)
    if not m:
        out["reason"] = "unparseable_id"
        return out
    item_id = str(m.group("id") or "").strip()
    if not item_id:
        out["reason"] = "missing_id"
        return out

    root = Path(candidate_root).resolve()
    planned_path = (root / relpath).resolve()
    try:
        planned_path.relative_to(root)
    except Exception:
        out["reason"] = "unsafe_planned_path"
        return out

    completed_dir = (root / "docs" / "backlog" / "completed").resolve()
    try:
        completed_dir.relative_to(root)
    except Exception:
        out["reason"] = "unsafe_completed_dir"
        return out

    matches: List[Path] = []
    try:
        if completed_dir.exists():
            matches = sorted(completed_dir.glob(f"{item_id}-*.md"))
    except Exception:
        matches = []

    if not matches:
        out["reason"] = "no_completed_file"
        return out

    if not planned_path.exists():
        out["ok"] = True
        out["reason"] = "planned_missing"
        out["completed_relpath"] = str(matches[0].relative_to(root)).replace("\\\\", "/")
        return out

    try:
        planned_path.unlink()
    except Exception as e:
        out["reason"] = f"unlink_failed: {e}"
        return out

    out.update(
        {
            "ok": True,
            "action": "deleted_planned_duplicate",
            "planned_relpath": relpath,
            "completed_relpath": str(matches[0].relative_to(root)).replace("\\\\", "/"),
        }
    )
    return out


def _candidate_manifest_path(run_dir: Path) -> Path:
    return (Path(run_dir).resolve() / "candidate_manifest.json").resolve()


_SKIP_UNTRACKED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    "dist",
    "build",
    ".next",
    ".turbo",
    "runtime",
    "logs",
    "untracked",
}


def _git_stdout(*, cwd: Path, args: List[str], timeout_s: float) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(Path(cwd).resolve()),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=float(timeout_s),
            check=False,
        )
    except Exception:
        return ""
    return str(proc.stdout or "")


def _collect_repo_candidate_manifest(repo_dir: Path) -> Dict[str, Any]:
    repo = Path(repo_dir).resolve()
    name = repo.name

    copy_paths: set[str] = set()
    delete_paths: set[str] = set()
    skipped: List[Dict[str, str]] = []

    diff = _git_stdout(cwd=repo, args=["diff", "--name-status"], timeout_s=30.0)
    for raw in diff.splitlines():
        line = raw.strip("\n").rstrip("\r")
        if not line:
            continue
        parts = line.split("\t")
        if not parts:
            continue
        st = str(parts[0] or "").strip()
        if not st:
            continue

        def _add_copy(p: str) -> None:
            ps = str(p or "").strip()
            if not _is_safe_repo_relpath(ps):
                skipped.append({"path": ps, "reason": "unsafe_path"})
                return
            copy_paths.add(ps.replace("\\", "/"))

        def _add_delete(p: str) -> None:
            ps = str(p or "").strip()
            if not _is_safe_repo_relpath(ps):
                skipped.append({"path": ps, "reason": "unsafe_path"})
                return
            delete_paths.add(ps.replace("\\", "/"))

        if st.startswith("R") and len(parts) >= 3:
            _add_delete(parts[1])
            _add_copy(parts[2])
            continue
        if st.startswith("C") and len(parts) >= 3:
            _add_copy(parts[2])
            continue

        code = st[0]
        if code in {"M", "A"} and len(parts) >= 2:
            _add_copy(parts[1])
        elif code == "D" and len(parts) >= 2:
            _add_delete(parts[1])
        else:
            # Unknown / ignored.
            continue

    untracked = _git_stdout(cwd=repo, args=["ls-files", "--others", "--exclude-standard"], timeout_s=30.0)
    for raw in untracked.splitlines():
        p = str(raw or "").strip()
        if not p:
            continue
        p = p.replace("\\", "/")
        if not _is_safe_repo_relpath(p):
            skipped.append({"path": p, "reason": "unsafe_path"})
            continue
        segs = [s for s in p.split("/") if s]
        if any(s in _SKIP_UNTRACKED_DIRS for s in segs):
            skipped.append({"path": p, "reason": "skipped_untracked_dir"})
            continue
        copy_paths.add(p)

    # If a path is deleted, don't also try to copy it.
    copy_paths.difference_update(delete_paths)

    return {
        "repo": name,
        "repo_relpath": name,
        "copy": sorted(copy_paths),
        "delete": sorted(delete_paths),
        "skipped": skipped[:500],
    }


def _write_candidate_manifest(*, candidate_root: Path, run_dir: Path, request_id: str, candidate_relpath: str) -> bool:
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False

    root = Path(candidate_root).resolve()
    repos: List[Path] = []
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            if not (child / ".git").exists():
                continue
            repos.append(child)
    except Exception:
        return False

    repoman: List[Dict[str, Any]] = []
    for repo in repos:
        try:
            repoman.append(_collect_repo_candidate_manifest(repo))
        except Exception:
            continue

    out_path = _candidate_manifest_path(run_dir)
    obj = {
        "version": 1,
        "created_at": _now_iso(),
        "request_id": str(request_id),
        "candidate_relpath": str(candidate_relpath or ""),
        "repos": repoman,
    }
    try:
        out_path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return out_path.exists()
    except Exception:
        return False


def _latest_feedback_text(req: Dict[str, Any]) -> str:
    fb = req.get("feedback")
    if not isinstance(fb, list) or not fb:
        return ""
    last = fb[-1]
    if isinstance(last, dict):
        return str(last.get("text") or "")
    return str(last or "")


def _prompt_with_feedback(prompt: str, *, req: Dict[str, Any]) -> str:
    base = str(prompt or "").strip()
    fb = _latest_feedback_text(req).strip()
    if not fb:
        return base
    if len(fb) > 20_000:
        fb = fb[:20_000].rstrip() + "\n…(truncated)…"
    attempt_raw = req.get("attempt")
    try:
        attempt = int(attempt_raw) if attempt_raw is not None else None
    except Exception:
        attempt = None
    suffix = "\n\n---\n"
    suffix += "Operator QA feedback"
    if attempt is not None:
        suffix += f" (attempt {attempt})"
    suffix += ":\n"
    suffix += fb
    suffix += "\n---\n"
    return base + suffix


def _build_gateway_stores(*, gateway_data_dir: Path):
    # Lazy import: keep this module usable in minimal contexts.
    from ..config import GatewayHostConfig  # type: ignore
    from ..stores import build_file_stores, build_sqlite_stores  # type: ignore

    base = Path(gateway_data_dir).expanduser().resolve()
    cfg = GatewayHostConfig.from_env()
    backend = str(getattr(cfg, "store_backend", "file") or "file").strip().lower() or "file"
    if backend == "sqlite":
        return build_sqlite_stores(base_dir=base, db_path=getattr(cfg, "db_path", None))
    return build_file_stores(base_dir=base)


def _store_backlog_exec_logs_to_ledger(
    *,
    gateway_data_dir: Path,
    request_id: str,
    req: Dict[str, Any],
    prompt: str,
    run_dir: Path,
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist backlog execution logs as a durable run+ledger entry.

    Contract:
    - Never raise (best-effort). Caller should still record completion in request JSON.
    - Store the full logs as artifacts and reference them from both RunState.vars and a ledger StepRecord.
    """

    try:
        stores = _build_gateway_stores(gateway_data_dir=gateway_data_dir)
    except Exception:
        return {}

    from abstractruntime.core.models import RunState, RunStatus, StepRecord, StepStatus  # type: ignore
    from abstractruntime.storage.artifacts import artifact_ref  # type: ignore

    rid = _backlog_exec_run_id(request_id)
    now = _now_iso()

    backlog = req.get("backlog") if isinstance(req.get("backlog"), dict) else {}
    backlog_queue = req.get("backlog_queue") if isinstance(req.get("backlog_queue"), dict) else {}
    backlog_kind = str(backlog.get("kind") or "").strip()
    backlog_filename = str(backlog.get("filename") or "").strip()
    backlog_relpath = str(backlog.get("relpath") or "").strip()
    item_id = None
    try:
        item_id = int(str(backlog_filename).split("-", 1)[0])
    except Exception:
        item_id = None

    base_tags = {
        "kind": "backlog_exec_log",
        "request_id": str(request_id),
        "backlog_kind": backlog_kind,
        "backlog_filename": backlog_filename,
        "backlog_relpath": backlog_relpath,
        "backlog_item_id": str(item_id) if item_id is not None else "",
    }
    base_tags = {k: v for k, v in base_tags.items() if isinstance(v, str) and v.strip()}

    def _store_file(*, path: Path, name: str, content_type: str) -> Optional[str]:
        try:
            if not path.exists():
                return None
            data = path.read_bytes()
            meta = stores.artifact_store.store(data, content_type=content_type, run_id=rid, tags={**base_tags, "name": name})
            return str(getattr(meta, "artifact_id", "") or "").strip() or None
        except Exception:
            return None

    events_id = _store_file(path=(run_dir / "codex_events.jsonl").resolve(), name="events", content_type="application/jsonl")
    stderr_id = _store_file(path=(run_dir / "codex_stderr.log").resolve(), name="stderr", content_type="text/plain")
    last_id = _store_file(path=(run_dir / "codex_last_message.txt").resolve(), name="last_message", content_type="text/plain")
    patch_id = _store_file(path=(run_dir / "candidate.patch").resolve(), name="candidate_patch", content_type="text/x-diff")
    manifest_id = _store_file(path=(run_dir / "candidate_manifest.json").resolve(), name="candidate_manifest", content_type="application/json")

    log_artifacts = {
        "events": artifact_ref(events_id) if events_id else None,
        "stderr": artifact_ref(stderr_id) if stderr_id else None,
        "last_message": artifact_ref(last_id) if last_id else None,
        "candidate_patch": artifact_ref(patch_id) if patch_id else None,
        "candidate_manifest": artifact_ref(manifest_id) if manifest_id else None,
    }

    ok = bool(result.get("ok") is True)
    status = RunStatus.COMPLETED if ok else RunStatus.FAILED
    started_at = str(req.get("started_at") or result.get("started_at") or now)
    finished_at = str(req.get("finished_at") or result.get("finished_at") or now)
    err = str(result.get("error") or "").strip() or None

    run_vars = {
        "kind": "backlog_exec",
        "request_id": str(request_id),
        "backlog": backlog,
        "backlog_queue": backlog_queue if backlog_queue else {},
        "executor": req.get("executor") if isinstance(req.get("executor"), dict) else {},
        "execution_mode": str(req.get("execution_mode") or ""),
        "candidate_relpath": str(req.get("candidate_relpath") or ""),
        "candidate_patch_relpath": str(req.get("candidate_patch_relpath") or ""),
        "candidate_manifest_relpath": str(req.get("candidate_manifest_relpath") or ""),
        "run_dir_relpath": str(req.get("run_dir_relpath") or ""),
        "prompt": str(prompt or ""),
        "result": {
            "ok": ok,
            "exit_code": result.get("exit_code"),
            "error": err,
            "log_artifacts": log_artifacts,
        },
    }

    try:
        run = RunState(
            run_id=rid,
            workflow_id="backlog_exec",
            status=status,
            current_node="backlog_exec",
            vars=run_vars,
            output=None,
            error=err,
            created_at=started_at,
            updated_at=finished_at,
            actor_id="backlog_exec_runner",
            session_id=None,
            parent_run_id=None,
        )
        stores.run_store.save(run)
    except Exception:
        pass

    try:
        rec = StepRecord(
            run_id=rid,
            step_id=str(request_id),
            node_id="backlog_exec",
            status=StepStatus.COMPLETED if ok else StepStatus.FAILED,
            effect={"type": "backlog_exec", "payload": {"request_id": str(request_id), "backlog": backlog, "backlog_queue": backlog_queue}},
            result={
                "ok": ok,
                "exit_code": result.get("exit_code"),
                "error": err,
                "log_artifacts": log_artifacts,
            },
            error=err,
            started_at=started_at,
            ended_at=finished_at,
            actor_id="backlog_exec_runner",
            session_id=None,
            attempt=1,
            idempotency_key=f"backlog_exec:{request_id}",
        )
        stores.ledger_store.append(rec)
    except Exception:
        pass

    return {"ledger_run_id": rid, "log_artifacts": log_artifacts}


@dataclass(frozen=True)
class BacklogExecRunnerConfig:
    enabled: bool = False
    poll_interval_s: float = 2.0
    workers: int = 2
    executor: str = "none"  # none|codex_cli|workflow_bundle
    notify: bool = False

    # Codex executor defaults.
    codex_bin: str = "codex"
    codex_model: str = "gpt-5.2"
    codex_reasoning_effort: str = ""  # low|medium|high|xhigh (optional)
    codex_sandbox: str = "workspace-write"
    codex_approvals: str = "never"

    # Execution safety (v0)
    exec_mode_default: str = "uat"  # uat|inplace

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
        workers_raw = (
            os.getenv("ABSTRACTGATEWAY_BACKLOG_EXEC_WORKERS")
            or os.getenv("ABSTRACT_BACKLOG_EXEC_WORKERS")
            or os.getenv("ABSTRACTGATEWAY_BACKLOG_EXEC_THREADS")
            or os.getenv("ABSTRACT_BACKLOG_EXEC_THREADS")
            or ""
        )
        workers = _as_int(workers_raw, 2)

        executor = str(os.getenv("ABSTRACTGATEWAY_BACKLOG_EXECUTOR") or os.getenv("ABSTRACT_BACKLOG_EXECUTOR") or "none").strip().lower()
        notify = _as_bool(os.getenv("ABSTRACTGATEWAY_BACKLOG_EXEC_NOTIFY"), False) or _as_bool(
            os.getenv("ABSTRACT_BACKLOG_EXEC_NOTIFY"), False
        )

        codex_bin = str(os.getenv("ABSTRACTGATEWAY_BACKLOG_CODEX_BIN") or os.getenv("ABSTRACT_BACKLOG_CODEX_BIN") or "codex").strip() or "codex"
        raw_model = str(os.getenv("ABSTRACTGATEWAY_BACKLOG_CODEX_MODEL") or os.getenv("ABSTRACT_BACKLOG_CODEX_MODEL") or "gpt-5.2").strip() or "gpt-5.2"
        raw_effort = str(
            os.getenv("ABSTRACTGATEWAY_BACKLOG_CODEX_REASONING_EFFORT")
            or os.getenv("ABSTRACT_BACKLOG_CODEX_REASONING_EFFORT")
            or ""
        ).strip()
        codex_model, inferred_effort = parse_codex_model_spec(raw_model)
        codex_effort = normalize_codex_reasoning_effort(raw_effort) or inferred_effort or None
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
            workers=max(1, min(32, int(workers))),
            executor=executor,
            notify=bool(notify),
            codex_bin=codex_bin,
            codex_model=codex_model,
            codex_reasoning_effort=str(codex_effort or "").strip(),
            codex_sandbox=codex_sandbox,
            codex_approvals=codex_approvals,
            exec_mode_default=_default_exec_mode(),
        )


def exec_queue_dir(gateway_data_dir: Path) -> Path:
    return (Path(gateway_data_dir).expanduser().resolve() / "backlog_exec_queue").resolve()


def exec_runs_dir(gateway_data_dir: Path) -> Path:
    return (Path(gateway_data_dir).expanduser().resolve() / "backlog_exec_runs").resolve()


def uat_deploy_lock_path(gateway_data_dir: Path) -> Path:
    return (Path(gateway_data_dir).expanduser().resolve() / "uat_deploy_lock.json").resolve()


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


def _read_uat_deploy_lock(gateway_data_dir: Path) -> Dict[str, Any]:
    path = uat_deploy_lock_path(gateway_data_dir)
    if not path.exists():
        return {}
    obj = _load_json(path)
    return obj if isinstance(obj, dict) else {}


def _write_uat_deploy_lock(*, gateway_data_dir: Path, request_id: str, candidate_relpath: str) -> Tuple[bool, str]:
    """Force-set the shared UAT deploy lock (operator-controlled).

    Returns: (ok, previous_owner_request_id)
    """
    rid = str(request_id or "").strip()
    if not rid:
        return False, ""

    path = uat_deploy_lock_path(gateway_data_dir)
    cur = _read_uat_deploy_lock(gateway_data_dir)
    prev_owner = str(cur.get("owner_request_id") or "").strip()

    now = _now_iso()
    payload = {
        "version": 2,
        "owner_request_id": rid,
        "candidate_relpath": str(candidate_relpath or "").strip(),
        "updated_at": now,
        "previous_owner_request_id": prev_owner,
        "previous_updated_at": str(cur.get("updated_at") or "").strip(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(path, payload)
        return True, prev_owner
    except Exception:
        return False, prev_owner


def deploy_uat_for_request(*, gateway_data_dir: Path, repo_root: Path, request_id: str) -> Dict[str, Any]:
    """Deploy/restart the shared UAT stack for a specific completed request (operator-controlled).

    Safety:
    - requires `status=awaiting_qa` and `execution_mode=uat`
    - only points UAT `current` symlink at a repo-root-contained candidate workspace
    - requires the process manager to be enabled (so we can restart services deterministically)
    """
    rid = str(request_id or "").strip()
    if not rid:
        return {"ok": False, "error": "missing_request_id"}

    # UAT deploy should make the UAT URLs usable, which requires starting/restarting services.
    # We keep the process manager opt-in for security, so refuse the deploy when disabled.
    if not _as_bool(os.getenv("ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER"), False):
        try:
            base = Path(gateway_data_dir).expanduser().resolve()
            qdir = exec_queue_dir(base)
            path = (qdir / f"{rid}.json").resolve()
            if path.exists():
                req = _load_json(path)
                if isinstance(req, dict):
                    req["uat_deploy"] = {"at": _now_iso(), "processes": {"status": "skipped", "reason": "process_manager_disabled"}}
                    req["uat_deploy_error"] = "process_manager_disabled"
                    try:
                        _atomic_write_json(path, req)
                    except Exception:
                        pass
        except Exception:
            pass
        return {"ok": False, "error": "process_manager_disabled"}

    rr = Path(repo_root).resolve()
    base = Path(gateway_data_dir).expanduser().resolve()
    qdir = exec_queue_dir(base)
    path = (qdir / f"{rid}.json").resolve()
    try:
        path.relative_to(qdir)
    except Exception:
        return {"ok": False, "error": "invalid_request_path"}
    if not path.exists():
        return {"ok": False, "error": "request_not_found"}

    req = _load_json(path)
    status = str(req.get("status") or "").strip().lower()
    if status != "awaiting_qa":
        return {"ok": False, "error": "invalid_status", "status": status}

    exec_mode = str(req.get("execution_mode") or "").strip().lower() or "uat"
    if exec_mode == "candidate":
        exec_mode = "uat"
    if not _is_uat_mode(exec_mode):
        return {"ok": False, "error": "invalid_execution_mode", "execution_mode": exec_mode}

    candidate_relpath = str(req.get("candidate_relpath") or "").strip()
    if not candidate_relpath:
        return {"ok": False, "error": "missing_candidate_relpath"}
    candidate_root = (rr / candidate_relpath).resolve()
    try:
        candidate_root.relative_to(rr)
    except Exception:
        return {"ok": False, "error": "unsafe_candidate_path"}
    if not candidate_root.exists():
        return {"ok": False, "error": "candidate_missing"}

    ok, prev_owner = _write_uat_deploy_lock(gateway_data_dir=base, request_id=rid, candidate_relpath=candidate_relpath)
    req["uat_lock_owner_request_id"] = rid
    req["uat_lock_acquired"] = bool(ok)
    if prev_owner and prev_owner != rid:
        req["uat_lock_previous_owner_request_id"] = prev_owner
    if not ok:
        try:
            _atomic_write_json(path, req)
        except Exception:
            pass
        return {"ok": False, "error": "uat_lock_write_failed", "previous_owner_request_id": prev_owner}

    # Operator-controlled deploy/restart: point the stable UAT pointer to this request's candidate.
    lock_path = uat_deploy_lock_path(base)

    def _release_lock_best_effort() -> None:
        try:
            obj = _read_uat_deploy_lock(base)
            cur_owner = str(obj.get("owner_request_id") or "").strip()
            if cur_owner and cur_owner != rid:
                return
            if lock_path.exists():
                lock_path.unlink()
        except Exception:
            pass

    #
    # IMPORTANT: Do NOT call `.resolve()` on the `current` path, because it would resolve the symlink
    # target (the previous candidate workspace) and break unlink/symlink updates.
    uat_root = _uat_root_from_env(repo_root=rr).resolve()
    cur = (uat_root / "current")
    try:
        cur.absolute().relative_to(uat_root)
    except Exception:
        _release_lock_best_effort()
        return {"ok": False, "error": "unsafe_uat_current_path"}

    if cur.exists() and not cur.is_symlink():
        # Safety: we never want to overwrite an on-disk directory/file here.
        _release_lock_best_effort()
        return {"ok": False, "error": "uat_current_not_symlink"}

    try:
        if cur.is_symlink() or cur.exists():
            cur.unlink()
    except Exception:
        _release_lock_best_effort()
        return {"ok": False, "error": "uat_current_unlink_failed"}

    try:
        cur.parent.mkdir(parents=True, exist_ok=True)
        cur.symlink_to(candidate_root)
        req["uat_current_relpath"] = _safe_relpath_from_repo_root(repo_root=rr, path=cur) or ""
    except Exception:
        req["uat_current_relpath"] = ""
        _release_lock_best_effort()
        return {"ok": False, "error": "uat_current_symlink_failed"}

    # Mark as no longer pending once we own the lock.
    req.pop("uat_pending", None)

    deployed: Dict[str, Any] = {}
    try:
        from .process_manager import get_process_manager  # type: ignore

        mgr = get_process_manager(base_dir=base, repo_root=rr)
        for pid in (
            "gateway_uat",
            "abstractflow_backend_uat",
            "abstractflow_frontend_uat",
            "abstractobserver_uat",
            "abstractcode_web_uat",
        ):
            try:
                deployed[pid] = mgr.restart(pid)
            except Exception as e:
                deployed[pid] = {"status": "error", "error": str(e)}

        # Best-effort: liveness probes so the operator can immediately see whether the UAT URLs
        # are actually reachable (process running != port listening).
        try:
            # Give dev servers a moment to bind their ports before probing.
            time.sleep(0.85)
            info_by_id = {p.get("id"): p for p in (mgr.list_processes() or []) if isinstance(p, dict)}
            for pid, st in list(deployed.items()):
                if not isinstance(st, dict):
                    continue
                info = info_by_id.get(pid) if isinstance(info_by_id.get(pid), dict) else {}
                url = str((info or {}).get("url") or "").strip()
                if url:
                    st["url"] = url
                    st["probe"] = _probe_service_url(url)
                    if not bool(st.get("probe", {}).get("ok") is True):
                        rel = str(st.get("log_relpath") or "").strip()
                        if rel:
                            log_path = (base / rel).resolve()
                            try:
                                log_path.relative_to(base)
                                st["log_tail"] = _tail_text(log_path, max_bytes=8000)
                            except Exception:
                                pass
                deployed[pid] = st
        except Exception:
            pass

        req["uat_deploy"] = {"at": _now_iso(), "processes": deployed}
    except Exception as e:
        req["uat_deploy_error"] = str(e)
        deployed = {"status": "error", "error": str(e)}

    try:
        _atomic_write_json(path, req)
    except Exception:
        pass

    return {"ok": True, "request_id": rid, "deployed": deployed}


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

    def execute(self, *, prompt: str, repo_root: Path, run_dir: Path, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        raise NotImplementedError


class CodexCliExecutor(BacklogExecutor):
    name = "codex_cli"

    def __init__(self, *, bin_path: str, model: str, reasoning_effort: str, sandbox: str, approvals: str):
        self.bin_path = str(bin_path or "codex").strip() or "codex"
        self.model = str(model or "").strip() or "gpt-5.2"
        self.reasoning_effort = str(reasoning_effort or "").strip()
        self.sandbox = str(sandbox or "").strip() or "workspace-write"
        self.approvals = str(approvals or "").strip() or "never"

    def execute(self, *, prompt: str, repo_root: Path, run_dir: Path, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        run_dir.mkdir(parents=True, exist_ok=True)
        events_path = (run_dir / "codex_events.jsonl").resolve()
        last_msg_path = (run_dir / "codex_last_message.txt").resolve()
        stderr_path = (run_dir / "codex_stderr.log").resolve()
        rel_base = Path("backlog_exec_runs") / str(run_dir.name)
        model_id, inferred_effort = parse_codex_model_spec(self.model)
        effort = normalize_codex_reasoning_effort(self.reasoning_effort) or inferred_effort or None

        # Preserve previous attempt logs (best-effort).
        try:
            for p in (events_path, last_msg_path, stderr_path):
                if not p.exists():
                    continue
                ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                p.rename(p.with_name(f"{p.name}.{ts}.bak"))
        except Exception:
            pass

        # Keep this invocation compatible with the installed Codex CLI.
        # As of 2026-01, `codex exec --help` shows no `--ask-for-approval` flag; passing it causes hard failure.
        # We also force `--` before the prompt to prevent prompt text from being parsed as flags.
        cmd: List[str] = [
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
        ]
        if effort:
            cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
        cmd.extend(["--sandbox", self.sandbox, "--", str(prompt or "")])

        started_at = _now_iso()
        exit_code = -1
        err: Optional[str] = None
        try:
            run_env = dict(os.environ)
            if isinstance(env, dict):
                for k, v in env.items():
                    ks = str(k or "").strip()
                    if not ks:
                        continue
                    run_env[ks] = str(v if v is not None else "")
            with open(events_path, "wb") as out, open(stderr_path, "wb") as errf:
                proc = subprocess.run(
                    cmd,
                    stdout=out,
                    stderr=errf,
                    cwd=str(repo_root),
                    env=run_env,
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
            "model": model_id,
            "reasoning_effort": effort,
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
        return CodexCliExecutor(
            bin_path=cfg.codex_bin,
            model=cfg.codex_model,
            reasoning_effort=cfg.codex_reasoning_effort,
            sandbox=cfg.codex_sandbox,
            approvals=cfg.codex_approvals,
        )
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

            exec_mode = str(req.get("execution_mode") or "").strip().lower() or str(getattr(cfg, "exec_mode_default", "") or "").strip().lower()
            if not exec_mode:
                exec_mode = "uat"
            if exec_mode == "candidate":
                exec_mode = "uat"
            req["execution_mode"] = exec_mode

            candidate_root = repo_root
            candidate_relpath = ""
            if _is_uat_mode(exec_mode):
                ws = _ensure_uat_workspace(repo_root=repo_root, request_id=request_id)
                if ws is None:
                    req["status"] = "failed"
                    req["finished_at"] = _now_iso()
                    req["result"] = {"ok": False, "error": "Failed to create UAT workspace"}
                    _atomic_write_json(p, req)
                    return True, request_id
                candidate_root = ws
                rel = _safe_relpath_from_repo_root(repo_root=repo_root, path=ws)
                candidate_relpath = rel or ""
                if candidate_relpath:
                    req["candidate_relpath"] = candidate_relpath

            # Update to running.
            req["status"] = "running"
            req["started_at"] = _now_iso()
            req.setdefault("executor", {})["type"] = executor.name
            req.setdefault("executor", {})["version"] = "v0"
            if isinstance(executor, CodexCliExecutor):
                try:
                    model_id, inferred_effort = parse_codex_model_spec(executor.model)
                except Exception:
                    model_id, inferred_effort = ("gpt-5.2", None)
                effort = normalize_codex_reasoning_effort(getattr(executor, "reasoning_effort", "")) or inferred_effort or None
                req.setdefault("executor", {})["model"] = model_id
                if effort:
                    req.setdefault("executor", {})["reasoning_effort"] = effort
                req.setdefault("executor", {})["sandbox"] = str(getattr(executor, "sandbox", "") or "").strip()
                # Older queued requests won't have these keys; fill for observability.
                if "target_model" not in req:
                    req["target_model"] = model_id
                if effort and "target_reasoning_effort" not in req:
                    req["target_reasoning_effort"] = effort
            req.setdefault("run_dir_relpath", str(Path("backlog_exec_runs") / request_id).replace("\\", "/"))
            _atomic_write_json(p, req)

            prompt = _prompt_with_feedback(str(req.get("prompt") or ""), req=req)
            if not prompt:
                req["status"] = "failed"
                req["finished_at"] = _now_iso()
                req["result"] = {"ok": False, "error": "Missing prompt"}
                _atomic_write_json(p, req)
                return True, request_id

            exec_env: Dict[str, str] = {}
            if _is_uat_mode(exec_mode):
                exec_env["PYTHONPATH"] = _build_pythonpath_for_repo(repo_root=candidate_root)

            result = executor.execute(prompt=prompt, repo_root=candidate_root, run_dir=run_dir, env=exec_env)

            # Backlog hygiene (UAT only): some runs create the completed file but forget to delete the planned one.
            # Do this *before* producing patch/manifest so the deletion is visible and gets promoted.
            if _is_uat_mode(exec_mode) and isinstance(result, dict) and bool(result.get("ok") is True):
                try:
                    cleanup = _maybe_fix_backlog_move_in_candidate(candidate_root=candidate_root, req=req)
                    if isinstance(cleanup, dict) and cleanup:
                        req["candidate_backlog_cleanup"] = cleanup
                except Exception as e:
                    req["candidate_backlog_cleanup"] = {"ok": False, "action": "error", "error": str(e)}

            # Candidate review artifacts (best-effort): patch + manifest.
            patch_relpath = ""
            manifest_relpath = ""
            if _is_uat_mode(exec_mode):
                patch_path = (run_dir / "candidate.patch").resolve()
                if _write_candidate_git_diff_patch(candidate_root=candidate_root, out_path=patch_path):
                    patch_relpath = str(Path("backlog_exec_runs") / request_id / "candidate.patch").replace("\\", "/")
                    req["candidate_patch_relpath"] = patch_relpath
                manifest_path = _candidate_manifest_path(run_dir)
                if _write_candidate_manifest(
                    candidate_root=candidate_root,
                    run_dir=run_dir,
                    request_id=request_id,
                    candidate_relpath=candidate_relpath,
                ):
                    manifest_relpath = str(Path("backlog_exec_runs") / request_id / manifest_path.name).replace("\\", "/")
                    req["candidate_manifest_relpath"] = manifest_relpath

            # Persist a durable copy of the execution log into the gateway ledger (best-effort).
            persisted = _store_backlog_exec_logs_to_ledger(
                gateway_data_dir=gateway_data_dir,
                request_id=request_id,
                req=req,
                prompt=prompt,
                run_dir=run_dir,
                result=result if isinstance(result, dict) else {},
            )
            if isinstance(result, dict) and isinstance(persisted, dict) and persisted:
                result.setdefault("ledger", {}).update(persisted)

            req["result"] = result
            req["finished_at"] = str(result.get("finished_at") or _now_iso())
            if bool(result.get("ok") is True):
                # Success always requires an explicit human decision (UAT approve or inplace approve).
                req["status"] = "awaiting_qa"
                if _is_uat_mode(exec_mode):
                    req["uat_deploy"] = {"at": _now_iso(), "processes": {"status": "not_deployed", "reason": "manual_operator_action_required"}}
                else:
                    req["inplace_warning"] = "Execution mode was inplace (prod may already be mutated)."
            else:
                req["status"] = "failed"
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
        self._threads: list[threading.Thread] = []
        self._last_error: Optional[str] = None

    def last_error(self) -> Optional[str]:
        return self._last_error

    def start(self) -> None:
        if not self.cfg.enabled:
            return
        if self.is_running():
            return
        self._stop.clear()
        self._threads = []
        workers = max(1, min(32, int(getattr(self.cfg, "workers", 1) or 1)))
        for i in range(workers):
            name = f"BacklogExecWorker-{i + 1}"
            t = threading.Thread(target=self._run, name=name, daemon=True)
            self._threads.append(t)
            t.start()

    def stop(self) -> None:
        self._stop.set()
        threads = list(self._threads)
        self._threads = []
        for t in threads:
            try:
                t.join(timeout=5.0)
            except Exception:
                pass

    def is_running(self) -> bool:
        if self._stop.is_set():
            return False
        return any(t.is_alive() for t in self._threads)

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
