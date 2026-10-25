"""Push state on disk (framework backlog 1049), all private (0600):

    <plane data dir>/push/devices.json     {schema_version: 1, devices: {device_id: {...}}}
    <plane data dir>/push/cursor.json      {after}: the last stream sequence the dispatcher read
    <plane data dir>/push/outbox.sqlite3   table `rows`, one row per (event, device)
    <gateway data dir>/push/token_owners.json  {schema_version: 1, owners: {sha256(token): {...}}}

A device record holds the APNs token (never returned by a route), the app (watch|phone), the
topic, the environment, the registration handle `reg`, an optional name, `refreshed_at` and the
dispatcher's `last_sent_at` / `last_error`. A registration expires 7 days after its last refresh.

The token owner map is gateway-wide so that a token registered by another principal (another
account signed in on the same app) moves: the latest sign-in wins. Writers lock a file next to the
data (`push/.lock`, fcntl where available) and never hold two locks at once.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

SCHEMA_VERSION = 1
DEVICE_TTL_S = 7 * 86400.0
# Watch-first (backlog 1049): a watch registration refreshed this recently takes the alerts.
WATCH_FRESH_S = 86400.0
PACKAGED_TOPICS = ("ai.abstractframework.assistant", "ai.abstractframework.assistant.watchkitapp")
APPS = ("watch", "phone")
OUTBOX_CAP = 5000
OUTBOX_KEEP_S = 7 * 86400.0

_THREAD_LOCKS: Dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def push_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "push"


def principal_key(tenant: Any, user: Any) -> str:
    """`tenant:user`, the canonical components of the principal's event stream."""
    from ..events_stream import stream_principal

    t, u = stream_principal(tenant, user)
    return f"{t}:{u}"


def token_hash(token: str) -> str:
    return hashlib.sha256(str(token).strip().lower().encode("utf-8")).hexdigest()


