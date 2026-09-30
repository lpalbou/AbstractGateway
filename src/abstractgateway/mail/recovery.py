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
- the rate check runs on the request thread before any worker exists (a flood costs no
  threads); it reads only the request counters, never the account;
- the request answer is HONEST (DESIGN 2026-09-30 §4.1): `sent` with the masked address
  ("l•••@•••": the first character of the local part, never the domain), `no_email_address`,
  `no_mailbox` (an address but no mailbox to send from), `send_failed` (the server refused or
  could not be reached — the answer waits up to SEND_WAIT_S for the real outcome)
  (an unknown account and one without an address or usable mailbox read the same), or
  `too_many_requests` with `retry_after_s`. The mail itself is sent off the request thread.
  Trade-off: a requester can learn that an account id has an email address; the rate limits
  and the audit log bound it, and the admin can turn sign-in by email off (then 404);
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
from .accounts import (
    EmailPrincipalRefused,
    _read_json,
    _write_private_json,
    email_context,
    email_usable,
    plane_for_principal,
    recovery_enabled,
    self_address,
)
from .audit import audit_email_event

PURPOSES = ("sign_in", "reset_token")
CODE_TTL_S = 600.0
MAX_ATTEMPTS = 5
ACCOUNT_WINDOW_S = 900.0
ACCOUNT_MAX_REQUESTS = 3
IP_WINDOW_S = 900.0
IP_MAX_REQUESTS = 10
NO_EMAIL_ADDRESS_MESSAGE = (
    "This account has no email address, so a code can't be sent. Ask your gateway admin for a token."
)
REDEEM_REFUSED = "That code is wrong, expired or already used. Send a new one."
MASK = "\u2022\u2022\u2022@\u2022\u2022\u2022"


def mask_address(address: str) -> str:
    """"l•••@•••": the first character of the local part, never the domain."""

    local = str(address or "").strip().split("@", 1)[0]
    return f"{local[:1]}{MASK}" if local else MASK


