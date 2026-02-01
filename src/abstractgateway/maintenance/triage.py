from __future__ import annotations

import datetime
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .backlog_parser import BacklogItem, iter_backlog_items
from .draft_generator import BacklogIdAllocator, write_backlog_draft
from .llm_assist import llm_assist, load_llm_assist_config
from .report_models import ReportRecord, SimilarityCandidate, TriageDecision
from .report_parser import parse_report_file
from .text_similarity import top_k_similar
from .triage_queue import decision_id_for_report, decisions_dir, iter_decisions, upsert_decision, load_decision, save_decision


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _env(name: str, fallback: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is not None and str(v).strip():
        return str(v).strip()
    if fallback:
        v2 = os.getenv(fallback)
        if v2 is not None and str(v2).strip():
            return str(v2).strip()
    return None


def find_repo_root(start: Path) -> Optional[Path]:
    p = Path(start).expanduser().resolve()
    for cand in [p, *p.parents]:
        if (cand / "docs" / "backlog" / "README.md").exists():
            return cand
    return None


def _scan_reports_in_dir(dir_path: Path, *, report_type: str) -> List[ReportRecord]:
    out: List[ReportRecord] = []
    if not dir_path.exists() or not dir_path.is_dir():
        return out
    for p in sorted(dir_path.glob("*.md")):
        if p.name == "template.md":
            continue
        try:
            rec = parse_report_file(p)
        except Exception:
            continue
        # Trust folder when it disagrees with header.
        if report_type in {"bug", "feature"}:
            rec = ReportRecord(
                report_type=report_type,  # type: ignore[arg-type]
                path=rec.path,
                header=rec.header,
                description=rec.description,
                sections=rec.sections,
                context=rec.context,
            )
        out.append(rec)
    return out


def scan_gateway_reports(*, gateway_data_dir: Path) -> List[ReportRecord]:
    base = Path(gateway_data_dir).expanduser().resolve()
    bugs = _scan_reports_in_dir(base / "bug_reports", report_type="bug")
    feats = _scan_reports_in_dir(base / "feature_requests", report_type="feature")
    return bugs + feats


def _content_missing_bug(report: ReportRecord, section_name: str) -> bool:
    raw = report.sections.get(section_name) or ""
    text = raw.strip()
    if not text:
        return True

    low = text.lower()
    if section_name == "Impact":
        return ("who is affected" in low) and ("how bad is it" in low)
    if section_name == "Steps to Reproduce":
        # Template placeholders: "1.\n2."
        nonempty = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not nonempty:
            return True
        if all(re.match(r"^\d+\.\s*$", ln) for ln in nonempty):  # type: ignore[name-defined]
            return True
        return False
    if section_name == "Workaround":
        return low in {"(if any)", "if any", "(none)"} or "(if any)" in low
    if section_name in {"Expected Behavior", "Actual Behavior", "Notes / Hypotheses"}:
        return not bool(text.strip())
    if section_name in {"Reproducibility", "Severity"}:
        # If the user didn't tick anything, all lines remain unchecked.
        return "- [x]" not in low and "- [X]" not in text
    return False


def _content_missing_feature(report: ReportRecord, section_name: str) -> bool:
    raw = report.sections.get(section_name) or ""
    text = raw.strip()
    if not text:
        return True
    low = text.lower()
    if section_name == "Problem / Motivation":
        return ("what is painful today" in low) and ("who needs this" in low)
    if section_name == "Proposed Solution":
        return ("what should the system do" in low) and ("ux expectations" in low)
    if section_name == "Acceptance Criteria":
        return "(clear, testable outcomes)" in low or text.strip() == "- [ ]"
    if section_name == "Scope":
        # If both Included/Excluded remain blank bullets.
        return "\n- \n" in (text.replace("\r\n", "\n") + "\n")
    return False


def compute_missing_fields(report: ReportRecord) -> List[str]:
    missing: List[str] = []
    if report.report_type == "bug":
        checks = [
            ("Impact", "Impact not filled"),
            ("Steps to Reproduce", "Steps to reproduce missing"),
            ("Expected Behavior", "Expected behavior missing"),
            ("Actual Behavior", "Actual behavior missing"),
            ("Reproducibility", "Reproducibility not specified"),
            ("Severity", "Severity not specified"),
            ("Workaround", "Workaround not specified"),
        ]
        for section, label in checks:
            if _content_missing_bug(report, section):
                missing.append(label)
    else:
        checks = [
            ("Problem / Motivation", "Problem/motivation not filled"),
            ("Proposed Solution", "Proposed solution not filled"),
            ("Acceptance Criteria", "Acceptance criteria missing"),
        ]
        for section, label in checks:
            if _content_missing_feature(report, section):
                missing.append(label)
    return missing


def _backlog_roots(repo_root: Path) -> Tuple[Path, Path, Path]:
    backlog_root = repo_root / "docs" / "backlog"
    return backlog_root / "planned", backlog_root / "completed", backlog_root / "proposed"


def scan_backlog(repo_root: Path) -> Tuple[List[BacklogItem], List[BacklogItem], List[BacklogItem]]:
    planned_dir, completed_dir, proposed_dir = _backlog_roots(repo_root)
    planned = list(iter_backlog_items(planned_dir, kind="planned"))
    completed = list(iter_backlog_items(completed_dir, kind="completed"))
    proposed = list(iter_backlog_items(proposed_dir, kind="proposed"))
    return planned, completed, proposed


def compute_duplicates(
    *,
    report: ReportRecord,
    all_reports: List[ReportRecord],
    backlog_planned: List[BacklogItem],
    backlog_completed: List[BacklogItem],
    k: int = 5,
) -> List[SimilarityCandidate]:
    query = report.to_similarity_text()

    report_candidates: List[Tuple[str, str]] = []
    report_title: Dict[str, str] = {}
    for r in all_reports:
        if r.path == report.path:
            continue
        ref = str(r.path)
        report_candidates.append((ref, r.to_similarity_text()))
        report_title[ref] = r.header.title

    planned_candidates: List[Tuple[str, str]] = []
    planned_title: Dict[str, str] = {}
    for item in backlog_planned:
        ref = str(item.path)
        planned_candidates.append((ref, item.to_similarity_text()))
        planned_title[ref] = item.title

    completed_candidates: List[Tuple[str, str]] = []
    completed_title: Dict[str, str] = {}
    for item in backlog_completed:
        ref = str(item.path)
        completed_candidates.append((ref, item.to_similarity_text()))
        completed_title[ref] = item.title

    out: List[SimilarityCandidate] = []
    for ref, score in top_k_similar(query_text=query, candidates=report_candidates, k=k, min_score=0.30):
        out.append(SimilarityCandidate(kind="report", ref=ref, score=score, title=report_title.get(ref, "")))
    for ref, score in top_k_similar(query_text=query, candidates=planned_candidates, k=k, min_score=0.25):
        out.append(SimilarityCandidate(kind="backlog_planned", ref=ref, score=score, title=planned_title.get(ref, "")))
    for ref, score in top_k_similar(query_text=query, candidates=completed_candidates, k=k, min_score=0.25):
        out.append(SimilarityCandidate(kind="backlog_completed", ref=ref, score=score, title=completed_title.get(ref, "")))

    out.sort(key=lambda c: c.score, reverse=True)
    return out[: max(0, int(k))]


def triage_reports(
    *,
    gateway_data_dir: Path,
    repo_root: Optional[Path],
    write_drafts: bool = False,
    enable_llm: bool = False,
) -> Dict[str, Any]:
    gw_dir = Path(gateway_data_dir).expanduser().resolve()
    reports = scan_gateway_reports(gateway_data_dir=gw_dir)

    resolved_repo_root = repo_root
    if resolved_repo_root is None:
        resolved_repo_root = find_repo_root(Path.cwd())

    backlog_planned: List[BacklogItem] = []
    backlog_completed: List[BacklogItem] = []
    backlog_proposed: List[BacklogItem] = []
    if resolved_repo_root is not None:
        backlog_planned, backlog_completed, backlog_proposed = scan_backlog(resolved_repo_root)

    proposed_by_report_relpath: Dict[str, BacklogItem] = {}
    proposed_by_report_id: Dict[str, BacklogItem] = {}
    for item in backlog_proposed:
        rel = str(getattr(item, "source_report_relpath", "") or "").strip()
        if rel:
            prev = proposed_by_report_relpath.get(rel)
            if prev is None or int(getattr(item, "item_id", 0)) > int(getattr(prev, "item_id", 0)):
                proposed_by_report_relpath[rel] = item
        rid = str(getattr(item, "source_report_id", "") or "").strip()
        if rid:
            prev = proposed_by_report_id.get(rid)
            if prev is None or int(getattr(item, "item_id", 0)) > int(getattr(prev, "item_id", 0)):
                proposed_by_report_id[rid] = item

    qdir = decisions_dir(gateway_data_dir=gw_dir)

    llm_cfg = load_llm_assist_config()
    llm_enabled = bool(enable_llm) or bool(llm_cfg.get("enabled"))

    backlog_root = (resolved_repo_root / "docs" / "backlog") if resolved_repo_root else None
    allocator = BacklogIdAllocator.from_backlog_root(backlog_root) if (write_drafts and backlog_root) else None

    updated: List[TriageDecision] = []
    wrote_drafts: List[str] = []

    for report in reports:
        try:
            relpath = str(report.path.relative_to(gw_dir))
        except Exception:
            relpath = str(report.path)

        did = decision_id_for_report(report_relpath=relpath)
        missing = compute_missing_fields(report)
        dups = compute_duplicates(
            report=report,
            all_reports=reports,
            backlog_planned=backlog_planned,
            backlog_completed=backlog_completed,
            k=5,
        )

        llm_suggestion = None
        if llm_enabled:
            normalized = {
                "report_type": report.report_type,
                "title": report.header.title,
                "description": report.description,
                "session_id": report.header.session_id,
                "relevant_run_id": report.header.active_run_id,
                "workflow_id": report.header.workflow_id,
                "client": report.header.client,
                "provider": report.header.provider,
                "model": report.header.model,
                "missing_fields": list(missing),
                "duplicates": [asdict(d) for d in dups],
            }
            suggestion, _err = llm_assist(
                normalized_input=normalized,
                base_url=str(llm_cfg.get("base_url") or ""),
                model=str(llm_cfg.get("model") or ""),
                api_key=str(llm_cfg.get("api_key") or ""),
                temperature=float(llm_cfg.get("temperature") or 0.2),
                timeout_s=float(llm_cfg.get("timeout_s") or 30.0),
                max_tokens=int(llm_cfg.get("max_tokens") or 800),
            )
            if suggestion is not None:
                llm_suggestion = suggestion

        decision = upsert_decision(
            dir_path=qdir,
            decision_id=did,
            report_type=report.report_type,
            report_relpath=relpath,
            missing_fields=missing,
            duplicates=dups,
            llm_suggestion=llm_suggestion,
        )

        # Link to an existing proposed backlog item (auto-bridge or manual), so triage never generates duplicates.
        if resolved_repo_root is not None and not decision.draft_relpath:
            linked: Optional[BacklogItem] = None
            if relpath in proposed_by_report_relpath:
                linked = proposed_by_report_relpath.get(relpath)
            elif report.header.report_id and report.header.report_id in proposed_by_report_id:
                linked = proposed_by_report_id.get(report.header.report_id)
            if linked is not None:
                try:
                    decision.draft_relpath = str(linked.path.relative_to(resolved_repo_root))
                except Exception:
                    decision.draft_relpath = str(linked.path)
                save_decision(dir_path=qdir, decision=decision)

        # Skip draft creation if repo/backlog is unavailable.
        if write_drafts and allocator is not None and resolved_repo_root is not None and backlog_root is not None:
            # Only write once; do not overwrite manual edits.
            if not decision.draft_relpath:
                draft_path, _new_id = write_backlog_draft(
                    repo_root=resolved_repo_root,
                    backlog_root=backlog_root,
                    allocator=allocator,
                    report=report,
                    decision=decision,
                    llm_suggestion=llm_suggestion,
                )
                save_decision(dir_path=qdir, decision=decision)
                wrote_drafts.append(str(draft_path))

        updated.append(decision)

    return {
        "gateway_data_dir": str(gw_dir),
        "repo_root": str(resolved_repo_root) if resolved_repo_root else "",
        "reports": len(reports),
        "decisions_dir": str(qdir),
        "updated_decisions": len(updated),
        "drafts_written": wrote_drafts,
    }


def apply_decision_action(
    *,
    gateway_data_dir: Path,
    decision_id: str,
    action: str,
    repo_root: Optional[Path] = None,
    write_draft_on_approve: bool = True,
    defer_days: Optional[int] = None,
) -> Tuple[Optional[TriageDecision], Optional[str]]:
    gw_dir = Path(gateway_data_dir).expanduser().resolve()
    qdir = decisions_dir(gateway_data_dir=gw_dir)
    decision = load_decision(dir_path=qdir, decision_id=str(decision_id))
    if decision is None:
        return None, "Decision not found"

    act = str(action or "").strip().lower()
    if act not in {"approve", "approved", "reject", "rejected", "defer", "deferred"}:
        return None, "Unsupported action"

    now = _now_utc_iso()
    if act in {"approve", "approved"}:
        decision.status = "approved"
        decision.defer_until = ""
        decision.updated_at = now
        save_decision(dir_path=qdir, decision=decision)

        resolved_repo_root = repo_root or find_repo_root(Path.cwd())
        if resolved_repo_root is None:
            return decision, None
        backlog_root = (resolved_repo_root / "docs" / "backlog").resolve()
        planned_dir = (backlog_root / "planned").resolve()

        if write_draft_on_approve and not decision.draft_relpath:
            allocator = BacklogIdAllocator.from_backlog_root(backlog_root)

            # Load report content (needed for proposed backlog creation).
            report_path = gw_dir / decision.report_relpath
            try:
                report = parse_report_file(report_path)
            except Exception:
                return decision, None

            suggestion = decision.llm_suggestion if isinstance(decision.llm_suggestion, dict) else None
            _draft_path, _ = write_backlog_draft(
                repo_root=resolved_repo_root,
                backlog_root=backlog_root,
                allocator=allocator,
                report=report,
                decision=decision,
                llm_suggestion=suggestion,
            )
            decision.updated_at = _now_utc_iso()
            save_decision(dir_path=qdir, decision=decision)

        # If a proposed backlog exists, treat approval as elevation to planned.
        if decision.draft_relpath:
            src = (resolved_repo_root / decision.draft_relpath).resolve()
            try:
                src.relative_to(backlog_root)
            except Exception:
                return decision, None

            # Only elevate from proposed -> planned (avoid moving already planned/completed items).
            try:
                rel = src.relative_to(backlog_root)
            except Exception:
                rel = None
            if rel and rel.parts and rel.parts[0] == "proposed":
                planned_dir.mkdir(parents=True, exist_ok=True)
                dest = (planned_dir / src.name).resolve()
                try:
                    dest.relative_to(planned_dir)
                except Exception:
                    return decision, "Invalid planned path"
                if dest.exists():
                    # Already elevated (or name collision); do not error.
                    try:
                        decision.draft_relpath = str(dest.relative_to(resolved_repo_root))
                        decision.updated_at = _now_utc_iso()
                        save_decision(dir_path=qdir, decision=decision)
                    except Exception:
                        pass
                    return decision, None
                try:
                    src.rename(dest)
                except Exception as e:
                    return decision, f"Failed to elevate backlog item: {e}"
                try:
                    decision.draft_relpath = str(dest.relative_to(resolved_repo_root))
                except Exception:
                    decision.draft_relpath = str(dest)
                decision.updated_at = _now_utc_iso()
                save_decision(dir_path=qdir, decision=decision)
        return decision, None

    if act in {"reject", "rejected"}:
        decision.status = "rejected"
        decision.defer_until = ""
        decision.updated_at = now
        save_decision(dir_path=qdir, decision=decision)
        resolved_repo_root = repo_root or find_repo_root(Path.cwd())
        if resolved_repo_root is None or not decision.draft_relpath:
            return decision, None

        backlog_root = (resolved_repo_root / "docs" / "backlog").resolve()
        deprecated_dir = (backlog_root / "deprecated").resolve()

        src = (resolved_repo_root / decision.draft_relpath).resolve()
        try:
            src.relative_to(backlog_root)
        except Exception:
            return decision, None

        # Only move from proposed -> deprecated (avoid touching planned/completed).
        try:
            rel = src.relative_to(backlog_root)
        except Exception:
            rel = None
        if rel and rel.parts and rel.parts[0] == "proposed":
            deprecated_dir.mkdir(parents=True, exist_ok=True)
            dest = (deprecated_dir / src.name).resolve()
            try:
                dest.relative_to(deprecated_dir)
            except Exception:
                return decision, "Invalid deprecated path"
            if dest.exists():
                try:
                    decision.draft_relpath = str(dest.relative_to(resolved_repo_root))
                    decision.updated_at = _now_utc_iso()
                    save_decision(dir_path=qdir, decision=decision)
                except Exception:
                    pass
                return decision, None
            try:
                src.rename(dest)
            except Exception as e:
                return decision, f"Failed to deprecate backlog item: {e}"
            try:
                decision.draft_relpath = str(dest.relative_to(resolved_repo_root))
            except Exception:
                decision.draft_relpath = str(dest)
            decision.updated_at = _now_utc_iso()
            save_decision(dir_path=qdir, decision=decision)
        return decision, None

    # defer
    days = None
    if defer_days is not None:
        try:
            days = max(1, int(defer_days))
        except Exception:
            days = None
    if days is None:
        # Defer duration is provided through env for v0 (e.g. action=defer and ABSTRACT_TRIAGE_DEFER_DAYS=7).
        days_raw = _env("ABSTRACT_TRIAGE_DEFER_DAYS", "ABSTRACTGATEWAY_TRIAGE_DEFER_DAYS") or "1"
        try:
            days = max(1, int(days_raw))
        except Exception:
            days = 1
    until = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)).isoformat()
    decision.status = "deferred"
    decision.defer_until = until
    decision.updated_at = now
    save_decision(dir_path=qdir, decision=decision)
    return decision, None
