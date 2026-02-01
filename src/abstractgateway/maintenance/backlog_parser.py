from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


_H1_RE = re.compile(r"^#\s+(?P<id>\d+)-(?P<pkg>[^:]+)\s*:\s*(?P<title>.+?)\s*$")
_H2_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$")
_META_TYPE_RE = re.compile(r"^>\s*type\s*:\s*(?P<type>[a-zA-Z0-9_-]+)\s*$", re.IGNORECASE)
_TITLE_TYPE_RE = re.compile(r"^\[(?P<type>bug|feature|task)\]\s*(?P<rest>.*)$", re.IGNORECASE)
_META_SOURCE_REPORT_RELPATH_RE = re.compile(r"^>\s*source\s+report\s+relpath\s*:\s*(?P<relpath>.+?)\s*$", re.IGNORECASE)
_META_SOURCE_REPORT_ID_RE = re.compile(r"^>\s*source\s+report\s+id\s*:\s*(?P<id>.+?)\s*$", re.IGNORECASE)
_INFER_SOURCE_REPORT_RELPATH_RE = re.compile(r"(?P<relpath>(?:bug_reports|feature_requests)/[A-Za-z0-9._-]{1,220}\.md)")


@dataclass(frozen=True)
class BacklogItem:
    kind: str  # planned|completed|proposed
    path: Path
    item_id: int
    package: str
    title: str
    task_type: str = "task"  # bug|feature|task
    summary: str = ""
    source_report_relpath: str = ""
    source_report_id: str = ""

    def ref(self) -> str:
        return str(self.path)

    def to_similarity_text(self) -> str:
        bits: List[str] = []
        bits.append(f"{self.package}: {self.title}")
        if self.summary:
            bits.append(self.summary)
        return "\n\n".join([b for b in bits if b]).strip()


def _parse_summary(text: str) -> str:
    lines = text.splitlines()
    in_summary = False
    acc: List[str] = []
    for raw in lines:
        m = _H2_RE.match(raw.strip())
        if m:
            name = str(m.group("name") or "").strip().lower()
            if name == "summary":
                in_summary = True
                acc = []
                continue
            if in_summary:
                break
        if in_summary:
            acc.append(raw)
    return "\n".join(acc).strip()


def _normalize_task_type(raw: str) -> Optional[str]:
    t = str(raw or "").strip().lower()
    if t in {"bug", "feature", "task"}:
        return t
    return None


def _parse_task_type(text: str, title: str) -> Tuple[str, str]:
    """Return (task_type, normalized_title)."""
    meta_type: Optional[str] = None
    for raw in text.splitlines()[:80]:
        line = raw.strip()
        if not line:
            continue
        m = _META_TYPE_RE.match(line)
        if not m:
            continue
        meta_type = _normalize_task_type(m.group("type"))
        if meta_type:
            break

    title_str = str(title or "").strip()
    m2 = _TITLE_TYPE_RE.match(title_str)
    if m2:
        prefix_type = _normalize_task_type(m2.group("type"))
        rest = str(m2.group("rest") or "").strip()
        if not meta_type and prefix_type:
            meta_type = prefix_type
        title_str = rest or title_str

    # Best-effort inference for legacy items (before typed backlog was introduced).
    if not meta_type:
        lowered = text.lower()
        if "bug_reports/" in lowered:
            meta_type = "bug"
        elif "feature_requests/" in lowered:
            meta_type = "feature"

    return (meta_type or "task"), title_str


def parse_backlog_item(path: Path, *, kind: str) -> Optional[BacklogItem]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    first_h1 = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            first_h1 = line
        break

    m = _H1_RE.match(first_h1)
    if not m:
        return None

    try:
        item_id = int(m.group("id"))
    except Exception:
        return None

    pkg = str(m.group("pkg") or "").strip().lower()
    raw_title = str(m.group("title") or "").strip()
    task_type, title = _parse_task_type(text, raw_title)
    summary = _parse_summary(text)

    source_report_relpath = ""
    source_report_id = ""
    lines = text.splitlines()
    for raw in lines[:120]:
        line = raw.strip()
        if not line or not line.startswith(">"):
            continue
        m_sr = _META_SOURCE_REPORT_RELPATH_RE.match(line)
        if m_sr:
            source_report_relpath = str(m_sr.group("relpath") or "").strip()
            continue
        m_id = _META_SOURCE_REPORT_ID_RE.match(line)
        if m_id:
            source_report_id = str(m_id.group("id") or "").strip()
            continue

    if not source_report_relpath:
        # Best-effort legacy inference: older drafts stored an absolute path in "Source report",
        # which still contains the stable folder+filename segment.
        hay = "\n".join(lines[:300])
        m_inf = _INFER_SOURCE_REPORT_RELPATH_RE.search(hay)
        if m_inf:
            source_report_relpath = str(m_inf.group("relpath") or "").strip()

    return BacklogItem(
        kind=kind,
        path=path,
        item_id=item_id,
        package=pkg,
        title=title,
        task_type=task_type,
        summary=summary,
        source_report_relpath=source_report_relpath,
        source_report_id=source_report_id,
    )


def iter_backlog_items(dir_path: Path, *, kind: str) -> Iterable[BacklogItem]:
    if not dir_path.exists() or not dir_path.is_dir():
        return []
    out: List[BacklogItem] = []
    for p in sorted(dir_path.glob("*.md")):
        item = parse_backlog_item(p, kind=kind)
        if item is not None:
            out.append(item)
    return out


def max_backlog_id(dirs: List[Tuple[Path, str]]) -> int:
    max_id = 0
    for d, kind in dirs:
        for item in iter_backlog_items(d, kind=kind):
            if item.item_id > max_id:
                max_id = item.item_id
    return max_id
