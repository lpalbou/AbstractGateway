from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .backlog_parser import max_backlog_id
from .report_models import ReportRecord, TriageDecision


def _now_local_timestamp() -> str:
    # Keep local time to match existing backlog conventions.
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def _slug(text: str) -> str:
    s = str(text or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = s[:80].strip("-") or "item"
    return s


def _guess_package(report: ReportRecord) -> str:
    client = str(report.header.client or "").strip().lower()
    if "abstractcode" in client:
        return "abstractcode"
    if "abstractgateway" in client or "gateway" in client:
        return "abstractgateway"
    if "abstractruntime" in client or "runtime" in client:
        return "abstractruntime"
    # Fallback: cross-cutting.
    return "framework"


@dataclass
class BacklogIdAllocator:
    next_id: int

    @classmethod
    def from_backlog_root(cls, backlog_root: Path) -> "BacklogIdAllocator":
        planned = backlog_root / "planned"
        completed = backlog_root / "completed"
        proposed = backlog_root / "proposed"
        max_id = max_backlog_id([(planned, "planned"), (completed, "completed"), (proposed, "proposed")])
        return cls(next_id=max_id + 1)

    def allocate(self) -> int:
        v = int(self.next_id)
        self.next_id = v + 1
        return v


def _draft_title(report: ReportRecord) -> str:
    base = report.header.title.strip() if report.header.title else ""
    if base:
        return base
    return "Bug fix" if report.report_type == "bug" else "Feature request"


def _draft_summary(report: ReportRecord) -> str:
    kind = "bug" if report.report_type == "bug" else "feature request"
    title = report.header.title.strip() if report.header.title else kind
    return f"Translate a reported {kind} into an actionable backlog item: {title}"


def _format_related(report: ReportRecord, decision: TriageDecision) -> str:
    rel: list[str] = []
    rel.append(f"- Source report: `{report.path}`")
    if report.header.session_id:
        rel.append(f"- Session ID: `{report.header.session_id}`")
    if report.header.active_run_id:
        rel.append(f"- Relevant run ID: `{report.header.active_run_id}`")
    if report.header.session_memory_run_id:
        rel.append(f"- Session memory run ID (attachments): `{report.header.session_memory_run_id}`")
    if decision.duplicates:
        rel.append("- Possible duplicates:")
        for d in decision.duplicates[:5]:
            rel.append(f"  - ({d.kind}, {d.score:.2f}) {d.title or d.ref}")
    return "\n".join(rel).strip()


def generate_backlog_draft_markdown(
    *,
    item_id: int,
    package: str,
    report: ReportRecord,
    decision: TriageDecision,
    llm_suggestion: Optional[Dict[str, Any]] = None,
) -> str:
    title = _draft_title(report)
    summary = _draft_summary(report)
    created = _now_local_timestamp()

    llm_title = ""
    llm_pkgs = ""
    llm_ac = ""
    if isinstance(llm_suggestion, dict):
        llm_title = str(llm_suggestion.get("backlog_title") or "").strip()
        llm_pkgs = str(llm_suggestion.get("packages") or "").strip()
        llm_ac = str(llm_suggestion.get("acceptance_criteria") or "").strip()
    if llm_title:
        title = llm_title
    if llm_pkgs:
        package = llm_pkgs.split(",", 1)[0].strip().lower() or package

    missing = decision.missing_fields or []
    missing_md = "\n".join([f"- [ ] {m}" for m in missing]) if missing else "- (none)"

    suggested_ac = llm_ac.strip()
    if not suggested_ac:
        suggested_ac = "- [ ] Reproduce the issue and identify root cause\n- [ ] Implement fix with minimal blast radius\n- [ ] Add targeted tests (ADR-0019)\n- [ ] Verify no regressions"

    related = _format_related(report, decision)

    return (
        f"# {item_id:03d}-{package}: {title}\n\n"
        f"> Created: {created}\n\n"
        "## Summary\n"
        f"{summary}\n\n"
        "## Diagram\n"
        "```\n"
        "Report inbox -> Triage -> Backlog draft -> (human review) -> Planned -> Implementation\n"
        "```\n\n"
        "## Context\n"
        f"{related}\n\n"
        "## Scope\n"
        "### Included\n"
        "- Fix/implement the behavior described in the report\n"
        "- Add/adjust tests per ADR-0019\n\n"
        "### Excluded\n"
        "- Unrelated refactors\n"
        "- Broad UX redesigns unless required by the report\n\n"
        "## Missing Info (to confirm)\n"
        f"{missing_md}\n\n"
        "## Implementation Plan\n"
        "1. Reproduce and narrow down the cause.\n"
        "2. Identify the smallest correct fix.\n"
        "3. Add tests (Level A/B as applicable).\n"
        "4. Verify in the relevant client(s).\n\n"
        "## Acceptance Criteria\n"
        f"{suggested_ac}\n\n"
        "## Testing (ADR-0019)\n"
        "- Level A (basic): add targeted unit/contract tests.\n"
        "- Level B (integration): reproduce with file-backed stores or the relevant gateway/client wiring.\n"
        "- Level C (optional): run a real client flow if it requires external infra.\n\n"
        "## Related\n"
        f"- Maintenance triage: `docs/backlog/planned/644-framework-automated-report-triage-pipeline-v0.md`\n"
        f"- Report inbox: `docs/backlog/README.md`\n"
    )


def write_backlog_draft(
    *,
    repo_root: Path,
    backlog_root: Path,
    allocator: BacklogIdAllocator,
    report: ReportRecord,
    decision: TriageDecision,
    llm_suggestion: Optional[Dict[str, Any]] = None,
) -> Tuple[Path, int]:
    proposed_dir = backlog_root / "proposed"
    proposed_dir.mkdir(parents=True, exist_ok=True)

    package = _guess_package(report)
    item_id = allocator.allocate()
    title = str((llm_suggestion or {}).get("backlog_title") or report.header.title or "").strip()
    slug = _slug(title or report.header.title or report.description or "draft")

    filename = f"{item_id:03d}-{package}-{slug}.md"
    path = proposed_dir / filename

    md = generate_backlog_draft_markdown(
        item_id=item_id,
        package=package,
        report=report,
        decision=decision,
        llm_suggestion=llm_suggestion,
    )
    path.write_text(md, encoding="utf-8")

    # Store repo-root relative path in the decision.
    try:
        rel = str(path.relative_to(repo_root))
    except Exception:
        rel = str(path)
    decision.draft_relpath = rel
    return path, item_id