def _minutes(seconds: int) -> str:
    n = max(1, -(-int(seconds) // 60))
    return f"{n} minute" if n == 1 else f"{n} minutes"


def sent_answer(to: str) -> Dict[str, Any]:
    masked = mask_address(to)
    return {
        "ok": True,
        "sent": True,
        "to": masked,
        "expires_in_s": int(CODE_TTL_S),
        "message": f"A sign-in code is on its way to {masked}. It expires in {_minutes(int(CODE_TTL_S))}.",
    }


def no_email_answer() -> Dict[str, Any]:
    return {"ok": True, "sent": False, "reason_code": "no_email_address", "message": NO_EMAIL_ADDRESS_MESSAGE}


NO_MAILBOX_MESSAGE = (
    "This account has an email address, but no mailbox is connected to send the code from. "
    "Ask your gateway admin for a token."
)


def no_mailbox_answer() -> Dict[str, Any]:
    return {"ok": True, "sent": False, "reason_code": "no_mailbox", "message": NO_MAILBOX_MESSAGE}


SEND_FAILED_MESSAGE = (
    "The code couldn't be emailed: the mail server refused it or couldn't be reached. "
    "Try again, or ask your gateway admin for a token."
)


def send_failed_answer(cause: str = "") -> Dict[str, Any]:
    # The cause (which can name an address or a server) goes to the audit log, never to the caller.
    return {"ok": True, "sent": False, "reason_code": "send_failed", "message": SEND_FAILED_MESSAGE}


# How long the request waits for the code mail to actually leave (the answer is the REAL outcome;
# a send still in flight after this is answered as on its way).
SEND_WAIT_S = 12.0


def rate_limited_answer(bucket: str, retry_after_s: int) -> Dict[str, Any]:
    who = "this account" if bucket == "account" else "this device"
    return {
        "ok": True,
        "sent": False,
        "reason_code": "too_many_requests",
        "retry_after_s": int(retry_after_s),
        "message": f"Too many codes requested for {who}. Try again in {_minutes(retry_after_s)}.",
    }

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
    """True when the administrator leaves sign-in by email on (the default) and at least one
    enabled human account of this gateway can receive a code."""

    if not recovery_enabled():
        return False
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


def _recipient(user_id: str, tenant_id: str, base: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """(address, refusal): the address a code would go to, or None (audited) with why the account
    cannot receive one — "no_email_address" (unknown / disabled / entity / no address: an unknown
    account reads like one without an address) or "no_mailbox" (an address, but no usable mailbox
    to send the code from)."""

    principal = _eligible_principal(user_id, tenant_id)
    if principal is None:
        audit_email_event("email.recovery_code_refused", reason="no_eligible_account", **base)
        return None, "no_email_address"
    try:
        plane = plane_for_principal(principal)
    except EmailPrincipalRefused:
        audit_email_event("email.recovery_code_refused", reason="no_eligible_account", **base)
        return None, "no_email_address"
    to = self_address(plane)
    if not to:
        audit_email_event("email.recovery_code_refused", reason="no_email_address", **base)
        return None, "no_email_address"
    if not email_usable(plane):
        audit_email_event("email.recovery_code_refused", reason="email_not_configured", **base)
        return None, "no_mailbox"
    return to, ""


def request_code(*, user_id: str, tenant_id: str = "default", purpose: str = "sign_in", client_ip: str) -> Dict[str, Any]:
    """The honest answer (DESIGN §4.1): sent (masked address) | no_email_address | no_mailbox |
    send_failed | too_many_requests. The code mail is sent on its own thread; the answer waits for
    its real outcome (up to SEND_WAIT_S)."""

    purpose0 = str(purpose or "").strip() or "sign_in"
    if purpose0 not in PURPOSES:
        raise ValueError(f"purpose must be one of: {', '.join(PURPOSES)}")
    args = {"user_id": str(user_id or ""), "tenant_id": str(tenant_id or "default"), "purpose": purpose0, "client_ip": str(client_ip or "unknown")}
    # The rate check runs first, before any account lookup or thread: a flood of requests costs
    # a file update each, never a thread each, and an unknown account is counted like a real one.
    refused = _rate_admit(**args)
    if refused is not None:
        return rate_limited_answer(*refused)
    base = {"tenant_id": args["tenant_id"], "user_id": args["user_id"], "purpose": purpose0, "client_ip": args["client_ip"]}
    to, refusal = _recipient(args["user_id"], args["tenant_id"], base)
    if to is None:
        return no_mailbox_answer() if refusal == "no_mailbox" else no_email_answer()
    # The mail is sent on its own thread, but the answer waits for the real outcome (up to
    # SEND_WAIT_S): a refused or unreachable server is reported, never a "sent" that never left.
    outcome: Dict[str, Any] = {}
    worker = threading.Thread(target=_issue, kwargs={**args, "outcome": outcome}, name="gateway-recovery-code", daemon=True)
    with _PENDING_LOCK:
        _PENDING[:] = [t for t in _PENDING if t.is_alive()]
        _PENDING.append(worker)
    worker.start()
    worker.join(SEND_WAIT_S)
    if outcome.get("result") == "send_failed":
        return send_failed_answer(str(outcome.get("cause") or ""))
    return sent_answer(to)


def drain(timeout_s: float = 30.0) -> None:
    """Wait for the pending code mails (shutdown, tests)."""

    deadline = time.time() + float(timeout_s)
    with _PENDING_LOCK:
        pending = list(_PENDING)
    for t in pending:
        t.join(max(0.0, deadline - time.time()))


def _retry_after(rows: List[float], window: float, now: float) -> int:
    oldest = min((float(t) for t in rows), default=now)
    return max(1, int(round(window - (now - oldest) + 0.4999)))


def _rate_admit(*, user_id: str, tenant_id: str, purpose: str, client_ip: str) -> Optional[Tuple[str, int]]:
    """Count this request against the client address and the account; None when admitted, else
    `(bucket, retry_after_s)` (audited) when either is over its window's budget."""

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
            return "client", _retry_after(ip_rows, IP_WINDOW_S, now)
        ip_rows.append(now)
        if len(acct_rows) >= ACCOUNT_MAX_REQUESTS:
            _save(doc)
            audit_email_event("email.recovery_code_refused", reason="rate_limited_account", **base)
            return "account", _retry_after(acct_rows, ACCOUNT_WINDOW_S, now)
        acct_rows.append(now)
        _save(doc)
    return None


def _issue(*, user_id: str, tenant_id: str, purpose: str, client_ip: str, outcome: Optional[Dict[str, Any]] = None) -> None:
    now = time.time()
    account_key = f"{tenant_id}:{user_id}"
    base = {"tenant_id": tenant_id, "user_id": user_id, "purpose": purpose, "client_ip": client_ip}
    if not recovery_enabled():
        audit_email_event("email.recovery_code_refused", reason="recovery_turned_off", **base)
        return
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
        # The same address the request's answer masked (the registered one, else the mailbox's).
        to = self_address(plane) or str(ctx.registered_address or ctx.account.address or "").strip()
        subject, text = _render(purpose, code)
        # Automatic mail (RFC 3834) with the framework marker: the watcher never admits it,
        # so a sign-in code never reaches an automation (or its model) reading this inbox.
        guarded_send(ctx, OutgoingMessage(to=(to,), subject=subject, text=text,
                                          auto_submitted="auto-generated", automation_marker=f"sign-in-code:{purpose}"))
    except EmailError as err:
        with _LOCK:
            doc = _load()
            doc["codes"] = [c for c in doc["codes"] if not (c.get("account") == account_key and c.get("purpose") == purpose)]
            _save(doc)
        audit_email_event("email.recovery_code_refused", reason="send_failed", code=err.code, cause=err.cause, fix=err.fix, **base)
        if outcome is not None:
            outcome["result"] = "send_failed"
            outcome["cause"] = str(err.cause or err.code or "").strip()
        return
    finally:
        code = ""
    if outcome is not None:
        outcome["result"] = "sent"
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
    if not recovery_enabled():
        audit_email_event("email.recovery_code_used", outcome="refused", reason="recovery_turned_off", **base)
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
