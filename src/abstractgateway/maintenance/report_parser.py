from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .report_models import ReportHeader, ReportRecord, ReportType


_TITLE_RE = re.compile(r"^#\s+(?P<kind>Bug|Feature)\s*:\s*(?P<title>.+?)\s*$", re.IGNORECASE)
_BLOCKQUOTE_KV_RE = re.compile(r"^\s*>\s*(?P<key>[^:]+?)\s*:\s*(?P<value>.*?)\s*$")
_H2_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$")
_FENCE_JSON_RE = re.compile(r"```json\s*(?P<body>.*?)\s*```", re.IGNORECASE | re.DOTALL)


def _infer_report_type_from_path(path: Path) -> Optional[ReportType]:
    parts = {p.lower() for p in path.parts}
    if "bug_reports" in parts:
        return "bug"
    if "feature_requests" in parts:
        return "feature"
    return None


def _parse_title_and_type(text: str, *, path: Path) -> Tuple[ReportType, str]:
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("#"):
            continue
        m = _TITLE_RE.match(line)
        if m:
            kind = (m.group("kind") or "").strip().lower()
            title = (m.group("title") or "").strip()
            if len(title) > 120:
                title = title[:120].rstrip()
            return ("bug" if kind == "bug" else "feature", title)
        break
    inferred = _infer_report_type_from_path(path) or "bug"
    # Best-effort: use first non-empty line as title.
    for raw in text.splitlines():
        s = raw.strip().lstrip("#").strip()
        if s:
            return inferred, s[:120]
    return inferred, "Report"


def _parse_blockquote_headers(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        m = _BLOCKQUOTE_KV_RE.match(raw)
        if not m:
            continue
        key = str(m.group("key") or "").strip().lower()
        val = str(m.group("value") or "").strip()
        if key:
            out[key] = val
    return out


def _parse_sections(text: str) -> Dict[str, str]:
    sections: Dict[str, list[str]] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        m = _H2_RE.match(raw.strip())
        if m:
            current = str(m.group("name") or "").strip()
            if current:
                sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(raw.rstrip("\n"))
    # Preserve leading whitespace (many templates store user-provided fields as
    # Markdown literals via indentation). Only strip surrounding newlines.
    return {k: "\n".join(v).strip("\n") for k, v in sections.items()}


def _extract_context_json(text: str) -> Dict[str, Any]:
    m = _FENCE_JSON_RE.search(text)
    if not m:
        return {}
    body = str(m.group("body") or "").strip()
    if not body:
        return {}
    try:
        obj = json.loads(body)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _first_nonempty_section(sections: Dict[str, str], *names: str) -> str:
    for name in names:
        content = sections.get(name)
        if isinstance(content, str) and content.strip():
            # Preserve indentation (see `_parse_sections`).
            return content.strip("\n")
    return ""


def _dedent_markdown_literal(text: str) -> str:
    """Remove a single Markdown-literal indentation level (4 spaces).

    Gateway report templates store user-provided descriptions as indented code
    blocks to avoid accidental Markdown/HTML rendering. Downstream consumers
    (triage + proposed backlog drafts) want the raw user text.
    """

    s = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not s:
        return ""
    lines = s.split("\n")
    out: list[str] = []
    for ln in lines:
        if ln.startswith("    "):
            out.append(ln[4:])
        elif ln.startswith("\t"):
            out.append(ln[1:])
        else:
            out.append(ln)
    return "\n".join(out).rstrip("\n")


def parse_report_file(path: Path) -> ReportRecord:
    text = path.read_text(encoding="utf-8", errors="replace")
    report_type, title = _parse_title_and_type(text, path=path)

    headers = _parse_blockquote_headers(text)

    def _h(*keys: str) -> str:
        for k in keys:
            v = headers.get(k.lower())
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    created_at = _h("created")
    report_id = _h("bug id", "feature id", "report id")
    session_id = _h("session id")
    session_memory_run_id = _h("session memory run id", "session_memory_run_id")
    active_run_id = _h("relevant run id", "active run id", "run id")
    workflow_id = _h("workflow id")

    sections = _parse_sections(text)

    description = _first_nonempty_section(
        sections,
        "User Description",
        "User Request",
        "Description",
    )
    description = _dedent_markdown_literal(description)

    # Environment info (best-effort; present in templates but may be missing/edited).
    env_section = sections.get("Environment") or ""
    client = ""
    client_version = ""
    provider = ""
    model = ""
    template = ""
    if env_section:
        # Parse "- Key: value" bullets.
        for raw in env_section.splitlines():
            line = raw.strip()
            if not line.startswith("-"):
                continue
            kv = line.lstrip("-").strip()
            if ":" not in kv:
                continue
            k, v = kv.split(":", 1)
            key = k.strip().lower()
            val = v.strip()
            if not val:
                continue
            if key == "client":
                client = val
            elif key == "client version":
                client_version = val
            elif key in {"provider/model", "provider"}:
                provider = val
            elif key == "model":
                model = val
            elif key in {"agent template", "template"}:
                template = val
            elif key == "provider/model":
                # "Provider/model: provider / model"
                if "/" in val:
                    pass

        # Special-case provider/model format: "- Provider/model: X / Y"
        m = re.search(r"^\s*-\s*Provider/model\s*:\s*(.+?)\s*/\s*(.+?)\s*$", env_section, re.IGNORECASE | re.MULTILINE)
        if m:
            provider = (m.group(1) or "").strip()
            model = (m.group(2) or "").strip()

    header = ReportHeader(
        title=title,
        created_at=created_at,
        report_id=report_id,
        session_id=session_id,
        session_memory_run_id=session_memory_run_id,
        active_run_id=active_run_id,
        workflow_id=workflow_id,
        client=client,
        client_version=client_version,
        provider=provider,
        model=model,
        template=template,
    )
    context = _extract_context_json(text)

    return ReportRecord(
        report_type=report_type,
        path=path,
        header=header,
        description=description,
        sections=sections,
        context=context,
    )
