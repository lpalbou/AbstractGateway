from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional, Tuple


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    s = str(text or "")
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("utf-8"))


def sign_action_token(*, payload: Dict[str, Any], secret: str) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(str(secret).encode("utf-8"), blob, hashlib.sha256).digest()
    return f"{_b64url_encode(blob)}.{_b64url_encode(sig)}"


def verify_action_token(*, token: str, secret: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    raw = str(token or "").strip()
    if not raw or "." not in raw:
        return None, "Invalid token"
    a, b = raw.split(".", 1)
    try:
        blob = _b64url_decode(a)
        sig = _b64url_decode(b)
    except Exception:
        return None, "Invalid token encoding"

    expected = hmac.new(str(secret).encode("utf-8"), blob, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None, "Invalid token signature"

    try:
        payload = json.loads(blob.decode("utf-8"))
    except Exception:
        return None, "Invalid token payload"
    if not isinstance(payload, dict):
        return None, "Invalid token payload"

    exp = payload.get("exp")
    try:
        exp_i = int(exp) if exp is not None else None
    except Exception:
        exp_i = None
    if exp_i is not None and exp_i > 0:
        now = int(time.time())
        if now > exp_i:
            return None, "Token expired"

    return payload, None


def build_action_payload(*, decision_id: str, action: str, ttl_s: int = 7 * 24 * 3600) -> Dict[str, Any]:
    now = int(time.time())
    exp = now + int(ttl_s) if int(ttl_s) > 0 else 0
    return {"decision_id": str(decision_id), "action": str(action), "iat": now, "exp": exp}


def build_action_links(
    *,
    decision_id: str,
    base_url: str,
    secret: str,
    ttl_s: int = 7 * 24 * 3600,
) -> Dict[str, str]:
    """Build triage action URLs for a decision.

    Returns a dict with keys: approve, defer_1d, defer_7d, reject.
    """
    base = str(base_url or "").rstrip("/")
    if not base:
        return {}

    def _url(payload: Dict[str, Any]) -> str:
        token = sign_action_token(payload=payload, secret=secret)
        return f"{base}/api/triage/action/{token}"

    approve = _url(build_action_payload(decision_id=decision_id, action="approve", ttl_s=ttl_s))
    reject = _url(build_action_payload(decision_id=decision_id, action="reject", ttl_s=ttl_s))

    defer1 = build_action_payload(decision_id=decision_id, action="defer", ttl_s=ttl_s)
    defer1["days"] = 1
    defer7 = build_action_payload(decision_id=decision_id, action="defer", ttl_s=ttl_s)
    defer7["days"] = 7

    return {
        "approve": approve,
        "defer_1d": _url(defer1),
        "defer_7d": _url(defer7),
        "reject": reject,
    }
