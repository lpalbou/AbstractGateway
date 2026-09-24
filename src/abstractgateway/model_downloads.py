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

PROGRESS (AbstractCore >= the progress contract). Each job also carries
`state` (queued|resolving|downloading|verifying|installing|done|failed|
cancelled|stalled), `bytes_done`/`bytes_total`/`size_unknown`/`size_note`,
`percent`, `bytes_per_second`, `eta_s`, `updated_at`, `files`,
`current_file`, a plain-language `message` and the provider's own `detail`;
see `docs/model-downloads.md` for the contract with one example per state.

AGGREGATE JOBS. "Use recommended defaults" fetches several artifacts; this
module groups their jobs under ONE parent (`grp_...`, `kind:
"download_group"`) whose numbers are computed from its children on every
read -- overall bytes, percent, speed, ETA, a `state`, and the child list --
so a console renders one bar for the whole action and one row per model.
Groups live in this Gateway process (children are Core jobs and persist).

WHAT THIS MODULE IS NOT. It does not know how any provider downloads anything
and owns no threads: that is AbstractCore's.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from .core_config import (
    HostActionRefused,
    core_host_job,
    core_host_job_cancel,
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

    return start_recommended_group(dry_run=dry_run)["jobs"]


def start_recommended_group(*, dry_run: bool = False) -> Dict[str, Any]:
    """Start the recommended set under ONE parent job: `{"group": view, "jobs": [...]}`.

    A child that could not even start (the provider refused the request) is
    kept in the group as a `failed` child with the full reason, so the parent
    never reports "done" over a model that was never fetched.
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
                    "state": "failed",
                    "message": str(exc),
                    "error": str(exc),
                }
            )
    group_id = f"grp_{uuid.uuid4().hex[:12]}"
    record = {
        "job_id": group_id,
        "label": "Recommended models",
        "started_at": time.time(),
        "dry_run": bool(dry_run),
        "children": [
            {
                "job_id": str(job.get("job_id") or job.get("job") or "") or None,
                "provider": job.get("provider"),
                "artifact": job.get("artifact"),
                "start_error": None if (job.get("job_id") or job.get("job")) else str(job.get("message") or "could not start"),
            }
            for job in jobs
        ],
    }
    with _GROUP_LOCK:
        _GROUPS[group_id] = record
    return {"group": group_view(group_id), "jobs": jobs}


# ---------------------------------------------------------------------------
# Aggregate (parent) jobs
# ---------------------------------------------------------------------------

_GROUPS: Dict[str, Dict[str, Any]] = {}
_GROUP_LOCK = threading.Lock()
_DONE_STATES = ("done", "failed", "cancelled")


def _iso(ts: float) -> str:
    import datetime as _dt

    stamp = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


def _fmt_bytes(value: Any) -> str:
    if not isinstance(value, (int, float)) or value < 0:
        return "?"
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if value >= scale:
            n = value / scale
            return f"{n:.1f} {unit}" if n < 10 or unit == "GB" else f"{n:.0f} {unit}"
    return f"{int(value)} B"


def _fmt_duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return "?"
    s = int(seconds + 0.999)
    if s < 60:
        return f"{max(1, s)} s"
    if s < 3600:
        return f"{(s + 59) // 60} min"
    h, rest = divmod(s, 3600)
    return f"{h} h {rest // 60} min" if rest >= 60 else f"{h} h"


def _child_state(job: Dict[str, Any]) -> str:
    state = str(job.get("state") or "")
    if state:
        return state
    host = str(job.get("host_status") or job.get("status") or "")
    return {"completed": "done", "running": "downloading", "queued": "queued"}.get(host, host or "failed")


def group_view(group_id: str) -> Optional[Dict[str, Any]]:
    """The parent job, computed from its children right now; None if unknown."""

    with _GROUP_LOCK:
        record = _GROUPS.get(str(group_id or "").strip())
        record = dict(record) if record else None
    if record is None:
        return None
    children: List[Dict[str, Any]] = []
    known = 0
    for child in record["children"]:
        job = get_job(child["job_id"]) if child.get("job_id") else None
        known += int(job is not None)
        if job is None:
            reason = child.get("start_error") or "this child job is no longer known to the Gateway"
            job = {
                "job": child.get("job_id"),
                "job_id": child.get("job_id"),
                "provider": child.get("provider"),
                "artifact": child.get("artifact"),
                "status": "failed",
                "state": "failed",
                "message": reason,
                "error": reason,
            }
        children.append(job)
    if not known and any(c.get("job_id") for c in record["children"]):
        # Every child is gone from Core's registry (pruned, or a registry
        # swap): a parent over nothing would be a phantom. Forget it.
        with _GROUP_LOCK:
            _GROUPS.pop(record["job_id"], None)
        return None

    states = [_child_state(j) for j in children]
    active = [s for s in states if s not in _DONE_STATES]
    if active:
        if all(s == "stalled" for s in active):
            state = "stalled"
        elif "downloading" in active:
            state = "downloading"
        elif any(s in ("queued", "resolving") for s in active):
            state = "resolving"
        elif "stalled" in active:
            state = "stalled"
        else:
            state = active[0]
    elif "failed" in states:
        state = "failed"
    elif "cancelled" in states:
        state = "cancelled"
    else:
        state = "done"

    # Bytes: only children that actually FETCH count (already-installed ones
    # finished with nothing to move and are excluded from the total).
    fetching = [
        j for j in children
        if not ((j.get("result") or {}).get("status") in ("already_installed", "planned", "not_applicable"))
    ]
    done_bytes = sum(int(j.get("bytes_done") or j.get("downloaded_bytes") or 0) for j in fetching)
    totals = [j.get("bytes_total") or j.get("total_bytes") for j in fetching if _child_state(j) != "failed" or j.get("bytes_total")]
    unknown = [j for j in fetching if not (j.get("bytes_total") or j.get("total_bytes")) and _child_state(j) not in ("failed", "cancelled")]
    total = sum(int(t) for t in totals if isinstance(t, int)) if totals and not unknown else None
    speed_values = [j.get("bytes_per_second") for j in children if _child_state(j) in ("downloading", "stalled")]
    speed = sum(float(v) for v in speed_values if isinstance(v, (int, float))) if any(isinstance(v, (int, float)) for v in speed_values) else None
    percent = round(min(100.0, done_bytes / total * 100.0), 2) if total else (100.0 if state == "done" else None)
    eta = None
    if total and speed and speed > 0 and state in ("downloading", "resolving"):
        eta = int(max(0, total - done_bytes) / speed + 0.999)
    etas = [j.get("eta_s") for j in children if isinstance(j.get("eta_s"), (int, float)) and _child_state(j) == "downloading"]
    if etas and eta is not None:
        eta = max(eta, int(max(etas)))  # the group ends with its slowest child

    n = len(children)
    n_done = states.count("done")
    parts: List[str] = []
    if state in _DONE_STATES:
        head = {"done": f"All {n} models ready", "failed": f"{states.count('failed')} of {n} models failed", "cancelled": "Cancelled"}[state]
        parts.append(head)
        if state != "done" and n_done:
            parts.append(f"{n_done} ready")
    else:
        n_failed = states.count("failed")
        if state == "stalled":
            stalled = [j for j in children if _child_state(j) == "stalled"]
            quiet = max((float(j.get("stalled_for_s") or 0) for j in stalled), default=0.0)
            names = ", ".join(f"{j.get('provider')} {j.get('artifact')}" for j in stalled)
            parts.append(f"Stalled: no data for {_fmt_duration(quiet)} from {names}")
            parts.append(f"{n_done} of {n} ready")
        else:
            verb = "Preparing" if state == "resolving" else "Downloading"
            parts.append(f"{verb} {n} models · {n_done} of {n} ready")
        if n_failed:
            parts.append(f"{n_failed} failed")
        if total:
            parts.append(f"{_fmt_bytes(done_bytes)} of {_fmt_bytes(total)}")
        elif done_bytes:
            parts.append(f"{_fmt_bytes(done_bytes)} so far")
        if speed is not None:
            parts.append(f"{_fmt_bytes(speed)}/s")
        if eta:
            parts.append(f"{_fmt_duration(eta)} left")
        elif unknown:
            parts.append(f"{len(unknown)} of {len(fetching)} sources cannot report their size yet")
    errors = [f"{j.get('provider')} {j.get('artifact')}: {j.get('error')}" for j in children if j.get("error") and _child_state(j) == "failed"]
    # Why the parent ended, in the children's own plain words (who cancelled,
    # what failed); None while it runs or when every child finished.
    reasons = [
        f"{j.get('artifact')}: {j.get('ended_reason')}"
        for j in children
        if j.get("ended_reason") and _child_state(j) in ("failed", "cancelled")
    ]
    updated = [str(j.get("updated_at") or "") for j in children]
    host_status = "running" if active else ("completed" if state == "done" else state)
    return {
        "schema": "host_job_v1",
        "kind": "download_group",
        "job_id": record["job_id"],
        "job": record["job_id"],
        "label": record["label"],
        "status": host_status,
        "host_status": host_status,
        "state": state,
        "dry_run": record["dry_run"],
        "bytes_done": done_bytes,
        "bytes_total": total,
        "downloaded_bytes": done_bytes,
        "total_bytes": total,
        "size_unknown": bool(unknown),
        "size_note": (f"{len(unknown)} of {len(fetching)} sources have not reported a size" if unknown else None),
        "percent": percent,
        "bytes_per_second": round(speed, 1) if speed is not None else None,
        "eta_s": eta,
        "started_at": _iso(record["started_at"]),
        "updated_at": max(updated) if any(updated) else _iso(time.time()),
        "message": " · ".join(parts),
        "error": "; ".join(errors) if errors else None,
        "ended_reason": " ".join(reasons) if (reasons and not active) else None,
        "files": [
            {
                "name": f"{j.get('provider')} {j.get('artifact')}",
                "job_id": j.get("job_id") or j.get("job"),
                "bytes_done": j.get("bytes_done") if j.get("bytes_done") is not None else j.get("downloaded_bytes"),
                "bytes_total": j.get("bytes_total") if j.get("bytes_total") is not None else j.get("total_bytes"),
                "state": _child_state(j),
            }
            for j in children
        ],
        "children": children,
        "child_job_ids": [c.get("job_id") for c in record["children"]],
    }


def _group_of_children() -> Dict[str, str]:
    with _GROUP_LOCK:
        return {
            str(child["job_id"]): gid
            for gid, record in _GROUPS.items()
            for child in record["children"]
            if child.get("job_id")
        }


CANCEL_VIAS = ("console", "api")


def cancel_job(job_id: str, *, via: str = "api", user: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Cancel a download (or every active child of a group); None when unknown.

    `via` is `console` when a person clicked Cancel in a console (the console
    says so in the request body), else `api`; `user` is the signed-in admin.
    Both are recorded on the job by AbstractCore (`cancelled_by`,
    `cancelled_by_user`, `ended_reason`), so its final state says who
    cancelled it. Nothing in the Gateway cancels a download on its own.
    """

    jid = str(job_id or "").strip()
    if not jid:
        return None
    by = via if via in CANCEL_VIAS else "api"
    if jid.startswith("grp_"):
        view = group_view(jid)
        if view is None:
            return None
        for child in view["children"]:
            cid = child.get("job_id") or child.get("job")
            if cid and _child_state(child) not in _DONE_STATES:
                core_host_job_cancel(str(cid), by=by, user=user)
        return group_view(jid)
    job = core_host_job_cancel(jid, by=by, user=user)
    if job is None:
        return None
    return legacy_job_view(job)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """One job in the legacy view, or `None` (unknown id, or forgotten after a restart)."""

    jid = str(job_id or "").strip()
    if not jid:
        return None
    if jid.startswith("grp_"):
        return group_view(jid)
    job = legacy_job_view(core_host_job(jid))
    if job is not None:
        parent = _group_of_children().get(jid)
        if parent:
            job["parent_job"] = parent
    return job


