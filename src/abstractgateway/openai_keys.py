"""Named API keys for the OpenAI API at /v1 (round 16, backlog 1000).

Each account may make several keys, each with a name ("laptop Cursor"). A key:

- is answered once, by the request that makes it; the registry keeps its PBKDF2 hash and its
  fingerprint (SHA-256, 12 hex) in the account's record (`users.json` `openai_keys`, additive);
- works at `/v1/*` only. Anywhere under `/api/gateway/*` (sign-in included) it answers 401 with
  `ENDPOINT_ONLY`: it never signs in to the console and never acts on runs, files, email or
  workflows;
- runs as its account and obeys the account's OpenAI API switch (403 `openai_api_off`) and its
  Active state (403 `account_inactive`);
- is revoked one at a time, immediately (the next request answers 401 `invalid_api_key`);
- names itself in the request log (`openai_api.key_label`, `openai_api.key_fingerprint`).

When each key was last used, and from which client address, is kept beside the registry in
`<data_dir>/auth/openai_key_usage.json` ({fingerprint: {last_used_at, last_client}}), so a busy
key does not rewrite the registry (and invalidate every auth cache) on each request.

    POST   /api/gateway/me/openai-keys {label}                 -> {key, item}   (the only time the key is answered)
    GET    /api/gateway/me/openai-keys                         -> {keys: [item]}
    DELETE /api/gateway/me/openai-keys/{fingerprint}           -> {revoked: item}
    GET    /api/gateway/admin/accounts/{id}/openai-keys        -> {account, keys: [item]}   (admin)
    DELETE /api/gateway/admin/accounts/{id}/openai-keys/{fp}   -> {revoked: item}           (admin)

item = {label, fingerprint, created_at, created_by, last_used_at, last_client}
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .users import GatewayUserRegistry, OpenAIKeyRecord, gateway_data_dir_from_env

ENDPOINT_ONLY = "This is an API key for /v1; sign in with your gateway token."
NO_ACCOUNT = ("Named API keys belong to a gateway account, and you are signed in with the gateway's own "
              "token: sign in with an account to make one.")
USAGE_FILE = ("auth", "openai_key_usage.json")
# A key used again from the same address within this many seconds is not rewritten to disk.
USAGE_WRITE_EVERY_S = 30.0

_USAGE_LOCK = threading.Lock()
_LAST_WRITTEN: Dict[str, tuple] = {}


def _usage_path(data_dir: Optional[Path] = None) -> Path:
    return Path(data_dir or gateway_data_dir_from_env()).joinpath(*USAGE_FILE)


def read_usage(data_dir: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    try:
        doc = json.loads(_usage_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in doc.items() if isinstance(v, dict)} if isinstance(doc, dict) else {}


def _write_usage(doc: Dict[str, Dict[str, Any]], data_dir: Optional[Path] = None) -> None:
    path = _usage_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def record_use(fingerprint: str, client: str, *, data_dir: Optional[Path] = None) -> None:
    """Note that the key `fingerprint` was just used from `client` (an address). Best effort."""
    if not fingerprint:
        return
    now = time.monotonic()
    with _USAGE_LOCK:
        last = _LAST_WRITTEN.get(fingerprint)
        if last is not None and last[1] == client and now - last[0] < USAGE_WRITE_EVERY_S:
            return
        try:
            doc = read_usage(data_dir)
            doc[fingerprint] = {"last_used_at": _now_iso(), "last_client": str(client or "")[:200]}
            _write_usage(doc, data_dir)
            _LAST_WRITTEN[fingerprint] = (now, client)
        except OSError:
            return


def forget_use(fingerprint: str, *, data_dir: Optional[Path] = None) -> None:
    with _USAGE_LOCK:
        _LAST_WRITTEN.pop(fingerprint, None)
        doc = read_usage(data_dir)
        if doc.pop(fingerprint, None) is not None:
            try:
                _write_usage(doc, data_dir)
            except OSError:
                pass


def _item(key: OpenAIKeyRecord, usage: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    out = key.public_dict()
    used = usage.get(key.fingerprint) or {}
    out["last_used_at"] = used.get("last_used_at") or None
    out["last_client"] = used.get("last_client") or None
    return out


def list_keys(user_id: str, tenant_id: str = "default") -> Optional[List[Dict[str, Any]]]:
    """The account's keys, oldest first (never a key or a hash), or None for an unknown account."""
    rec = GatewayUserRegistry().get_user(user_id, tenant_id=tenant_id)
    if rec is None:
        return None
    usage = read_usage()
    return [_item(k, usage) for k in rec.openai_keys]


def create_key(user_id: str, tenant_id: str, label: str, *, created_by: str) -> Dict[str, Any]:
    item, key = GatewayUserRegistry().create_openai_key(user_id=user_id, tenant_id=tenant_id, label=label,
                                                        created_by=created_by)
    return {"key": key, "item": _item(item, {})}


def revoke_key(user_id: str, tenant_id: str, fingerprint: str) -> Dict[str, Any]:
    gone = GatewayUserRegistry().revoke_openai_key(user_id=user_id, tenant_id=tenant_id, fingerprint=fingerprint)
    usage = read_usage()
    out = _item(gone, usage)
    forget_use(gone.fingerprint)
    return out