@contextlib.contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    key = str(lock_path)
    with _THREAD_LOCKS_GUARD:
        tlock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    with tlock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - Windows: the thread lock still holds
                pass
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover
                pass
            os.close(fd)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def write_private_json(path: Path, obj: Mapping[str, Any]) -> None:
    """Atomic write, mode 0600 from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, sort_keys=True, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    import datetime

    return datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc).isoformat()


def _expired(rec: Mapping[str, Any], now: float) -> bool:
    return float(rec.get("refreshed_at") or 0.0) + DEVICE_TTL_S <= float(now)


# ---------------------------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------------------------


class DeviceStore:
    """The device registrations of ONE plane, plus the gateway-wide token owner map."""

    def __init__(self, plane_dir: Path, root_dir: Path) -> None:
        self.plane_dir = Path(plane_dir)
        self.root_dir = Path(root_dir)
        self.dir = push_dir(self.plane_dir)
        self.path = self.dir / "devices.json"
        self.lock_path = self.dir / ".lock"
        self.owners_path = push_dir(self.root_dir) / "token_owners.json"
        self.owners_lock = push_dir(self.root_dir) / ".owners.lock"

    # -- reads -------------------------------------------------------------------------------

    def change_token(self) -> Tuple[int, int]:
        """(mtime_ns, size) of devices.json: one stat (moves on every write, any process)."""
        try:
            st = self.path.stat()
            return int(st.st_mtime_ns), int(st.st_size)
        except OSError:
            return (0, 0)

    def _load(self) -> Dict[str, Dict[str, Any]]:
        raw = _read_json(self.path) or {}
        if int(raw.get("schema_version") or SCHEMA_VERSION) != SCHEMA_VERSION:
            raise RuntimeError(f"{self.path}: unknown push devices schema_version {raw.get('schema_version')!r}")
        devices = raw.get("devices")
        return {str(k): dict(v) for k, v in devices.items() if isinstance(v, dict)} if isinstance(devices, dict) else {}

    def _save(self, devices: Mapping[str, Mapping[str, Any]]) -> None:
        write_private_json(self.path, {"schema_version": SCHEMA_VERSION, "devices": dict(devices)})

    def active(self, *, principal: Optional[str] = None, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Registrations not expired, optionally of one principal, oldest first."""
        clock = float(now if now is not None else time.time())
        out = [
            dict(rec, device_id=did)
            for did, rec in self._load().items()
            if not _expired(rec, clock) and (principal is None or rec.get("principal") == principal)
        ]
        return sorted(out, key=lambda r: (float(r.get("registered_at") or 0.0), r["device_id"]))

    def get(self, device_id: str) -> Optional[Dict[str, Any]]:
        rec = self._load().get(str(device_id))
        return dict(rec, device_id=str(device_id)) if rec else None

    # -- writes ------------------------------------------------------------------------------

    def put(self, *, principal: str, device_id: str, token: str, topic: str, env: str, app: str, reg: str,
            name: Optional[str], now: Optional[float] = None) -> Tuple[Dict[str, Any], bool]:
        """Register or refresh one device; the token leaves every other registration (any principal,
        any plane of this gateway). Returns (record, first): `first` = the plane had no active
        registration of this principal before (the dispatcher's cursor restarts at the stream head)."""
        clock = float(now if now is not None else time.time())
        token = str(token).strip().lower()
        h = token_hash(token)
        with _locked(self.lock_path):
            devices = {k: v for k, v in self._load().items() if not _expired(v, clock)}
            first = not any(v.get("principal") == principal for v in devices.values())
            old = devices.get(device_id)
            same = bool(old) and old.get("token_hash") == h and old.get("principal") == principal
            rec: Dict[str, Any] = {
                "principal": principal,
                "token": token,
                "token_hash": h,
                "topic": topic,
                "env": env,
                "app": app,
                "reg": str(reg).lower(),
                "name": name or None,
                "registered_at": float(old.get("registered_at")) if same and old.get("registered_at") else clock,
                "refreshed_at": clock,
                "last_sent_at": old.get("last_sent_at") if same else None,
                "last_error": old.get("last_error") if same else None,
                "relay_unreachable_since": old.get("relay_unreachable_since") if same else None,
            }
            # The same token under another device id of this plane (a reinstalled app) goes away.
            for other in [k for k, v in devices.items() if k != device_id and v.get("token_hash") == h]:
                devices.pop(other, None)
            devices[device_id] = rec
            self._save(devices)
        moved_from = self._claim_token(h, principal=principal, device_id=device_id)
        if moved_from is not None:
            plane, other_device = moved_from
            if Path(plane) != self.plane_dir:
                DeviceStore(Path(plane), self.root_dir).drop_token(h, keep_device=None)
        return dict(rec, device_id=device_id), first

    def _claim_token(self, h: str, *, principal: str, device_id: str) -> Optional[Tuple[str, str]]:
        """Record this plane/device as the token's owner; returns the previous owner's (plane, device)."""
        with _locked(self.owners_lock):
            raw = _read_json(self.owners_path) or {}
            owners = dict(raw.get("owners") or {})
            prev = owners.get(h) if isinstance(owners.get(h), dict) else None
            owners[h] = {"principal": principal, "plane": str(self.plane_dir), "device_id": device_id}
            write_private_json(self.owners_path, {"schema_version": SCHEMA_VERSION, "owners": owners})
        if prev and (prev.get("plane") != str(self.plane_dir) or prev.get("device_id") != device_id or prev.get("principal") != principal):
            return str(prev.get("plane") or ""), str(prev.get("device_id") or "")
        return None

    def _release_token(self, h: str, *, device_id: str) -> None:
        with _locked(self.owners_lock):
            raw = _read_json(self.owners_path) or {}
            owners = dict(raw.get("owners") or {})
            cur = owners.get(h)
            if isinstance(cur, dict) and cur.get("plane") == str(self.plane_dir) and cur.get("device_id") == device_id:
                owners.pop(h, None)
                write_private_json(self.owners_path, {"schema_version": SCHEMA_VERSION, "owners": owners})

    def drop_token(self, h: str, *, keep_device: Optional[str]) -> int:
        """Remove every registration of this plane holding the token (but `keep_device`)."""
        with _locked(self.lock_path):
            devices = self._load()
            gone = [k for k, v in devices.items() if v.get("token_hash") == h and k != keep_device]
            for k in gone:
                devices.pop(k, None)
            if gone:
                self._save(devices)
        return len(gone)

    def remove(self, *, principal: Optional[str], device_id: str) -> bool:
        """Delete one registration (only the caller's own when `principal` is given)."""
        with _locked(self.lock_path):
            devices = self._load()
            rec = devices.get(device_id)
            if rec is None or (principal is not None and rec.get("principal") != principal):
                return False
            devices.pop(device_id, None)
            self._save(devices)
        self._release_token(str(rec.get("token_hash") or ""), device_id=device_id)
        return True

    def update(self, device_id: str, **fields: Any) -> bool:
        """Set dispatcher fields (`last_sent_at`, `last_error`, `relay_unreachable_since`) of one device."""
        with _locked(self.lock_path):
            devices = self._load()
            rec = devices.get(device_id)
            if rec is None:
                return False
            changed = {k: v for k, v in fields.items() if rec.get(k) != v}
            if not changed:
                return False
            rec.update(changed)
            self._save(devices)
        return True

    def remove_if_gone(self, device_id: str, *, since_ms: Optional[float]) -> bool:
        """Apple says the token is no longer valid since `since_ms`: delete the registration only when
        that is later than its last refresh (the app may have registered the same token again)."""
        rec = self.get(device_id)
        if rec is None:
            return False
        if since_ms is not None and float(since_ms) / 1000.0 <= float(rec.get("refreshed_at") or 0.0):
            return False
        return self.remove(principal=None, device_id=device_id)


