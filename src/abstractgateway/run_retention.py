from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from abstractruntime.storage.base import QueryableRunIndexStore, QueryableRunStore

from .service import is_draft_run_lifecycle, run_summary


TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
DEFAULT_DRAFT_RUN_TTL_S = 7 * 24 * 60 * 60
GATEWAY_WORKSPACE_MARKER = ".abstractgateway-workspace.json"
RUN_WORKSPACE_KIND = "run_workspace"
SESSION_WORKSPACE_KIND = "session_workspace"
SESSION_WORKSPACE_PREFIX = "session-"
_HEX_WORKSPACE_RE = re.compile(r"^[a-fA-F0-9]{32}$")
_SESSION_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


class DraftRunPurgeUnsupported(RuntimeError):
    """Configured stores do not expose the deletion APIs required for purge."""


@dataclass(frozen=True)
class DraftRunPurgeOptions:
    dry_run: bool = True
    limit: int = 200
    session_id: Optional[str] = None
    workflow_id: Optional[str] = None
    run_ids: Optional[List[str]] = None
    force: bool = False
    include_active: bool = False
    delete_artifacts: bool = True
    delete_workspaces: bool = True


def default_draft_run_ttl_s() -> int:
    for key in ("ABSTRACTGATEWAY_DRAFT_RUN_RETENTION_TTL_S", "ABSTRACTGATEWAY_DRAFT_RUN_TTL_S"):
        raw = os.getenv(key)
        if isinstance(raw, str) and raw.strip():
            try:
                return max(0, int(raw.strip()))
            except Exception:
                return DEFAULT_DRAFT_RUN_TTL_S
    return DEFAULT_DRAFT_RUN_TTL_S


def session_workspace_dirname(
    session_id: Any,
    *,
    tenant_id: Any = "",
    user_id: Any = "",
) -> Optional[str]:
    """Deterministic folder name for a session's gateway-owned workspace.

    `None` when there is no session id — the caller then falls back to the
    historical per-run `uuid4().hex` folder.

    The name is derived, never looked up: the same session resolves to the
    same folder after a gateway restart, from any process (HTTP route, chat
    bridge), with no store read on the run-start hot path. The digest covers
    tenant + user so two principals sharing one data dir (single-user mode
    with several accounts) can never land in the same folder even when a
    client reuses a session id; the readable slug is cosmetic.

    The `session-` prefix is load-bearing for retention: it keeps the folder
    out of `_HEX_WORKSPACE_RE`, the markerless per-run shape the draft purge
    deletes (see `_gateway_owned_workspace_path`).
    """
    sid = str(session_id or "").strip()
    if not sid:
        return None
    key = "\x1f".join([str(tenant_id or ""), str(user_id or ""), sid])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    slug = _SESSION_SLUG_RE.sub("-", sid).strip("-._")[:40].strip("-._")
    if not slug:
        return f"{SESSION_WORKSPACE_PREFIX}{digest}"
    return f"{SESSION_WORKSPACE_PREFIX}{slug}-{digest}"


def resolve_gateway_run_workspace(
    data_dir: Any,
    *,
    session_id: Any = None,
    tenant_id: Any = "",
    user_id: Any = "",
) -> tuple[Path, bool]:
    """The gateway-owned folder a run works in when the client named none.

    Returns `(path, session_scoped)`; the directory exists on return.

    Session-stable by default (2026-09-22). A per-run `workspaces/<uuid4>`
    is rendered into the SYSTEM prompt as the run's default working
    directory, so in a conversation — where every turn is a new run — the
    HEAD of the prompt changed on every turn and no prefix/KV cache could
    restore anything (Mission A measured `cold`, `cached_tokens: 0` on all
    7 turns of a session). Clients that read the first run's workspace back
    and echo it (abstractcode web, abstractassistant) papered over this;
    clients that do not (the Telegram bridge, thin HTTP callers) paid full
    prefill every turn. Deriving the folder from the session id fixes it for
    every client at once, and is also what multi-turn file work wants: turn
    2 sees the files turn 1 wrote.
    """
    base = Path(data_dir).expanduser() / "workspaces"
    base.mkdir(parents=True, exist_ok=True)
    name = session_workspace_dirname(session_id, tenant_id=tenant_id, user_id=user_id)
    session_scoped = name is not None
    if not name:
        name = uuid.uuid4().hex
    ws_dir = base / name
    ws_dir.mkdir(parents=True, exist_ok=True)
    return ws_dir, session_scoped


