"""The Accounts page's backend (DESIGN-v2 §2, §6): users and entities in one list, the Active
switch for both, and what each row's actions can do (with the reason when they can't).

    GET  /api/gateway/admin/accounts                 list_accounts()
    PUT  /api/gateway/admin/accounts/{id}/active     set_active()

Email address and mailbox come from ONE resolver, `mail.accounts.account_email_view`, the
function `GET /me/email` uses too, so an admin's row and card never disagree (item 4).

Entities: Active OFF = suspend (registry `enabled=false` AND entity state `paused`, through the
entities lane's own `POST /entities/{name}/state` handler so an open visit is torn down); ON =
resume (`enabled=true` and the state the entity had before, written through the registry's
single state writer). The previous state is stored at suspend in
`<data_dir>/auth/entity_suspended.json` (a new, additive file; nothing else is rewritten).
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .security.principal import GatewayPrincipal, safe_principal_component
from .users import GatewayUserRecord, GatewayUserRegistry, gateway_data_dir_from_env

SUSPEND_FILE = ("auth", "entity_suspended.json")
_SUSPEND_LOCK = threading.Lock()

CANNOT_DEACTIVATE_SELF = "You can't deactivate your own account."
REASON_OWN_DELETE = "You can't delete your own account."
REASON_ENTITY_DELETE = "An entity's name is kept for life; suspend it instead."
REASON_ENTITY_ROTATE = (
    "An entity has no token to rotate: its credential is discarded when it is created and a fresh one "
    "is bound only while it is summoned."
)
REASON_USER_MANAGE = "Only entities have a management page."
REASON_ENTITY_NO_HOME = "This entity's home is not on this gateway's runtime, so it can't be managed here."
REASON_LAST_ADMIN = "This is the last active admin account; make another account admin first."
REASON_ENTITY_EMAIL = "Entities can't have their own mailbox yet: mailboxes belong to a user's runtime."

_ROLE_ORDER = {"admin": 0, "user": 1, "entity": 2}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _act(available: bool, reason: Optional[str] = None) -> Dict[str, Any]:
    return {"available": bool(available), "reason": None if available else reason}


def _same(principal: GatewayPrincipal, user_id: str, tenant_id: str) -> bool:
    return safe_principal_component(principal.user_id, default="") == safe_principal_component(user_id, default="") and (
        safe_principal_component(principal.tenant_id or "default", default="default")
        == safe_principal_component(tenant_id or "default", default="default")
    )


def _is_admin_record(rec: GatewayUserRecord) -> bool:
    return "admin" in {str(r).strip() for r in rec.roles}


def _last_enabled_admin(rec: GatewayUserRecord, records: List[GatewayUserRecord]) -> bool:
    if not (rec.enabled and _is_admin_record(rec)) or rec.principal_kind == "entity":
        return False
    return not any(
        o.key != rec.key and o.enabled and _is_admin_record(o) and o.principal_kind != "entity" for o in records
    )


# ---------------------------------------------------------------------------------------
# Entity suspend store
# ---------------------------------------------------------------------------------------


def _suspend_path() -> Path:
    return gateway_data_dir_from_env().joinpath(*SUSPEND_FILE)


def _read_suspended() -> Dict[str, Any]:
    try:
        doc = json.loads(_suspend_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _write_suspended(doc: Dict[str, Any]) -> None:
    path = _suspend_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def suspended_record(slug: str) -> Optional[Dict[str, Any]]:
    rec = _read_suspended().get(str(slug))
    return rec if isinstance(rec, dict) else None


# ---------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------


def _entity_homes() -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """The entity census of the calling admin's runtime (`GET /entities`): slug -> summary.
    A census that cannot be read is reported (`entities_warning`), never hidden."""

    try:
        from .routes.entities import _registry

        rows = _registry().list_entities()
    except Exception as exc:  # noqa: BLE001
        return {}, f"The entity list could not be read: {exc}"
    return {str(r.get("slug")): r for r in rows if isinstance(r, dict) and r.get("slug") and not r.get("error")}, None


def _user_row(rec: GatewayUserRecord, caller: GatewayPrincipal, records: List[GatewayUserRecord]) -> Dict[str, Any]:
    from .mail.accounts import account_email_view

    own = _same(caller, rec.user_id, rec.tenant_id)
    view = account_email_view(rec.to_principal())
    last_admin = _last_enabled_admin(rec, records)
    if own:
        suspend = _act(False, CANNOT_DEACTIVATE_SELF)
        delete = _act(False, REASON_OWN_DELETE)
    elif last_admin:
        suspend = _act(False, REASON_LAST_ADMIN)
        delete = _act(False, REASON_LAST_ADMIN)
    else:
        suspend = _act(True)
        delete = _act(True)
    return {
        "id": rec.user_id,
        "tenant_id": rec.tenant_id,
        "kind": "user",
        "role": "admin" if _is_admin_record(rec) else "user",
        "own": own,
        "email_address": view["email_address"],
        "mailbox": view["mailbox"],
        "runtime_id": rec.runtime_id or rec.user_id,
        "active": bool(rec.enabled),
        "entity_state": None,
        "actions": {
            "email": _act(True),
            "logs": _act(True),
            "workspace": _act(True),
            "rotate": _act(True),
            "manage": _act(False, REASON_USER_MANAGE),
            "delete": delete,
            "suspend": suspend,
        },
    }


def _entity_row(slug: str, rec: Optional[GatewayUserRecord], home: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    state = None
    if home is not None:
        st = home.get("state")
        state = str((st or {}).get("state") or "awake") if isinstance(st, dict) else None
    enabled = bool(rec.enabled) if rec is not None else True
    has_home = home is not None
    return {
        "id": slug,
        "tenant_id": rec.tenant_id if rec is not None else "default",
        "kind": "entity",
        "role": "entity",
        "own": False,
        "email_address": (str(rec.email or "").strip().lower() or None) if rec is not None else None,
        "mailbox": {"state": "unavailable", "address": None, "provider": None, "reason": REASON_ENTITY_EMAIL},
        "runtime_id": (rec.runtime_id or rec.user_id) if rec is not None else slug,
        "active": bool(enabled and state != "paused"),
        "entity_state": state,
        "actions": {
            "email": _act(False, REASON_ENTITY_EMAIL),
            "logs": _act(True),
            "workspace": _act(has_home, REASON_ENTITY_NO_HOME),
            "rotate": _act(False, REASON_ENTITY_ROTATE),
            "manage": _act(has_home, REASON_ENTITY_NO_HOME),
            "delete": _act(False, REASON_ENTITY_DELETE),
            "suspend": _act(has_home or rec is not None, REASON_ENTITY_NO_HOME),
        },
    }


def _sort_key(row: Dict[str, Any]) -> Tuple[int, str, str]:
    return (_ROLE_ORDER.get(str(row.get("role")), 3), str(row.get("id") or ""), str(row.get("tenant_id") or ""))


def list_accounts(caller: GatewayPrincipal) -> Dict[str, Any]:
    records = GatewayUserRegistry().list_users()
    homes, warning = _entity_homes()
    rows: List[Dict[str, Any]] = []
    seen_entities: set = set()
    for rec in records:
        if rec.principal_kind == "entity":
            seen_entities.add(rec.user_id)
            rows.append(_entity_row(rec.user_id, rec, homes.get(rec.user_id)))
        else:
            rows.append(_user_row(rec, caller, records))
    for slug, home in homes.items():
        if slug not in seen_entities:
            # A home created before entity principals were minted: no registry row, still an entity.
            rows.append(_entity_row(slug, None, home))
    rows.sort(key=_sort_key)
    out: Dict[str, Any] = {"accounts": rows}
    if warning:
        out["entities_warning"] = warning
    return out


def account_row(caller: GatewayPrincipal, account_id: str, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
    for row in list_accounts(caller)["accounts"]:
        if row["id"] == account_id and (row["tenant_id"] == tenant_id or row["kind"] == "entity"):
            return row
    return None


# ---------------------------------------------------------------------------------------
# Active switch
# ---------------------------------------------------------------------------------------


class AccountError(Exception):
    def __init__(self, status: int, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.reason_code = reason_code
        self.message = message


def _suspend_entity(slug: str, actor: str) -> None:
    from .routes.entities import SetEntityStateRequest, _registry, set_entity_state

    try:
        before = str((_registry().state_of(slug) or {}).get("state") or "awake")
    except KeyError:
        before = None  # no home here: only the door credential is switched off
    if before is not None:
        with _SUSPEND_LOCK:
            doc = _read_suspended()
            if str(slug) not in doc:
                doc[str(slug)] = {
                    "previous_state": before if before in ("awake", "asleep") else "awake",
                    "suspended_at": _now_iso(),
                    "by": actor,
                }
                _write_suspended(doc)
        if before != "paused":
            # The entities lane's own door: tears an open visit down, host-marks the moment.
            set_entity_state(slug, SetEntityStateRequest(state="paused", reason="Suspended from Accounts"))


def _resume_entity(slug: str, actor: str) -> None:
    from .routes.entities import _registry

    with _SUSPEND_LOCK:
        doc = _read_suspended()
        stored = doc.pop(str(slug), None)
        if stored is not None:
            _write_suspended(doc)
    try:
        current = str((_registry().state_of(slug) or {}).get("state") or "awake")
    except KeyError:
        current = None
    if current == "paused":
        previous = str((stored or {}).get("previous_state") or "awake")
        # The single state writer (not the operator sleep door: resuming must not disarm grants).
        _registry().set_state(name=slug, state=previous, reason=f"Resumed from Accounts [by {actor}]")


def set_active(caller: GatewayPrincipal, account_id: str, *, active: bool, tenant_id: str = "default") -> Dict[str, Any]:
    registry = GatewayUserRegistry()
    records = registry.list_users()
    rec = next((r for r in records if r.user_id == account_id and (r.tenant_id == tenant_id or r.principal_kind == "entity")), None)
    actor = f"person:{caller.user_id}"
    if rec is not None and rec.principal_kind != "entity":
        if not active and _same(caller, rec.user_id, rec.tenant_id):
            raise AccountError(409, "cannot_deactivate_self", CANNOT_DEACTIVATE_SELF)
        if not active and _last_enabled_admin(rec, records):
            raise AccountError(409, "last_admin", REASON_LAST_ADMIN)
        registry.update_user(user_id=rec.user_id, tenant_id=rec.tenant_id, enabled=bool(active))
    else:
        homes, _warning = _entity_homes()
        if rec is None and account_id not in homes:
            raise AccountError(404, "account_not_found", f"There is no account named {account_id!r} on this gateway.")
        if active:
            if rec is not None:
                registry.update_user(user_id=rec.user_id, tenant_id=rec.tenant_id, enabled=True)
            if account_id in homes:
                _resume_entity(account_id, actor)
        else:
            if account_id in homes:
                _suspend_entity(account_id, actor)
            if rec is not None:
                registry.update_user(user_id=rec.user_id, tenant_id=rec.tenant_id, enabled=False)
    row = account_row(caller, account_id, rec.tenant_id if rec is not None else tenant_id)
    if row is None:
        raise AccountError(404, "account_not_found", f"There is no account named {account_id!r} on this gateway.")
    return row
