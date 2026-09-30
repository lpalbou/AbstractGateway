from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

# The same maintenance notice (subject + body) is sent at most once per UTC day: a retry or a
# second scan the same day never sends it twice, and a condition still true tomorrow is
# reported again (the key used to be "once ever").
NOTICE_DEDUPE_WINDOW_S = 86400


def _notice_bucket() -> int:
    return int(time.time() // NOTICE_DEDUPE_WINDOW_S)


def _env(name: str, fallback: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is not None and str(v).strip():
        return str(v).strip()
    if fallback:
        v2 = os.getenv(fallback)
        if v2 is not None and str(v2).strip():
            return str(v2).strip()
    return None


def _telegram_chat_id() -> Optional[int]:
    raw = (
        _env("ABSTRACT_BACKLOG_TELEGRAM_CHAT_ID", "ABSTRACTGATEWAY_BACKLOG_TELEGRAM_CHAT_ID")
        or _env("ABSTRACT_TRIAGE_TELEGRAM_CHAT_ID", "ABSTRACTGATEWAY_TRIAGE_TELEGRAM_CHAT_ID")
    )
    if not raw:
        return None
    try:
        return int(str(raw).strip())
    except Exception:
        return None

def send_telegram_notification(*, text: str) -> Tuple[bool, Optional[str]]:
    chat_id = _telegram_chat_id()
    if chat_id is None:
        return False, "Missing/invalid TELEGRAM_CHAT_ID"

    try:
        from abstractruntime.integrations.abstractcore import send_telegram_message
    except Exception as e:
        return False, f"Telegram helpers unavailable: {e}"

    try:
        out: Dict[str, Any] = send_telegram_message(chat_id=chat_id, text=str(text or ""))
    except Exception as e:
        return False, str(e)

    if isinstance(out, dict) and out.get("success") is True:
        return True, None
    err = out.get("error") if isinstance(out, dict) else None
    return False, str(err or "Telegram send failed")


def send_email_notification(*, subject: str, body_text: str) -> Tuple[bool, Optional[str]]:
    """Email the gateway operator through the notification dispatcher (framework backlog 0992).

    The notice goes to the admin's registered address, sent by the admin's own email account
    (Settings -> My email) through the durable outbox: the recipient policy and send limits
    apply, a retry never sends it twice, and the same notice is sent at most once per UTC day
    (`NOTICE_DEDUPE_WINDOW_S`). The retired recipient/account variables
    (ABSTRACT_BACKLOG_EMAIL_TO, ABSTRACT_TRIAGE_EMAIL_TO, *_EMAIL_ACCOUNT, ABSTRACT_EMAIL_*)
    are ignored. Returns (sent, error).
    """

    import hashlib

    try:
        from ..mail.accounts import admin_plane, email_usable
        from ..mail.notifications import SUBJECT_PREFIX, NotificationOutbox, idempotency_key
    except Exception as e:  # noqa: BLE001
        return False, f"Email notifications unavailable: {e}"
    try:
        plane = admin_plane()
        if not email_usable(plane):
            return False, "The admin has no connected, turned-on email account (Settings -> My email)."
        body = str(body_text or "").rstrip() + "\n\nSent by your AbstractFramework gateway (maintenance notice).\n"
        subj0 = str(subject or "").strip() or "Maintenance notice"
        subj = subj0 if subj0.startswith(SUBJECT_PREFIX) else f"{SUBJECT_PREFIX} {subj0}"
        key = idempotency_key("maintenance", subj, f"{hashlib.sha256(body.encode('utf-8')).hexdigest()}:{_notice_bucket()}")
        outbox = NotificationOutbox(plane)
        outbox.enqueue(key, "maintenance", subj, body)
        outbox.deliver()
        row = next((r for r in outbox.rows(limit=200) if r["idempotency_key"] == key), None)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if row is None:
        return False, "The notice was not recorded."
    if row["state"] == "sent":
        return True, None
    if row["state"] == "queued":
        return False, f"Queued: {row.get('error_cause') or 'waiting for the send window'}"
    return False, str(row.get("error_cause") or row["state"])
