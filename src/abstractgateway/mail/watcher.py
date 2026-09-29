"""The per-user mail watcher: read-only, durable cursor, UIDVALIDITY resync, durable inbox.

Replaces the retired `integrations/email_bridge.py` (one process-wide account from
`ABSTRACT_EMAIL_*`, never started under user accounts, no UIDVALIDITY check, events emitted
non-durably). One watcher per user plane, run by that plane's email worker (worker.py):

- Mail is read with AbstractCore's read-only client (`EXAMINE`, `BODY.PEEK[]`): it never sets
  `\\Seen`, never moves or deletes a message (D12).
- `fetch_new(cursor)` returns the messages after the cursor. Each message becomes ONE event
  appended to the plane's durable inbox (`<plane>/email/inbox.sqlite3`, committed with
  `synchronous=FULL`) under `event_id = sha256(account_ref, folder, uidvalidity, uid)`, and
  ONLY THEN does the cursor (`<plane>/email/watcher.json`, fsynced) move past it. A crash
  between the two re-reads the message and the unique `event_id` admits it once.
- UIDVALIDITY changed (the server rebuilt the folder): `email.cursor_reset` is recorded, the
  folder is re-read from the last seen INTERNALDATE (one day earlier, server time zones) and
  messages already in the inbox (same Message-ID) are passed, so nothing is lost or doubled.
- A message that cannot be read three polls in a row is recorded
  `email.message_unprocessable {uid, code, cause, fix}` and passed: one bad message never
  blocks the mailbox. Connection failures back off 60 s -> 15 min and the status carries the
  typed cause and fix; nothing is paused.
- The inbox is what the `email.received@1` automation trigger reads (each automation keeps
  its own cursor over it, so it reads a message at most once). The raw message is kept
  whole (ADR-0026: no truncation); a message over `max_message_bytes` is recorded with
  `raw_skipped: {code: "message_too_large"}`, never clamped.

Cadence (operator, 2026-09-29): the watcher checks every 60 s (minimum 30 s). How often an
automation RUNS on new mail is the automation's own schedule (hourly by default when it runs
a model), enforced where automations are scheduled.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from abstractcore.comms.email import EmailError, MailCursor

from .accounts import EmailPlane, account_store, admin_email_enabled, email_context, _read_json, _write_private_json
from .audit import audit_email_event

DEFAULT_INTERVAL_S = 60.0
MIN_INTERVAL_S = 30.0
MAX_BACKOFF_S = 900.0
MAX_MESSAGES_PER_POLL = 50
UNPROCESSABLE_AFTER = 3
DEFAULT_MAX_MESSAGE_BYTES = 50 * 1024 * 1024

_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _plane_lock(plane: EmailPlane) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(str(plane.root))
        if lock is None:
            lock = _LOCKS[str(plane.root)] = threading.Lock()
        return lock


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def event_id_for(account_ref: str, folder: str, uidvalidity: int, uid: int) -> str:
    canonical = json.dumps([str(account_ref), str(folder), int(uidvalidity), int(uid)], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------
# Durable inbox
# ---------------------------------------------------------------------------------------


class MailInbox:
    """Received-mail events of one plane (SQLite, one row per message, raw kept whole)."""

    def __init__(self, plane: EmailPlane) -> None:
        self.path = plane.email_dir / "inbox.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            " seq INTEGER PRIMARY KEY AUTOINCREMENT,"
            " event_id TEXT NOT NULL UNIQUE,"
            " account_ref TEXT NOT NULL,"
            " folder TEXT NOT NULL,"
            " uidvalidity INTEGER NOT NULL,"
            " uid INTEGER NOT NULL,"
            " message_id TEXT NOT NULL DEFAULT '',"
            " internaldate TEXT NOT NULL DEFAULT '',"
            " received_at TEXT NOT NULL,"
            " payload TEXT NOT NULL,"
            " raw BLOB)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS events_message_id ON events(message_id)")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return conn

    def append(self, event: Dict[str, Any], raw: Optional[bytes]) -> bool:
        """Durably store one event. True when new, False when this event_id is already in."""

        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO events(event_id, account_ref, folder, uidvalidity, uid, message_id, internaldate,"
                    " received_at, payload, raw) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        event["event_id"],
                        event["account_ref"],
                        event["folder"],
                        int(event["uidvalidity"]),
                        int(event["uid"]),
                        str(event.get("message_id") or ""),
                        str(event.get("internaldate") or ""),
                        str(event.get("received_at") or _now_iso()),
                        json.dumps(event, ensure_ascii=False, sort_keys=True),
                        sqlite3.Binary(raw) if raw is not None else None,
                    ),
                )
                return cur.rowcount == 1
        finally:
            conn.close()

    def has_message_id(self, message_id: str) -> bool:
        if not message_id:
            return False
        conn = self._connect()
        try:
            row = conn.execute("SELECT 1 FROM events WHERE message_id = ? LIMIT 1", (str(message_id),)).fetchone()
            return row is not None
        finally:
            conn.close()

    def list_after(self, seq: int = 0, *, limit: int = 100) -> List[Dict[str, Any]]:
        """Events after `seq`, oldest first: `{seq, event_id, ...metadata}` (no raw bytes)."""

        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT seq, payload FROM events WHERE seq > ? ORDER BY seq ASC LIMIT ?", (int(seq), max(1, int(limit)))
            ).fetchall()
        finally:
            conn.close()
        out = []
        for seq_v, payload in rows:
            doc = json.loads(payload)
            doc["seq"] = int(seq_v)
            out.append(doc)
        return out

    def get(self, event_id: str, *, include_raw: bool = False) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT seq, payload, raw FROM events WHERE event_id = ?", (str(event_id),)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        doc = json.loads(row[1])
        doc["seq"] = int(row[0])
        if include_raw:
            doc["raw"] = bytes(row[2]) if row[2] is not None else None
        return doc

    def count(self) -> int:
        if not self.path.exists():
            return 0
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        finally:
            conn.close()

    def latest_seq(self) -> int:
        if not self.path.exists():
            return 0
        conn = self._connect()
        try:
            v = conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]
            return int(v or 0)
        finally:
            conn.close()


# ---------------------------------------------------------------------------------------
# Watcher state
# ---------------------------------------------------------------------------------------


def _state_path(plane: EmailPlane) -> Path:
    return plane.email_dir / "watcher.json"


def read_watcher_state(plane: EmailPlane) -> Dict[str, Any]:
    return _read_json(_state_path(plane))


def _write_state(plane: EmailPlane, doc: Dict[str, Any]) -> None:
    _write_private_json(_state_path(plane), doc)


def reset_watcher_cursor(plane: EmailPlane) -> None:
    """A new / removed account starts from a fresh baseline (only mail that arrives after)."""

    with _plane_lock(plane):
        doc = read_watcher_state(plane)
        for key in ("cursor", "failures", "backoff_until", "consecutive_errors", "last_error"):
            doc.pop(key, None)
        doc["state"] = "idle"
        _write_state(plane, doc)


def watcher_public_status(plane: EmailPlane) -> Dict[str, Any]:
    doc = read_watcher_state(plane)
    cursor = doc.get("cursor") if isinstance(doc.get("cursor"), dict) else None
    return {
        "state": str(doc.get("state") or "idle"),
        "interval_s": float(doc.get("interval_s") or DEFAULT_INTERVAL_S),
        "last_poll": doc.get("last_poll") or "",
        "last_ok": doc.get("last_ok") or "",
        "last_error": doc.get("last_error"),
        "next_poll_after": doc.get("backoff_until") or "",
        "cursor": {"uidvalidity": cursor.get("uidvalidity"), "last_uid": cursor.get("last_uid"), "folder": cursor.get("folder")}
        if cursor
        else None,
        "received": MailInbox(plane).count(),
    }


# ---------------------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------------------


def _event_for(plane: EmailPlane, summary: Any, detail: Any, *, raw_len: int, raw_skipped: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    folder = str(summary.folder or "INBOX")
    event: Dict[str, Any] = {
        "schema": "email_received_v1",
        "event_id": event_id_for(plane.account_ref, folder, int(summary.uidvalidity), int(summary.uid)),
        "account_ref": plane.account_ref,
        "account": "self",
        "folder": folder,
        "uidvalidity": int(summary.uidvalidity),
        "uid": int(summary.uid),
        "message_id": str(summary.message_id or ""),
        "internaldate": str(summary.internaldate or ""),
        "received_at": _now_iso(),
        "content_trust": "untrusted",
        "subject": str(summary.subject or ""),
        "from": str(summary.from_ or ""),
        "from_address": str(summary.from_address or ""),
        "to": str(summary.to or ""),
        "cc": str(summary.cc or ""),
        "date": str(summary.date or ""),
        "size": summary.size,
        "raw_bytes": int(raw_len),
    }
    if detail is not None:
        event["in_reply_to"] = str(detail.in_reply_to or "")
        event["references"] = list(detail.references or ())
        event["has_attachment"] = bool(detail.attachments)
        event["attachments"] = [a.to_dict() for a in detail.attachments]
    if raw_skipped:
        event["raw_skipped"] = raw_skipped
    return event


def _backoff_s(consecutive_errors: int, interval_s: float) -> float:
    return min(MAX_BACKOFF_S, max(interval_s, DEFAULT_INTERVAL_S) * (2 ** max(0, consecutive_errors - 1)))


class MailWatcher:
    """Polls one plane's mailbox. `poll_once()` is deterministic (tests drive it)."""

    def __init__(
        self,
        plane: EmailPlane,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        has_consumers: Optional[Callable[[], bool]] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.plane = plane
        self.interval_s = max(MIN_INTERVAL_S, float(interval_s or DEFAULT_INTERVAL_S))
        self._has_consumers = has_consumers
        self._on_event = on_event
        self.max_message_bytes = int(max_message_bytes)
        self._clock = clock
        self.inbox = MailInbox(plane)

    # -- scheduling ------------------------------------------------------------------------

    def due(self) -> bool:
        doc = read_watcher_state(self.plane)
        now = self._clock()
        try:
            backoff_until = float(doc.get("backoff_until_ts") or 0.0)
        except (TypeError, ValueError):
            backoff_until = 0.0
        if backoff_until and now < backoff_until:
            return False
        try:
            last = float(doc.get("last_poll_ts") or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        return now - last >= self.interval_s

    # -- one poll --------------------------------------------------------------------------

    def _set_state(self, doc: Dict[str, Any], state: str) -> None:
        doc["state"] = state
        doc["interval_s"] = self.interval_s

    def poll_once(self) -> Dict[str, Any]:
        """One poll. Returns `{state, new, skipped, reset, error?}`."""

        with _plane_lock(self.plane):
            return self._poll_locked()

    def _poll_locked(self) -> Dict[str, Any]:
        plane = self.plane
        doc = read_watcher_state(plane)
        now = self._clock()
        doc["last_poll"] = _now_iso()
        doc["last_poll_ts"] = now
        out: Dict[str, Any] = {"state": "", "new": 0, "skipped": 0, "reset": False}

        if not admin_email_enabled(plane):
            self._set_state(doc, "off (turned off by an administrator)")
            _write_state(plane, doc)
            out["state"] = doc["state"]
            return out
        try:
            settings = account_store(plane).settings()
        except EmailError as err:
            return self._fail(doc, err, out)
        if settings.account is None:
            self._set_state(doc, "not connected")
            _write_state(plane, doc)
            out["state"] = doc["state"]
            return out
        if not settings.enabled:
            self._set_state(doc, "off (turned off by the user)")
            _write_state(plane, doc)
            out["state"] = doc["state"]
            return out
        if not settings.account.can_read:
            self._set_state(doc, "not watching (the account has no IMAP settings)")
            _write_state(plane, doc)
            out["state"] = doc["state"]
            return out
        if self._has_consumers is not None:
            try:
                wanted = bool(self._has_consumers())
            except Exception:  # noqa: BLE001 - a probe failure never stops the others
                wanted = False
            if not wanted:
                self._set_state(doc, "idle (no email-triggered automation)")
                _write_state(plane, doc)
                out["state"] = doc["state"]
                return out

        try:
            ctx = email_context(plane)
            client = ctx.client()
            cursor = MailCursor.from_dict(doc.get("cursor"))
            result = client.fetch_new(cursor, limit=MAX_MESSAGES_PER_POLL)
        except EmailError as err:
            return self._fail(doc, err, out)

        if result.baseline:
            doc["cursor"] = result.cursor.to_dict()
            self._ok(doc)
            _write_state(plane, doc)
            out["state"] = doc["state"]
            return out

        if result.reset:
            out["reset"] = True
            previous = cursor.uidvalidity if cursor else None
            audit_email_event(
                "email.cursor_reset",
                tenant_id=plane.tenant_id,
                user_id=plane.user_id,
                folder=result.cursor.folder,
                uidvalidity=result.cursor.uidvalidity,
                previous_uidvalidity=previous,
            )
            # A reset with nothing to re-read still moves the cursor into the new epoch.
            doc["cursor"] = MailCursor(
                result.cursor.uidvalidity, 0, result.cursor.folder, cursor.last_internaldate if cursor else ""
            ).to_dict()
            doc["failures"] = {}
            _write_state(plane, doc)

        failures: Dict[str, int] = {str(k): int(v) for k, v in (doc.get("failures") or {}).items()}
        stopped_on: Optional[EmailError] = None
        for summary in result.messages:
            uid_key = str(summary.uid)
            try:
                if result.reset and summary.message_id and self.inbox.has_message_id(summary.message_id):
                    out["skipped"] += 1
                else:
                    self._store_message(client, summary)
                    out["new"] += 1
                failures.pop(uid_key, None)
            except EmailError as err:
                if err.retryable and err.code in ("email_unreachable", "email_transient"):
                    # The connection, not the message: stop here, retry the rest next poll.
                    stopped_on = err
                    break
                count = failures.get(uid_key, 0) + 1
                if count < UNPROCESSABLE_AFTER:
                    failures[uid_key] = count
                    stopped_on = err
                    break
                failures.pop(uid_key, None)
                audit_email_event(
                    "email.message_unprocessable",
                    tenant_id=plane.tenant_id,
                    user_id=plane.user_id,
                    uid=int(summary.uid),
                    folder=summary.folder,
                    code=err.code,
                    cause=err.cause,
                    fix=err.fix,
                )
                out["skipped"] += 1
            # Durable event (or a recorded pass) first, cursor second.
            doc["cursor"] = MailCursor(
                int(summary.uidvalidity), int(summary.uid), str(summary.folder or "INBOX"), str(summary.internaldate or "")
            ).to_dict()
            doc["failures"] = failures
            _write_state(plane, doc)
        doc["failures"] = failures
        if stopped_on is not None:
            return self._fail(doc, stopped_on, out)
        self._ok(doc)
        _write_state(plane, doc)
        out["state"] = doc["state"]
        return out

    def _store_message(self, client: Any, summary: Any) -> None:
        raw: Optional[bytes] = None
        raw_skipped: Optional[Dict[str, Any]] = None
        detail = None
        size = summary.size
        if size is not None and self.max_message_bytes and int(size) > self.max_message_bytes:
            raw_skipped = {
                "code": "message_too_large",
                "cause": f"The message is {int(size)} bytes, over the {self.max_message_bytes}-byte fetch limit; its body was not fetched.",
                "fix": "Read it in your mail client; the event carries its headers.",
            }
        else:
            detail = client.get(summary.uid, folder=summary.folder, include_raw=True)
            raw = bytes(detail.raw or b"")
        event = _event_for(self.plane, summary, detail, raw_len=len(raw or b""), raw_skipped=raw_skipped)
        inserted = self.inbox.append(event, raw)
        if inserted and self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001 - the event is durable; the hook is a courtesy
                pass

    def _ok(self, doc: Dict[str, Any]) -> None:
        self._set_state(doc, "watching")
        doc["last_ok"] = _now_iso()
        doc.pop("last_error", None)
        doc.pop("backoff_until", None)
        doc.pop("backoff_until_ts", None)
        doc["consecutive_errors"] = 0

    def _fail(self, doc: Dict[str, Any], err: EmailError, out: Dict[str, Any]) -> Dict[str, Any]:
        errors = int(doc.get("consecutive_errors") or 0) + 1
        wait = _backoff_s(errors, self.interval_s)
        doc["consecutive_errors"] = errors
        doc["last_error"] = {"code": err.code, "cause": err.cause, "fix": err.fix, "at": _now_iso()}
        doc["backoff_until_ts"] = self._clock() + wait
        doc["backoff_until"] = datetime.datetime.fromtimestamp(doc["backoff_until_ts"], datetime.timezone.utc).replace(microsecond=0).isoformat()
        self._set_state(doc, "needs action" if not err.retryable else "retrying")
        _write_state(self.plane, doc)
        try:
            account_store(self.plane).record_error(err)
        except Exception:  # noqa: BLE001
            pass
        out["state"] = doc["state"]
        out["error"] = err.to_dict(include_details=False)
        return out


def plane_has_email_automations(svc: Any) -> bool:
    """True when the plane has an active automation on the `email.received` trigger."""

    try:
        from abstractruntime.automation_queries import list_automations

        run_store = svc.host.run_store
        cursor = None
        while True:
            page = list_automations(run_store, status="active", cursor=cursor, limit=200)
            for item in page.items:
                trig = item.get("trigger") if isinstance(item, dict) else None
                if isinstance(trig, dict) and str(trig.get("source_id") or "") == "email.received":
                    return True
            if not page.next_cursor:
                return False
            cursor = page.next_cursor
    except Exception:  # noqa: BLE001 - no automations API / unreadable store: nothing consumes mail
        return False
