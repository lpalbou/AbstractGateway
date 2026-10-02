"""Email notifications: per-user preferences, a durable outbox, fixed templates, a collector.

Who gets what (D4/D5): a notice goes to the user's registered address (else their mailbox
address), sent THROUGH THEIR OWN ACCOUNT with `guarded_send` — so the user's recipient
policy and send limits apply exactly as for an agent's send. No account, email turned off
by the user or by an administrator: no email notifications (no gateway-wide sender).

The user has TWO switches (preferences v2, 2026-09-30), both ON by default; nothing is sent
until a mailbox is connected and in use:

    job_failed          "Job failed": an automation of theirs failed after its retries
    approval_needed     "Approval needed": a run of theirs waits on a person (tool approval, question)

The explicit opt-ins stand on their own, with no global preference involved:

    automation result   an occurrence asked to notify (`notify` in its output) and the automation
                        delivers to email (`notify.channels` holds "email", schema v2)
    job finished/failed a run started with `_runtime.notify = {on: ["finished" | "failed"],
                        channels: ["email"]}` ("email me when done")

Preferences v1 (five kinds, all OFF by default) are read as v2: `job_failed = job_failed OR
automation_failed`, `approval_needed = approval_needed`; a file never saved gets the v2 defaults.

The outbox (`<plane>/email/outbox.sqlite3`) makes delivery exactly-once-or-visible:

- `idempotency_key = sha256(kind, subject id, sequence)` is the primary key: a notice is
  queued once however many times the collector sees its event;
- `queued -> sending -> sent | failed`; `sending` is committed BEFORE the SMTP exchange, so a
  crash mid-send leaves `sending`, which the next start turns into `unknown` — never resent
  automatically (the user may have received it);
- SMTP 4xx / network problems are retried with backoff; 5xx, sign-in and policy refusals are
  `failed` with the typed cause and fix (and shown in the status);
- over the send limits (100/hour, 1000/day by default, the user's own), notices wait for the
  window and then go out as ONE digest message.

Every notice is automatic mail (RFC 3834): it carries `Auto-Submitted: auto-generated` and the
framework marker (`X-AbstractFramework-Automation: notification:<key>`), and its Message-ID is
recorded here (`sent_messages`, with every other automatic send through the account: automation
occurrences, sign-in codes). The mail watcher never admits the account's own marked mail nor a
recorded Message-ID, so a notice can never trigger an automation (the 0.7.0 self-trigger loop).

Templates are fixed (text + minimal HTML). The only model-authored text is an automation's
own `notify` title/body, labelled as such. No approve/deny links (D8).
"""

from __future__ import annotations

import datetime
import hashlib
import html
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .core_mail import EmailError, EmailRateLimited, OutgoingMessage, guarded_send

from .accounts import (
    EmailPlane,
    SETTINGS_LABEL,
    account_store,
    email_context,
    email_usable,
    mailbox_unavailable_reason,
    _read_json,
    _write_private_json,
)
from .audit import audit_email_event

EVENTS = ("job_failed", "approval_needed")
# The two notifications are the events that need the user; both ON by default (they only
# send once a mailbox is connected and in use).
DEFAULT_PREFERENCES = {"job_failed": True, "approval_needed": True}
EVENT_LABELS = {"job_failed": "Job failed", "approval_needed": "Approval needed"}
EVENT_DESCRIPTIONS = {
    "job_failed": "A run or automation of yours failed, after its retries.",
    "approval_needed": "A run is waiting for your answer.",
}
# Preferences v1 kinds, still accepted in a PUT body and mapped (`automation_failed` ->
# `job_failed`); `automation_result` and `job_finished` are explicit per-automation / per-run
# opt-ins now and are ignored as preferences.
LEGACY_EVENTS = ("automation_result", "automation_failed", "job_finished")
PREFERENCES_VERSION = 2
RETRY_BASE_S = 60.0
RETRY_MAX_S = 3600.0
MAX_TRANSIENT_ATTEMPTS = 12
SUBJECT_PREFIX = "[AbstractFramework]"