def list_jobs(*, include_groups: bool = True) -> List[Dict[str, Any]]:
    """Every download job the host knows about, newest first (legacy view).

    Parent (`download_group`) jobs are listed too, and each child names its
    parent under `parent_job`, so a console can nest instead of double-count.
    """

    payload = core_host_jobs(kind="download")
    parents = _group_of_children()
    jobs: List[Dict[str, Any]] = []
    for job in payload.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        view = legacy_job_view(job) or {}
        parent = parents.get(str(view.get("job_id") or view.get("job") or ""))
        if parent:
            view["parent_job"] = parent
        jobs.append(view)
    if include_groups:
        with _GROUP_LOCK:
            group_ids = list(_GROUPS)
        for gid in group_ids:
            view = group_view(gid)
            if view is not None:
                jobs.append(view)
    jobs.sort(key=lambda j: str(j.get("started_at") or ""), reverse=True)
    return jobs


def active_job_for(provider: str, artifact: str) -> Optional[Dict[str, Any]]:
    """The RUNNING job for this artifact, if any -- what a grid renders a bar for."""

    provider, artifact = _normalize(provider, artifact)
    for job in list_jobs(include_groups=False):
        if job.get("provider") == provider and job.get("artifact") == artifact and job.get("status") == "running":
            return job
    return None
