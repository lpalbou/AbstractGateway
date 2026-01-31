from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


_H1_RE = re.compile(r"^#\s+(?P<id>\d+)-(?P<pkg>[^:]+)\s*:\s*(?P<title>.+?)\s*$")
_H2_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$")


@dataclass(frozen=True)
class BacklogItem:
    kind: str  # planned|completed|proposed
    path: Path
    item_id: int
    package: str
    title: str
    summary: str = ""

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
    title = str(m.group("title") or "").strip()
    summary = _parse_summary(text)
    return BacklogItem(kind=kind, path=path, item_id=item_id, package=pkg, title=title, summary=summary)


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