_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _plane_lock(plane: EmailPlane) -> threading.Lock:
    with _LOCKS_GUARD:
        key = "notify:" + str(plane.root)
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def idempotency_key(kind: str, subject_id: str, seq: Any = "") -> str:
    canonical = json.dumps([str(kind), str(subject_id), str(seq)], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------------------


def _prefs_path(plane: EmailPlane) -> Path:
    return plane.email_dir / "notifications.json"


def read_preferences(plane: EmailPlane) -> Dict[str, bool]:
    """The two preferences. A v1 file (five kinds) reads as `job_failed = job_failed OR
    automation_failed`, `approval_needed = approval_needed`; a never-saved one gets the defaults."""

    doc = _read_json(_prefs_path(plane))
    stored = doc.get("email") if isinstance(doc.get("email"), dict) else None
    if stored is None:
        return dict(DEFAULT_PREFERENCES)
    if int(doc.get("version") or 1) < PREFERENCES_VERSION:
        return {
            "job_failed": bool(stored.get("job_failed")) or bool(stored.get("automation_failed")),
            "approval_needed": bool(stored.get("approval_needed")),
        }
    return {k: bool(stored.get(k, DEFAULT_PREFERENCES[k])) for k in EVENTS}


def map_legacy_changes(changes: Dict[str, Any]) -> Dict[str, Any]:
    """A v1 body (`{kind: bool}` over five kinds) as v2 changes: `automation_failed` counts for
    `job_failed` (either true = on); `automation_result` / `job_finished` are dropped. Unknown
    kinds and non-booleans are a ValueError."""

    known = set(EVENTS) | set(LEGACY_EVENTS)
    unknown = sorted(k for k in changes if k not in known)
    if unknown:
        raise ValueError(f"unknown notification event(s): {', '.join(unknown)} (known: {', '.join(EVENTS)})")
    for k, v in changes.items():
        if not isinstance(v, bool):
            raise ValueError(f"{k} must be true or false")
    out: Dict[str, Any] = {}
    failed = [changes[k] for k in ("job_failed", "automation_failed") if k in changes]
    if failed:
        out["job_failed"] = any(failed)
    if "approval_needed" in changes:
        out["approval_needed"] = changes["approval_needed"]
    return out


def write_preferences(plane: EmailPlane, changes: Dict[str, Any]) -> Dict[str, bool]:
    mapped = map_legacy_changes(changes)
    with _plane_lock(plane):
        current = read_preferences(plane)
        current.update({k: bool(v) for k, v in mapped.items()})
        _write_private_json(_prefs_path(plane), {"version": PREFERENCES_VERSION, "email": current, "updated_at": _now_iso()})
    return current


def preferences_public(plane: EmailPlane) -> Dict[str, Any]:
    prefs = read_preferences(plane)
    usable = email_usable(plane)
    st = None
    try:
        st = account_store(plane).settings()
    except EmailError:
        st = None
    to = st.self_address if st is not None else ""
    return {
        "channels": {"email": {"available": usable, "to": to if usable else ""}},
        "events": [{"id": k, "label": EVENT_LABELS[k], "description": EVENT_DESCRIPTIONS[k], "email": prefs[k]} for k in EVENTS],
        "email": prefs,
        "unavailable_reason": "" if usable else (mailbox_unavailable_reason(plane) or "Connect a mailbox first."),
        "outbox": NotificationOutbox(plane).summary(),
    }


# ---------------------------------------------------------------------------------------
# Templates (fixed)
# ---------------------------------------------------------------------------------------


def _console_link() -> str:
    try:
        from ..first_run import read_serve_record
        from ..users import gateway_data_dir_from_env

        rec = read_serve_record(gateway_data_dir_from_env()) or {}
        return str(rec.get("console_url") or "")
    except Exception:  # noqa: BLE001
        return ""


def render_notice(kind: str, facts: Dict[str, Any]) -> Tuple[str, str, str]:
    """(subject, text, html) for one notice. `facts` holds typed fields; `model_title` /
    `model_body` are the automation's own notify text (model-authored, labelled)."""

    title = str(facts.get("title") or "").strip() or "Untitled"
    status = {
        "automation_result": "result",
        "automation_failed": "failed",
        "approval_needed": "needs your action",
        "job_finished": "finished",
        "job_failed": "failed",
        "test": "test notification",
    }.get(kind, kind)
    subject = f"{SUBJECT_PREFIX} {title}: {status}"
    lines: List[str] = []
    if kind == "automation_result":
        lines.append(f"The automation “{title}” finished an occurrence.")
    elif kind == "automation_failed":
        lines.append(f"The automation “{title}” failed after its retries. It is not paused: the next occurrence runs as scheduled.")
    elif kind == "approval_needed":
        what = str(facts.get("wait_kind") or "")
        if what == "tool_approval":
            tools = ", ".join(str(t) for t in (facts.get("tools") or []) if str(t)) or "a tool"
            lines.append(f"“{title}” waits for your approval before running {tools}.")
        elif what == "ask_user":
            lines.append(f"“{title}” asked you a question and waits for your answer.")
        else:
            lines.append(f"“{title}” waits for you.")
        lines.append("Open the console to approve, deny or answer. Replying to this email does nothing.")
    elif kind == "job_finished":
        lines.append(f"The run “{title}” finished.")
    elif kind == "job_failed":
        lines.append(f"The run “{title}” failed.")
    elif kind == "test":
        lines.append("This is a test notification from your AbstractFramework gateway. Email notifications work.")
    cause = str(facts.get("cause") or "").strip()
    fix = str(facts.get("fix") or "").strip()
    if cause:
        lines.append(f"Cause: {cause}")
    if fix:
        lines.append(f"Fix: {fix}")
    model_title = str(facts.get("model_title") or "").strip()
    model_body = str(facts.get("model_body") or "").strip()
    if model_title or model_body:
        lines.append("")
        lines.append("Written by the automation (model-authored text):")
        if model_title and model_title != title:
            lines.append(model_title)
        if model_body:
            lines.append(model_body)
    ref = str(facts.get("ref") or "").strip()
    if ref:
        lines.append("")
        lines.append(f"Reference: {ref}")
    link = _console_link()
    if link:
        lines.append(f"Console: {link}")
    lines.append("")
    lines.append(_why_line(kind))
    text = "\n".join(lines).strip() + "\n"
    paragraphs = "".join(
        f"<p>{html.escape(line)}</p>" if line else "" for line in lines
    )
    html_body = f"<!doctype html><html><body style=\"font-family:sans-serif\">{paragraphs}</body></html>"
    return subject, text, html_body


def _why_line(kind: str) -> str:
    if kind == "automation_result":
        return "You receive this because this automation is set to \u201cEmail result\u201d."
    if kind in ("job_finished", "job_failed"):
        return "You receive this because this run was started with \u201cemail me when done\u201d."
    if kind == "automation_failed":
        return f"You receive this because \u201cJob failed\u201d is on in {SETTINGS_LABEL} \u2192 Notifications."
    if kind == "approval_needed":
        return f"You receive this because \u201cApproval needed\u201d is on in {SETTINGS_LABEL} \u2192 Notifications."
    return f"You receive this because you asked for a test notification in {SETTINGS_LABEL}."


def render_digest(rows: List[Dict[str, Any]]) -> Tuple[str, str, str]:
    subject = f"{SUBJECT_PREFIX} {len(rows)} notifications"
    lines = [
        f"{len(rows)} notifications were held back by your send limits and are grouped here.",
        "",
    ]
    for row in rows:
        lines.append(f"— {row['subject'][len(SUBJECT_PREFIX):].strip() if row['subject'].startswith(SUBJECT_PREFIX) else row['subject']}")
        for line in str(row.get("text") or "").splitlines():
            if line.startswith("You receive this because"):
                continue
            lines.append(f"   {line}" if line else "")
        lines.append("")
    lines.append(f"You receive this because email notifications are on in {SETTINGS_LABEL} → Notifications.")
    text = "\n".join(lines).strip() + "\n"
    html_body = "<!doctype html><html><body style=\"font-family:sans-serif\">" + "".join(
        f"<p>{html.escape(line)}</p>" for line in lines if line
    ) + "</body></html>"
    return subject, text, html_body


# ---------------------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------------------


class NotificationOutbox:
    STATES = ("queued", "sending", "sent", "failed", "unknown")

    def __init__(self, plane: EmailPlane, *, clock: Callable[[], float] = time.time) -> None:
        self.plane = plane
        self.path = plane.email_dir / "outbox.sqlite3"
        self._clock = clock

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS notices ("
            " idempotency_key TEXT PRIMARY KEY,"
            " kind TEXT NOT NULL,"
            " subject TEXT NOT NULL,"
            " text TEXT NOT NULL,"
            " html TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL,"
            " state TEXT NOT NULL,"
            " attempts INTEGER NOT NULL DEFAULT 0,"
            " next_attempt_ts REAL NOT NULL DEFAULT 0,"
            " coalesce_flag INTEGER NOT NULL DEFAULT 0,"
            " sent_at TEXT NOT NULL DEFAULT '',"
            " message_id TEXT NOT NULL DEFAULT '',"
            " smtp_code INTEGER,"
            " error_code TEXT NOT NULL DEFAULT '',"
            " error_cause TEXT NOT NULL DEFAULT '',"
            " error_fix TEXT NOT NULL DEFAULT '',"
            " to_self INTEGER NOT NULL DEFAULT 1)"
        )
        columns = {r[1] for r in conn.execute("PRAGMA table_info(notices)")}
        if "recipients_json" not in columns:
            try:
                conn.execute("ALTER TABLE notices ADD COLUMN recipients_json TEXT NOT NULL DEFAULT '[\"self\"]'")
                conn.commit()
            except sqlite3.OperationalError:
                # Another connection may have completed the same migration.
                if "recipients_json" not in {r[1] for r in conn.execute("PRAGMA table_info(notices)")}:
                    raise
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sent_messages ("
            " message_id TEXT PRIMARY KEY,"
            " kind TEXT NOT NULL,"
            " marker TEXT NOT NULL DEFAULT '',"
            " sent_at TEXT NOT NULL)"
        )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return conn

    # -- writes ----------------------------------------------------------------------------

    def enqueue(self, key: str, kind: str, subject: str, text: str, html_body: str = "", *, recipients: Optional[List[str]] = None) -> bool:
        """Queue one notice. False when this idempotency key was queued before (any state)."""

        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO notices(idempotency_key, kind, subject, text, html, created_at, recipients_json, state)"
                    " VALUES (?,?,?,?,?,?,?, 'queued')",
                    (str(key), str(kind), str(subject), str(text), str(html_body or ""), _now_iso(), json.dumps(recipients or ["self"])),
                )
                return cur.rowcount == 1
        finally:
            conn.close()

    def record_sent(self, message_id: str, *, kind: str, marker: str = "") -> None:
        """Remember a Message-ID this account sent automatically (the watcher skips it).

        Never takes the plane lock: it runs from inside a send (`EmailContext.on_sent`),
        possibly while `deliver` holds that lock.
        """

        mid = str(message_id or "").strip()
        if not mid:
            return
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO sent_messages(message_id, kind, marker, sent_at) VALUES (?,?,?,?)",
                    (mid, str(kind), str(marker or ""), _now_iso()),
                )
        finally:
            conn.close()

    def was_sent(self, message_id: str) -> bool:
        """Did the framework send this Message-ID from this account automatically (recorded by
        the account context's `on_sent` for every marked send, notices included)? False when
        nothing was ever sent (no outbox file)."""

        mid = str(message_id or "").strip()
        if not mid or not self.path.exists():
            return False
        conn = self._connect()
        try:
            row = conn.execute("SELECT 1 FROM sent_messages WHERE message_id=? LIMIT 1", (mid,)).fetchone()
            return row is not None
        finally:
            conn.close()

    def recover_interrupted(self) -> int:
        """`sending` rows left by a crash become `unknown`: the SMTP exchange may have
        completed, so they are never resent automatically."""

        if not self.path.exists():
            return 0  # lazy: no outbox file until a notice exists
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE notices SET state='unknown', error_code='email_send_interrupted',"
                    " error_cause='The gateway stopped while this notice was being sent; it may or may not have arrived.',"
                    " error_fix='Nothing to do; it is not resent automatically so you never get it twice.'"
                    " WHERE state='sending'"
                )
                n = int(cur.rowcount or 0)
        finally:
            conn.close()
        return n

    def _set(self, conn: sqlite3.Connection, keys: Iterable[str], **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        for key in keys:
            conn.execute(f"UPDATE notices SET {cols} WHERE idempotency_key=?", (*fields.values(), key))

    # -- reads -----------------------------------------------------------------------------

    def rows(self, *, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        conn = self._connect()
        try:
            if state:
                cur = conn.execute("SELECT * FROM notices WHERE state=? ORDER BY created_at, rowid LIMIT ?", (state, int(limit)))
            else:
                cur = conn.execute("SELECT * FROM notices ORDER BY created_at DESC, rowid DESC LIMIT ?", (int(limit),))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def summary(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"queued": 0, "sent": 0, "failed": 0, "unknown": 0, "last_failure": None}
        conn = self._connect()
        try:
            counts = {r[0]: int(r[1]) for r in conn.execute("SELECT state, COUNT(*) FROM notices GROUP BY state")}
            last = conn.execute(
                "SELECT kind, error_code, error_cause, error_fix, created_at FROM notices WHERE state='failed'"
                " ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        waiting = self._waiting_on_limit()
        return {
            "rate_limited": waiting,
            "queued": counts.get("queued", 0) + counts.get("sending", 0),
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
            "unknown": counts.get("unknown", 0),
            "last_failure": (
                {"kind": last["kind"], "code": last["error_code"], "cause": last["error_cause"], "fix": last["error_fix"], "at": last["created_at"]}
                if last
                else None
            ),
        }

    def _waiting_on_limit(self) -> Optional[Dict[str, Any]]:
        """Queued notices held back by the send limit: {count, cause, resets_at} or None."""

        if not self.path.exists():
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n, MAX(next_attempt_ts) AS ts, MAX(error_cause) AS cause FROM notices"
                " WHERE state='queued' AND error_code='email_rate_limited'"
            ).fetchone()
        finally:
            conn.close()
        if not row or not int(row["n"] or 0):
            return None
        return {"count": int(row["n"]), "cause": str(row["cause"] or ""), "resets_at": _iso_from_ts(float(row["ts"] or 0.0))}

    def queued_before(self, key: str) -> int:
        """Queued notices other than `key` (the ones a new notice waits behind)."""

        if not self.path.exists():
            return 0
        conn = self._connect()
        try:
            row = conn.execute("SELECT COUNT(*) FROM notices WHERE state='queued' AND idempotency_key<>?", (key,)).fetchone()
        finally:
            conn.close()
        return int(row[0] or 0) if row else 0

    # -- delivery --------------------------------------------------------------------------

    def deliver(self, *, send: Optional[Callable[[Any, OutgoingMessage], Any]] = None) -> Dict[str, int]:
        """Send what is due. `send(ctx, message)` defaults to `guarded_send` (tests inject
        a failing one to prove crash semantics)."""

        with _plane_lock(self.plane):
            return self._deliver_locked(send or guarded_send)

    def _deliver_locked(self, send: Callable[[Any, OutgoingMessage], Any]) -> Dict[str, int]:
        out = {"sent": 0, "failed": 0, "deferred": 0}
        if not self.path.exists():
            return out
        now = self._clock()
        conn = self._connect()
        try:
            ready = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM notices WHERE state='queued' AND next_attempt_ts <= ? ORDER BY created_at, rowid", (now,)
                ).fetchall()
            ]
        finally:
            conn.close()
        if not ready:
            return out
        try:
            ctx = email_context(self.plane)
            to = str(ctx.registered_address or ctx.account.address or "").strip()
            if not to:
                raise EmailError(
                    "There is no address to send notifications to.",
                    f"Set your registered email (Users → your account) or connect a mailbox ({SETTINGS_LABEL}).",
                    code="email_no_recipient",
                )
        except EmailError as err:
            # No usable account (D5: no gateway-wide sender): these notices cannot go out.
            self._mark_failed([r["idempotency_key"] for r in ready], err)
            out["failed"] += len(ready)
            return out

        groups: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
        for row in ready:
            recipients = tuple(sorted(set(to if address == "self" else address for address in json.loads(row["recipients_json"]))))
            groups.setdefault(recipients, []).append(row)
        batches: List[List[Dict[str, Any]]] = []
        for rows in groups.values():
            coalesce = len(rows) > 1 and any(int(r.get("coalesce_flag") or 0) for r in rows)
            batches.extend([rows] if coalesce else [[r] for r in rows])
        for batch in batches:
            keys = [r["idempotency_key"] for r in batch]
            if len(batch) == 1:
                subject, text, html_body = batch[0]["subject"], batch[0]["text"], batch[0]["html"]
            else:
                subject, text, html_body = render_digest(batch)
            marker = f"notification:{keys[0]}" if len(keys) == 1 else f"notification-digest:{keys[0]}"
            message = OutgoingMessage(
                to=tuple(sorted(set(to if address == "self" else address for address in json.loads(batch[0]["recipients_json"])))), subject=subject, text=text, html=html_body,
                auto_submitted="auto-generated", automation_marker=marker[:200],
            )
            conn = self._connect()
            try:
                with conn:  # committed (fsynced) BEFORE the SMTP exchange
                    self._set(conn, keys, state="sending", attempts=int(batch[0].get("attempts") or 0) + 1)
            finally:
                conn.close()
            try:
                result = send(ctx, message)
            except EmailRateLimited as err:
                retry = float((err.details or {}).get("retry_after_s") or 0.0) or RETRY_BASE_S
                # The rows keep WHY they wait (code + cause + fix) and WHEN they go
                # (next_attempt_ts), so a later read can say "hourly limit reached - resets at 14:05".
                self._requeue(keys, delay=retry, coalesce=True, err=err)
                out["deferred"] += len(keys)
                d = err.details or {}
                out["rate_limited"] = {
                    "window": d.get("window"), "limit": d.get("limit"), "used": d.get("used"),
                    "resets_at_ts": self._clock() + max(1.0, retry),
                }
                # Everything else waits for the same window: stop here.
                remaining = [r["idempotency_key"] for b in batches[batches.index(batch) + 1 :] for r in b]
                if remaining:
                    self._requeue(remaining, delay=retry, coalesce=True, err=err)
                    out["deferred"] += len(remaining)
                break
            except EmailError as err:
                attempts = int(batch[0].get("attempts") or 0) + 1
                if err.retryable and attempts < MAX_TRANSIENT_ATTEMPTS:
                    self._requeue(keys, delay=min(RETRY_MAX_S, RETRY_BASE_S * (2 ** (attempts - 1))), coalesce=False, err=err)
                    out["deferred"] += len(keys)
                else:
                    self._mark_failed(keys, err)
                    out["failed"] += len(keys)
                continue
            except Exception as exc:  # noqa: BLE001 - unknown outcome: never resend
                self._mark_unknown(keys, exc)
                continue
            message_id = str(getattr(result, "message_id", "") or "")
            conn = self._connect()
            try:
                with conn:
                    self._set(conn, keys, state="sent", sent_at=_now_iso(), message_id=message_id, error_code="", error_cause="", error_fix="")
            finally:
                conn.close()
            out["sent"] += len(keys)
            for row in batch:
                audit_email_event(
                    "email.notification_sent",
                    tenant_id=self.plane.tenant_id,
                    user_id=self.plane.user_id,
                    kind=row["kind"],
                    idempotency_key=row["idempotency_key"],
                    message_id=message_id,
                    outcome="sent" if len(batch) == 1 else "sent_in_digest",
                )
        return out

    def _requeue(self, keys: List[str], *, delay: float, coalesce: bool, err: Optional[EmailError] = None) -> None:
        conn = self._connect()
        try:
            with conn:
                fields: Dict[str, Any] = {"state": "queued", "next_attempt_ts": self._clock() + max(1.0, float(delay))}
                if coalesce:
                    fields["coalesce_flag"] = 1
                if err is not None:
                    fields.update(error_code=err.code, error_cause=err.cause, error_fix=err.fix)
                self._set(conn, keys, **fields)
        finally:
            conn.close()

    def _mark_failed(self, keys: List[str], err: EmailError) -> None:
        conn = self._connect()
        try:
            with conn:
                smtp_code = (err.details or {}).get("smtp_code")
                self._set(
                    conn, keys, state="failed", error_code=err.code, error_cause=err.cause, error_fix=err.fix,
                    smtp_code=int(smtp_code) if isinstance(smtp_code, int) else None,
                )
        finally:
            conn.close()
        for key in keys:
            audit_email_event(
                "email.notification_failed",
                tenant_id=self.plane.tenant_id,
                user_id=self.plane.user_id,
                idempotency_key=key,
                code=err.code,
                cause=err.cause,
                fix=err.fix,
            )
        try:
            if err.code not in ("email_not_configured", "email_disabled"):
                account_store(self.plane).record_error(err)
        except Exception:  # noqa: BLE001
            pass

    def _mark_unknown(self, keys: List[str], exc: BaseException) -> None:
        conn = self._connect()
        try:
            with conn:
                self._set(
                    conn, keys, state="unknown", error_code="email_send_outcome_unknown",
                    error_cause=f"Sending failed in an unexpected way ({type(exc).__name__}); the notice may or may not have arrived.",
                    error_fix="Nothing to do; it is not resent automatically so you never get it twice.",
                )
        finally:
            conn.close()


