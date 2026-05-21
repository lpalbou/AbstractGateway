from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple


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


def _email_recipients() -> List[str]:
    raw = (
        _env("ABSTRACT_BACKLOG_EMAIL_TO", "ABSTRACTGATEWAY_BACKLOG_EMAIL_TO")
        or _env("ABSTRACT_TRIAGE_EMAIL_TO", "ABSTRACTGATEWAY_TRIAGE_EMAIL_TO")
        or ""
    )
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    return parts


def _email_account() -> Optional[str]:
    return (
        _env("ABSTRACT_BACKLOG_EMAIL_ACCOUNT", "ABSTRACTGATEWAY_BACKLOG_EMAIL_ACCOUNT")
        or _env("ABSTRACT_TRIAGE_EMAIL_ACCOUNT", "ABSTRACTGATEWAY_TRIAGE_EMAIL_ACCOUNT")
        or _env("ABSTRACT_EMAIL_DEFAULT_ACCOUNT")
        or _env("ABSTRACT_EMAIL_ACCOUNT_NAME")
    )


def send_email_notification(*, subject: str, body_text: str) -> Tuple[bool, Optional[str]]:
    to = _email_recipients()
    if not to:
        return False, "Missing EMAIL_TO recipients"

    try:
        from abstractruntime.integrations.abstractcore import send_email
    except Exception as e:
        return False, f"Email helpers unavailable: {e}"

    try:
        out: Dict[str, Any] = send_email(
            account=_email_account(),
            to=to,
            subject=str(subject or ""),
            body_text=str(body_text or ""),
        )
    except Exception as e:
        return False, str(e)

    if isinstance(out, dict) and out.get("success") is True:
        return True, None
    err = out.get("error") if isinstance(out, dict) else None
    return False, str(err or "Email send failed")
