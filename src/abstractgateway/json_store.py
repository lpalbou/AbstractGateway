"""A small JSON document per data dir, shared by the per-session stores (session_archive.py,
session_titles.py, session_kinds.py): cached reads, and a read-modify-write that two threads or two
gateway processes on one data dir can run at once without losing a change.

- Reads: the last parsed content, re-stat-ed at most every REVALIDATE_S (another process's write
  is seen within that); a write through this module invalidates it at once, and a reader that
  loaded while a write landed never stores its older copy (a generation per file). A failed read
  is remembered for REVALIDATE_S too (re-raised, not retried on every call). A read that meets a
  sharing violation (another program holds the file without read sharing) is retried for a bounded
  time. Readers never take a lock a writer holds: the atomic replace means they see a whole file.
- Writes (`update`): one writer per file in this process, one across processes (`<file>.lock`, an
  OS byte-range lock), both waited for within ONE deadline (LOCK_WAIT_S): past it, StoreBusy. The
  file is re-read from disk under both, so a write is never based on a stale copy. The new content
  goes to one temp file (`<file>.<pid>.<12 hex>.tmp`, created exclusively, ONE attempt: a folder
  that refuses new files is an immediate error, never a retry loop) and replaces the file
  atomically; a replace refused by a sharing violation is retried for a bounded time. On failure
  the temp file is removed and the store is left as it was. Temp files of that exact shape older
  than a minute (a crash between write and replace) are removed by the next successful write.
- Errors name the file relative to the data dir, never the absolute path (it names the account).
"""

from __future__ import annotations

import datetime
import errno
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

REVALIDATE_S = 1.0
RETRY_S = 1.0           # bounded retry of a read, a replace or a lock-file open that meets a sharing violation
LOCK_WAIT_S = 5.0       # one deadline for the in-process and the inter-process write lock
ORPHAN_TMP_AGE_S = 60.0

_os_replace = os.replace  # the module's own reference (tests patch this, never the global os.replace)
_CONTENTION = {errno.EACCES, errno.EAGAIN, errno.EDEADLK, getattr(errno, "EWOULDBLOCK", errno.EAGAIN)}


class StoreUnreadable(RuntimeError):
    """The file exists but cannot be read as the store. `damaged`: it is UTF-8 text (a BOM
    allowed) that is not JSON (truncated, zero-length, cut mid-write). Not damaged: a valid JSON
    document of another shape, a file in another encoding (UTF-16, ANSI) and an I/O failure (a
    permission, a file held open): those are never treated as damage."""

    def __init__(self, message: str, *, damaged: bool = False) -> None:
        super().__init__(message)
        self.damaged = damaged


class StoreUnwritable(RuntimeError):
    """The store could not be saved (the folder refuses new files, the file stays held open).
    The store is left as it was."""


class StoreBusy(StoreUnwritable):
    """Another writer (this process or another one) held the store past LOCK_WAIT_S; try again."""


def _why(exc: BaseException) -> str:
    """An OS error without its path (`strerror`), else the exception's own text."""
    if isinstance(exc, OSError):
        return f"{type(exc).__name__}: {exc.strerror or 'error ' + str(exc.errno)}"
    return f"{type(exc).__name__}: {exc}"


def _retrying(fn: Callable[[], Any], *, until: Optional[float] = None) -> Any:
    """`fn()`, retried while another program refuses it for a moment (PermissionError: a Windows
    sharing violation while a scanner, indexer or backup holds the file), at most RETRY_S."""
    deadline = time.monotonic() + RETRY_S if until is None else min(until, time.monotonic() + RETRY_S)
    delay = 0.02
    while True:
        try:
            return fn()
        except PermissionError:
            if time.monotonic() + delay > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def _fsync(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        pass


class _InterProcessLock:
    """`<file>.lock`, an exclusive OS lock on its first byte, waited for until `deadline`."""

    def __init__(self, path: Path, label: str, deadline: float) -> None:
        self.path = path
        self.label = label
        self.deadline = deadline
        self.fd: Optional[int] = None

    def __enter__(self) -> "_InterProcessLock":
        try:
            self.fd = _retrying(lambda: os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600), until=self.deadline)
        except OSError as exc:
            raise StoreUnwritable(f"{self.label} cannot be changed: its lock file cannot be opened ({_why(exc)})") from None
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in _CONTENTION:
                    self._close()
                    raise StoreUnwritable(f"{self.label} cannot be changed: its lock file cannot be locked ({_why(exc)})") from None
                if time.monotonic() > self.deadline:
                    self._close()
                    raise StoreBusy(f"{self.label} is being changed by another process; try again") from None
                time.sleep(0.01)

    def _close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __exit__(self, *_exc: Any) -> None:
        if self.fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._close()


