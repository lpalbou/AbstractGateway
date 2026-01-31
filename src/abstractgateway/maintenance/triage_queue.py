from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .report_models import SimilarityCandidate, TriageDecision


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def decision_id_for_report(*, report_relpath: str) -> str:
    # Stable id so repeated triage runs update the same decision entry.
    h = hashlib.sha1(str(report_relpath).encode("utf-8")).hexdigest()
    return h[:16]


def decisions_dir(*, gateway_data_dir: Path) -> Path:
    out = Path(gateway_data_dir).expanduser().resolve() / "triage_queue"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _decision_path(*, dir_path: Path, decision_id: str) -> Path:
    safe = "".join([c for c in str(decision_id or "") if c.isalnum() or c in {"_", "-"}]).strip()
    safe = safe or "decision"
    return Path(dir_path) / f"{safe}.json"


def load_decision(*, dir_path: Path, decision_id: str) -> Optional[TriageDecision]:
    path = _decision_path(dir_path=dir_path, decision_id=decision_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    try:
        dup_raw = data.get("duplicates") or []
        dups: List[SimilarityCandidate] = []
        if isinstance(dup_raw, list):
            for item in dup_raw:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind") or "").strip() or "report"
                ref = str(item.get("ref") or "").strip()
                title = str(item.get("title") or "").strip()
                try:
                    score = float(item.get("score"))
                except Exception:
                    score = 0.0
                if not ref:
                    continue
                dups.append(SimilarityCandidate(kind=kind, ref=ref, score=score, title=title))
    except Exception:
        dups = []

    missing = data.get("missing_fields") or []
    missing2 = [str(m).strip() for m in missing if isinstance(m, str) and m.strip()] if isinstance(missing, list) else []

    decision = TriageDecision(
        decision_id=str(data.get("decision_id") or decision_id),
        report_type=str(data.get("report_type") or "bug"),  # type: ignore[arg-type]
        report_relpath=str(data.get("report_relpath") or ""),
        status=str(data.get("status") or "pending"),  # type: ignore[arg-type]
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
        defer_until=str(data.get("defer_until") or ""),
        missing_fields=missing2,
        duplicates=dups,
        draft_relpath=str(data.get("draft_relpath") or ""),
        llm_suggestion=data.get("llm_suggestion") if isinstance(data.get("llm_suggestion"), dict) else {},
    )
    return decision


def save_decision(*, dir_path: Path, decision: TriageDecision) -> Path:
    path = _decision_path(dir_path=dir_path, decision_id=decision.decision_id)
    payload: Dict[str, Any] = asdict(decision)
    # Convert dataclass list to dict list.
    payload["duplicates"] = [asdict(d) for d in decision.duplicates]
    if not payload.get("updated_at"):
        payload["updated_at"] = _now_utc_iso()
    if not payload.get("created_at"):
        payload["created_at"] = payload["updated_at"]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def upsert_decision(
    *,
    dir_path: Path,
    decision_id: str,
    report_type: str,
    report_relpath: str,
    missing_fields: List[str],
    duplicates: List[SimilarityCandidate],
    llm_suggestion: Optional[Dict[str, Any]] = None,
) -> TriageDecision:
    existing = load_decision(dir_path=dir_path, decision_id=decision_id)
    now = _now_utc_iso()

    if existing is None:
        existing = TriageDecision(
            decision_id=decision_id,
            report_type=report_type,  # type: ignore[arg-type]
            report_relpath=report_relpath,
            status="pending",
            created_at=now,
            updated_at=now,
        )

    # Keep status/defer_until/draft_relpath if already set; refresh computed fields.
    existing.missing_fields = list(missing_fields)
    existing.duplicates = list(duplicates)
    existing.updated_at = now
    if llm_suggestion is not None:
        existing.llm_suggestion = dict(llm_suggestion)

    save_decision(dir_path=dir_path, decision=existing)
    return existing


def iter_decisions(dir_path: Path) -> List[TriageDecision]:
    out: List[TriageDecision] = []
    if not dir_path.exists():
        return out
    for p in sorted(dir_path.glob("*.json")):
        did = p.stem
        d = load_decision(dir_path=dir_path, decision_id=did)
        if d is not None:
            out.append(d)
    return out

