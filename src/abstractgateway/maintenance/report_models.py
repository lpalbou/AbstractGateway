from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional


ReportType = Literal["bug", "feature"]


@dataclass(frozen=True)
class ReportHeader:
    title: str
    created_at: str = ""
    report_id: str = ""
    session_id: str = ""
    session_memory_run_id: str = ""
    active_run_id: str = ""
    workflow_id: str = ""
    client: str = ""
    client_version: str = ""
    provider: str = ""
    model: str = ""
    template: str = ""


@dataclass(frozen=True)
class ReportRecord:
    report_type: ReportType
    path: Path
    header: ReportHeader
    description: str
    sections: Dict[str, str] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)

    def to_similarity_text(self) -> str:
        bits: List[str] = []
        if self.header.title:
            bits.append(self.header.title)
        if self.description:
            bits.append(self.description)
        # Include a small amount of environment context for disambiguation.
        env_bits: List[str] = []
        if self.header.client:
            env_bits.append(f"client={self.header.client}")
        if self.header.workflow_id:
            env_bits.append(f"workflow={self.header.workflow_id}")
        if self.header.template:
            env_bits.append(f"template={self.header.template}")
        if env_bits:
            bits.append(" ".join(env_bits))
        return "\n\n".join([b for b in bits if b]).strip()


@dataclass(frozen=True)
class SimilarityCandidate:
    kind: Literal["report", "backlog_planned", "backlog_completed"]
    ref: str
    score: float
    title: str = ""


@dataclass
class TriageDecision:
    decision_id: str
    report_type: ReportType
    report_relpath: str
    status: Literal["pending", "approved", "deferred", "rejected"] = "pending"
    created_at: str = ""
    updated_at: str = ""
    defer_until: str = ""

    missing_fields: List[str] = field(default_factory=list)
    duplicates: List[SimilarityCandidate] = field(default_factory=list)

    # When we write a draft file, store its relative path (repo-root relative).
    draft_relpath: str = ""

    # Optional LLM suggestions (best-effort; informational only).
    llm_suggestion: Dict[str, Any] = field(default_factory=dict)