# ---------------------------------------------------------------------------------------
# Collector: events -> queued notices
# ---------------------------------------------------------------------------------------


def _collector_path(plane: EmailPlane) -> Path:
    return plane.email_dir / "notify_cursor.json"


def queue_notice(plane: EmailPlane, kind: str, key: str, facts: Dict[str, Any]) -> bool:
    subject, text, html_body = render_notice(kind, facts)
    return NotificationOutbox(plane).enqueue(key, kind, subject, text, html_body, recipients=facts.get("recipients"))


def _iso_from_ts(ts: float) -> str:
    return datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc).replace(microsecond=0).isoformat()


def _local_hhmm(ts: float, *, now: Optional[float] = None) -> str:
    """The gateway's local time, "14:05" (today) / "tomorrow at 14:05" / "Oct 3 at 14:05"."""

    at = datetime.datetime.fromtimestamp(float(ts))
    today = datetime.datetime.fromtimestamp(float(now if now is not None else time.time())).date()
    if at.date() == today:
        return at.strftime("%H:%M")
    if at.date() == today + datetime.timedelta(days=1):
        return "tomorrow at " + at.strftime("%H:%M")
    return at.strftime("%b %-d at %H:%M")


_WINDOW_WORDS = {"hour": ("hourly", "this hour"), "day": ("daily", "today")}