def public_view(rec: Mapping[str, Any]) -> Dict[str, Any]:
    """What a route returns of a registration: never the token."""
    return {
        "device_id": rec.get("device_id"),
        "app": rec.get("app"),
        "name": rec.get("name"),
        "refreshed_at": _iso(rec.get("refreshed_at")),
        "last_sent_at": _iso(rec.get("last_sent_at")),
        "last_error": rec.get("last_error"),
    }


# ---------------------------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------------------------


def read_cursor(plane_dir: Path) -> Optional[int]:
    raw = _read_json(push_dir(plane_dir) / "cursor.json")
    if raw is None or not isinstance(raw.get("after"), int):
        return None
    return max(0, int(raw["after"]))


def write_cursor(plane_dir: Path, after: int) -> None:
    write_private_json(push_dir(plane_dir) / "cursor.json", {"after": int(after)})


# ---------------------------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rows (
  event_id   TEXT NOT NULL,
  device_id  TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  kind       TEXT NOT NULL,
  collapse   TEXT NOT NULL,
  record_key TEXT NOT NULL,
  created_ts REAL NOT NULL,
  attempts   INTEGER NOT NULL DEFAULT 0,
  next_ts    REAL NOT NULL,
  state      TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'sent', 'dropped')),
  error      TEXT,
  seen_key   TEXT NOT NULL DEFAULT '',
  seen_seq   INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (event_id, device_id)
);
CREATE INDEX IF NOT EXISTS rows_due ON rows (state, next_ts);
CREATE INDEX IF NOT EXISTS rows_seen ON rows (seen_key, state);
CREATE INDEX IF NOT EXISTS rows_age ON rows (created_ts);
"""


class Outbox:
    """`push/outbox.sqlite3`: one row per (event, device); pk `event_id|device_id`.

    `seen_key`/`seen_seq` say which `attention.seen` ends a queued row (a wait's key, a published
    alert's record key, or an automation id with its attention seq)."""

    def __init__(self, plane_dir: Path) -> None:
        self.path = push_dir(plane_dir) / "outbox.sqlite3"
        self._ready = False

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                os.close(os.open(str(self.path), os.O_WRONLY | os.O_CREAT, 0o600))
            os.chmod(str(self.path), 0o600)
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        try:
            if not self._ready:
                conn.executescript(_SCHEMA)
                self._ready = True
            yield conn
            conn.commit()
        finally:
            conn.close()

    def enqueue(self, rows: List[Mapping[str, Any]]) -> int:
        if not rows:
            return 0
        with self._db() as db:
            before = db.total_changes
            db.executemany(
                "INSERT OR IGNORE INTO rows (event_id, device_id, seq, kind, collapse, record_key, created_ts, attempts,"
                " next_ts, state, seen_key, seen_seq) VALUES (:event_id, :device_id, :seq, :kind, :collapse, :record_key,"
                " :created_ts, 0, :next_ts, 'queued', :seen_key, :seen_seq)",
                [dict(r) for r in rows],
            )
            return db.total_changes - before

    def drop_seen(self, seen_key: str, seen_seq: int) -> int:
        with self._db() as db:
            cur = db.execute(
                "UPDATE rows SET state='dropped', error='seen' WHERE state='queued' AND seen_key=? AND seen_seq<=?",
                (str(seen_key), int(seen_seq)),
            )
            return int(cur.rowcount or 0)

    def due(self, now: float, limit: int = 200) -> List[Dict[str, Any]]:
        with self._db() as db:
            db.row_factory = sqlite3.Row
            cur = db.execute(
                "SELECT * FROM rows WHERE state='queued' AND next_ts<=? ORDER BY seq, device_id LIMIT ?", (float(now), int(limit))
            )
            return [dict(r) for r in cur.fetchall()]

    def next_due(self) -> Optional[float]:
        with self._db() as db:
            row = db.execute("SELECT MIN(next_ts) FROM rows WHERE state='queued'").fetchone()
            return float(row[0]) if row and row[0] is not None else None

    def mark(self, event_id: str, device_id: str, *, state: str, error: Optional[str] = None,
             attempts: Optional[int] = None, next_ts: Optional[float] = None) -> None:
        sets, args = ["state=?", "error=?"], [state, error]
        if attempts is not None:
            sets.append("attempts=?")
            args.append(int(attempts))
        if next_ts is not None:
            sets.append("next_ts=?")
            args.append(float(next_ts))
        with self._db() as db:
            db.execute(f"UPDATE rows SET {', '.join(sets)} WHERE event_id=? AND device_id=?", (*args, event_id, device_id))

    def rows(self) -> List[Dict[str, Any]]:
        with self._db() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM rows ORDER BY seq, device_id").fetchall()]

    def count(self) -> int:
        with self._db() as db:
            return int(db.execute("SELECT COUNT(*) FROM rows").fetchone()[0])

    def purge(self, now: float, *, cap: int = OUTBOX_CAP, keep_s: float = OUTBOX_KEEP_S) -> int:
        """Sent/dropped rows older than `keep_s` go; then the table is held at `cap` rows (finished rows
        first, then the oldest queued ones)."""
        with self._db() as db:
            n = db.execute(
                "DELETE FROM rows WHERE state IN ('sent', 'dropped') AND created_ts < ?", (float(now) - float(keep_s),)
            ).rowcount or 0
            total = int(db.execute("SELECT COUNT(*) FROM rows").fetchone()[0])
            over = total - int(cap)
            if over > 0:
                n += db.execute(
                    "DELETE FROM rows WHERE rowid IN (SELECT rowid FROM rows ORDER BY (state='queued'), created_ts LIMIT ?)",
                    (over,),
                ).rowcount or 0
            return int(n)


__all__ = [
    "APPS",
    "DEVICE_TTL_S",
    "DeviceStore",
    "OUTBOX_CAP",
    "Outbox",
    "PACKAGED_TOPICS",
    "WATCH_FRESH_S",
    "principal_key",
    "public_view",
    "push_dir",
    "read_cursor",
    "token_hash",
    "write_cursor",
    "write_private_json",
]
