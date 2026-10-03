"""Delete a downloaded model's files from the gateway host ("Delete download").

Downloaded weights are a cache, not history: the Models page may delete them.
This module is the Gateway-proper half of that act; the removal itself is
AbstractCore's one delete verb (`model_materializer.delete_artifact`, reached
through `core_config.core_model_delete`), which uses each engine's own
mechanism:

  ollama              the daemon's `DELETE /api/delete` (what `ollama rm` calls)
  mlx / huggingface   the Hugging Face cache API (`delete_revisions`) on the
                      repo's cache folder; `org/repo:QUANT` (a llama.cpp GGUF
                      quant) removes only that quant's files
  lmstudio            refused: LM Studio keeps its own library and its CLI has
                      no remove command, so the files are managed in LM Studio

What the Gateway adds before anything is touched:

  - a model this gateway holds in memory (runtime residency) is refused with
    "Unload it first"; a locked one with "Unlock and unload it first";
  - a model an engine reports loaded (Ollama `/api/ps`) is refused the same way;
  - a download of the same artifact still running is refused ("Cancel it first");
  - `dry_run` answers the exact bytes that would be freed, for the console's
    confirmation sentence, and runs the same refusals;
  - every outcome is a typed audit event in `<data_dir>/audit_log.jsonl`
    (`model.download_deleted` / `model.download_delete_refused`).

The answer (`model_download_delete_v1`) carries `freed_bytes`, the engine's
own `paths` and `command`, and `presence: "absent"` once deleted. Words the
operator reads (`message`, `fix`) are composed here, never in a client.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Callable, Dict, Iterable, List, Optional

SCHEMA = "model_download_delete_v1"

# Engines whose models live in the shared Hugging Face cache.
_HF_FAMILY = frozenset({"mlx", "huggingface", "mlx-vlm", "mlx-gen", "diffusers", "transformers", "mflux", "llamacpp"})

_ENGINE_LABEL = {
    "ollama": "Ollama",
    "lmstudio": "LM Studio",
    "mlx": "MLX",
    "huggingface": "Hugging Face",
    "llamacpp": "llama.cpp",
}


class DownloadDeleteRefused(Exception):
    """A refusal with its HTTP status and a body the console shows as is."""

    def __init__(self, status_code: int, reason: str, message: str, fix: str = "", **extra: Any) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.body: Dict[str, Any] = {
            "schema": SCHEMA,
            "ok": False,
            "status": "refused" if status_code == 409 else ("not_found" if status_code == 404 else "failed"),
            "reason": reason,
            "message": message,
            "fix": fix,
        }
        self.body.update({k: v for k, v in extra.items() if v is not None})


def _label(provider: str) -> str:
    return _ENGINE_LABEL.get(provider, provider)


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def _repo_of(artifact: str) -> str:
    head, sep, tail = artifact.partition(":")
    return head if sep and "/" in head and tail else artifact


def _ollama_same(a: str, b: str) -> bool:
    def full(x: str) -> str:
        x = _norm(x)
        return x if ":" in x else x + ":latest"

    return full(a) == full(b)


def _same_model(provider: str, artifact: str, row: Dict[str, Any]) -> bool:
    """Is this residency row the artifact about to be deleted?"""

    rp = _norm(row.get("provider"))
    model = str(row.get("model") or "").strip()
    if not model:
        return False
    if provider == "ollama":
        return rp == "ollama" and _ollama_same(model, artifact)
    if provider in _HF_FAMILY:
        if rp not in _HF_FAMILY:
            return False
        names = {_norm(artifact), _norm(_repo_of(artifact))}
        return _norm(model) in names or _norm(_repo_of(model)) in names
    return rp == provider and _norm(model) == _norm(artifact)


def resident_block(provider: str, artifact: str, rows: Iterable[Dict[str, Any]]) -> Optional[DownloadDeleteRefused]:
    """The refusal for a model this gateway holds in memory, or None."""

    for row in rows or []:
        if not isinstance(row, dict) or not _same_model(provider, artifact, row):
            continue
        if row.get("locked") is True:
            return DownloadDeleteRefused(
                409,
                "locked",
                "This model is locked in memory, so its files cannot be deleted.",
                "Unlock and unload it first (Models › Loaded), then delete the download.",
                runtime_id=row.get("runtime_id"),
            )
        if row.get("resident") is not False:
            return DownloadDeleteRefused(
                409,
                "resident",
                "This model is loaded, so its files cannot be deleted.",
                "Unload it first (Models › Loaded), then delete the download.",
                runtime_id=row.get("runtime_id"),
            )
    return None


def _refusal_from_blockers(provider: str, artifact: str, blockers: List[str], message: str) -> DownloadDeleteRefused:
    if "loaded" in blockers:
        return DownloadDeleteRefused(
            409, "resident", f"{_label(provider)} has this model loaded, so its files cannot be deleted.",
            "Unload it first, then delete the download.", delete_blockers=blockers,
        )
    if "engine_not_running" in blockers:
        return DownloadDeleteRefused(
            409, "engine_not_running", f"{_label(provider)} is not running, so it cannot remove its own files.",
            f"Start {_label(provider)}, then delete the download.", delete_blockers=blockers,
        )
    if "unknown_location" in blockers:
        return DownloadDeleteRefused(
            409, "unknown_location", "The files of this model could not be located, so nothing was deleted.",
            f"Remove it from {_label(provider)} itself.", delete_blockers=blockers,
        )
    if "remote_engine" in blockers:
        return DownloadDeleteRefused(
            409, "remote_engine", f"{_label(provider)} serves this model from another computer; nothing is stored here.",
            "", delete_blockers=blockers,
        )
    return DownloadDeleteRefused(409, "refused", message or "AbstractCore refused the delete.", "", delete_blockers=blockers)


def _shared_only(blockers: List[str]) -> bool:
    return bool(blockers) and all(str(b).startswith("shared_cache:") for b in blockers)


def _also_used_by(provider: str, blockers: List[str]) -> List[str]:
    out: List[str] = []
    for b in blockers:
        if not str(b).startswith("shared_cache:"):
            continue
        for name in str(b).split(":", 1)[1].split(","):
            name = name.strip()
            if name and name != provider and _label(name) not in out:
                out.append(_label(name))
    return out


def audit_event(event: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Append one typed model event to the gateway audit log. Never raises."""

    allowed = {"provider", "artifact", "actor", "outcome", "reason", "freed_bytes", "dry_run", "paths"}
    try:
        from .security.gateway_security import _AUDIT_LOCK, _audit_data_dir_from_env, _audit_log_enabled

        if not _audit_log_enabled(default=True):
            return None
        entry: Dict[str, Any] = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(), "event": str(event)}
        entry.update({k: v for k, v in fields.items() if k in allowed and v is not None})
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _AUDIT_LOCK:
            path = (_audit_data_dir_from_env() / "audit_log.jsonl").resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as fh:
                fh.write(line.encode("utf-8", errors="replace"))
        return entry
    except Exception:
        return None


