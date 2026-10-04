"""Every entity has a gateway account (round 11, DESIGN R11.2).

Entities created since GW-H get their account at creation (`EntityRegistry._ensure_entity_principal`:
user id = the slug, roles ("entity",), scopes its own home, credential discarded). Homes that
predate it (on the operator's gateway: castor, mnemosyne) have none, so the Accounts page could not
offer their workspace action. `ensure_entity_accounts` closes that gap at boot, ONCE per home:

- every runtime's `entities/<slug>/manifest.json` (entity_access.entities_dirs) without a user record
  named `<slug>` gets one, minted exactly like a creation (same registry file, same record shape,
  token discarded);
- a record named `<slug>` that is NOT an entity (a person, or an admin-shaped record) is never adopted
  or changed: it is reported, and the home keeps no account (operator drift to fix by hand);
- each mint is audited (`entity_account_created`, reason `migration`) and idempotent (a second boot
  finds the record and does nothing).
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def _registry_path(data_dir: Path) -> Path:
    """The door's users file, resolved exactly like EntityRegistry._principal_registry_path."""
    raw = os.getenv("ABSTRACTGATEWAY_USERS_FILE")
    if raw and str(raw).strip():
        return Path(str(raw)).expanduser().resolve()
    return Path(data_dir) / "auth" / "users.json"


def _audit(slug: str, *, minted: bool, note: Optional[str] = None) -> None:
    try:
        from .security.gateway_security import _AUDIT_LOCK, _audit_data_dir_from_env, _audit_log_enabled

        if not _audit_log_enabled(default=True):
            return
        entry: Dict[str, Any] = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "event": "entity_account_created" if minted else "entity_account_not_created",
            "entity": slug,
            "reason": "migration",
            "actor": "system:entity_account_migration",
        }
        if note:
            entry["note"] = note
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _AUDIT_LOCK:
            path = (_audit_data_dir_from_env() / "audit_log.jsonl").resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as fh:
                fh.write(line.encode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - auditing never breaks the boot
        return


def entity_home_slugs() -> List[str]:
    """Every entity home on this gateway (all runtimes), by slug, first runtime first."""
    from .entity_access import entities_dirs

    out: List[str] = []
    for entities_dir in entities_dirs():
        if not entities_dir.is_dir():
            continue
        for child in sorted(entities_dir.iterdir()):
            if child.is_dir() and not child.name.startswith(".") and (child / "manifest.json").is_file() and child.name not in out:
                out.append(child.name)
    return out


def ensure_entity_accounts(data_dir: Optional[Path] = None) -> List[str]:
    """Mint the missing entity accounts. Returns one sentence per account minted or refused."""
    from .users import GatewayUserRegistry, gateway_data_dir_from_env

    base = Path(data_dir) if data_dir is not None else gateway_data_dir_from_env()
    slugs = entity_home_slugs()
    if not slugs:
        return []
    reg = GatewayUserRegistry(path=_registry_path(base))
    notes: List[str] = []
    for slug in slugs:
        existing = reg.get_user(slug)
        if existing is not None:
            if existing.principal_kind != "entity" or "admin" in existing.roles:
                note = (
                    f"entity {slug!r} has no account of its own: the account named {slug!r} is not an entity account "
                    "and is left as it is"
                )
                notes.append(note)
                _audit(slug, minted=False, note=note)
            continue
        try:
            reg.create_user(user_id=slug, roles=["entity"], scopes=[f"entity:{slug}"], runtime_id=slug)
        except Exception as exc:  # noqa: BLE001 - reported; the next boot retries
            note = f"entity {slug!r}: its account could not be created ({exc})"
            notes.append(note)
            _audit(slug, minted=False, note=note)
            continue
        # The credential returned by create_user is discarded here, as at creation.
        notes.append(f"entity {slug!r} now has a gateway account")
        _audit(slug, minted=True)
    return notes
