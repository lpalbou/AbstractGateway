"""Named API keys for the OpenAI API at /v1 (round 16, backlog 1000).

Each account may make keys, each with a name ("laptop Cursor"). One key can serve every app;
separate keys are optional, for revoking one app without the others. A key:

- is answered by the request that makes it; the registry keeps its PBKDF2 hash and its
  fingerprint (SHA-256, 12 hex) in the account's record (`users.json` `openai_keys`, additive);
- can be REVEALED again by its owner, and only its owner (`POST /me/openai-keys/{fp}/reveal`,
  one audit line per reveal), while the gateway setting "API keys can be revealed by their owner"
  (`config/core_endpoint.json` `owner_reveal`, default on) is on: the key is then also kept
  ENCRYPTED AT REST in `<data_dir>/auth/openai_key_secrets/` — the same sealing as email
  credentials (AES-256-GCM with the data folder's key file `<data_dir>/secrets/sealing.key`,
  security/sealing.py; never the OS keychain). Admins see fingerprints, never keys. Turning the
  setting off erases every sealed copy and makes every key hash-only (show-once) from then on;
  turning it back on applies to keys made afterwards. Keys sealed by gateway <= 0.13 with the
  key in the macOS keychain are never read: the old file is moved aside, their rows say
  "Reveal unavailable: created before the key moved — create a new key" (`reveal_unavailable`)
  and Revoke still works;
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

    POST   /api/gateway/me/openai-keys {label}                 -> {key, item}
    GET    /api/gateway/me/openai-keys                         -> {keys: [item]}
    POST   /api/gateway/me/openai-keys/{fingerprint}/reveal    -> {key, item}   (the owner; audited)
    DELETE /api/gateway/me/openai-keys/{fingerprint}           -> {revoked: item}
    GET    /api/gateway/admin/accounts/{id}/openai-keys        -> {account, keys: [item]}   (admin)
    DELETE /api/gateway/admin/accounts/{id}/openai-keys/{fp}   -> {revoked: item}           (admin)

item = {label, fingerprint, created_at, created_by, last_used_at, last_client, revealable,
        reveal_unavailable (a sentence, only on a key sealed before the key moved)}
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
NOT_OWNER = "Only the key's owner can reveal it; admins see its fingerprint."
REVEAL_OFF = ("Revealing API keys is turned off on this gateway: a key is shown once, when it is made. "
              "If you lost one, make a new key.")
NOT_REVEALABLE = ("This key was made while revealing was off, so the gateway keeps only its hash. "
                  "If you lost it, make a new key.")
STORE_UNAVAILABLE = ("The encrypted key store can't be opened (the data folder's key file secrets/sealing.key is "
                     "missing or was replaced): restore it from the backup, or make a new key.")
KEY_MOVED = "Reveal unavailable: created before the key moved \u2014 create a new key"
MOVED_FILE = ("auth", "openai_key_secrets_moved.json")
USAGE_FILE = ("auth", "openai_key_usage.json")
SECRETS_DIR = ("auth", "openai_key_secrets")
# A key used again from the same address within this many seconds is not rewritten to disk.
USAGE_WRITE_EVERY_S = 30.0

_USAGE_LOCK = threading.Lock()
_SEAL_LOCK = threading.Lock()
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


def _item(key: OpenAIKeyRecord, usage: Dict[str, Dict[str, Any]], *, reveal_on: bool,
          moved: frozenset = frozenset()) -> Dict[str, Any]:
    out = key.public_dict()
    used = usage.get(key.fingerprint) or {}
    out["last_used_at"] = used.get("last_used_at") or None
    out["last_client"] = used.get("last_client") or None
    # Can the owner reveal it now? Only a sealed key, and only while the setting is on.
    out["revealable"] = bool(key.sealed and reveal_on)
    if key.sealed and key.fingerprint in moved:
        # Sealed with the old keychain key (never read): the key still works; revoke it or make a new one.
        out["revealable"] = False
        out["reveal_unavailable"] = KEY_MOVED
    return out


# ---- the encrypted copies (owner reveal) ------------------------------------------------

def owner_reveal_on(data_dir: Optional[Path] = None) -> bool:
    """The admin setting "API keys can be revealed by their owner" (default on)."""
    from .core_endpoint import read_settings

    return bool(read_settings(Path(data_dir or gateway_data_dir_from_env())).owner_reveal)


def _vault(data_dir: Optional[Path] = None):
    # The same sealing as the email credentials and MCP header values: the data folder's key
    # file (security/sealing.py; AES-256-GCM, never the OS keychain).
    from .security.sealing import sealed_vault

    root = Path(data_dir or gateway_data_dir_from_env())
    return sealed_vault(root.joinpath(*SECRETS_DIR), data_dir=root, legacy_cause=KEY_MOVED,
                       legacy_fix="Create a new key; revoking the old one still works.")


def _moved_path(data_dir: Optional[Path] = None) -> Path:
    return Path(data_dir or gateway_data_dir_from_env()).joinpath(*MOVED_FILE)


def moved_fingerprints(data_dir: Optional[Path] = None) -> frozenset:
    """Keys whose sealed copy was made with the old keychain key (never read): not revealable."""
    try:
        doc = json.loads(_moved_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    fps = doc.get("fingerprints") if isinstance(doc, dict) else None
    return frozenset(str(f) for f in (fps or []) if isinstance(f, str))


def _retire_legacy(vault, fingerprints: frozenset, data_dir: Optional[Path] = None) -> bool:
    """Move a keychain-sealed key store aside (never read) and remember which keys it held
    (a superset is fine: a row says "Reveal unavailable" only when its key was sealed)."""
    if not vault.legacy_keychain():
        return False
    from .security.sealing import write_private

    path = _moved_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    known = moved_fingerprints(data_dir) | frozenset(fingerprints)
    write_private(path, (json.dumps({"version": 1, "at": _now_iso(), "fingerprints": sorted(known)}, indent=2) + "\n").encode("utf-8"))
    vault.retire_legacy_keychain()
    from .mail.audit import audit_email_event

    audit_email_event("secret_key_migrated_to_file", actor="gateway", outcome="reveal_unavailable",
                      reason=KEY_MOVED, store="openai_keys", count=len(known))
    return True


def migrate_legacy_store(data_dir: Optional[Path] = None) -> bool:
    """Boot (round 16): a key store sealed with the old OS-keychain key is never opened."""
    with _SEAL_LOCK:
        vault = _vault(data_dir)
        if not vault.legacy_keychain():
            return False
        fps = frozenset(k.fingerprint for rec in GatewayUserRegistry().list_users() for k in rec.openai_keys)
        return _retire_legacy(vault, fps, data_dir)


def _sealed(vault, live: frozenset = frozenset(), data_dir: Optional[Path] = None) -> Dict[str, str]:
    if vault.legacy_keychain():
        # Not migrated at boot (a CLI, a test): never read it; move it aside now.
        _retire_legacy(vault, live, data_dir)
        return {}
    if not vault.exists():
        return {}
    payload = vault.load() or {}
    keys = payload.get("keys") if isinstance(payload, dict) else None
    return {str(k): str(v) for k, v in (keys or {}).items() if isinstance(v, str) and v}


def _store_sealed(vault, keys: Dict[str, str]) -> None:
    if keys:
        vault.store({"keys": keys}, reuse_key=vault.exists())
    elif vault.exists():
        vault.store({"keys": {}}, reuse_key=True)


def sealed_fingerprints(data_dir: Optional[Path] = None) -> frozenset:
    """The fingerprints whose key is kept sealed (tests and diagnostics; never a key)."""
    with _SEAL_LOCK:
        return frozenset(_sealed(_vault(data_dir)))


def _seal(fingerprint: str, key: str, live: frozenset, *, data_dir: Optional[Path] = None) -> bool:
    """Keep `key` sealed under its fingerprint (pruning copies of keys that no longer exist).
    False when the store can't be written: the key is then hash-only, and its item says so."""
    import logging

    try:
        with _SEAL_LOCK:
            vault = _vault(data_dir)
            keys = {fp: k for fp, k in _sealed(vault, live, data_dir).items() if fp in live}
            keys[fingerprint] = key
            _store_sealed(vault, keys)
        return True
    except Exception as exc:  # noqa: BLE001 - the key still works; it is just not revealable
        logging.getLogger(__name__).warning("named API key %s not sealed for owner reveal: %s",
                                            fingerprint, type(exc).__name__)
        return False


