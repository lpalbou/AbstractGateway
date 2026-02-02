from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .backlog_parser import max_backlog_id
from .report_models import ReportRecord, TriageDecision
from .text_similarity import similarity, tokenize


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
        recurrent = backlog_root / "recurrent"
        deprecated = backlog_root / "deprecated"
        trash = backlog_root / "trash"
        max_id = max_backlog_id(
            [
                (planned, "planned"),
                (completed, "completed"),
                (proposed, "proposed"),
                (recurrent, "recurrent"),
                (deprecated, "deprecated"),
                (trash, "trash"),
            ]
        )
        return cls(next_id=max_id + 1)

    def allocate(self) -> int:
        v = int(self.next_id)
        self.next_id = v + 1
        return v


def _draft_title(report: ReportRecord) -> str:
    def _from_desc(desc: str) -> str:
        raw = str(desc or "").replace("\r", "")
        for line in raw.split("\n"):
            s = line.strip()
            if not s:
                continue
            s2 = re.sub(r"^\s*/(bug|feature)\b\s*", "", s, flags=re.IGNORECASE).strip()
            s2 = re.sub(r"\s+", " ", s2).strip()
            return s2 or s
        return ""

    base = report.header.title.strip() if report.header.title else ""
    desc_line = _from_desc(report.description)

    # Prefer the report's first-line description when it looks like the stored title
    # was previously truncated (older gateway versions clamped report titles).
    if desc_line:
        if not base:
            base = desc_line
        elif len(base) >= 100 and len(desc_line) > len(base) and desc_line.lower().startswith(base.lower()):
            base = desc_line

    if base:
        base = re.sub(r"\s+", " ", base).strip()
        if len(base) > 120:
            base = base[:120].rstrip()
        return base
    return "Bug fix" if report.report_type == "bug" else "Feature request"


def _draft_summary(report: ReportRecord) -> str:
    # In proposed backlog items, preserve the user's report description verbatim
    # (never truncate). This is the "source of truth" for what was requested.
    desc = str(report.description or "")
    if desc.strip():
        lines = desc.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return "\n".join([f"    {ln}" for ln in lines]).rstrip()

    title = report.header.title.strip() if report.header.title else ""
    if report.report_type == "bug":
        t = title or "bug"
        return (
            f"Fix reported issue: {t}. Use the linked report/session/run artifacts to reproduce, identify root cause, and implement a minimal fix "
            "with targeted tests (ADR-0019)."
        )
    t = title or "feature request"
    return (
        f"Implement requested feature: {t}. Use the linked report/session/run artifacts to capture context, define clear acceptance criteria, "
        "and deliver a scoped change with minimal dependencies."
    )