class JsonStore:
    """One JSON document named `filename` in each data dir.

    `empty()` is the content of a store that does not exist yet; `valid(doc)` says whether a parsed
    document is this store's (else StoreUnreadable, never touched by a write)."""

    def __init__(self, filename: str, *, empty: Callable[[], Dict[str, Any]], valid: Callable[[Any], bool],
                 reader: Optional[Callable[[Any], Dict[str, Any]]] = None,
                 revalidate_s: Callable[[], float] = lambda: REVALIDATE_S) -> None:
        """`reader` (default `load`) is what a cache miss calls and `revalidate_s` how long a cached
        read is trusted: a store module routes both through its own module names, so its tests can
        count or stretch them."""
        self.filename = filename
        self._empty = empty
        self._valid = valid
        self._reader = reader
        self._revalidate_s = revalidate_s
        self._guard = threading.Lock()
        self._write_locks: Dict[str, threading.Lock] = {}
        self._generation: Dict[str, int] = {}
        # path -> (checked_at, signature, document or None, derived values, failure or None)
        self._cache: Dict[str, Tuple[float, Any, Optional[Dict[str, Any]], Dict[str, Any], Optional[BaseException]]] = {}
        self._orphan = re.compile(re.escape(filename) + r"\.\d+\.[0-9a-f]{12}\.tmp")

    # -- paths -------------------------------------------------------------------------------

    def path(self, data_dir: Any) -> Path:
        return Path(data_dir) / self.filename

    def _write_lock(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._write_locks.get(key)
            if lock is None:
                lock = self._write_locks[key] = threading.Lock()
            return lock

    # -- reading -----------------------------------------------------------------------------

    def load(self, data_dir: Any) -> Dict[str, Any]:
        """The document as it is on disk now (a fresh, mutable copy)."""
        path = self.path(data_dir)
        try:
            raw = _retrying(path.read_bytes)
        except FileNotFoundError:
            return self._empty()
        except OSError as exc:
            raise StoreUnreadable(f"{self.filename} cannot be read ({_why(exc)})") from None
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise StoreUnreadable(f"{self.filename} is not UTF-8 text (saved in another encoding?); it is left as it is") from None
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StoreUnreadable(f"{self.filename} is damaged (not JSON: {exc})", damaged=True) from None
        if not self._valid(doc):
            raise StoreUnreadable(f"{self.filename} is not a document this gateway can read (another schema or format)")
        return doc

    def _entry(self, data_dir: Any) -> Tuple[float, Any, Optional[Dict[str, Any]], Dict[str, Any], Optional[BaseException]]:
        path = self.path(data_dir)
        key = str(path)
        now = time.monotonic()
        with self._guard:
            hit = self._cache.get(key)
            generation = self._generation.get(key, 0)
        if hit is not None and (now - hit[0]) < self._revalidate_s():
            return hit
        try:
            st = os.stat(path)
            sig: Any = (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            sig = None
        except OSError:
            sig = object()  # never equal: read (and raise) below
        if hit is not None and hit[4] is None and hit[1] == sig:
            entry = (now, sig, hit[2], hit[3], None)
        else:
            try:
                entry = (now, sig, (self._reader or self.load)(data_dir), {}, None)
            except Exception as exc:  # noqa: BLE001 - remembered for REVALIDATE_S, re-raised
                entry = (now, sig, None, {}, exc)
        with self._guard:
            # A write that landed while this read ran has its own, newer content: never cache the older copy.
            if self._generation.get(key, 0) == generation:
                self._cache[key] = entry
        return entry

    def _document(self, data_dir: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        entry = self._entry(data_dir)
        if entry[4] is not None:
            raise entry[4]
        assert entry[2] is not None
        return entry[2], entry[3]

    def read(self, data_dir: Any) -> Dict[str, Any]:
        """The cached document, for READERS (never mutate it)."""
        return self._document(data_dir)[0]

    def derived(self, data_dir: Any, name: str, build: Callable[[Dict[str, Any]], Any]) -> Any:
        """`build(document)`, computed once per revision of the file (kept with the read cache)."""
        doc, values = self._document(data_dir)
        if name not in values:
            values[name] = build(doc)
        return values[name]

    def forget(self, data_dir: Any) -> None:
        """Drop the cached read; a read in flight will not store what it loaded."""
        key = str(self.path(data_dir))
        with self._guard:
            self._cache.pop(key, None)
            self._generation[key] = self._generation.get(key, 0) + 1

    # -- writing -----------------------------------------------------------------------------

    def update(self, data_dir: Any, change: Callable[[Dict[str, Any]], Tuple[Any, bool]], *,
               heal_damaged: bool = False) -> Tuple[Any, Optional[str]]:
        """Run `change(document)` on the file as it is on disk, under this process's and every other
        process's lock (one deadline for both: StoreBusy past it); `change` returns `(result,
        changed)` and the document is saved only when `changed`. With `heal_damaged`, a damaged
        file (UTF-8 text that is not JSON) is moved aside as `<stem>.damaged-<UTC>.json` and
        `change` runs on an empty store. Returns `(result, aside)`. Raises StoreUnreadable (the
        file is not this store's, or cannot be read), StoreBusy or StoreUnwritable."""
        path = self.path(data_dir)
        deadline = time.monotonic() + LOCK_WAIT_S
        lock = self._write_lock(str(path))
        if not lock.acquire(timeout=LOCK_WAIT_S):
            raise StoreBusy(f"{self.filename} is being changed by another request; try again")
        try:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StoreUnwritable(f"{self.filename} cannot be saved: its folder cannot be created ({_why(exc)})") from None
            with _InterProcessLock(path.with_name(path.name + ".lock"), self.filename, deadline):
                aside: Optional[str] = None
                try:
                    doc = self.load(data_dir)
                except StoreUnreadable as exc:
                    if not (heal_damaged and exc.damaged):
                        raise
                    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                    aside = f"{path.stem}.damaged-{stamp}{path.suffix}"
                    try:
                        _retrying(lambda: _os_replace(str(path), str(path.with_name(aside))))
                    except OSError as move_exc:
                        raise StoreUnwritable(f"{self.filename} is damaged and could not be set aside ({_why(move_exc)})") from None
                    self.forget(data_dir)
                    doc = self._empty()
                result, changed = change(doc)
                if changed or aside is not None:
                    self._save(path, doc)
                return result, aside
        finally:
            lock.release()

    def _save(self, path: Path, doc: Dict[str, Any]) -> None:
        data = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
        try:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except OSError as exc:
            raise StoreUnwritable(f"{self.filename} cannot be saved: the folder refused a new file ({_why(exc)})") from None
        try:
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                _fsync(fd)
            finally:
                os.close(fd)
            _retrying(lambda: _os_replace(str(tmp), str(path)))
        except OSError as exc:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
            raise StoreUnwritable(f"{self.filename} could not be saved ({_why(exc)}); it was left as it was") from None
        finally:
            self.forget(path.parent)
        self._remove_orphans(path)

    def _remove_orphans(self, path: Path) -> None:
        """This module's temp files (`<file>.<pid>.<12 hex>.tmp` exactly) of earlier writes that
        never reached the replace (a crash, a kill). Nothing else is touched."""
        cutoff = time.time() - ORPHAN_TMP_AGE_S
        try:
            for p in path.parent.glob(path.name + ".*.tmp"):
                if not self._orphan.fullmatch(p.name):
                    continue
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    pass
        except OSError:
            pass


__all__ = ["JsonStore", "StoreBusy", "StoreUnreadable", "StoreUnwritable"]