def write_gateway_workspace_marker(
    workspace: Path,
    *,
    run_id: str,
    session_id: Any = None,
    session_scoped: bool = False,
) -> None:
    """Stamp gateway ownership on a minted workspace.

    A session-scoped folder is stamped ONCE (the first run of the session)
    and records `first_run_id`, never `run_id`: the marker's `run_id` is what
    the draft purge matches a folder to a run tree by, and a shared folder
    must not die with the run that happened to create it.
    """
    rid = str(run_id or "").strip()
    if not rid:
        return
    path = Path(workspace)
    if not path.exists() or not path.is_dir():
        return
    marker_path = path / GATEWAY_WORKSPACE_MARKER
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if session_scoped:
        if marker_path.exists():
            return
        marker: Dict[str, Any] = {
            "owner": "abstractgateway",
            "kind": SESSION_WORKSPACE_KIND,
            "session_id": str(session_id or "").strip(),
            "first_run_id": rid,
            "created_at": now,
        }
    else:
        marker = {
            "owner": "abstractgateway",
            "kind": RUN_WORKSPACE_KIND,
            "run_id": rid,
            "created_at": now,
        }
    marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")


def purge_ephemeral_draft_runs(svc: Any, options: DraftRunPurgeOptions) -> Dict[str, Any]:
    run_store = svc.host.run_store
    now = datetime.datetime.now(datetime.timezone.utc)
    ttl_s = default_draft_run_ttl_s()
    roots, scanned = _load_candidate_roots(run_store=run_store, options=options)

    items: list[Dict[str, Any]] = []
    eligible = 0
    purged = 0
    skipped = 0

    for summary in roots:
        rid = str(summary.get("run_id") or "").strip()
        item: Dict[str, Any] = {
            "run_id": rid,
            "status": summary.get("status"),
            "workflow_id": summary.get("workflow_id"),
            "session_id": summary.get("session_id"),
            "run_lifecycle": summary.get("run_lifecycle"),
        }
        if not rid or summary.get("missing") is True:
            item.update({"eligible": False, "reason": "missing"})
            skipped += 1
            items.append(item)
            continue

        lifecycle = summary.get("run_lifecycle")
        if not _is_ephemeral_draft_lifecycle(lifecycle):
            item.update({"eligible": False, "reason": "not_ephemeral_draft"})
            skipped += 1
            items.append(item)
            continue

        status = str(summary.get("status") or "").strip().lower()
        if status not in TERMINAL_RUN_STATUSES and not options.include_active:
            item.update({"eligible": False, "reason": "active_run"})
            skipped += 1
            items.append(item)
            continue

        expires_at = _effective_expires_at(summary, default_ttl_s=ttl_s)
        if expires_at is not None:
            item["effective_expires_at"] = expires_at.isoformat()
        if not options.force:
            if expires_at is None:
                item.update({"eligible": False, "reason": "no_effective_expiration"})
                skipped += 1
                items.append(item)
                continue
            if expires_at > now:
                item.update({"eligible": False, "reason": "not_expired"})
                skipped += 1
                items.append(item)
                continue

        result = purge_run_tree(
            svc=svc,
            root_run_id=rid,
            dry_run=options.dry_run,
            include_active=options.include_active,
            delete_artifacts=options.delete_artifacts,
            delete_workspaces=options.delete_workspaces,
        )
        if result.get("skipped"):
            item.update({"eligible": False, "reason": result.get("reason") or "skipped", "purge": result})
            skipped += 1
            items.append(item)
            continue

        item.update({"eligible": True, "purge": result})
        eligible += 1
        if not options.dry_run:
            purged += 1
        items.append(item)

    return {
        "ok": True,
        "dry_run": bool(options.dry_run),
        "policy": {
            "purpose": "draft_test",
            "retention_mode": "ephemeral",
            "default_ttl_s": ttl_s,
            "terminal_statuses": sorted(TERMINAL_RUN_STATUSES),
        },
        "scanned": int(scanned),
        "eligible": int(eligible),
        "purged": int(purged),
        "skipped": int(skipped),
        "items": items,
    }