def _read_text_bounded(path: Path, *, max_chars: int = 80_000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if len(text) > int(max_chars):
        return text[: int(max_chars)]
    return text


_KEYWORD_BOOSTS: Dict[str, float] = {
    # UX + theming.
    "theme": 0.22,
    "themes": 0.22,
    "ui": 0.08,
    "ux": 0.08,
    # Durability / replay / observability.
    "replay": 0.18,
    "ledger": 0.18,
    "history": 0.10,
    "tool": 0.14,
    "tools": 0.14,
    "attachment": 0.14,
    "attachments": 0.14,
    # Security / auth.
    "auth": 0.14,
    "security": 0.14,
    # Process.
    "backlog": 0.10,
    "triage": 0.10,
}


def _boosted_similarity(*, query_text: str, candidate_text: str) -> float:
    """Similarity with small keyword boosts for higher-signal terms.

    The underlying similarity function is intentionally lightweight and tends to produce
    low raw scores on long documents (ADRs). Keyword boosts help surface relevant items
    without pretending they are definitive matches.
    """

    base = similarity(query_text, candidate_text)
    qt = set(tokenize(query_text))
    ct = set(tokenize(candidate_text))
    boost = 0.0
    for kw, w in _KEYWORD_BOOSTS.items():
        if kw in qt and kw in ct:
            boost += float(w)
    return min(1.0, base + min(boost, 0.35))


def _related_adrs_markdown(*, repo_root: Optional[Path], report: ReportRecord, k: int = 4) -> str:
    if repo_root is None:
        return "- (repo unavailable)"
    rr = Path(repo_root).expanduser().resolve()
    adr_dir = (rr / "docs" / "adr").resolve()
    if not adr_dir.exists():
        return "- (no docs/adr directory)"

    query = report.to_similarity_text()
    candidates: List[Tuple[str, str]] = []
    for p in sorted(adr_dir.glob("*.md")):
        if p.name.lower() == "readme.md":
            continue
        rel = ""
        try:
            rel = str(p.relative_to(rr))
        except Exception:
            rel = str(p)
        text = _read_text_bounded(p, max_chars=60_000)
        if not text.strip():
            continue
        # Focus the candidate text on the ADR "front matter" to avoid long-body dilution.
        head = "\n".join(text.splitlines()[:120]).strip()
        cand_text = f"{p.name}\n{head}".strip()
        candidates.append((rel, cand_text))
        if len(candidates) >= 250:
            break

    if not candidates:
        return "- (none found)"

    scored: List[Tuple[str, float]] = []
    for ref, text in candidates:
        s = _boosted_similarity(query_text=query, candidate_text=text)
        scored.append((ref, s))
    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[: max(0, int(k))]
    top = [(ref, s) for ref, s in top if s >= 0.06]
    if not top:
        return "- (none)"
    return "\n".join([f"- ({s:.2f}) `{ref}`" for ref, s in top]).strip()


def _related_backlog_markdown(*, repo_root: Optional[Path], report: ReportRecord, k: int = 8) -> str:
    """Best-effort related backlog pointers (fast enough for auto-bridge).

    We intentionally include both planned (dependencies) and completed (related prior work).
    This is deterministic and does not use an LLM.
    """
    if repo_root is None:
        return "- (repo unavailable)"
    rr = Path(repo_root).expanduser().resolve()
    planned_dir = (rr / "docs" / "backlog" / "planned").resolve()
    completed_dir = (rr / "docs" / "backlog" / "completed").resolve()
    if not planned_dir.exists() and not completed_dir.exists():
        return "- (no backlog)"

    try:
        from .backlog_parser import iter_backlog_items  # type: ignore
    except Exception:
        return "- (backlog parser unavailable)"

    planned_items = list(iter_backlog_items(planned_dir, kind="planned")) if planned_dir.exists() else []
    completed_items = list(iter_backlog_items(completed_dir, kind="completed")) if completed_dir.exists() else []
    items = planned_items + completed_items
    if not items:
        return "- (none)"

    query = report.to_similarity_text()
    scored: List[Tuple[float, str, Any]] = []
    for item in items[:1200]:
        try:
            rel = str(item.path.relative_to(rr))
        except Exception:
            rel = str(item.path)
        text = item.to_similarity_text()
        score = _boosted_similarity(query_text=query, candidate_text=text)
        scored.append((score, rel, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [x for x in scored if x[0] >= 0.10][: max(0, int(k))]
    if not top:
        return "- (none)"

    lines: List[str] = []
    for score, ref, item in top:
        title = str(getattr(item, "title", "") or "").strip()
        pkg = str(getattr(item, "package", "") or "").strip()
        kind = str(getattr(item, "kind", "") or "").strip()
        label = f"{pkg}: {title}" if pkg and title else title or pkg or ref
        kind_tag = f"{kind}" if kind else "backlog"
        lines.append(f"- ({score:.2f}, {kind_tag}) {label} — `{ref}`")
    return "\n".join(lines).strip()


def _format_related(report: ReportRecord, decision: TriageDecision, *, repo_root: Optional[Path] = None) -> str:
    rel: list[str] = []
    if str(getattr(decision, "report_relpath", "") or "").strip():
        rel.append(f"- Source report relpath: `{decision.report_relpath}`")

    # Prefer a repo-root relative path for docs portability when possible.
    if repo_root is not None:
        try:
            rr = Path(repo_root).expanduser().resolve()
            rp = Path(report.path).expanduser().resolve()
            rel_repo = str(rp.relative_to(rr))
        except Exception:
            rel_repo = ""
        if rel_repo:
            rel.append(f"- Source report file (repo): `{rel_repo}`")

    if report.header.report_id:
        rel.append(f"- Report ID: `{report.header.report_id}`")
    if report.header.session_id:
        rel.append(f"- Session ID: `{report.header.session_id}`")
    if report.header.active_run_id:
        rel.append(f"- Relevant run ID: `{report.header.active_run_id}`")
    if report.header.session_memory_run_id:
        rel.append(f"- Session memory run ID (attachments): `{report.header.session_memory_run_id}`")
    if decision.duplicates:
        rel.append("- Possible duplicates:")
        for d in decision.duplicates[:5]:
            ref = str(getattr(d, "ref", "") or "").strip()
            title = str(getattr(d, "title", "") or "").strip()
            ref_out = ref
            if repo_root is not None and ref:
                try:
                    rr = Path(repo_root).expanduser().resolve()
                    rp = Path(ref).expanduser().resolve()
                    if str(rp).startswith(str(rr)):
                        ref_out = str(rp.relative_to(rr))
                except Exception:
                    ref_out = ref
            rel.append(f"  - ({d.kind}, {d.score:.2f}) {title or ref_out}")
    return "\n".join(rel).strip()


def generate_backlog_draft_markdown(
    *,
    item_id: int,
    package: str,
    report: ReportRecord,
    decision: TriageDecision,
    llm_suggestion: Optional[Dict[str, Any]] = None,
    repo_root: Optional[Path] = None,
) -> str:
    task_type = "bug" if report.report_type == "bug" else "feature"
    type_tag = "BUG" if task_type == "bug" else "FEATURE"
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
        title = re.sub(r"\s+", " ", llm_title).strip()
        if len(title) > 120:
            title = title[:120].rstrip()
    if llm_pkgs:
        package = llm_pkgs.split(",", 1)[0].strip().lower() or package

    missing = decision.missing_fields or []
    missing_md = "\n".join([f"- [ ] {m}" for m in missing]) if missing else "- (none)"

    suggested_ac = llm_ac.strip()
    if not suggested_ac:
        if report.report_type == "bug":
            suggested_ac = "- [ ] Reproduce the issue and identify root cause\n- [ ] Implement fix with minimal blast radius\n- [ ] Add targeted tests (ADR-0019)\n- [ ] Verify no regressions"
        else:
            suggested_ac = (
                "- [ ] Confirm problem/motivation and proposed solution\n"
                "- [ ] Define clear acceptance criteria (2–5 items)\n"
                "- [ ] Implement the feature with minimal dependencies\n"
                "- [ ] Add/adjust tests per ADR-0019\n"
                "- [ ] Verify UX + durability expectations (replay/session context)"
            )

    related = _format_related(report, decision, repo_root=repo_root)
    related_backlog = _related_backlog_markdown(repo_root=repo_root, report=report, k=6)
    related_adrs = _related_adrs_markdown(repo_root=repo_root, report=report, k=4)

    return (
        f"# {item_id:03d}-{package}: [{type_tag}] {title}\n\n"
        f"> Created: {created}\n"
        f"> Type: {task_type}\n"
        f"> Source report relpath: {decision.report_relpath}\n"
        f"> Source report id: {report.header.report_id or ''}\n\n"
        "## Summary\n"
        f"{summary}\n\n"
        "## Diagram\n"
        "```\n"
        "/bug|/feature -> gateway report -> auto-proposed backlog -> (optional triage) -> planned -> implementation\n"
        "```\n\n"
        "## Context\n"
        f"{related}\n\n"
        "## Related (best-effort)\n"
        "### Potential ADRs\n"
        f"{related_adrs}\n\n"
        "### Potential dependencies / related backlog items\n"
        f"{related_backlog}\n\n"
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
        f"- Report inbox + triage: `docs/backlog/planned/644-framework-automated-report-triage-pipeline-v0.md`\n"
        f"- Backlog conventions: `docs/backlog/README.md`\n"
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
    title = str((llm_suggestion or {}).get("backlog_title") or report.header.title or "").strip()
    slug = _slug(title or report.header.title or report.description or "draft")

    last_err: Optional[str] = None
    for _ in range(0, 500):
        item_id = allocator.allocate()
        filename = f"{item_id:03d}-{package}-{slug}.md"
        path = proposed_dir / filename

        md = generate_backlog_draft_markdown(
            item_id=item_id,
            package=package,
            report=report,
            decision=decision,
            llm_suggestion=llm_suggestion,
            repo_root=repo_root,
        )
        try:
            with open(path, "x", encoding="utf-8") as f:
                f.write(md)
            last_err = None
            break
        except FileExistsError:
            last_err = "Filename collision"
            continue

    if last_err is not None:
        raise RuntimeError(last_err)

    # Store repo-root relative path in the decision.
    try:
        rel = str(path.relative_to(repo_root))
    except Exception:
        rel = str(path)
    decision.draft_relpath = rel
    return path, item_id