def _unseal_one(fingerprint: str, *, data_dir: Optional[Path] = None) -> None:
    with _SEAL_LOCK:
        vault = _vault(data_dir)
        if not vault.exists():
            return
        keys = _sealed(vault)
        if keys.pop(fingerprint, None) is not None:
            _store_sealed(vault, keys)


def erase_sealed_keys(data_dir: Optional[Path] = None) -> int:
    """Owner reveal turned off: every key becomes hash-only and every sealed copy is erased
    (the sealed file and its encryption key are deleted)."""
    changed = GatewayUserRegistry().clear_openai_key_seals()
    with _SEAL_LOCK:
        vault = _vault(data_dir)
        if vault.exists():
            vault.delete()
        try:
            _moved_path(data_dir).unlink()
        except OSError:
            pass
    return changed


def list_keys(user_id: str, tenant_id: str = "default") -> Optional[List[Dict[str, Any]]]:
    """The account's keys, oldest first (never a key or a hash), or None for an unknown account."""
    rec = GatewayUserRegistry().get_user(user_id, tenant_id=tenant_id)
    if rec is None:
        return None
    usage = read_usage()
    on = owner_reveal_on()
    moved = moved_fingerprints()
    return [_item(k, usage, reveal_on=on, moved=moved) for k in rec.openai_keys]