def purge_run_tree(
    *,
    svc: Any,
    root_run_id: str,
    dry_run: bool,
    include_active: bool,
    delete_artifacts: bool,
    delete_workspaces: bool,
) -> Dict[str, Any]:
    run_store = svc.host.run_store
    ledger_store = svc.host.ledger_store
    command_store = getattr(svc.stores, "command_store", None)
    artifact_store = getattr(svc.stores, "artifact_store", None)

    run_delete = getattr(run_store, "delete", None)
    ledger_delete = getattr(ledger_store, "delete", None)
    if not dry_run and (not callable(run_delete) or not callable(ledger_delete)):
        raise DraftRunPurgeUnsupported("Configured Runtime stores do not support run purge")

    run_ids = collect_run_tree(run_store, root_run_id)
    loaded: Dict[str, Any] = {}
    active: list[str] = []
    for rid in run_ids:
        try:
            run = run_store.load(rid)
        except Exception:
            run = None
        if run is None:
            continue
        loaded[rid] = run
        status = str(getattr(getattr(run, "status", None), "value", getattr(run, "status", "")) or "").strip().lower()
        if status not in TERMINAL_RUN_STATUSES:
            active.append(rid)

    if active and not include_active:
        return {"ok": False, "skipped": True, "reason": "active_run_tree", "run_ids": run_ids, "active_run_ids": active}

    workspace_paths = _owned_workspace_paths(svc, loaded)
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "run_ids": run_ids,
            "would_delete": {
                "runs": len(run_ids),
                "workspaces": len(workspace_paths) if delete_workspaces else 0,
                "artifacts": None,
                "ledger_records": None,
                "commands": None,
            },
        }

    artifacts_deleted = 0
    ledger_records_deleted = 0
    commands_deleted = 0
    run_checkpoints_deleted = 0
    workspaces_deleted = 0

    artifact_delete_by_run = getattr(artifact_store, "delete_by_run", None) if artifact_store is not None else None
    command_delete_by_run = getattr(command_store, "delete_by_run", None) if command_store is not None else None

    for rid in reversed(run_ids):
        if delete_artifacts and callable(artifact_delete_by_run):
            artifacts_deleted += int(artifact_delete_by_run(rid) or 0)
        if callable(command_delete_by_run):
            commands_deleted += int(command_delete_by_run(rid) or 0)
        ledger_records_deleted += int(ledger_delete(rid) or 0)
        if bool(run_delete(rid)):
            run_checkpoints_deleted += 1

    if delete_workspaces:
        for ws in workspace_paths:
            if _delete_owned_workspace(ws):
                workspaces_deleted += 1

    gc_report = None
    if delete_artifacts and artifact_store is not None:
        gc = getattr(artifact_store, "gc", None)
        if callable(gc):
            try:
                gc_report = gc(dry_run=False)
            except Exception as e:
                gc_report = {"ok": False, "error": str(e)}

    return {
        "ok": True,
        "dry_run": False,
        "run_ids": run_ids,
        "deleted": {
            "runs": run_checkpoints_deleted,
            "ledger_records": ledger_records_deleted,
            "commands": commands_deleted,
            "artifacts": artifacts_deleted,
            "workspaces": workspaces_deleted,
        },
        "artifact_gc": gc_report,
    }


def collect_run_tree(run_store: Any, root_run_id: str, *, limit: int = 2000) -> list[str]:
    out: list[str] = []
    queue: list[str] = [str(root_run_id)]
    seen: set[str] = set()
    list_children = getattr(run_store, "list_children", None)
    while queue and len(out) < int(limit):
        rid = str(queue.pop(0) or "").strip()
        if not rid or rid in seen:
            continue
        seen.add(rid)
        out.append(rid)
        if not callable(list_children):
            continue
        for child in list_children(parent_run_id=rid) or []:
            cid = getattr(child, "run_id", None)
            if isinstance(cid, str) and cid and cid not in seen:
                queue.append(cid)
    return out


def _load_candidate_roots(*, run_store: Any, options: DraftRunPurgeOptions) -> tuple[list[Dict[str, Any]], int]:
    exact_ids = [str(r or "").strip() for r in (options.run_ids or []) if str(r or "").strip()]
    if exact_ids:
        rows: list[Dict[str, Any]] = []
        for rid in exact_ids[: int(options.limit)]:
            run = run_store.load(rid)
            if run is None:
                rows.append({"run_id": rid, "missing": True})
            else:
                rows.append(run_summary(run))
        return rows, len(rows)

    wid = str(options.workflow_id).strip() if isinstance(options.workflow_id, str) and options.workflow_id.strip() else None
    sid = str(options.session_id).strip() if isinstance(options.session_id, str) and options.session_id.strip() else None
    scan_limit = max(int(options.limit), min(5000, int(options.limit) * 5))

    if isinstance(run_store, QueryableRunIndexStore):
        rows = run_store.list_run_index(status=None, workflow_id=wid, session_id=sid, root_only=True, limit=scan_limit)
        out: list[Dict[str, Any]] = []
        for row in rows or []:
            summary = _summary_from_index_row(row)
            if _is_ephemeral_draft_lifecycle(summary.get("run_lifecycle")):
                out.append(summary)
                if len(out) >= int(options.limit):
                    break
        return out, len(rows or [])

    if not isinstance(run_store, QueryableRunStore):
        raise DraftRunPurgeUnsupported("Run store does not support listing runs")

    runs = run_store.list_runs(status=None, workflow_id=wid, limit=scan_limit)
    out = []
    scanned = 0
    for run in runs or []:
        scanned += 1
        if sid and str(getattr(run, "session_id", "") or "").strip() != sid:
            continue
        if str(getattr(run, "parent_run_id", "") or "").strip():
            continue
        summary = run_summary(run)
        if _is_ephemeral_draft_lifecycle(summary.get("run_lifecycle")):
            out.append(summary)
            if len(out) >= int(options.limit):
                break
    return out, scanned


