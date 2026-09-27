"""Per-principal attention preferences for automations (contract C7 / F).

Attention RECORDS (notify / failure items) live in each automation's ledger
(AbstractRuntime); what a given user has SEEN is the gateway's: a monotonic
cursor per (principal, automation), stored in the principal's plane:

    <plane data_dir>/automations/attention/<sha256(json.dumps([tenant, user]))>.json
    {"schema_version": 1, "principal": [tenant, user],
     "automations": {<automation_id>: {"attention_cursor": "att1:<seq>", "updated_at": ...}}}

The filename hashes the canonical tuple, so ("a_b","c") and ("a","b_c") never
collide and a user id like "../x" cannot escape the directory; the tuple is
stored inside the file and verified on read. Writes are read-modify-replace
(temp file + os.replace) serialized under a per-file process lock — v1 runs
one gateway process per store.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ATTENTION_CURSOR_PREFIX = "att1:"
_CURSOR_RE = re.compile(r"^att1:(0|[1-9][0-9]*)$")
SCHEMA_VERSION = 1

_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class AttentionCursorError(ValueError):
    """A malformed attention cursor (reason_code invalid_request)."""


class AttentionStoreCorrupt(RuntimeError):
    """The preference file does not belong to the principal it is named for."""


def format_attention_cursor(seq: int) -> str:
    return f"{ATTENTION_CURSOR_PREFIX}{int(seq)}"


def parse_attention_cursor(value: Any) -> int:
    """`att1:<seq>` -> seq (a non-negative integer); anything else raises."""
    m = _CURSOR_RE.match(str(value or "")) if isinstance(value, str) else None
    if m is None:
        raise AttentionCursorError(f"attention_cursor must look like 'att1:<seq>', got {value!r}")
    return int(m.group(1))


def principal_key(tenant: str, user: str) -> str:
    """sha256 of the canonical JSON of the (tenant, user) tuple."""
    canonical = json.dumps([str(tenant), str(user)], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class AttentionPreferenceStore:
    """Seen cursors of ONE principal in ONE plane."""

    def __init__(self, plane_data_dir: Path, *, tenant: str, user: str) -> None:
        self.tenant = str(tenant)
        self.user = str(user)
        self.dir = Path(plane_data_dir) / "automations" / "attention"
        self.path = self.dir / f"{principal_key(self.tenant, self.user)}.json"

    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "principal": [self.tenant, self.user], "automations": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("principal") != [self.tenant, self.user]:
            raise AttentionStoreCorrupt(f"{self.path} does not belong to principal {[self.tenant, self.user]!r}")
        if not isinstance(data.get("automations"), dict):
            data["automations"] = {}
        return data

    def _write(self, data: Dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".attention_", suffix=".json", dir=str(self.dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def seen_seq(self, automation_id: str) -> int:
        """The highest attention seq this principal has seen (0 = none)."""
        with _lock_for(self.path):
            row = self._read()["automations"].get(str(automation_id))
        if not isinstance(row, dict):
            return 0
        return parse_attention_cursor(row.get("attention_cursor"))

    def mark_seen(self, automation_id: str, seq: int) -> Tuple[int, bool]:
        """Keep max(stored, seq); returns (stored seq, changed)."""
        with _lock_for(self.path):
            data = self._read()
            row = data["automations"].get(str(automation_id))
            current = parse_attention_cursor(row.get("attention_cursor")) if isinstance(row, dict) else 0
            if int(seq) <= current:
                return current, False
            data["automations"][str(automation_id)] = {"attention_cursor": format_attention_cursor(seq), "updated_at": _now()}
            data["schema_version"] = SCHEMA_VERSION
            data["principal"] = [self.tenant, self.user]
            self._write(data)
            return int(seq), True


def attention_store_for(svc: Any, principal: Any) -> AttentionPreferenceStore:
    """The store for `principal` in the plane `svc` serves (the principal's own
    data dir under multi-user; the shared default plane otherwise — the file
    name still separates principals there)."""
    tenant = str(getattr(principal, "tenant_id", None) or "default")
    user = str(getattr(principal, "user_id", None) or "")
    if not user:
        raise ValueError("attention preferences need an identified principal (user_id)")
    return AttentionPreferenceStore(Path(svc.config.data_dir), tenant=tenant, user=user)


__all__ = [
    "ATTENTION_CURSOR_PREFIX",
    "AttentionCursorError",
    "AttentionPreferenceStore",
    "AttentionStoreCorrupt",
    "attention_store_for",
    "format_attention_cursor",
    "parse_attention_cursor",
    "principal_key",
]
