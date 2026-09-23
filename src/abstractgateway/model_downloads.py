"""Model downloads for the Gateway: the `{ok, job}` lane over AbstractCore's job registry.

Since AbstractCore 2.14.0 the job machinery (a worker thread per job,
single-flight per provider/artifact, pulled progress, cancel, bounded history,
snapshots persisted for other processes) lives ONCE in AbstractCore's host job
registry (`abstractcore.config.host_jobs`), reached through the one seam
(`core_config` -> Runtime `config_facade`). The Models tab, the CLI and the
`/api/gateway/jobs/*` routes read that registry directly in its own
`host_job_v1` shape.

This module keeps the OLDER lane working unchanged for the consoles that
already speak it -- the Multimodal grid's "Download" buttons, the first-run
guide and the terminal console poll `POST /models/download` and
`GET /models/download/{job}` and treat `status == "running"` as "still going":

  - the job id stays under `job` (Core also carries it as `job_id`);
  - Core's `queued` state reads as `running` here (the original Core status
    is kept under `host_status`), so a just-started job is never mistaken for
    a finished one;
  - `joined` counts the requests on this job INCLUDING the first (Core counts
    only the extra ones), as it always has on this lane.

Every other field is Core's own (`schema`, `job_id`, `kind`, `percent`,
`downloaded_bytes`, `total_bytes`, `message`, `events`/`log_tail`, `command`,
`result`, `cli_equivalent` naming `abstractgateway`, ISO `started_at`).

WHAT THIS MODULE IS NOT. It does not know how any provider downloads anything
and owns no threads: that is AbstractCore's.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .core_config import (
    HostActionRefused,
    core_host_job,
    core_host_jobs,
    core_start_model_download,
    recommended_core_model_downloads,
)

_ACTIVE = ("queued", "running")


def legacy_job_view(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A Core `host_job_v1` dict as the `{ok, job}` lane has always shown it."""

    if not isinstance(job, dict):
        return job
    out = dict(job)
    status = str(out.get("status") or "")
    out["host_status"] = status
    if status in _ACTIVE:
        out["status"] = "running"
    out["job"] = str(out.get("job") or out.get("job_id") or "")
    out["joined"] = int(out.get("joined") or 0) + 1
    if "events" not in out:
        out["events"] = list(out.get("log_tail") or [])
    # A job that ended without a result (its worker never started) keeps its
    # last progress word ("queued") as `message`; the reason is in `error`,
    # and this lane has always shown the reason as the message.
    if status in ("failed", "cancelled") and out.get("error") and str(out.get("message") or "") in ("", "queued", "running"):
        out["message"] = str(out["error"])
    return out


def _normalize(provider: Any, artifact: Any) -> tuple[str, str]:
    return str(provider or "").strip(), str(artifact or "").strip()


def start_download(
    provider: str,
    artifact: str,
    *,
    dry_run: bool = False,
    expected_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """Start (or JOIN) a download job for one artifact; returns the job snapshot.

    `joined` > 1 means this request attached to work that was already running
    -- the caller did not start a second `ollama pull`. A dry run finishes
    before this returns. Raises `ValueError` when provider or artifact is empty.
    """

    provider, artifact = _normalize(provider, artifact)
    if not provider or not artifact:
        raise ValueError("a provider and an artifact are required")
    try:
        job = core_start_model_download(provider, artifact, dry_run=dry_run, expected_bytes=expected_bytes)
    except HostActionRefused as exc:
        if exc.status_code == 400:
            raise ValueError(exc.message) from exc
        raise
    return legacy_job_view(job) or {}


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
    """One job in the legacy view, or `None` (unknown id, or forgotten after a restart)."""

    jid = str(job_id or "").strip()
    if not jid:
        return None
    return legacy_job_view(core_host_job(jid))


def list_jobs() -> List[Dict[str, Any]]:
    """Every download job the host knows about, newest first (legacy view)."""

    payload = core_host_jobs(kind="download")
    return [legacy_job_view(job) for job in (payload.get("jobs") or []) if isinstance(job, dict)]


def active_job_for(provider: str, artifact: str) -> Optional[Dict[str, Any]]:
    """The RUNNING job for this artifact, if any -- what a grid renders a bar for."""

    provider, artifact = _normalize(provider, artifact)
    for job in list_jobs():
        if job.get("provider") == provider and job.get("artifact") == artifact and job.get("status") == "running":
            return job
    return None