def create_key(user_id: str, tenant_id: str, label: str, *, created_by: str) -> Dict[str, Any]:
    on = owner_reveal_on()
    item, key = GatewayUserRegistry().create_openai_key(
        user_id=user_id, tenant_id=tenant_id, label=label, created_by=created_by,
        seal=(lambda fp, k, live: _seal(fp, k, live)) if on else None)
    return {"key": key, "item": _item(item, {}, reveal_on=on)}


def reveal_key(user_id: str, tenant_id: str, fingerprint: str) -> Dict[str, Any]:
    """The owner's key in clear, for the owner only: {key, item}. Refused (OpenAIKeyError) when
    the fingerprint is someone else's (403 not_owner), unknown (404 key_not_found), the setting
    is off (404 reveal_off), the key is hash-only (404 not_revealable) or the store can't be
    opened (503 key_store_unavailable)."""
    from .users import OpenAIKeyError

    fp = str(fingerprint or "").strip().lower()
    reg = GatewayUserRegistry()
    rec = reg.get_user(user_id, tenant_id=tenant_id)
    mine = next((k for k in (rec.openai_keys if rec is not None else ()) if k.fingerprint == fp), None)
    if mine is None:
        if fp and fp in reg.openai_key_fingerprints():
            raise OpenAIKeyError(403, "not_owner", NOT_OWNER)
        raise OpenAIKeyError(404, "key_not_found", "There is no such key on this account (already revoked?).")
    if not owner_reveal_on():
        raise OpenAIKeyError(404, "reveal_off", REVEAL_OFF)
    if not mine.sealed:
        raise OpenAIKeyError(404, "not_revealable", NOT_REVEALABLE)
    if fp in moved_fingerprints():
        raise OpenAIKeyError(404, "reveal_unavailable_key_moved", KEY_MOVED)
    try:
        with _SEAL_LOCK:
            key = _sealed(_vault(), frozenset(reg.openai_key_fingerprints())).get(fp)
    except Exception:  # noqa: BLE001 - key file missing / replaced: say so, never a stack
        raise OpenAIKeyError(503, "key_store_unavailable", STORE_UNAVAILABLE) from None
    if not key and fp in moved_fingerprints():
        raise OpenAIKeyError(404, "reveal_unavailable_key_moved", KEY_MOVED)
    if not key:
        raise OpenAIKeyError(404, "not_revealable", NOT_REVEALABLE)
    return {"key": key, "item": _item(mine, read_usage(), reveal_on=True)}


def revoke_key(user_id: str, tenant_id: str, fingerprint: str) -> Dict[str, Any]:
    gone = GatewayUserRegistry().revoke_openai_key(user_id=user_id, tenant_id=tenant_id, fingerprint=fingerprint)
    usage = read_usage()
    out = _item(gone, usage, reveal_on=owner_reveal_on(), moved=moved_fingerprints())
    forget_use(gone.fingerprint)
    if gone.sealed:
        try:
            _unseal_one(gone.fingerprint)
        except Exception:  # noqa: BLE001 - the key is revoked either way; the copy is pruned on the next seal
            pass
    return out
