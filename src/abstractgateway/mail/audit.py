"""Typed email events in the gateway audit log (`<data_dir>/audit_log.jsonl`).

Request lines are written by the security middleware; these are the events that happen
off a request (the watcher, the dispatcher) or that a route wants recorded as a typed
fact (`email.connected`, `email.recovery_code_issued`, ...). Never a password, token,
code, AUTH exchange or message content: callers pass typed fields only, and the few
free-text fields allowed (`cause`, `fix`) come from `EmailError`, which never carries a
secret.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, Dict, Optional

# Fields an email audit event may carry. Anything else is dropped, so a caller cannot
# leak a body, a password or a code into the log by passing it along.
_ALLOWED_FIELDS = frozenset(
    {
        "tenant_id",
        "user_id",
        "actor",
        "code",
        "cause",
        "fix",
        "outcome",
        "kind",
        "idempotency_key",
        "message_id",
        "smtp_code",
        "uid",
        "uidvalidity",
        "previous_uidvalidity",
        "folder",
        "enabled",
        "count",
        "reason",
        "client_ip",
        "purpose",
        "auth_kind",
        "provider",
        "leg",
        # capabilities.json migration, email address / connect facts (typed, never content)
        "from_version",
        "to_version",
        "pinned_off",
        "source",
        "discovered",
        # round 16: which sealed store moved off the OS keychain (mailbox, oauth_clients, ...)
        "store",
    }
)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def audit_email_event(event: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Append one typed event. Never raises (auditing must not break the caller)."""

    try:
        from ..security.gateway_security import _AUDIT_LOCK, _audit_data_dir_from_env, _audit_log_enabled

        if not _audit_log_enabled(default=True):
            return None
        entry: Dict[str, Any] = {"ts": _now_iso(), "event": str(event)}
        for key, value in fields.items():
            if key in _ALLOWED_FIELDS and value is not None:
                entry[key] = value
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _AUDIT_LOCK:
            path = (_audit_data_dir_from_env() / "audit_log.jsonl").resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as fh:
                fh.write(line.encode("utf-8", errors="replace"))
        return entry
    except Exception:
        return None
