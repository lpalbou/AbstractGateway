"""Parse a file once per version (gateway load 2026-10-26).

Flow's library polls `GET /visualflows` (107 files, 2.6 MB on the operator's plane) and the
backlog board polls `GET /backlog/{kind}` (one Markdown file per item, across every repo);
both re-read and re-parsed every file on every call. `FileVersionCache.get(path, parse)`
returns the value parsed from the file's current version: the file's identity
(`st_mtime_ns`, `st_size`, `st_ino`) is compared by one stat, the file is read again only when
it changed. Callers run in worker threads; the cache is thread-safe.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Hashable, Iterable, List, Optional, Tuple

Signature = Tuple[int, int, int]


def signature_of(st: os.stat_result) -> Signature:
    return (int(st.st_mtime_ns), int(st.st_size), int(st.st_ino))


def listing(dir_path: Path, suffix: str, *, files_only: bool = True) -> List[Tuple[str, Path, Signature]]:
    """(name, path, signature) of the entries in `dir_path` whose name ends with `suffix`
    (case-sensitive, like `Path.glob("*<suffix>")` on POSIX), sorted by name: regular files
    (symlinks followed) by default, every entry with `files_only=False` (what the glob returns).
    One scandir and one stat per entry."""
    out: List[Tuple[str, Path, Signature]] = []
    try:
        it = os.scandir(dir_path)
    except FileNotFoundError:
        return out
    with it:
        for entry in it:
            if not entry.name.endswith(suffix):
                continue
            try:
                if files_only and not entry.is_file():
                    continue
                sig = signature_of(entry.stat())
            except OSError:
                continue
            out.append((entry.name, Path(entry.path), sig))
    out.sort(key=lambda t: t[0])
    return out


class FileVersionCache:
    def __init__(self, max_entries: int = 20_000) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[Hashable, Tuple[Signature, Any]] = {}
        self._max = int(max_entries)
        self.parses = 0  # how many times `parse` ran (tests, diagnostics)

    def get(self, key: Hashable, sig: Signature, parse: Callable[[], Any]) -> Any:
        """The value for `key` at version `sig`; `parse()` only when the version is new. An
        exception from `parse` propagates and is not cached."""
        with self._lock:
            hit = self._entries.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
        value = parse()
        with self._lock:
            self.parses += 1
            if len(self._entries) >= self._max:
                self._entries.clear()
            self._entries[key] = (sig, value)
        return value

    def get_path(self, path: Path, parse: Callable[[Path], Any], *, key_extra: Hashable = None) -> Any:
        """`get` for one file by path (one stat). A missing file raises FileNotFoundError."""
        sig = signature_of(os.stat(path))
        return self.get((str(path), key_extra), sig, lambda: parse(path))

    def prune(self, keep: Iterable[Hashable]) -> None:
        wanted = set(keep)
        with self._lock:
            for k in [k for k in self._entries if k not in wanted]:
                del self._entries[k]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class Memo:
    """One computed value kept while its key (e.g. a directory's listing signature) is equal."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: Optional[Hashable] = None
        self._value: Any = None
        self._set = False

    def get(self, key: Hashable) -> Tuple[bool, Any]:
        with self._lock:
            if self._set and self._key == key:
                return True, self._value
            return False, None

    def put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._key, self._value, self._set = key, value, True

    def clear(self) -> None:
        with self._lock:
            self._key, self._value, self._set = None, None, False


__all__ = ["FileVersionCache", "Memo", "listing", "signature_of"]
