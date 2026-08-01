"""Background model downloads for the Gateway: jobs, progress, single-flight.

A model download is minutes of network I/O. Three things follow, and this
module exists to make all three true at once:

  - IT MUST NOT BLOCK THE EVENT LOOP. The work runs on a worker thread; the
    request that starts it returns a job id immediately.
  - A SECOND REQUEST FOR THE SAME ARTIFACT JOINS THE FIRST. Two operators (or
    one operator and their double-click) asking for `qwen/qwen3.5-9b@4bit` must
    produce ONE `lms get`, not two writing the same files. The single-flight
    key is provider+artifact, and a duplicate POST returns the running job.
  - PROGRESS IS PULLABLE, NOT PUSHED. The job keeps the last progress line and
    a bounded tail of events; a console polls `GET .../download/{job}`.

WHAT THIS MODULE IS NOT. It does not know how any provider downloads anything
-- that is AbstractCore's materializer, reached through `core_config`, the one
seam. It owns threads, ids and buffers, which are Gateway-proper concerns.

RESTART SEMANTICS, STATED. Jobs live in this process only. A Gateway restart
loses the job list; the DOWNLOAD may well have completed (the provider tool
owns the bytes), so the honest recovery is to re-probe availability rather than
to resume a job. A console that polls a job id across a restart gets 404 and
should fall back to the availability grid.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .core_config import core_model_download, recommended_core_model_downloads

# The tail a console needs to show "what is it doing right now" without the
# server growing a transcript of every byte counter Ollama ever emitted.
_MAX_EVENTS = 60


@dataclass
class _Job:
    id: str
    provider: str
    artifact: str
    dry_run: bool
    status: str = "running"  # running | completed | failed
    message: str = ""
    percent: Optional[float] = None
    downloaded_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    events: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    result: Optional[Dict[str, Any]] = None
    #: How many requests joined this job (1 = the one that started it).
    joined: int = 1

    def key(self) -> str:
        return f"{self.provider}/{self.artifact}"

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "job": self.id,
            "provider": self.provider,
            "artifact": self.artifact,
            "status": self.status,
            "dry_run": bool(self.dry_run),
            "message": self.message,
            "events": list(self.events),
            "started_at": self.started_at,
            "elapsed_s": round((self.finished_at or time.time()) - self.started_at, 1),
            "joined": self.joined,
        }
        for name in ("percent", "downloaded_bytes", "total_bytes", "finished_at"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.result is not None:
            out["result"] = dict(self.result)
        return out


_LOCK = threading.Lock()
_JOBS: Dict[str, _Job] = {}
_BY_KEY: Dict[str, str] = {}


def _normalize(provider: Any, artifact: Any) -> tuple[str, str]:
    return str(provider or "").strip(), str(artifact or "").strip()


def start_download(provider: str, artifact: str, *, dry_run: bool = False) -> Dict[str, Any]:
    """Start (or JOIN) a download job for one artifact.

    Returns the job snapshot. `joined` > 1 on the snapshot means this request
    attached to work that was already running -- the caller did not start a
    second `ollama pull`.
    """

    provider, artifact = _normalize(provider, artifact)
    if not provider or not artifact:
        raise ValueError("a provider and an artifact are required")

    key = f"{provider}/{artifact}"
    with _LOCK:
        existing_id = _BY_KEY.get(key)
        existing = _JOBS.get(existing_id or "")
        if existing is not None and existing.status == "running":
            existing.joined += 1
            return existing.to_dict()
        job = _Job(id=uuid.uuid4().hex[:12], provider=provider, artifact=artifact, dry_run=bool(dry_run))
        job.message = f"queued {artifact}"
        _JOBS[job.id] = job
        _BY_KEY[key] = job.id
        _prune_locked()

    try:
        threading.Thread(target=_run, args=(job.id,), name=f"model-download-{job.id}", daemon=True).start()
    except Exception as exc:
        # A job whose worker never started would sit at `running` forever, and
        # -- worse -- keep the single-flight slot, so EVERY later request for
        # that artifact would join a job that can never finish. Fail it here
        # and free the slot; the next request starts real work.
        _finish(job.id, {"provider": provider, "artifact": artifact, "ok": False, "status": "failed", "message": f"could not start the download worker: {exc}"})
    return get_job(job.id) or job.to_dict()


def start_recommended_downloads(*, dry_run: bool = False) -> List[Dict[str, Any]]:
    """One action, exactly the recommended set.

    Every recommended artifact gets a job. Artifacts already installed finish
    immediately as `already_installed` (the materializer short-circuits), so
    the caller does not need to pre-filter -- and cannot race a probe into
    re-downloading something that landed a second ago.
    """

    jobs: List[Dict[str, Any]] = []
    for item in recommended_core_model_downloads():
        try:
            jobs.append(start_download(item["provider"], item["artifact"], dry_run=dry_run))
        except Exception as exc:
            jobs.append(
                {
                    "job": None,
                    "provider": item.get("provider"),
                    "artifact": item.get("artifact"),
                    "status": "failed",
                    "message": str(exc),
                }
            )
    return jobs


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        job = _JOBS.get(str(job_id or "").strip())
        return job.to_dict() if job is not None else None


def list_jobs() -> List[Dict[str, Any]]:
    with _LOCK:
        return [job.to_dict() for job in sorted(_JOBS.values(), key=lambda j: j.started_at, reverse=True)]


def active_job_for(provider: str, artifact: str) -> Optional[Dict[str, Any]]:
    """The RUNNING job for this artifact, if any -- what a grid renders a bar for."""

    provider, artifact = _normalize(provider, artifact)
    with _LOCK:
        job = _JOBS.get(_BY_KEY.get(f"{provider}/{artifact}", ""))
        if job is None or job.status != "running":
            return None
        return job.to_dict()


def reset_for_tests() -> None:
    with _LOCK:
        _JOBS.clear()
        _BY_KEY.clear()


def _prune_locked(limit: int = 40) -> None:
    """Keep the finished-job tail bounded; never drop a running job."""

    if len(_JOBS) <= limit:
        return
    finished = sorted(
        (j for j in _JOBS.values() if j.status != "running"),
        key=lambda j: j.finished_at or j.started_at,
    )
    for job in finished[: max(0, len(_JOBS) - limit)]:
        _JOBS.pop(job.id, None)
        if _BY_KEY.get(job.key()) == job.id:
            _BY_KEY.pop(job.key(), None)


def _progress_sink(job_id: str) -> Callable[[Any], None]:
    def emit(progress: Any) -> None:
        message = str(getattr(progress, "message", "") or "").strip()
        percent = getattr(progress, "percent", None)
        with _LOCK:
            job = _JOBS.get(job_id)
            if job is None:
                return
            if message:
                job.message = message
            if percent is not None:
                job.percent = float(percent)
            downloaded = getattr(progress, "downloaded_bytes", None)
            total = getattr(progress, "total_bytes", None)
            if downloaded is not None:
                job.downloaded_bytes = int(downloaded)
            if total is not None:
                job.total_bytes = int(total)
            # Only STATE changes earn an event line; a byte counter that ticks
            # 4000 times must not become 4000 rows in the buffer.
            if message and (not job.events or job.events[-1] != message):
                job.events.append(message)
                if len(job.events) > _MAX_EVENTS:
                    del job.events[: len(job.events) - _MAX_EVENTS]

    return emit


def _run(job_id: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        provider, artifact, dry_run = job.provider, job.artifact, job.dry_run

    try:
        result = core_model_download(
            provider,
            artifact,
            progress_cb=_progress_sink(job_id),
            dry_run=dry_run,
        )
    except Exception as exc:
        result = {
            "provider": provider,
            "artifact": artifact,
            "ok": False,
            "status": "failed",
            "message": str(exc),
        }

    _finish(job_id, result)


def _finish(job_id: str, result: Dict[str, Any]) -> None:
    """Land a job's outcome and free its single-flight slot. Idempotent."""

    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.result = dict(result)
        job.status = "completed" if result.get("ok") else "failed"
        job.message = str(result.get("message") or job.message)
        job.finished_at = time.time()
        if job.status == "completed":
            job.percent = 100.0
        # The single-flight slot frees on finish: the NEXT request for the same
        # artifact must be able to start real work (a failed pull is worth
        # retrying), rather than being handed a corpse to poll forever.
        if _BY_KEY.get(job.key()) == job.id:
            _BY_KEY.pop(job.key(), None)