def _limit_sentence(limit: Dict[str, Any], resets_ts: float) -> str:
    adjective, span = _WINDOW_WORDS.get(str(limit.get("window") or ""), ("send", "in this window"))
    return f"{adjective} limit reached ({limit.get('used')} of {limit.get('limit')} {span}) \u2014 resets at {_local_hhmm(resets_ts)}"


def _smtp_where(plane: EmailPlane) -> Tuple[str, str]:
    try:
        st = account_store(plane).settings()
    except EmailError:
        return "", ""
    smtp = st.account.smtp if st.account is not None else None
    if smtp is None:
        return "", ""
    host = str(getattr(smtp, "host", "") or "")
    port = getattr(smtp, "port", None)
    return host, (f"{host}:{port}" if port else host)


def _send_failed_sentence(plane: EmailPlane, code: str, cause: str) -> str:
    """"Not sent: <why>." for a failed send, from the error code (an explicit table)."""

    host, where = _smtp_where(plane)
    cause = str(cause or "").strip().rstrip(".")
    if code == "email_no_recipient":
        return "Not sent: there is no address to send it to \u2014 set your email address first."
    if code == "email_auth_failed" and host:
        return f"Not sent: {host} refused the sign-in \u2014 check the mailbox password ({cause})."
    if code in ("email_unreachable", "email_transient") and where:
        return f"Not sent: couldn't reach {where} ({cause})."
    if code == "email_tls_failed" and where:
        return f"Not sent: couldn't set up a secure connection to {where} ({cause})."
    if code == "email_policy_refused":
        return f"Not sent: your recipient rules refused it ({cause})."
    if host:
        return f"Not sent: {host} refused the message ({cause})." if cause else f"Not sent: {host} refused the message."
    return f"Not sent: {cause}." if cause else "Not sent."


