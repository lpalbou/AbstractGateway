"""Email notifications: per-user preferences, a durable outbox, fixed templates, a collector.

Who gets what (D4/D5): a notice goes to the user's registered address (else their mailbox
address), sent THROUGH THEIR OWN ACCOUNT with `guarded_send` — so the user's recipient
policy and send limits apply exactly as for an agent's send. No account, email turned off
by the user or by an administrator: no email notifications (no gateway-wide sender).

Events (each one switchable in the preferences):

    automation_result   an occurrence asked to notify (`notify` in its output) and the automation
                        delivers to email (`notify.channels` holds "email", schema v2)
    automation_failed   an occurrence failed after its retries (same channel rule)
    approval_needed     a run of this user waits on a person (tool approval, question)
    job_finished        a run started with `_runtime.notify = {on: ["finished"], channels: ["email"]}`
    job_failed          same, `on: ["failed"]`

The outbox (`<plane>/email/outbox.sqlite3`) makes delivery exactly-once-or-visible:

- `idempotency_key = sha256(kind, subject id, sequence)` is the primary key: a notice is
  queued once however many times the collector sees its event;
- `queued -> sending -> sent | failed`; `sending` is committed BEFORE the SMTP exchange, so a
  crash mid-send leaves `sending`, which the next start turns into `unknown` — never resent
  automatically (the user may have received it);
- SMTP 4xx / network problems are retried with backoff; 5xx, sign-in and policy refusals are
  `failed` with the typed cause and fix (and shown in the status);
- over the send limits (20/hour, 100/day by default, the user's own), notices wait for the
  window and then go out as ONE digest message.

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
    _read_json,
    _write_private_json,
)
from .audit import audit_email_event

EVENTS = ("automation_result", "automation_failed", "approval_needed", "job_finished", "job_failed")
# Email notifications are OPT-IN: every event is off until the user turns it on (the
# console stays the default channel).
DEFAULT_PREFERENCES = {
    "automation_result": False,
    "automation_failed": False,
    "approval_needed": False,
    "job_finished": False,
    "job_failed": False,
}
EVENT_LABELS = {
    "automation_result": "Automation results (automations set to “email me the result”)",
    "automation_failed": "Automation failures after the retries (automations set to “email me the result”)",
    "approval_needed": "Approval or answer needed",
    "job_finished": "Job finished (runs started with “email me when done”)",
    "job_failed": "Job failed (runs started with “email me when done”)",
}
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
    doc = _read_json(_prefs_path(plane))
    stored = doc.get("email") if isinstance(doc.get("email"), dict) else {}
    return {k: bool(stored.get(k, DEFAULT_PREFERENCES[k])) for k in EVENTS}


def write_preferences(plane: EmailPlane, changes: Dict[str, Any]) -> Dict[str, bool]:
    unknown = sorted(k for k in changes if k not in EVENTS)
    if unknown:
        raise ValueError(f"unknown notification event(s): {', '.join(unknown)} (known: {', '.join(EVENTS)})")
    for k, v in changes.items():
        if not isinstance(v, bool):
            raise ValueError(f"{k} must be true or false")
    with _plane_lock(plane):
        current = read_preferences(plane)
        current.update({k: bool(v) for k, v in changes.items()})
        _write_private_json(_prefs_path(plane), {"version": 1, "email": current, "updated_at": _now_iso()})
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
        "events": [{"id": k, "label": EVENT_LABELS[k], "email": prefs[k]} for k in EVENTS],
        "email": prefs,
        "unavailable_reason": "" if usable else f"Email notifications need a connected, turned-on email account ({SETTINGS_LABEL}).",
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
        lines.append(f"The automation “{title}” finished an occurrence and asked to notify you.")
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
    lines.append(f"You receive this because email notifications are on in {SETTINGS_LABEL} → Notifications.")
    text = "\n".join(lines).strip() + "\n"
    paragraphs = "".join(
        f"<p>{html.escape(line)}</p>" if line else "" for line in lines
    )
    html_body = f"<!doctype html><html><body style=\"font-family:sans-serif\">{paragraphs}</body></html>"
    return subject, text, html_body


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
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return conn

    # -- writes ----------------------------------------------------------------------------

    def enqueue(self, key: str, kind: str, subject: str, text: str, html_body: str = "") -> bool:
        """Queue one notice. False when this idempotency key was queued before (any state)."""

        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO notices(idempotency_key, kind, subject, text, html, created_at, state)"
                    " VALUES (?,?,?,?,?,?, 'queued')",
                    (str(key), str(kind), str(subject), str(text), str(html_body or ""), _now_iso()),
                )
                return cur.rowcount == 1
        finally:
            conn.close()

    def recover_interrupted(self) -> int:
        """`sending` rows left by a crash become `unknown`: the SMTP exchange may have
        completed, so they are never resent automatically."""

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
        return {
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

        coalesce = len(ready) > 1 and any(int(r.get("coalesce_flag") or 0) for r in ready)
        batches: List[List[Dict[str, Any]]] = [ready] if coalesce else [[r] for r in ready]
        for batch in batches:
            keys = [r["idempotency_key"] for r in batch]
            if len(batch) == 1:
                subject, text, html_body = batch[0]["subject"], batch[0]["text"], batch[0]["html"]
            else:
                subject, text, html_body = render_digest(batch)
            message = OutgoingMessage(to=(to,), subject=subject, text=text, html=html_body)
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
                self._requeue(keys, delay=retry, coalesce=True)
                out["deferred"] += len(keys)
                # Everything else waits for the same window: stop here.
                remaining = [r["idempotency_key"] for b in batches[batches.index(batch) + 1 :] for r in b]
                if remaining:
                    self._requeue(remaining, delay=retry, coalesce=True)
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
    return NotificationOutbox(plane).enqueue(key, kind, subject, text, html_body)


def send_test_notification(plane: EmailPlane) -> Dict[str, Any]:
    """Queue and deliver one test notice now (the click is the request)."""

    key = idempotency_key("test", plane.key, time.time_ns())
    queue_notice(plane, "test", key, {"title": "Test"})
    result = NotificationOutbox(plane).deliver()
    row = next((r for r in NotificationOutbox(plane).rows(limit=50) if r["idempotency_key"] == key), None)
    state = row["state"] if row else "unknown"
    out: Dict[str, Any] = {"ok": state == "sent", "state": state, "delivery": result}
    if row and state != "sent":
        out["error"] = {"code": row["error_code"], "cause": row["error_cause"], "fix": row["error_fix"]}
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


def _attention_items(ledger_store: Any, automation_id: str, *, after_seq: int) -> List[Dict[str, Any]]:
    """Attention items (`automation.completed` records carrying `attention`) after `after_seq`,
    oldest first, with the delivery channels the runtime stamped on each."""

    from abstractruntime.automations.ledger import automation_records

    out: List[Dict[str, Any]] = []
    for rec in automation_records(ledger_store, automation_id, "automation.completed"):
        p = rec["payload"]
        att = p.get("attention")
        if not isinstance(att, dict) or int(att.get("seq") or 0) <= int(after_seq):
            continue
        channels = att.get("channels")
        out.append(
            {
                "kind": att.get("kind"),
                "seq": int(att["seq"]),
                "title": att.get("title"),
                "body": att.get("body"),
                "index": p.get("index"),
                "run_id": p.get("run_id"),
                "channels": [str(c) for c in channels] if isinstance(channels, list) else ["console"],
            }
        )
    out.sort(key=lambda it: it["seq"])
    return out


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
            from abstractruntime.automations.attention import pending_waits

            cursor = None
            while True:
                page = list_automations(run_store, cursor=cursor, limit=200)
                for item in page.items:
                    aid = str(item.get("automation_id") or "")
                    if not aid:
                        continue
                    title = str(item.get("title") or "Automation")
                    floor = att.get(aid)
                    items = _attention_items(ledger_store, aid, after_seq=floor or 0) if ledger_store is not None else []
                    if floor is None and first:
                        # Baseline: never mail the history that existed before notifications were on.
                        att[aid] = max([int(i["seq"]) for i in items] or [0])
                        continue
                    for it in items:
                        seq = int(it["seq"])
                        att[aid] = max(att.get(aid, 0), seq)
                        kind = "automation_failed" if str(it.get("kind")) == "failure" else "automation_result"
                        # The automation asks for email delivery (`notify.channels`, schema v2;
                        # the runtime stamps the channels on the attention item; v1 = console only).
                        if "email" not in it["channels"]:
                            continue
                        if usable and prefs.get(kind):
                            facts = {"title": title, "model_title": it.get("title"), "ref": f"automation {aid}, occurrence {it.get('index')}"}
                            if kind == "automation_result":
                                facts["model_body"] = it.get("body")
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
                        if "email" not in channels or ("finished" if kind == "job_finished" else "failed") not in want_on:
                            continue
                        if not (usable and prefs.get(kind)):
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
