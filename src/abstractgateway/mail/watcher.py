"""The per-user mail watcher: when to poll, whom it serves, what the user sees.

Replaces the retired `integrations/email_bridge.py` (one process-wide account from
`ABSTRACT_EMAIL_*`, never started under user accounts, no UIDVALIDITY check, events emitted
non-durably). One watcher per user plane, run by that plane's email worker (worker.py).

The mailbox -> inbox step itself is AbstractRuntime's `EmailInboxFeeder` (framework backlog
0992 B4), the one implementation the `email.received@1` trigger is built on:

- read-only through AbstractCore (`EXAMINE`, `BODY.PEEK[]`): never `\\Seen`, never a move or a
  delete (D12);
- the first poll is a baseline; each new message is fetched whole and appended to the runtime's
  durable event inbox under `sha256(account_ref, folder, uidvalidity, uid)`, and ONLY THEN does
  the cursor move past it; a crash in between re-reads it and the unique event id admits it once;
- UIDVALIDITY reset: resync by date, messages already in the inbox (same Message-ID) passed;
- a message that cannot be fetched on 3 polls in a row is recorded and passed; connection
  failures back off 60 s -> 15 min with a typed cause and fix; nothing is paused.

This module adds the gateway's part:

- gating: nothing is read while the admin or the user turned email off, while no account is
  connected, or while the plane has no active `email.received` automation (the consumer probe);
- cadence: at most one poll per 60 s (minimum 30 s);
- after new mail, the email-triggered automations are woken (`wake_email_automations`);
- typed audit events (`email.cursor_reset`, `email.message_unprocessable`) and the account's last
  error for the status views; `watcher_public_status` for `GET /me/email` and the admin view.

How often an automation RUNS on new mail is the automation's own trigger setting (hourly by
default when it runs a model, every 60 s when it does not), enforced by the runtime.
"""

from __future__ import annotations

import datetime
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .core_mail import EmailError

from .accounts import EmailPlane, account_store, admin_email_enabled, email_context, _read_json, _write_private_json
from .audit import audit_email_event

DEFAULT_INTERVAL_S = 60.0
MIN_INTERVAL_S = 30.0

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


def event_inbox_dir(plane: EmailPlane) -> Path:
    """Where the plane's runtime keeps its durable event inbox (runtime_wiring.py)."""

    return plane.runtime_data_dir / "event_inbox"


def open_event_inbox(plane: EmailPlane) -> Any:
    from abstractruntime.email import JsonFileEventInbox

    return JsonFileEventInbox(event_inbox_dir(plane))


def make_feeder(plane: EmailPlane, inbox: Any) -> Any:
    from abstractruntime.email import EmailInboxFeeder

    from .notifications import NotificationOutbox

    # Second loop guard behind the marker header: a Message-ID this account sent automatically
    # (recorded in the plane's outbox) is never admitted, even if a server dropped the header.
    return EmailInboxFeeder(inbox, account_ref=plane.account_ref, is_own_sent=NotificationOutbox(plane).was_sent)


# ---------------------------------------------------------------------------------------
# Gateway-side watcher state (gating + cadence); the cursor lives in the runtime inbox
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
        doc.pop("last_poll_ts", None)
        doc["state"] = "idle"
        _write_state(plane, doc)
        inbox_dir = event_inbox_dir(plane)
        if inbox_dir.exists():
            from abstractruntime.email import email_stream

            inbox = open_event_inbox(plane)
            try:
                folder = ""
                st = account_store(plane).settings()
                if st.account is not None and st.account.imap is not None:
                    folder = st.account.imap.folder
            except EmailError:
                folder = ""
            for f in {folder or "INBOX", "INBOX"}:
                inbox.set_stream_state(email_stream(plane.account_ref, f), {})


def watcher_public_status(plane: EmailPlane) -> Dict[str, Any]:
    doc = read_watcher_state(plane)
    out: Dict[str, Any] = {
        "state": str(doc.get("state") or "idle"),
        "interval_s": float(doc.get("interval_s") or DEFAULT_INTERVAL_S),
        "last_poll": doc.get("last_poll") or "",
        "last_ok": "",
        "last_error": None,
        "next_poll_after": "",
        "cursor": None,
        "unprocessable": [],
        "received": 0,
    }
    if event_inbox_dir(plane).exists():
        try:
            inbox = open_event_inbox(plane)
            feeder = make_feeder(plane, inbox)
            folder = None
            try:
                st = account_store(plane).settings()
                if st.account is not None and st.account.imap is not None:
                    folder = st.account.imap.folder
            except EmailError:
                folder = None
            fs = feeder.status(folder)
            cursor = fs.get("cursor") if isinstance(fs.get("cursor"), dict) else None
            out.update(
                {
                    "last_ok": fs.get("last_ok") or "",
                    "last_error": fs.get("last_error"),
                    "next_poll_after": fs.get("next_poll_at") or "",
                    "cursor": {"uidvalidity": cursor.get("uidvalidity"), "last_uid": cursor.get("last_uid"), "folder": cursor.get("folder")} if cursor else None,
                    "unprocessable": list(fs.get("unprocessable") or []),
                    "received": len(inbox.read(stream=feeder.stream(folder))),
                }
            )
            if fs.get("last_poll"):
                out["last_poll"] = fs.get("last_poll")
        except Exception:  # noqa: BLE001 - the status view never fails on the inbox
            pass
    return out