def send_test_notification(plane: EmailPlane) -> Dict[str, Any]:
    """Queue and deliver one test notice now (the click is the request). The answer always
    carries a sentence (`message`) and a `reason_code` (null when sent): no_mailbox |
    mailbox_paused | rate_limited | queued_behind | send_failed; `limit` {window, limit, used,
    resets_at} when a send limit held it back. `state` / `error` / `delivery` stay as before."""

    base: Dict[str, Any] = {"sent": False, "reason_code": None, "message": "", "limit": None}
    if not email_usable(plane):
        try:
            email_context(plane)  # raises the precise typed reason (not connected / turned off)
        except EmailError as err:
            from .accounts import admin_email_enabled

            if err.code == "email_not_configured":
                reason, msg = "no_mailbox", "Not sent: no mailbox connected."
            elif err.code == "email_disabled" and not admin_email_enabled(plane):
                reason, msg = "send_failed", "Not sent: your admin turned mailboxes off for your account."
            elif err.code == "email_disabled":
                reason, msg = "mailbox_paused", "Not sent: your mailbox is paused."
            else:
                reason, msg = "send_failed", _send_failed_sentence(plane, err.code, err.cause)
            return {
                **base, "ok": False, "state": "not sent", "reason_code": reason, "message": msg,
                "error": {"code": err.code, "cause": err.cause, "fix": err.fix},
            }
    key = idempotency_key("test", plane.key, time.time_ns())
    queue_notice(plane, "test", key, {"title": "Test"})
    outbox = NotificationOutbox(plane)
    result = outbox.deliver()
    row = next((r for r in outbox.rows(limit=50) if r["idempotency_key"] == key), None)
    state = row["state"] if row else "unknown"
    out: Dict[str, Any] = {**base, "ok": state == "sent", "state": state, "delivery": result}
    if row and state != "sent":
        out["error"] = {"code": row["error_code"], "cause": row["error_cause"], "fix": row["error_fix"]}
    if state == "sent":
        try:
            ctx = email_context(plane)
            to = str(ctx.registered_address or ctx.account.address or "").strip()
        except EmailError:
            to = ""
        out.update(sent=True, message=f"Sent to {to}." if to else "Sent.")
        return out
    code = str((row or {}).get("error_code") or "")
    if state == "queued" and code == "email_rate_limited":
        rl = result.get("rate_limited") or {}
        resets_ts = float(rl.get("resets_at_ts") or (row or {}).get("next_attempt_ts") or time.time())
        limit = {"window": rl.get("window"), "limit": rl.get("limit"), "used": rl.get("used"), "resets_at": _iso_from_ts(resets_ts)}
        out["limit"] = limit
        behind = outbox.queued_before(key)
        if behind:
            noun = "notification" if behind == 1 else "notifications"
            out.update(
                reason_code="queued_behind", queued_behind=behind,
                message=f"Queued behind {behind} earlier {noun}; they go out when the limit resets at {_local_hhmm(resets_ts)}.",
            )
        else:
            out.update(reason_code="rate_limited", message=f"Not sent: {_limit_sentence(limit, resets_ts)}.")
        return out
    if state == "queued":
        # A transient failure: retried automatically.
        sentence = _send_failed_sentence(plane, code, str((row or {}).get("error_cause") or ""))
        out.update(reason_code="send_failed", message=sentence[:-1] + "; it is retried automatically.")
        return out
    out.update(reason_code="send_failed", message=_send_failed_sentence(plane, code, str((row or {}).get("error_cause") or "")))
    return out