def _summary_from_index_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": row.get("run_id"),
        "workflow_id": row.get("workflow_id"),
        "status": str(row.get("status") or "").strip(),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "actor_id": row.get("actor_id"),
        "session_id": row.get("session_id"),
        "parent_run_id": row.get("parent_run_id"),
        "run_lifecycle": row.get("run_lifecycle") if isinstance(row.get("run_lifecycle"), dict) else None,
    }


def _is_ephemeral_draft_lifecycle(lifecycle: Any) -> bool:
    if not is_draft_run_lifecycle(lifecycle):
        return False
    retention = lifecycle.get("retention") if isinstance(lifecycle, dict) else None
    if not isinstance(retention, dict):
        return False
    return str(retention.get("mode") or "").strip().lower() == "ephemeral"


def _parse_iso_datetime(value: Any) -> Optional[datetime.datetime]:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _effective_expires_at(summary: Dict[str, Any], *, default_ttl_s: int) -> Optional[datetime.datetime]:
    lifecycle = summary.get("run_lifecycle") if isinstance(summary, dict) else None
    retention = lifecycle.get("retention") if isinstance(lifecycle, dict) else None
    if not isinstance(retention, dict):
        return None

    explicit_expires = _parse_iso_datetime(retention.get("expires_at"))
    if explicit_expires is not None:
        return explicit_expires

    ttl_s: Optional[int] = None
    raw_ttl = retention.get("ttl_s")
    if raw_ttl is not None and not isinstance(raw_ttl, bool):
        try:
            ttl = int(raw_ttl)
        except Exception:
            ttl = 0
        if ttl > 0:
            ttl_s = ttl
    if ttl_s is None:
        ttl_s = int(default_ttl_s)
    if ttl_s <= 0:
        return None

    base = _parse_iso_datetime(summary.get("updated_at")) or _parse_iso_datetime(summary.get("created_at"))
    if base is None:
        return None
    return base + datetime.timedelta(seconds=ttl_s)


def _owned_workspace_paths(svc: Any, loaded_runs: Dict[str, Any]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    run_ids = set(loaded_runs.keys())
    for rid, run in loaded_runs.items():
        vars_obj = getattr(run, "vars", None)
        raw_ws = vars_obj.get("workspace_root") if isinstance(vars_obj, dict) else None
        ws = _gateway_owned_workspace_path(svc, raw_ws, run_id=rid, run_tree_ids=run_ids)
        if ws is None:
            continue
        key = str(ws)
        if key in seen:
            continue
        seen.add(key)
        out.append(ws)
    return out


def _gateway_owned_workspace_path(
    svc: Any,
    raw_path: Any,
    *,
    run_id: str,
    run_tree_ids: set[str],
) -> Optional[Path]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    base = (Path(getattr(svc.stores, "base_dir", "")) / "workspaces").expanduser().resolve()
    try:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = Path(getattr(svc.stores, "base_dir", "")).expanduser() / path
        resolved = path.resolve()
        resolved.relative_to(base)
    except Exception:
        return None
    if resolved == base:
        return None

    # A session-scoped workspace is shared by every run of the conversation:
    # purging one draft run of it must never delete the folder the next turn
    # (and every other turn already recorded) works in.
    if resolved.name.startswith(SESSION_WORKSPACE_PREFIX):
        return None

    marker_path = resolved / GATEWAY_WORKSPACE_MARKER
    if marker_path.exists():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8") or "{}")
        except Exception:
            return None
        if not isinstance(marker, dict) or marker.get("owner") != "abstractgateway":
            return None
        if marker.get("kind") == SESSION_WORKSPACE_KIND:
            return None
        marker_run_id = str(marker.get("run_id") or "").strip()
        if marker_run_id and marker_run_id in run_tree_ids:
            return resolved
        return None

    if _HEX_WORKSPACE_RE.match(resolved.name) and str(run_id or "").strip():
        return resolved
    return None


def _delete_owned_workspace(path: Path) -> bool:
    if path.is_dir():
        shutil.rmtree(path)
        return True
    if path.exists():
        path.unlink()
        return True
    return False
