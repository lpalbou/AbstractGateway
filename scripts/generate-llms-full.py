#!/usr/bin/env python3
from __future__ import annotations

import posixpath
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit


FILES_IN_ORDER: list[str] = [
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "ACKNOWLEDGMENTS.md",
    "docs/README.md",
    "docs/getting-started.md",
    "docs/faq.md",
    "docs/architecture.md",
    "docs/configuration.md",
    "docs/api.md",
    "docs/security.md",
    "docs/maintenance.md",
]


HEADER = (
    "# AbstractGateway — llms-full\n\n"
    "This file is a single-document snapshot of the human-facing docs in this repo, intended for LLM/agent ingestion.\n"
    "It is generated from the sources listed in `llms.txt` in the same order. Relative links are normalized to repo-root paths.\n\n"
    "---\n\n"
)


_MD_LINK_RE = re.compile(r"\]\(([^)]+)\)")


def _normalize_link_dest(dest: str, *, source_relpath: str) -> str:
    raw = str(dest or "")
    u = urlsplit(raw)
    if u.scheme or u.netloc:
        return raw
    if not u.path:
        # Anchor-only links like (#section)
        return raw
    if u.path.startswith("/"):
        # Absolute web paths (/api/...) are not repo files.
        return raw

    # Resolve path relative to the source file directory.
    base = PurePosixPath(source_relpath).parent
    resolved = posixpath.normpath(str(base / PurePosixPath(u.path)))
    if resolved == ".":
        resolved = str(base)

    return urlunsplit(("", "", resolved, u.query, u.fragment))


def _normalize_links(text: str, *, source_relpath: str) -> str:
    def repl(m: re.Match[str]) -> str:
        dest = m.group(1)
        norm = _normalize_link_dest(dest, source_relpath=source_relpath)
        return f"]({norm})"

    return _MD_LINK_RE.sub(repl, text)


def _read_utf8(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    out_path = root / "llms-full.txt"

    chunks: list[str] = [HEADER]

    for i, rel in enumerate(FILES_IN_ORDER):
        p = root / rel
        if not p.exists():
            continue
        chunks.append(f"## {rel}\n\n")
        body = _read_utf8(p).rstrip()
        chunks.append(_normalize_links(body, source_relpath=rel))
        chunks.append("\n")
        if i != len(FILES_IN_ORDER) - 1:
            chunks.append("\n---\n\n")

    out_path.write_text("".join(chunks).rstrip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