def _run_title(run: Any) -> str:
    vars0 = getattr(run, "vars", None) or {}
    meta = vars0.get("_meta") if isinstance(vars0, dict) else None
    for candidate in (
        (meta or {}).get("title") if isinstance(meta, dict) else None,
        vars0.get("title") if isinstance(vars0, dict) else None,
        getattr(run, "workflow_id", None),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return str(getattr(run, "run_id", "") or "run")


def _run_notify(run: Any) -> Dict[str, Any]:
    vars0 = getattr(run, "vars", None) or {}
    rt = vars0.get("_runtime") if isinstance(vars0, dict) else None
    notify = rt.get("notify") if isinstance(rt, dict) else None
    return notify if isinstance(notify, dict) else {}


def _run_error(run: Any) -> Tuple[str, str]:
    err = getattr(run, "error", None)
    if isinstance(err, dict):
        return str(err.get("cause") or err.get("message") or ""), str(err.get("fix") or "")
    if isinstance(err, str):
        return err.strip(), ""
    return "", ""


class NotificationCollector:
    """Turns the plane's ledger / run facts into queued notices (never sends)."""

    def __init__(self, plane: EmailPlane, svc: Any) -> None:
        self.plane = plane
        self.svc = svc

    def collect(self) -> Dict[str, int]:
        with _plane_lock(self.plane):
            return self._collect_locked()

    def _collect_locked(self) -> Dict[str, int]:
        out = {"queued": 0}
        state = _read_json(_collector_path(self.plane))
        first = not state.get("baseline_at")
        prefs = read_preferences(self.plane)
        usable = email_usable(self.plane)
        att: Dict[str, int] = {str(k): int(v) for k, v in (state.get("attention") or {}).items()}
        host = getattr(self.svc, "host", None)
        run_store = getattr(host, "run_store", None)
        ledger_store = getattr(host, "ledger_store", None)
        if run_store is None:
            return out

        # Automations: notify / failure attention items (ledger), human waits (run state).
        try:
            from abstractruntime.automation_queries import list_automations
            from abstractruntime.automations.attention import list_attention, pending_waits

            cursor = None
            while True:
                page = list_automations(run_store, cursor=cursor, limit=200)
                for item in page.items:
                    aid = str(item.get("automation_id") or "")
                    if not aid:
                        continue
                    title = str(item.get("title") or "Automation")
                    floor = att.get(aid)
                    items = list_attention(ledger_store, aid, after_seq=floor or 0, limit=500)["items"] if ledger_store is not None else []
                    if floor is None and first:
                        # Baseline: never mail the history that existed before notifications were on.
                        att[aid] = max([int(i["seq"]) for i in items] or [0])
                        items = [it for it in items if "email_result" in it]
                    for it in items:
                        seq = int(it["seq"])
                        att[aid] = max(att.get(aid, 0), seq)
                        kind = "automation_failed" if str(it.get("kind")) == "failure" else "automation_result"
                        if kind == "automation_result":
                            # "Email result": the automation asks for email delivery
                            # (`notify.channels`, schema v2; the runtime stamps the channels on the
                            # attention item; v1 = console only). No global preference involved.
                            wanted = "email" in (it.get("channels") or [])
                        else:
                            # A failure after the retries: the user's "Job failed" switch.
                            wanted = bool(prefs.get("job_failed"))
                        if usable and wanted:
                            facts = {"title": title, "model_title": it.get("title"), "ref": f"automation {aid}, occurrence {it.get('index')}"}
                            if kind == "automation_result":
                                facts["model_body"] = it.get("email_result", it.get("body"))
                                facts["recipients"] = it.get("recipients", ["self"])
                            else:
                                facts["cause"] = it.get("body")
                                facts["fix"] = "Open the automation's last occurrence in the console to see what failed."
                            if queue_notice(self.plane, kind, idempotency_key(kind, aid, seq), facts):
                                out["queued"] += 1
                    if str(item.get("status") or "") == "active" and usable and prefs.get("approval_needed") and not first:
                        for w in pending_waits(run_store, aid, limit=20):
                            if w.get("kind") not in ("tool_approval", "ask_user", "event"):
                                continue
                            tools = [str(c.get("name") or "") for c in (w.get("details") or []) if isinstance(c, dict)] if w.get("kind") == "tool_approval" else []
                            key = idempotency_key("approval_needed", w.get("run_id"), w.get("wait_key"))
                            if queue_notice(self.plane, "approval_needed", key, {"title": title, "wait_kind": w.get("kind"), "tools": tools, "ref": f"automation {aid}"}):
                                out["queued"] += 1
                if not page.next_cursor:
                    break
                cursor = page.next_cursor
        except Exception:  # noqa: BLE001 - one broken source never blocks the others
            pass

        # Runs of this user waiting on a person (outside automations) and jobs that asked to be notified.
        baseline_at = str(state.get("baseline_at") or _now_iso())
        try:
            from abstractruntime.automations.attention import typed_wait
            from abstractruntime.core.models import RunStatus, WaitReason

            if usable and prefs.get("approval_needed") and not first:
                for run in run_store.list_runs(status=RunStatus.WAITING, wait_reason=WaitReason.USER, limit=200):
                    meta = (run.vars or {}).get("_meta") if isinstance(run.vars, dict) else None
                    if isinstance(meta, dict) and (meta.get("occurrence") or meta.get("automation")):
                        continue  # automation trees are handled above, with the automation's title
                    w = typed_wait(run)
                    if w is None:
                        continue
                    tools = [str(c.get("name") or "") for c in (w.get("details") or []) if isinstance(c, dict)] if w.get("kind") == "tool_approval" else []
                    key = idempotency_key("approval_needed", run.run_id, w.get("wait_key"))
                    if queue_notice(self.plane, "approval_needed", key, {"title": _run_title(run), "wait_kind": w.get("kind"), "tools": tools, "ref": f"run {run.run_id}"}):
                        out["queued"] += 1
            if not first:
                for status, kind in ((RunStatus.COMPLETED, "job_finished"), (RunStatus.FAILED, "job_failed")):
                    for run in run_store.list_runs(status=status, limit=200):
                        if str(getattr(run, "updated_at", "") or "") < baseline_at:
                            continue
                        notify = _run_notify(run)
                        want_on = {str(x) for x in (notify.get("on") or [])}
                        channels = {str(x) for x in (notify.get("channels") or [])}
                        # "Email me when done": the run's own opt-in suffices (no global preference).
                        if "email" not in channels or ("finished" if kind == "job_finished" else "failed") not in want_on:
                            continue
                        if not usable:
                            continue
                        facts: Dict[str, Any] = {"title": _run_title(run), "ref": f"run {run.run_id}"}
                        if kind == "job_failed":
                            cause, fix = _run_error(run)
                            facts["cause"] = cause
                            facts["fix"] = fix or "Open the run in the console to see what failed."
                        if queue_notice(self.plane, kind, idempotency_key(kind, run.run_id), facts):
                            out["queued"] += 1
        except Exception:  # noqa: BLE001
            pass

        _write_private_json(
            _collector_path(self.plane),
            {"version": 1, "baseline_at": baseline_at, "attention": att, "updated_at": _now_iso()},
        )
        return out
