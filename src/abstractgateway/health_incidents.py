"""An incident record when the gateway's health turns bad (OVH 2026-10-28/29: nothing on disk said when
or why the gateway stopped answering well).

`/api/health` keeps its liveness contract (always 200). When its `status` moves from healthy (or
starting) to "degraded", one record is written to `<data dir>/incidents/health-<UTC stamp>.json`:
when, the reasons in words (a failed boot, dead workers, a degraded runner) and the health body as it
stood. One record per transition (the next one only after health was good again), at most one per
minute, never raising and never blocking the probe on a slow disk for long (a small JSON write).
The newest one is what `last_health_incident()` returns (the console can show it).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = "abstractgateway.health_incident.v1"
MIN_INTERVAL_S = 60.0

_lock = threading.Lock()
_dir: Optional[Path] = None
_bad = False
_last_written_at = 0.0
_last: Optional[Dict[str, Any]] = None


def configure(incident_dir: Optional[Path]) -> None:
    global _dir
    _dir = Path(incident_dir) if incident_dir is not None else None


def reasons(body: Dict[str, Any]) -> List[str]:
    """What made health bad, in words (from the health body's own fields)."""
    out: List[str] = []
    boot = body.get("boot") if isinstance(body.get("boot"), dict) else {}
    if boot.get("state") == "failed":
        out.append(f"the gateway's boot failed: {boot.get('error') or 'no error recorded'}")
    dead = body.get("dead_workers") or []
    if dead:
        out.append("background workers stopped: " + ", ".join(str(d) for d in dead))
    runner = body.get("runner") if isinstance(body.get("runner"), dict) else {}
    if runner.get("degraded"):
        out.append(f"the runner is degraded: {runner.get('reason') or runner.get('error') or 'no reason given'}")
    elif runner.get("error"):
        out.append(f"the runner could not be read: {runner.get('error')}")
    return out or ["health reported degraded"]


def note(body: Dict[str, Any], *, now: Optional[float] = None) -> Optional[Path]:
    """Record a transition to "degraded" (see the module docstring); returns the file written, if any."""
    global _bad, _last_written_at, _last
    status = str(body.get("status") or "")
    clock = float(now if now is not None else time.time())
    with _lock:
        if status != "degraded":
            if status == "healthy":
                _bad = False
            return None
        if _bad or (clock - _last_written_at) < MIN_INTERVAL_S or _dir is None:
            _bad = True
            return None
        _bad = True
        _last_written_at = clock
        target = _dir
    at = datetime.fromtimestamp(clock, tz=timezone.utc)
    stamp = at.strftime("%Y%m%dT%H%M%SZ")
    record = {
        "schema": SCHEMA,
        "at": at.isoformat().replace("+00:00", "Z"),
        "reasons": reasons(body),
        "health": body,
    }
    try:
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"health-{stamp}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True, default=str), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - the probe never fails on a record
        return None
    with _lock:
        _last = record
    return path


def last_health_incident() -> Optional[Dict[str, Any]]:
    with _lock:
        return dict(_last) if _last else None


def reset_for_tests() -> None:
    global _dir, _bad, _last_written_at, _last
    with _lock:
        _dir, _bad, _last_written_at, _last = None, False, 0.0, None