# ---------------------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------------------


class MailWatcher:
    """Gates and schedules one plane's feeder. `poll_once()` is deterministic (tests drive it)."""

    def __init__(
        self,
        plane: EmailPlane,
        *,
        inbox: Any = None,
        interval_s: float = DEFAULT_INTERVAL_S,
        has_consumers: Optional[Callable[[], bool]] = None,
        on_appended: Optional[Callable[[list], None]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.plane = plane
        self.interval_s = max(MIN_INTERVAL_S, float(interval_s or DEFAULT_INTERVAL_S))
        self._has_consumers = has_consumers
        self._on_appended = on_appended
        self._clock = clock
        self._inbox = inbox

    @property
    def inbox(self) -> Any:
        if self._inbox is None:
            self._inbox = open_event_inbox(self.plane)
        return self._inbox

    @property
    def feeder(self) -> Any:
        return make_feeder(self.plane, self.inbox)

    def due(self) -> bool:
        doc = read_watcher_state(self.plane)
        try:
            last = float(doc.get("last_poll_ts") or 0.0)
        except (TypeError, ValueError):
            last = 0.0
        return self._clock() - last >= self.interval_s

    def _gate(self, doc: Dict[str, Any], state: str, out: Dict[str, Any]) -> Dict[str, Any]:
        doc["state"] = state
        doc["interval_s"] = self.interval_s
        _write_state(self.plane, doc)
        out["state"] = state
        return out

    def poll_once(self, *, force: bool = False) -> Dict[str, Any]:
        """One poll. Returns `{state, new, skipped, reset, unprocessable, error?}`."""

        with _plane_lock(self.plane):
            return self._poll_locked(force=force)

    def _poll_locked(self, *, force: bool) -> Dict[str, Any]:
        plane = self.plane
        doc = read_watcher_state(plane)
        doc["last_poll"] = _now_iso()
        doc["last_poll_ts"] = self._clock()
        out: Dict[str, Any] = {"state": "", "new": 0, "skipped": 0, "reset": False, "unprocessable": 0}

        if not admin_email_enabled(plane):
            return self._gate(doc, "off (turned off by an administrator)", out)
        try:
            settings = account_store(plane).settings()
        except EmailError as err:
            out["error"] = err.to_dict(include_details=False)
            return self._gate(doc, "needs action", out)
        if settings.account is None:
            return self._gate(doc, "not connected", out)
        if not settings.enabled:
            return self._gate(doc, "off (turned off by the user)", out)
        if not settings.account.can_read:
            return self._gate(doc, "not watching (the account has no IMAP settings)", out)
        if self._has_consumers is not None:
            try:
                wanted = bool(self._has_consumers())
            except Exception:  # noqa: BLE001 - a probe failure never stops the others
                wanted = False
            if not wanted:
                return self._gate(doc, "idle (no email-triggered automation)", out)

        try:
            ctx = email_context(plane)
        except EmailError as err:
            account_store(plane).record_error(err)
            out["error"] = err.to_dict(include_details=False)
            return self._gate(doc, "needs action", out)

        report = self.feeder.poll(ctx, force=force)
        out["new"] = len(report.appended)
        out["skipped"] = int(report.duplicates)
        # This account's own automatic mail (notices, automation sends): never an event.
        out["own_automatic"] = int(report.own_automatic)
        out["reset"] = bool(report.reset)
        out["unprocessable"] = len(report.unprocessable)
        if report.reset:
            cur = self.feeder.status(settings.account.imap.folder).get("cursor") or {}
            audit_email_event(
                "email.cursor_reset",
                tenant_id=plane.tenant_id,
                user_id=plane.user_id,
                folder=cur.get("folder"),
                uidvalidity=cur.get("uidvalidity"),
            )
        for item in report.unprocessable:
            audit_email_event(
                "email.message_unprocessable",
                tenant_id=plane.tenant_id,
                user_id=plane.user_id,
                uid=item.get("uid"),
                uidvalidity=item.get("uidvalidity"),
                code=item.get("code"),
                cause=item.get("cause"),
                fix=item.get("fix"),
            )
        if report.skipped:
            return self._gate(doc, str(doc.get("state") or "retrying"), out)
        if report.error:
            err = report.error
            out["error"] = {k: err.get(k) for k in ("code", "cause", "fix", "retryable")}
            try:
                account_store(plane).record_error(
                    EmailError(str(err.get("cause") or ""), str(err.get("fix") or ""), code=str(err.get("code") or "email_error"), retryable=bool(err.get("retryable")))
                )
            except Exception:  # noqa: BLE001
                pass
            return self._gate(doc, "retrying" if err.get("retryable") else "needs action", out)
        if report.appended and self._on_appended is not None:
            try:
                self._on_appended(list(report.appended))
            except Exception:  # noqa: BLE001 - the events are durable; the wake is a courtesy
                pass
        return self._gate(doc, "watching", out)


def plane_has_email_automations(svc: Any) -> bool:
    """True when the plane's runtime has an active automation on `email.received@1`."""

    from abstractruntime.email import email_trigger_consumers

    return bool(email_trigger_consumers(svc.host.runtime))
