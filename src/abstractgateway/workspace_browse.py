"""Browse and preview a run's workspace folder (CONTRACTS §W, amendment A-1).

Pure filesystem helpers behind the three user-level routes
`GET /runs/{run_id}/workspace`, `/workspace/files` and `/workspace/content`:
confinement of a client-given relative path to the workspace root, a listing
that says what it did not show (and why), and byte-range reads.

Confinement: a requested path is RELATIVE to the root. Absolute paths and
`..` that leave the root are refused; the resolved REAL path (symlinks
followed) must stay inside the root's real path, so a symlink that points
outside is refused on read and hidden (and counted) in a listing. The
gateway's ownership marker (`.abstractgateway-workspace.json`) at the root is
never listed nor served. A caller-supplied deny check (`is_blocked`) is
applied to every listed entry and every read: the operator's workspace deny
lists hold at browse time, not only at run start.

Nothing is capped silently: a listing `limit` is the caller's, and the reply
says `truncated` when it stopped there; hidden entries are counted by reason.
"""
from __future__ import annotations

import datetime
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from .run_retention import GATEWAY_WORKSPACE_MARKER

CHUNK_BYTES = 1024 * 1024


class WorkspacePathError(Exception):
    """A refused path: `status` is the HTTP status the route answers."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.message = str(message)


def _real(p: Path) -> Path:
    return Path(os.path.realpath(str(p)))


def _inside(child: Path, root: Path) -> bool:
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


def normalize_rel(raw: Any) -> str:
    """A client path -> a clean relative POSIX path ("" = the root).
    Refuses absolute paths and any `..` component (400)."""
    text = str(raw if raw is not None else "").strip().replace("\\", "/")
    if text in ("", ".", "./"):
        return ""
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise WorkspacePathError(400, f"path {text!r} is absolute; give a path relative to the workspace folder")
    parts = [p for p in PurePosixPath(text).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise WorkspacePathError(400, f"path {text!r} leaves the workspace folder ('..' is not accepted)")
    return "/".join(parts)


def confine(root: Path, rel: Any) -> Tuple[Path, str]:
    """(real path, normalized relative path) of `rel` inside `root`; 403 when
    a symlink leads outside, 404 for the gateway marker or a missing path."""
    rel_n = normalize_rel(rel)
    root_real = _real(root)
    target = root_real / rel_n if rel_n else root_real
    real = _real(target)
    if not _inside(real, root_real):
        raise WorkspacePathError(403, f"{rel_n!r} leads outside the workspace folder (a link to elsewhere); it is not served")
    if rel_n and real == root_real / GATEWAY_WORKSPACE_MARKER:
        raise WorkspacePathError(404, f"{rel_n!r} not found in the workspace folder")
    if not real.exists():
        raise WorkspacePathError(404, f"{rel_n or '.'!r} not found in the workspace folder")
    return real, rel_n


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def list_entries(
    root: Path,
    rel: Any = "",
    *,
    recursive: bool = False,
    limit: Optional[int] = None,
    is_blocked: Callable[[Path], bool] = lambda _p: False,
) -> Dict[str, Any]:
    """{path, entries[{name, path, type, size_bytes, mtime}], truncated,
    limit, hidden{outside_links, blocked, other}}. Folders first, then files,
    by name; a recursive listing walks breadth-first in that order."""
    if limit is not None and int(limit) < 1:
        raise WorkspacePathError(400, "limit must be at least 1 (omit it to list everything)")
    start, rel_n = confine(root, rel)
    if not start.is_dir():
        raise WorkspacePathError(400, f"{rel_n!r} is a file, not a folder")
    if is_blocked(start):
        raise WorkspacePathError(403, f"{rel_n or '.'!r} is inside a folder the gateway's workspace deny list blocks")
    root_real = _real(root)
    entries: List[Dict[str, Any]] = []
    hidden = {"outside_links": 0, "blocked": 0, "other": 0}
    truncated = False
    queue: List[Tuple[Path, str]] = [(start, rel_n)]
    seen_dirs = {str(start)}
    while queue and not truncated:
        folder, folder_rel = queue.pop(0)
        try:
            with os.scandir(folder) as it:
                items = sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=True), e.name.lower(), e.name))
        except OSError as exc:
            raise WorkspacePathError(403, f"{folder_rel or '.'!r} cannot be read: {exc.strerror or exc}")
        for e in items:
            child_rel = f"{folder_rel}/{e.name}" if folder_rel else e.name
            if folder == root_real and e.name == GATEWAY_WORKSPACE_MARKER:
                continue  # the gateway's own marker: never shown, not counted
            real = _real(Path(e.path))
            if not _inside(real, root_real):
                hidden["outside_links"] += 1
                continue
            if is_blocked(real):
                hidden["blocked"] += 1
                continue
            try:
                st = real.stat()
            except OSError:
                hidden["other"] += 1
                continue
            if real.is_dir():
                kind, size = "dir", None
            elif real.is_file():
                kind, size = "file", int(st.st_size)
            else:
                hidden["other"] += 1
                continue
            if limit is not None and len(entries) >= int(limit):
                truncated = True
                break
            entries.append({"name": e.name, "path": child_rel, "type": kind, "size_bytes": size, "mtime": _iso(st.st_mtime)})
            if recursive and kind == "dir" and str(real) not in seen_dirs:
                seen_dirs.add(str(real))
                queue.append((real, child_rel))
    return {
        "path": rel_n,
        "entries": entries,
        "truncated": truncated,
        "limit": int(limit) if limit is not None else None,
        "recursive": bool(recursive),
        "hidden": hidden,
    }


@dataclass(frozen=True)
class FileSlice:
    path: Path
    rel: str
    content_type: str
    size: int
    start: int
    end: int  # inclusive
    partial: bool

    @property
    def length(self) -> int:
        return max(0, self.end - self.start + 1)

    def headers(self) -> Dict[str, str]:
        """Artifact-content headers (CSP sandbox, nosniff, inline) + range."""
        name = self.path.name.replace('"', "").replace("\r", "").replace("\n", "")
        try:
            name.encode("latin-1")
            disposition = f'inline; filename="{name}"'
        except UnicodeEncodeError:
            from urllib.parse import quote

            disposition = f"inline; filename*=UTF-8''{quote(name)}"
        out = {
            "Content-Security-Policy": "sandbox",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": disposition,
            "Accept-Ranges": "bytes",
            "Content-Length": str(self.length),
        }
        if self.partial:
            out["Content-Range"] = f"bytes {self.start}-{self.end}/{self.size}"
        return out

    def iter_bytes(self) -> Iterator[bytes]:
        remaining = self.length
        with open(self.path, "rb") as fh:
            fh.seek(self.start)
            while remaining > 0:
                chunk = fh.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk


def _parse_range(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """`bytes=a-b` | `bytes=a-` | `bytes=-n` -> (start, end inclusive);
    None = no/ignored header; 416 for an unsatisfiable one."""
    text = str(header or "").strip()
    if not text:
        return None
    unit, _, spec = text.partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None  # an unknown unit or several ranges: the whole file (RFC 9110 allows ignoring)
    first, _, last = spec.strip().partition("-")
    try:
        if first == "":
            n = int(last)
            if n <= 0:
                raise ValueError
            start, end = max(0, size - n), size - 1
        else:
            start = int(first)
            end = int(last) if last.strip() else size - 1
    except ValueError:
        raise WorkspacePathError(416, f"Range {text!r} is not a byte range")
    if start < 0 or start >= size or end < start:
        raise WorkspacePathError(416, f"Range {text!r} is outside the file ({size} bytes)")
    return start, min(end, size - 1)


def open_slice(
    root: Path,
    rel: Any,
    *,
    range_header: Optional[str] = None,
    is_blocked: Callable[[Path], bool] = lambda _p: False,
) -> FileSlice:
    real, rel_n = confine(root, rel)
    if not rel_n or real.is_dir():
        raise WorkspacePathError(400, f"{rel_n or '.'!r} is a folder; list it with /workspace/files")
    if not real.is_file():
        raise WorkspacePathError(404, f"{rel_n!r} is not a regular file")
    if is_blocked(real):
        raise WorkspacePathError(403, f"{rel_n!r} is inside a folder the gateway's workspace deny list blocks")
    size = int(real.stat().st_size)
    ctype = mimetypes.guess_type(real.name)[0] or "application/octet-stream"
    rng = _parse_range(range_header, size) if size > 0 else None
    if rng is None:
        return FileSlice(real, rel_n, ctype, size, 0, size - 1, False)
    return FileSlice(real, rel_n, ctype, size, rng[0], rng[1], True)


def workspace_kind(root: Path) -> str:
    """session | run | launch_folder, from the gateway's marker."""
    import json

    marker = Path(root) / GATEWAY_WORKSPACE_MARKER
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no/garbled marker: not a gateway-made folder
        return "launch_folder"
    kind = str((data or {}).get("kind") or "")
    if kind == "session_workspace":
        return "session"
    if kind == "run_workspace":
        return "run"
    return "launch_folder"


def read_marker(root: Path) -> Optional[Dict[str, Any]]:
    import json

    try:
        data = json.loads((Path(root) / GATEWAY_WORKSPACE_MARKER).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None
