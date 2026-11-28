"""The person's files of an automation run (automations wave 2026-11-27, spec F "Files").

An occurrence row's `artifacts` lists only what a person handed the run or what the run produced
for the person: the `{"$artifact": id}` references found in the occurrence run's INPUT (its vars:
what the trigger carried) and its OUTPUT (what the agent returned), at a DISPLAY PATH, with a
real file name. Never listed: storage offloads (`run_store_offload`, `run_output_offload`,
`run_input_offload`, `ledger_effect_offload`, `ledger_result_offload`, `node_trace_offload`, ...),
workflow snapshots, evidence records, internal/bookkeeping artifacts, voice transport.

Reused, not re-invented:
- the reference walk with each ref's path is the replay view's (`abstractruntime.replay.view.
  _artifact_refs`, the one the chat messages' `artifact_ids` come from);
- the display-path rule is the shared chat's (`abstractuic/panel-chat/src/artifact_provenance.ts`
  `displayArtifactPath`: a ref under `attachments`, `files`, `artifacts`, `media`, `artifact`,
  `*_artifact`, not under a `_private`, scratchpad or trace part, and not inside a conversation
  `context`/`messages`/`history`); no server-side twin of it existed (session turns list every
  artifact of a run tree but voice transport, `history_bundle.is_voice_transport`, also reused);
- the internal-metadata rule is the chat's `internalArtifactMetadata`, plus the offload kinds the
  runtime writes.

Decided from typed metadata (`tags.source`, `tags.kind`, `tags.role`) and the ref's place in the
input/output, never from a file name or a mime type. An output offloaded whole
(`run_output_offload`, the runtime moves a large output to an artifact) is read and walked under
the same path, so the files inside it are still found.
"""

from __future__ import annotations

import mimetypes
import re
from typing import Any, Dict, List, Optional, Tuple

from abstractruntime.history_bundle import is_voice_transport
from abstractruntime.replay.view import _artifact_refs

_SLOTS = frozenset({
    "attachments", "files", "artifacts", "media", "artifact", "artifact_ref",
    "image_artifact", "audio_artifact", "video_artifact", "music_artifact",
})
_INTERNAL_PARTS = frozenset({"scratchpad", "node_traces", "node_trace", "trace", "traces", "bookkeeping"})
_CONVERSATION_PARTS = frozenset({"session", "sessions", "history", "conversation", "transcript", "messages", "context"})
OFFLOAD_SOURCES = frozenset({
    "run_store_offload", "run_output_offload", "run_input_offload", "ledger_effect_offload",
    "ledger_result_offload", "node_trace_offload", "conversation_offload", "session_offload", "state_offload",
})
_INTERNAL_KINDS = frozenset({"workflow_snapshot", "evidence"})
_INTERNAL_ROLES = frozenset({"internal", "bookkeeping"})
# An output moved whole (or a large part of it) to an artifact: walked into, never listed.
_WALK_INTO = frozenset({"run_output_offload", "run_input_offload"})
_MAX_NESTED = 4


def display_artifact_path(path: str) -> bool:
    """panel-chat `displayArtifactPath`, the same rule."""
    parts = [p for p in re.split(r"[.\[\]]", str(path or "")) if p]
    if any(p.startswith("_") or p in _INTERNAL_PARTS for p in parts):
        return False
    slot = next((i for i, p in enumerate(parts) if p in _SLOTS), -1)
    if slot < 0:
        return False
    return not any(p in _CONVERSATION_PARTS for p in parts[:slot])


def internal_artifact(meta: Any) -> bool:
    tags = meta.tags if isinstance(getattr(meta, "tags", None), dict) else {}
    extra = meta.metadata if isinstance(getattr(meta, "metadata", None), dict) else {}
    envelope = extra.get("artifact_envelope_v1") if isinstance(extra.get("artifact_envelope_v1"), dict) else {}
    return (
        str(tags.get("source") or extra.get("source") or "") in OFFLOAD_SOURCES
        or str(tags.get("kind") or "") in _INTERNAL_KINDS
        or str(tags.get("role") or envelope.get("role") or extra.get("role") or "") in _INTERNAL_ROLES
        or is_voice_transport(tags)
    )


def _file_name(meta: Any, index: int) -> str:
    tags = meta.tags if isinstance(getattr(meta, "tags", None), dict) else {}
    extra = meta.metadata if isinstance(getattr(meta, "metadata", None), dict) else {}
    descriptor = getattr(meta, "descriptor", None)
    for value in (tags.get("filename"), extra.get("filename"), extra.get("name"),
                  getattr(descriptor, "filename", None), getattr(descriptor, "title", None)):
        if isinstance(value, str) and value.strip():
            return value.strip()
    # A person's file the producer did not name: a plain name, never the storage id.
    ext = mimetypes.guess_extension(str(getattr(meta, "content_type", "") or "").split(";")[0].strip()) or ""
    return f"file-{index}{ext}"


def _refs(store: Any, value: Any, prefix: str, depth: int = 0) -> List[Tuple[str, str, Any]]:
    """(artifact id, path, metadata) of every ref under `value`, offloaded parts walked into."""
    out: List[Tuple[str, str, Any]] = []
    for art_id, path in _artifact_refs(value):
        full = prefix if path == "$" else (f"{prefix}.{path}" if prefix else path)
        try:
            meta = store.get_metadata(art_id)
        except Exception:  # noqa: BLE001 - an unreadable ref is not a file the person can open
            meta = None
        if meta is None:
            continue
        source = str((meta.tags or {}).get("source") or "") if isinstance(getattr(meta, "tags", None), dict) else ""
        if source in _WALK_INTO and depth < _MAX_NESTED:
            try:
                inner = store.load_json(art_id)
            except Exception:  # noqa: BLE001
                inner = None
            if isinstance(inner, (dict, list)):
                out.extend(_refs(store, inner, full, depth + 1))
            continue
        out.append((art_id, full, meta))
    return out


def person_files(child: Any, artifact_store: Any) -> List[Dict[str, Any]]:
    """The row's `artifacts`: `[{artifact_id, name, mime_type, url}]`, input files first."""
    if child is None or artifact_store is None:
        return []
    found: List[Tuple[str, str, Any]] = []
    vars_ = child.vars if isinstance(getattr(child, "vars", None), dict) else {}
    found += _refs(artifact_store, vars_, "")
    if getattr(child, "output", None) is not None:
        found += _refs(artifact_store, child.output, "output")
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for art_id, path, meta in found:
        if art_id in seen or not display_artifact_path(path) or internal_artifact(meta):
            continue
        seen.add(art_id)
        # The artifact's own run (a file made in a sub-run, or a session attachment): the content
        # route serves it there.
        owner = str(getattr(meta, "run_id", "") or "") or str(child.run_id)
        out.append({
            "artifact_id": art_id,
            "name": _file_name(meta, len(out) + 1),
            "mime_type": str(getattr(meta, "content_type", "") or "application/octet-stream"),
            "url": f"/api/gateway/runs/{owner}/artifacts/{art_id}/content",
        })
    return out


__all__ = ["OFFLOAD_SOURCES", "display_artifact_path", "internal_artifact", "person_files"]
