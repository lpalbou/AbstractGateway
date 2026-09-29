"""Account recovery by email: "Forgot your token?" and "Email me a sign-in code".

Only for users who configured email (operator addition 2026-09-29). The code travels by the
user's OWN account (the gateway holds the sealed credentials, so no sign-in is needed to
send it) to their registered address, through `guarded_send` (their recipient policy and
send limits apply).

Rules, each one pinned by a test:

- a code is 8 digits, single use, valid 10 minutes, and at most 5 wrong tries;
- only its HMAC is stored (`<data_dir>/auth/recovery_codes.json`, key in
  `<data_dir>/auth/recovery.key`, both 0600): a copy of the file alone reveals nothing;
- a new code for the same account and purpose replaces the previous one;
- requests are rate-limited per account (3 per 15 minutes) and per client address
  (10 per 15 minutes);
- the request answer is the same whether or not the account exists, has email, or is rate
  limited, and the mail is sent off the request thread (no timing difference): no account
  enumeration. The sign-in page shows the options when at least one account of this gateway
  has email configured, and never says which;
- every issue, refusal and use is audited without the code.

Purposes: `sign_in` (the code opens a browser session) and `reset_token` (the code rotates
the user's gateway token — "password" in everyday words — and returns the new one once,
with a session).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core_mail import EmailError, OutgoingMessage, guarded_send

from ..users import gateway_data_dir_from_env
from .accounts import EmailPrincipalRefused, _read_json, _write_private_json, email_context, email_usable, plane_for_principal
from .audit import audit_email_event

PURPOSES = ("sign_in", "reset_token")
CODE_TTL_S = 600.0
MAX_ATTEMPTS = 5
ACCOUNT_WINDOW_S = 900.0
ACCOUNT_MAX_REQUESTS = 3
IP_WINDOW_S = 900.0
IP_MAX_REQUESTS = 10
CONSTANT_MESSAGE = (
    "If this account has email configured, a code is on its way to its registered address. "
    "It works once and expires in 10 minutes."
)
REDEEM_REFUSED = "The code is wrong, expired or already used. Request a new one."

_LOCK = threading.Lock()
_PENDING: List[threading.Thread] = []
_PENDING_LOCK = threading.Lock()


def _auth_dir() -> Path:
    return gateway_data_dir_from_env() / "auth"


def _codes_path() -> Path:
    return _auth_dir() / "recovery_codes.json"


def _key() -> bytes:
    path = _auth_dir() / "recovery.key"
    try:
        raw = path.read_bytes().strip()
        if len(raw) >= 32:
            return raw
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32).encode("ascii")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def _digest(tenant_id: str, user_id: str, purpose: str, code: str) -> str:
    msg = "|".join((str(tenant_id), str(user_id), str(purpose), str(code))).encode("utf-8")
    return hmac.new(_key(), msg, hashlib.sha256).hexdigest()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _load() -> Dict[str, Any]:
    doc = _read_json(_codes_path())
    doc.setdefault("codes", [])
    doc.setdefault("requests", {"account": {}, "ip": {}})
    return doc


def _save(doc: Dict[str, Any]) -> None:
    _write_private_json(_codes_path(), doc)


def _prune(doc: Dict[str, Any], now: float) -> None:
    doc["codes"] = [c for c in doc.get("codes") or [] if float(c.get("expires_at") or 0) > now and not c.get("used")]
    req = doc.get("requests") or {}
    for bucket, window in (("account", ACCOUNT_WINDOW_S), ("ip", IP_WINDOW_S)):
        rows = req.get(bucket) if isinstance(req.get(bucket), dict) else {}
        req[bucket] = {k: [t for t in v if now - float(t) < window] for k, v in rows.items() if any(now - float(t) < window for t in v)}
    doc["requests"] = req


def recovery_available() -> bool:
    """True when at least one enabled human account of this gateway can receive a code."""

    try:
        from ..service import gateway_multi_user_enabled
        from ..users import GatewayUserRegistry

        records = GatewayUserRegistry().list_users()
        for rec in records:
            if not rec.enabled or rec.principal_kind == "entity":
                continue
            principal = rec.to_principal()
            if not gateway_multi_user_enabled() and not principal.is_admin():
                continue
            try:
                if email_usable(plane_for_principal(principal)):
                    return True
            except EmailPrincipalRefused:
                continue
    except Exception:  # noqa: BLE001 - the page simply hides the options
        return False
    return False


def _eligible_principal(user_id: str, tenant_id: str):
    from ..security.sessions import principal_barred_from_shared_runtime
    from ..users import GatewayUserRegistry

    rec = GatewayUserRegistry().get_user(str(user_id or "").strip(), tenant_id=str(tenant_id or "default").strip() or "default")
    if rec is None or not rec.enabled or rec.principal_kind == "entity":
        return None
    principal = rec.to_principal()
    if principal_barred_from_shared_runtime(principal):
        return None
    return principal


def request_code(*, user_id: str, tenant_id: str = "default", purpose: str, client_ip: str) -> Dict[str, Any]:
    """Always the same answer. The decision and the mail happen off the request thread."""

    purpose0 = str(purpose or "").strip()
    if purpose0 not in PURPOSES:
        raise ValueError(f"purpose must be one of: {', '.join(PURPOSES)}")
    worker = threading.Thread(
        target=_issue,
        kwargs={"user_id": str(user_id or ""), "tenant_id": str(tenant_id or "default"), "purpose": purpose0, "client_ip": str(client_ip or "unknown")},
        name="gateway-recovery-code",
        daemon=True,
    )
    with _PENDING_LOCK:
        _PENDING[:] = [t for t in _PENDING if t.is_alive()]
        _PENDING.append(worker)
    worker.start()
    return {"ok": True, "message": CONSTANT_MESSAGE, "expires_in_s": int(CODE_TTL_S)}


def drain(timeout_s: float = 30.0) -> None:
    """Wait for the pending code mails (shutdown, tests)."""

    deadline = time.time() + float(timeout_s)
    with _PENDING_LOCK:
        pending = list(_PENDING)
    for t in pending:
        t.join(max(0.0, deadline - time.time()))


def _issue(*, user_id: str, tenant_id: str, purpose: str, client_ip: str) -> None:
    now = time.time()
    account_key = f"{tenant_id}:{user_id}"
    base = {"tenant_id": tenant_id, "user_id": user_id, "purpose": purpose, "client_ip": client_ip}
    with _LOCK:
        doc = _load()
        _prune(doc, now)
        req = doc["requests"]
        ip_rows = req["ip"].setdefault(client_ip, [])
        acct_rows = req["account"].setdefault(account_key, [])
        if len(ip_rows) >= IP_MAX_REQUESTS:
            _save(doc)
            audit_email_event("email.recovery_code_refused", reason="rate_limited_client", **base)
            return
        ip_rows.append(now)
        if len(acct_rows) >= ACCOUNT_MAX_REQUESTS:
            _save(doc)
            audit_email_event("email.recovery_code_refused", reason="rate_limited_account", **base)
            return
        acct_rows.append(now)
        _save(doc)
    principal = _eligible_principal(user_id, tenant_id)
    if principal is None:
        audit_email_event("email.recovery_code_refused", reason="no_eligible_account", **base)
        return
    try:
        plane = plane_for_principal(principal)
    except EmailPrincipalRefused:
        audit_email_event("email.recovery_code_refused", reason="no_eligible_account", **base)
        return
    if not email_usable(plane):
        audit_email_event("email.recovery_code_refused", reason="email_not_configured", **base)
        return
    code = f"{secrets.randbelow(10**8):08d}"
    with _LOCK:
        doc = _load()
        _prune(doc, time.time())
        doc["codes"] = [c for c in doc["codes"] if not (c.get("account") == account_key and c.get("purpose") == purpose)]
        doc["codes"].append(
            {
                "id": secrets.token_hex(8),
                "account": account_key,
                "purpose": purpose,
                "hash": _digest(tenant_id, user_id, purpose, code),
                "issued_at": now,
                "expires_at": now + CODE_TTL_S,
                "attempts": 0,
                "used": False,
            }
        )
        _save(doc)
    try:
        ctx = email_context(plane)
        to = str(ctx.registered_address or ctx.account.address or "").strip()
        subject, text = _render(purpose, code)
        guarded_send(ctx, OutgoingMessage(to=(to,), subject=subject, text=text))
    except EmailError as err:
        with _LOCK:
            doc = _load()
            doc["codes"] = [c for c in doc["codes"] if not (c.get("account") == account_key and c.get("purpose") == purpose)]
            _save(doc)
        audit_email_event("email.recovery_code_refused", reason="send_failed", code=err.code, cause=err.cause, fix=err.fix, **base)
        return
    finally:
        code = ""
    audit_email_event("email.recovery_code_issued", outcome="sent", **base)


def _render(purpose: str, code: str) -> Tuple[str, str]:
    if purpose == "reset_token":
        subject = "[AbstractFramework] Your code to set a new gateway token"
        first = "Someone (hopefully you) asked to set a new sign-in token for your AbstractFramework gateway account."
        then = "Enter this code on the sign-in page (“Forgot your token?”). Your old token stops working when the new one is issued."
    else:
        subject = "[AbstractFramework] Your sign-in code"
        first = "Someone (hopefully you) asked for a sign-in code for your AbstractFramework gateway account."
        then = "Enter this code on the sign-in page (“Email me a sign-in code”)."
    text = (
        f"{first}\n\n    {code}\n\n{then}\n"
        "It works once and expires in 10 minutes.\n\n"
        "If you did not ask for it, ignore this email: nothing changes without the code.\n"
    )
    return subject, text


def redeem_code(*, user_id: str, tenant_id: str = "default", purpose: str, code: str, client_ip: str):
    """The principal when the code is right (the code is consumed), else None."""

    purpose0 = str(purpose or "").strip()
    tenant0 = str(tenant_id or "default").strip() or "default"
    user0 = str(user_id or "").strip()
    account_key = f"{tenant0}:{user0}"
    base = {"tenant_id": tenant0, "user_id": user0, "purpose": purpose0, "client_ip": str(client_ip or "unknown")}
    if purpose0 not in PURPOSES:
        return None
    given = "".join(ch for ch in str(code or "") if ch.isdigit())
    now = time.time()
    with _LOCK:
        doc = _load()
        _prune(doc, now)
        match = None
        for c in doc["codes"]:
            if c.get("account") == account_key and c.get("purpose") == purpose0:
                match = c
                break
        if match is None:
            _save(doc)
            audit_email_event("email.recovery_code_used", outcome="refused", reason="no_active_code", **base)
            return None
        ok = len(given) == 8 and hmac.compare_digest(str(match.get("hash") or ""), _digest(tenant0, user0, purpose0, given))
        if not ok:
            match["attempts"] = int(match.get("attempts") or 0) + 1
            if match["attempts"] >= MAX_ATTEMPTS:
                match["used"] = True
                reason = "too_many_attempts"
            else:
                reason = "wrong_code"
            _prune(doc, now)
            _save(doc)
            audit_email_event("email.recovery_code_used", outcome="refused", reason=reason, **base)
            return None
        match["used"] = True
        _prune(doc, now)
        _save(doc)
    principal = _eligible_principal(user0, tenant0)
    if principal is None:
        audit_email_event("email.recovery_code_used", outcome="refused", reason="no_eligible_account", **base)
        return None
    audit_email_event("email.recovery_code_used", outcome="accepted", **base)
    return principal