def delete_download(
    provider: str,
    artifact: str,
    *,
    dry_run: bool = False,
    actor: str = "",
    resident_rows: Optional[Callable[[], List[Dict[str, Any]]]] = None,
    active_job: Optional[Callable[[str, str], Optional[Dict[str, Any]]]] = None,
    core_delete: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Delete (or, with `dry_run`, measure) one downloaded artifact.

    Returns the `model_download_delete_v1` answer; raises
    `DownloadDeleteRefused` (409 refused / 404 not_found / 502 failed) with a
    body ready to send. The three callables are seams for tests; the defaults
    are the live ones.
    """

    from .core_config import HostActionRefused, core_model_delete
    from .model_downloads import active_job_for

    provider = _norm(provider)
    artifact = str(artifact or "").strip()
    if not provider or not artifact:
        raise DownloadDeleteRefused(400, "invalid", "A provider and an artifact are required.")
    try:
        return _delete(provider, artifact, dry_run, actor, resident_rows, active_job or active_job_for, core_delete or core_model_delete, HostActionRefused)
    except DownloadDeleteRefused as exc:
        if not dry_run:
            audit_event(
                "model.download_delete_refused", provider=provider, artifact=artifact, actor=actor or None,
                outcome=exc.body.get("status"), reason=exc.body.get("reason"),
            )
        raise


def _delete(
    provider: str,
    artifact: str,
    dry_run: bool,
    actor: str,
    resident_rows: Optional[Callable[[], List[Dict[str, Any]]]],
    active_job: Callable[[str, str], Optional[Dict[str, Any]]],
    core_delete: Callable[..., Dict[str, Any]],
    host_refused: type,
) -> Dict[str, Any]:
    if provider == "lmstudio":
        raise DownloadDeleteRefused(
            409, "managed_elsewhere", "LM Studio keeps its own model library, so this model is deleted in LM Studio.",
            "Open LM Studio › My Models and delete it there.",
        )
    if active_job(provider, artifact) is not None:
        raise DownloadDeleteRefused(
            409, "downloading", "This model is still downloading.", "Cancel the download first, then delete it.",
        )
    if resident_rows is not None:
        block = resident_block(provider, artifact, resident_rows())
        if block is not None:
            raise block
    force = False
    try:
        job = core_delete(provider, artifact, dry_run=True, force=False, run_inline=True)
    except host_refused as exc:  # type: ignore[misc]
        payload = exc.payload() if hasattr(exc, "payload") else {}
        blockers = [str(b) for b in (payload.get("delete_blockers") or [])]
        if getattr(exc, "status_code", 0) == 404 or payload.get("status") == "not_found":
            raise DownloadDeleteRefused(404, "not_downloaded", "This model is not downloaded on this computer.", "", presence="absent") from None
        if not _shared_only(blockers):
            raise _refusal_from_blockers(provider, artifact, blockers, str(payload.get("message") or exc)) from None
        # The same files are classified under a sibling engine of the shared
        # Hugging Face cache (MLX vs Hugging Face). Files are files: deleting
        # them from this row is the operator's explicit act, and every OTHER
        # blocker (loaded, unknown location) was ruled out above.
        force = True
        job = core_delete(provider, artifact, dry_run=True, force=True, run_inline=True)
        job = dict(job, _also_used_by=_also_used_by(provider, blockers))
    plan = _result_of(job)
    also = list(job.get("_also_used_by") or [])
    if plan.get("status") == "not_found":
        raise DownloadDeleteRefused(404, "not_downloaded", "This model is not downloaded on this computer.", "", presence="absent")
    if plan.get("status") != "planned":
        raise _refusal_from_blockers(provider, artifact, list(plan.get("delete_blockers") or []), str(plan.get("message") or ""))
    if dry_run:
        return _answer("planned", provider, artifact, plan, also)
    done = _result_of(core_delete(provider, artifact, dry_run=False, force=force, run_inline=True))
    if done.get("status") != "deleted":
        raise DownloadDeleteRefused(
            502, "engine_failed", f"{_label(provider)} did not delete the files: {done.get('message') or 'no reason given'}",
            "Check again; if it persists, remove it with the engine's own tool.", command=done.get("command"),
        )
    out = _answer("deleted", provider, artifact, done, also)
    audit_event(
        "model.download_deleted", provider=provider, artifact=artifact, outcome="deleted", actor=actor or None,
        freed_bytes=out["freed_bytes"], paths=out["paths"],
    )
    return out


def _result_of(job: Any) -> Dict[str, Any]:
    if not isinstance(job, dict):
        return {}
    result = job.get("result")
    return dict(result) if isinstance(result, dict) else {}


def _answer(status: str, provider: str, artifact: str, result: Dict[str, Any], also: List[str]) -> Dict[str, Any]:
    freed = result.get("freed_bytes")
    deleted = status == "deleted"
    return {
        "schema": SCHEMA,
        "ok": True,
        "status": status,
        "provider": provider,
        "artifact": artifact,
        "freed_bytes": int(freed) if isinstance(freed, int) and not isinstance(freed, bool) else None,
        "paths": [str(p) for p in (result.get("paths") or [])],
        "command": [str(c) for c in (result.get("command") or [])],
        "also_used_by": also,
        "presence": "absent" if deleted else "installed",
        "message": "Deleted from this computer." if deleted else "Ready to delete.",
    }
