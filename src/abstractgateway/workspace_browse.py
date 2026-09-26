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


def marker_identity(root: Path) -> Optional[Tuple[int, int]]:
    """(st_dev, st_ino) of the gateway's marker file at `root` (a regular
    file, not a link), or None. Hidden by IDENTITY, so a link to it under
    another name is hidden too."""
    try:
        st = os.lstat(str(_real(root) / GATEWAY_WORKSPACE_MARKER))
    except OSError:
        return None
    import stat as _stat

    return (st.st_dev, st.st_ino) if _stat.S_ISREG(st.st_mode) else None


def _identity(p: Path) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(str(p))
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def confine(root: Path, rel: Any) -> Tuple[Path, str]:
    """(real path, normalized relative path) of `rel` inside `root`; 403 when
    a symlink leads outside, 404 for the gateway marker or a missing path."""
    rel_n = normalize_rel(rel)
    root_real = _real(root)
    target = root_real / rel_n if rel_n else root_real
    real = _real(target)
    if not _inside(real, root_real):
        raise WorkspacePathError(403, f"{rel_n!r} leads outside the workspace folder (a link to elsewhere); it is not served")
    if not real.exists():
        raise WorkspacePathError(404, f"{rel_n or '.'!r} not found in the workspace folder")
    marker = marker_identity(root)
    if rel_n and marker is not None and _identity(real) == marker:
        raise WorkspacePathError(404, f"{rel_n!r} not found in the workspace folder")
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
    if rel_n and is_blocked(start):
        raise WorkspacePathError(404, f"{rel_n!r} not found in the workspace folder")
    root_real = _real(root)
    marker = marker_identity(root)
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
            real = _real(Path(e.path))
            if not _inside(real, root_real):
                hidden["outside_links"] += 1
                continue
            if marker is not None and _identity(real) == marker:
                continue  # the gateway's own marker (by identity): never shown, not counted
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
    fd: int = -1  # the file, already opened and verified (open_slice)

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
        """Reads the descriptor open_slice verified (never re-opens by path,
        so a component swapped for a link in between is not followed)."""
        remaining = self.length
        with os.fdopen(self.fd, "rb") as fh:
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
        # Blocked (a deny list, a credential folder, the gateway's data
        # folder): answered like a missing file, so its existence is not told.
        raise WorkspacePathError(404, f"{rel_n!r} not found in the workspace folder")
    fd = _open_verified(real, root)
    try:
        import stat as _stat

        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise WorkspacePathError(404, f"{rel_n!r} is not a regular file")
        marker = marker_identity(root)
        if marker is not None and (st.st_dev, st.st_ino) == marker:
            raise WorkspacePathError(404, f"{rel_n!r} not found in the workspace folder")
        size = int(st.st_size)
        ctype = mimetypes.guess_type(real.name)[0] or "application/octet-stream"
        rng = _parse_range(range_header, size) if size > 0 else None
    except BaseException:
        os.close(fd)
        raise
    if rng is None:
        return FileSlice(real, rel_n, ctype, size, 0, size - 1, False, fd)
    return FileSlice(real, rel_n, ctype, size, rng[0], rng[1], True, fd)


def _fd_path(fd: int) -> Optional[str]:
    """The path the kernel reports for an open descriptor (macOS F_GETPATH,
    Linux /proc/self/fd), or None where neither exists."""
    import fcntl

    getpath = getattr(fcntl, "F_GETPATH", None)
    if getpath is not None:
        try:
            raw = fcntl.fcntl(fd, getpath, b"\0" * 1024)
            return raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
        except OSError:
            return None
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


def _open_verified(real: Path, root: Path) -> int:
    """Open `real` without following a final link, then check the OPENED
    file is still inside `root` (closes the check-then-open race)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(real), flags)
    except OSError as exc:
        raise WorkspacePathError(403, f"{real.name!r} changed while it was being opened; it is not served ({exc.strerror or exc})")
    where = _fd_path(fd)
    if where is None or not _inside(_real(Path(where)), _real(root)):
        os.close(fd)
        raise WorkspacePathError(403, f"{real.name!r} changed while it was being opened; it is not served")
    return fd


# ---- The built-in deny list (operator ruling) -------------------
# Credential and configuration folders of the gateway's user account, plus the
# gateway's data folder: never listed nor served by the workspace routes, and
# added to every run's tool deny list (an admin may turn that off for runs with
# the stored setting `workspace_builtin_deny`).
BUILTIN_DENY_HOME_RELPATHS: Tuple[str, ...] = (
    ".ssh", ".aws", ".gnupg", ".config/gcloud", ".kube", "Library/Keychains",
    ".abstractgateway", ".abstractcode", ".abstractassistant", ".abstractcontinuum", ".abstractcore",
)


def builtin_deny_paths(data_root: Path, *, home: Optional[Path] = None) -> List[Path]:
    base = Path(home) if home is not None else Path.home()
    out = [Path(os.path.realpath(str(base / rel))) for rel in BUILTIN_DENY_HOME_RELPATHS]
    out.append(_real(Path(data_root)))
    return out


def deny_check(
    *,
    data_root: Path,
    own_folder: Optional[Path],
    blocked_roots: List[Path],
    home: Optional[Path] = None,
) -> Callable[[Path], bool]:
    """is_blocked(real path) for browse: the operator's deny lists, always;
    the built-in list and the whole gateway data folder, except inside the
    run's own gateway-made folder."""
    builtin = builtin_deny_paths(data_root, home=home)
    blocked = [_real(Path(b)) for b in blocked_roots]
    own = _real(Path(own_folder)) if own_folder is not None else None

    def is_blocked(p: Path) -> bool:
        rp = _real(Path(p))
        if any(_inside(rp, b) for b in blocked):
            return True
        if own is not None and _inside(rp, own):
            return False
        return any(_inside(rp, b) for b in builtin)

    return is_blocked
