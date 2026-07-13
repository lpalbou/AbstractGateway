"""Definition-of-Ready gate for backlog execution (continuum c1088 ask 3;
contract + test vectors delivered c1124).

continuum's execute dialog runs an ADVISORY client-side DoR checklist, but a
raw curl bypasses it. This is the SERVER-SIDE mirror so the discipline is
real from any client, with the operator explicitly outranking it via
`override=true`. continuum's board_model.ts is the reference implementation;
these rules match it by construction (their V1-V6 vectors are the pins).

Four checks, all must pass:
  1. type       — task_type is a recognized work-item type.
  2. summary    — the Summary section has >=1 non-placeholder line.
  3. acceptance — >=1 checkbox bullet under an "Acceptance Criteria" heading.
  4. tests      — >=1 backticked command under a "Testing" heading.

SECTION SEMANTICS (load-bearing, continuum's adversary find): a section is
the lines after a matching heading of ANY depth (H1..H6) until the NEXT
heading of any depth — so `### Acceptance Criteria` counts, not only H2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Tuple

# The ruled work-item OFFER enum (semantics c1123: bug|feature|improvement|
# task — "improvement" is the maintainer's word; task is the residual
# bucket). CANONICAL COPY: decision:workitem-type-enum in the agora commons
# store — this constant is a CITATION of it, and widening goes through
# semantics' rule-4b path (the one-copy rule / diary_type-clamp lesson).
# Kept in sync with the backlog parser's _normalize_task_type. The DoR
# "type" check passes for any of these; an at-rest value outside the set
# (e.g. "enhancement") FAILS the write-side gate with the as-written value
# named in evidence (c1136 V8), even though the READ side accepts it.
_DOR_TYPES = {"bug", "feature", "improvement", "task"}

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(?P<title>.+?)\s*#*\s*$")
_SUMMARY_HEADING_RE = re.compile(r"^#{1,6}\s*summary\b", re.IGNORECASE)
_ACCEPTANCE_HEADING_RE = re.compile(r"acceptance criteria", re.IGNORECASE)
_TESTING_HEADING_RE = re.compile(r"\btesting\b", re.IGNORECASE)
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(?P<label>.*)$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")

_SUMMARY_PLACEHOLDER = "one paragraph describing"
_ACCEPTANCE_PLACEHOLDER = "criterion 1 (clear, testable)"


@dataclass(frozen=True)
class DorCheck:
    id: str
    label: str
    ok: bool
    evidence: str

    def to_dict(self) -> Dict[str, object]:
        return {"id": self.id, "label": self.label, "ok": self.ok, "evidence": self.evidence}


def _section_lines(md: str, heading_pred) -> List[str]:
    """Lines under the FIRST heading (any depth) matching `heading_pred`, up to
    the next heading of any depth. `heading_pred(depth, title, raw_line)`."""
    lines = md.splitlines()
    out: List[str] = []
    capturing = False
    for raw in lines:
        m = _HEADING_RE.match(raw)
        if m:
            if capturing:
                break  # next heading of any depth ends the section
            depth = len(m.group(1))
            title = str(m.group("title") or "").strip()
            if heading_pred(depth, title, raw):
                capturing = True
            continue
        if capturing:
            out.append(raw)
    return out


def _check_type(task_type: str) -> DorCheck:
    t = str(task_type or "").strip().lower()
    ok = t in _DOR_TYPES
    return DorCheck("type", "Type is set", ok, evidence=(t or "(none)") if ok else f"unrecognized type {t!r}")


def _check_summary(md: str) -> DorCheck:
    section = _section_lines(md, lambda d, title, raw: bool(_SUMMARY_HEADING_RE.match(raw.strip())))
    for line in section:
        s = line.strip()
        if not s:
            continue
        if s.lower().startswith(_SUMMARY_PLACEHOLDER):
            continue
        return DorCheck("summary", "Real summary", True, evidence=f'found: "{s[:80]}"')
    return DorCheck("summary", "Real summary", False, evidence="no non-placeholder line under a Summary heading")


def _check_acceptance(md: str) -> DorCheck:
    section = _section_lines(md, lambda d, title, raw: bool(_ACCEPTANCE_HEADING_RE.search(title if title else raw)))
    found: List[str] = []
    for line in section:
        m = _CHECKBOX_RE.match(line)
        if not m:
            continue
        label = str(m.group("label") or "").strip()
        if label.lower().startswith(_ACCEPTANCE_PLACEHOLDER):
            continue
        found.append(label or "(empty)")
    if found:
        shown = ", ".join(f'"{f[:50]}"' for f in found[:3])
        return DorCheck("acceptance", ">=1 acceptance criterion", True, evidence=f"found {len(found)}: {shown}")
    return DorCheck(
        "acceptance", ">=1 acceptance criterion", False, evidence="none found under an Acceptance Criteria heading"
    )


def _check_tests(md: str) -> DorCheck:
    section = _section_lines(md, lambda d, title, raw: bool(_TESTING_HEADING_RE.search(title if title else raw)))
    found: List[str] = []
    for line in section:
        for cmd in _BACKTICK_RE.findall(line):
            c = str(cmd or "").strip()
            if not c or c == "..." or c.lower() == "n/a":
                continue
            found.append(c)
    if found:
        shown = ", ".join(f'"{f[:50]}"' for f in found[:3])
        return DorCheck("tests", ">=1 test command", True, evidence=f"found {len(found)}: {shown}")
    return DorCheck("tests", ">=1 test command", False, evidence="no backticked command under a Testing heading")


def evaluate_dor(md: str, task_type: str) -> Tuple[bool, List[DorCheck]]:
    """Run all four checks. Returns (ready, checks). `ready` is the AND of every
    check's `ok`."""
    checks = [
        _check_type(task_type),
        _check_summary(md),
        _check_acceptance(md),
        _check_tests(md),
    ]
    return all(c.ok for c in checks), checks
