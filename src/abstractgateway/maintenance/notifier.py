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
    raw = _env("ABSTRACT_TRIAGE_TELEGRAM_CHAT_ID", "ABSTRACTGATEWAY_TRIAGE_TELEGRAM_CHAT_ID")
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

    # Prefer the framework tool (supports TDLib Secret Chats when configured).
    try:
        from abstractcore.tools.telegram_tools import send_telegram_message  # type: ignore
    except Exception as e:
        return False, f"Telegram tools unavailable: {e}"

    try:
        out: Dict[str, Any] = send_telegram_message(chat_id=chat_id, text=str(text or ""))
    except Exception as e:
        return False, str(e)

    if isinstance(out, dict) and out.get("success") is True:
        return True, None
    err = out.get("error") if isinstance(out, dict) else None
    return False, str(err or "Telegram send failed")


def _email_recipients() -> List[str]:
    raw = _env("ABSTRACT_TRIAGE_EMAIL_TO", "ABSTRACTGATEWAY_TRIAGE_EMAIL_TO") or ""
    parts = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    return parts


def send_email_notification(*, subject: str, body_text: str) -> Tuple[bool, Optional[str]]:
    to = _email_recipients()
    if not to:
        return False, "Missing EMAIL_TO recipients"

    smtp_host = _env("ABSTRACT_TRIAGE_EMAIL_SMTP_HOST", "ABSTRACTGATEWAY_TRIAGE_EMAIL_SMTP_HOST") or ""
    username = _env("ABSTRACT_TRIAGE_EMAIL_USERNAME", "ABSTRACTGATEWAY_TRIAGE_EMAIL_USERNAME") or ""
    password_env_var = _env("ABSTRACT_TRIAGE_EMAIL_PASSWORD_ENV_VAR", "ABSTRACTGATEWAY_TRIAGE_EMAIL_PASSWORD_ENV_VAR") or "EMAIL_PASSWORD"

    if not smtp_host or not username:
        return False, "Missing SMTP host/username (set ABSTRACT_TRIAGE_EMAIL_SMTP_HOST/USERNAME)"

    # Prefer the framework tool (secrets via env var indirection).
    try:
        from abstractcore.tools.comms_tools import send_email  # type: ignore
    except Exception as e:
        return False, f"Email tools unavailable: {e}"

    try:
        out: Dict[str, Any] = send_email(
            smtp_host=smtp_host,
            username=username,
            password_env_var=password_env_var,
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

