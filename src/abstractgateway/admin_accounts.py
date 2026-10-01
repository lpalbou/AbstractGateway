"""The Accounts page's backend (DESIGN-v2 §2, §6): users and entities in one list, the Active
switch for both, and what each row's actions can do (with the reason when they can't).

    GET  /api/gateway/admin/accounts                 list_accounts()  (?include_archived=true)
    PUT  /api/gateway/admin/accounts/{id}/active     set_active()
    POST /api/gateway/admin/accounts/{id}/archive    archive_account()   (admin: any account)
    POST /api/gateway/admin/accounts/{id}/unarchive  unarchive_account() (admin only)
    POST /api/gateway/me/accounts/{id}/archive       archive_account()   (non-admin: an entity they created)

Accounts are ARCHIVED, never deleted (round 3, operator decision): an archived user can't sign in
(`users.json` `archived`, refused by `authenticate`); an archived entity is suspended (paused,
door credential off) and never wakes (`entity_access.entity_archived`, checked at every wake
entry point). Records, runtimes, runs and history are kept. Unarchive leaves the account
inactive; an admin turns Active on.

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
REASON_OWN_ARCHIVE = "You can't archive your own account."
REASON_ARCHIVED = "Archived accounts stay inactive: unarchive it first."
REASON_NOT_ARCHIVED = "This account isn't archived."
REASON_ADMIN_UNARCHIVE = "Only an admin can unarchive an account."
REASON_ENTITY_ROTATE = (
    "An entity has no token to rotate: its credential is discarded when it is created and no one holds it."
)
REASON_USER_MANAGE = "Only entities have a management page."
REASON_ENTITY_NO_HOME = "This entity's home is not on this gateway's runtime, so it can't be managed here."
REASON_LAST_ADMIN = "This is the last active admin account; make another account admin first."
# Non-admin rows (GET /me/accounts): what only an admin can do, said once per action.
REASON_ADMIN_ROTATE = "Only an admin can rotate your token."
REASON_ADMIN_SUSPEND_ENTITY = "Only an admin can suspend an entity."

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
    archived = bool(rec.archived)
    if archived:
        suspend = _act(False, REASON_ARCHIVED)
        archive = _act(False, REASON_ARCHIVED)
    elif own:
        suspend = _act(False, CANNOT_DEACTIVATE_SELF)
        archive = _act(False, REASON_OWN_ARCHIVE)
    elif last_admin:
        suspend = _act(False, REASON_LAST_ADMIN)
        archive = _act(False, REASON_LAST_ADMIN)
    else:
        suspend = _act(True)
        archive = _act(True)
    return {
        "id": rec.user_id,
        "tenant_id": rec.tenant_id,
        "kind": "user",
        "role": "admin" if _is_admin_record(rec) else "user",
        "own": own,
        "email_address": view["email_address"],
        "mailbox": view["mailbox"],
        "runtime_id": rec.runtime_id or rec.user_id,
        "active": bool(rec.enabled) and not archived,
        "entity_state": None,
        "archived": archived,
        "archived_at": rec.archived_at or None,
        "actions": {
            "email": _act(not archived, REASON_ARCHIVED),
            "logs": _act(True),
            "workspace": _act(not archived, REASON_ARCHIVED),
            "rotate": _act(not archived, REASON_ARCHIVED),
            "manage": _act(False, REASON_USER_MANAGE),
            "archive": archive,
            "unarchive": _act(archived, REASON_NOT_ARCHIVED),
            "suspend": suspend,
        },
    }


def _entity_row(
    slug: str,
    rec: Optional[GatewayUserRecord],
    home: Optional[Dict[str, Any]],
    created_by: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    state = None
    if home is not None:
        st = home.get("state")
        state = str((st or {}).get("state") or "awake") if isinstance(st, dict) else None
    enabled = bool(rec.enabled) if rec is not None else True
    has_home = home is not None
    archived = _entity_is_archived(slug, rec)
    view = _entity_email_view(slug, rec)
    return {
        "id": slug,
        "tenant_id": rec.tenant_id if rec is not None else "default",
        "kind": "entity",
        "role": "entity",
        "own": False,
        "email_address": view["email_address"],
        "mailbox": view["mailbox"],
        "runtime_id": (rec.runtime_id or rec.user_id) if rec is not None else slug,
        "active": bool(enabled and state != "paused") and not archived,
        "archived": archived,
        "archived_at": _entity_archived_at(slug, rec),
        "entity_state": state,
        # Who created it ({tenant_id, user_id}); null for an entity created before creators were
        # recorded (admins only see those).
        "created_by": created_by,
        "actions": {
            "email": _act(has_home and not archived, REASON_ARCHIVED if archived else REASON_ENTITY_NO_HOME),
            "logs": _act(True),
            "workspace": _act(has_home and not archived, REASON_ARCHIVED if archived else REASON_ENTITY_NO_HOME),
            "rotate": _act(False, REASON_ENTITY_ROTATE),
            "manage": _act(has_home and not archived, REASON_ARCHIVED if archived else REASON_ENTITY_NO_HOME),
            "archive": _act(not archived, REASON_ARCHIVED),
            "unarchive": _act(archived, REASON_NOT_ARCHIVED),
            "suspend": _act((has_home or rec is not None) and not archived, REASON_ARCHIVED if archived else REASON_ENTITY_NO_HOME),
        },
    }


def _entity_is_archived(slug: str, rec: Optional[GatewayUserRecord]) -> bool:
    if rec is not None and rec.archived:
        return True
    mark = suspended_record(slug)
    return bool(mark and mark.get("archived"))


def _entity_archived_at(slug: str, rec: Optional[GatewayUserRecord]) -> Optional[str]:
    if rec is not None and rec.archived:
        return rec.archived_at or None
    mark = suspended_record(slug) or {}
    return (str(mark.get("archived_at") or "") or None) if mark.get("archived") else None


def _entity_email_view(slug: str, rec: Optional[GatewayUserRecord]) -> Dict[str, Any]:
    """The entity's address and mailbox through the ONE resolver (`account_email_view`): an entity
    is an AI user with its own mailbox (round 3 §3). A home without a principal record has no
    plane to resolve from: no address, not connected."""
    from .mail.accounts import account_email_view

    if rec is None:
        return {"email_address": None, "mailbox": {"state": "not_connected", "address": None, "provider": None, "reason": None}}
    return account_email_view(rec.to_principal())


def _sort_key(row: Dict[str, Any]) -> Tuple[int, str, str]:
    return (_ROLE_ORDER.get(str(row.get("role")), 3), str(row.get("id") or ""), str(row.get("tenant_id") or ""))


def _entity_creators() -> Dict[str, Dict[str, Any]]:
    """slug -> created_by over EVERY runtime on this gateway (the admin's and each user's), from
    the homes' manifests (read-only). An admin sees all entities, including ones whose home is in
    another user's runtime."""
    from .entity_access import entities_dirs, manifest_creator

    out: Dict[str, Dict[str, Any]] = {}
    for entities_dir in entities_dirs():
        if not entities_dir.is_dir():
            continue
        for child in sorted(entities_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            _exists, created_by = manifest_creator(entities_dir, child.name)
            if isinstance(created_by, dict) and created_by.get("user_id") and child.name not in out:
                out[child.name] = {
                    "tenant_id": str(created_by.get("tenant_id") or "default"),
                    "user_id": str(created_by["user_id"]),
                }
    return out


def _creator(slug: str, home: Optional[Dict[str, Any]], creators: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if home is not None and isinstance(home.get("created_by"), dict):
        return dict(home["created_by"])
    return creators.get(slug)


def list_accounts(caller: GatewayPrincipal, *, include_archived: bool = False) -> Dict[str, Any]:
    records = GatewayUserRegistry().list_users()
    homes, warning = _entity_homes()
    creators = _entity_creators()
    rows: List[Dict[str, Any]] = []
    seen_entities: set = set()
    for rec in records:
        if rec.principal_kind == "entity":
            seen_entities.add(rec.user_id)
            home = homes.get(rec.user_id)
            rows.append(_entity_row(rec.user_id, rec, home, _creator(rec.user_id, home, creators)))
        else:
            rows.append(_user_row(rec, caller, records))
    for slug, home in homes.items():
        if slug not in seen_entities:
            # A home created before entity principals were minted: no registry row, still an entity.
            rows.append(_entity_row(slug, None, home, _creator(slug, home, creators)))
    if not include_archived:
        rows = [r for r in rows if not r["archived"]]
    rows.sort(key=_sort_key)
    out: Dict[str, Any] = {"accounts": rows}
    if warning:
        out["entities_warning"] = warning
    return out


def list_my_accounts(caller: GatewayPrincipal) -> Dict[str, Any]:
    """`GET /me/accounts`, for ANY signed-in account (operator ruling 2026-10-01): the caller's own
    row plus the entities the caller created — never another user, never an entity someone else
    (or no recorded creator) made. Same row shape as `GET /admin/accounts`; actions only an admin
    can take are unavailable with the reason. An admin sees everything on `GET /admin/accounts`."""
    from .entity_access import entity_visible_to, same_account

    registry = GatewayUserRegistry()
    records = registry.list_users()
    rows: List[Dict[str, Any]] = []
    me = next((r for r in records if _same(caller, r.user_id, r.tenant_id)), None)
    if me is not None and me.principal_kind != "entity" and not me.archived:
        row = _user_row(me, caller, records)
        if not caller.is_admin():
            row["actions"]["rotate"] = _act(False, REASON_ADMIN_ROTATE)
        rows.append(row)
    homes, warning = _entity_homes()  # the caller's own runtime: where its entities live
    by_id = {r.user_id: r for r in records if r.principal_kind == "entity"}
    for slug, home in homes.items():
        created_by = home.get("created_by")
        if caller.is_admin():
            # "Entities they created" holds for admins too here; the full list is /admin/accounts.
            if not (isinstance(created_by, dict) and same_account(caller, created_by)):
                continue
        elif not (isinstance(created_by, dict) and entity_visible_to(caller, slug, created_by)):
            continue
        row = _entity_row(slug, by_id.get(slug), home, dict(created_by))
        if row["archived"]:
            continue  # archived accounts are never listed here (A16); admins: /admin/accounts
        if not caller.is_admin():
            row["actions"]["suspend"] = _act(False, REASON_ADMIN_SUSPEND_ENTITY)
            row["actions"]["unarchive"] = _act(False, REASON_ADMIN_UNARCHIVE)
        rows.append(row)
    rows.sort(key=_sort_key)
    out: Dict[str, Any] = {"accounts": rows, "scope": "own"}
    if warning:
        out["entities_warning"] = warning
    return out


def my_account_ids(caller: GatewayPrincipal) -> List[Tuple[str, str]]:
    """(id, tenant_id) of every row `GET /me/accounts` answers: the ids whose activity the caller
    may read."""
    return [(str(r["id"]), str(r["tenant_id"] or "default")) for r in list_my_accounts(caller)["accounts"]]


def account_row(caller: GatewayPrincipal, account_id: str, tenant_id: str = "default") -> Optional[Dict[str, Any]]:
    for row in list_accounts(caller, include_archived=True)["accounts"]:
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
    if active and (
        (rec is not None and rec.archived) or (rec is None and bool((suspended_record(account_id) or {}).get("archived")))
    ):
        raise AccountError(409, "archived", f"{account_id} is archived: unarchive it first, then turn Active on.")
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


# ---------------------------------------------------------------------------------------
# Archive / unarchive (round 3: no pure deletion)
# ---------------------------------------------------------------------------------------


def _find_record(records: List[GatewayUserRecord], account_id: str, tenant_id: str) -> Optional[GatewayUserRecord]:
    return next(
        (r for r in records if r.user_id == account_id and (r.tenant_id == tenant_id or r.principal_kind == "entity")), None
    )


def _mark_entity_archived(slug: str, *, archived: bool, actor: str) -> None:
    """The archive mark in the suspend store (the record for homes without a principal; kept
    beside the registry flag for every entity so the previous state survives for a later
    resume)."""
    with _SUSPEND_LOCK:
        doc = _read_suspended()
        entry = dict(doc.get(str(slug)) or {})
        if archived:
            entry.setdefault("previous_state", "awake")
            entry.setdefault("suspended_at", _now_iso())
            entry.setdefault("by", actor)
            entry.update({"archived": True, "archived_at": _now_iso(), "archived_by": actor})
        else:
            if not entry:
                return
            entry.pop("archived", None)
            entry.pop("archived_at", None)
            entry.pop("archived_by", None)
        doc[str(slug)] = entry
        _write_suspended(doc)


def _stop_entity_loop(slug: str, actor: str) -> None:
    """An archived entity stops acting: its own-time loop gets the durable stop command."""
    try:
        from .entity_loop import stop_loop
        from .routes.entities import _registry

        registry = _registry()
        home_dir = registry.entities_dir / registry.manifest_for(slug).slug
    except Exception:  # noqa: BLE001 - no home on this runtime: nothing runs here
        return
    stop_loop(home_dir, reason="Archived from Accounts", requested_by=actor)


def archive_account(caller: GatewayPrincipal, account_id: str, *, tenant_id: str = "default") -> Dict[str, Any]:
    """Archive a user or an entity. Admin: any account except their own and the last active
    admin. Non-admin: only an entity they created (`entity_visible_to`); anything else answers
    404 like a missing account. Returns the archived row."""
    from .entity_access import entity_visible_to

    registry = GatewayUserRegistry()
    records = registry.list_users()
    rec = _find_record(records, account_id, tenant_id)
    actor = f"person:{caller.user_id}"
    missing = AccountError(404, "account_not_found", f"There is no account named {account_id!r} on this gateway.")
    is_entity = rec is None or rec.principal_kind == "entity"
    homes, _warning = _entity_homes() if is_entity else ({}, None)
    if rec is None and account_id not in homes:
        raise missing
    if not caller.is_admin():
        if not is_entity:
            raise missing
        home = homes.get(account_id)
        created_by = (home or {}).get("created_by") if home is not None else _entity_creators().get(account_id)
        if not (isinstance(created_by, dict) and created_by.get("user_id") and entity_visible_to(caller, account_id, created_by)):
            raise missing
    if not is_entity:
        assert rec is not None
        if rec.archived:
            raise AccountError(409, "already_archived", f"{rec.user_id} is already archived.")
        if _same(caller, rec.user_id, rec.tenant_id):
            raise AccountError(409, "cannot_archive_self", REASON_OWN_ARCHIVE)
        if _last_enabled_admin(rec, records):
            raise AccountError(409, "last_admin", REASON_LAST_ADMIN)
        # Signed out from now on: authenticate() and the session check refuse an archived record.
        registry.set_archived(user_id=rec.user_id, tenant_id=rec.tenant_id, archived=True, actor=actor)
    else:
        if _entity_is_archived(account_id, rec):
            raise AccountError(409, "already_archived", f"{account_id} is already archived.")
        if account_id in homes:
            _suspend_entity(account_id, actor)
            _stop_entity_loop(account_id, actor)
        _mark_entity_archived(account_id, archived=True, actor=actor)
        if rec is not None:
            registry.set_archived(user_id=rec.user_id, tenant_id=rec.tenant_id, archived=True, actor=actor)
    row = account_row(caller, account_id, rec.tenant_id if rec is not None else tenant_id)
    if row is None:
        raise missing
    if not caller.is_admin():
        row["actions"]["unarchive"] = _act(False, REASON_ADMIN_UNARCHIVE)
        row["actions"]["suspend"] = _act(False, REASON_ADMIN_SUSPEND_ENTITY)
    return row


def unarchive_account(caller: GatewayPrincipal, account_id: str, *, tenant_id: str = "default") -> Dict[str, Any]:
    """Unarchive (admin only, enforced by the route): the account comes back INACTIVE — users
    stay `enabled=false`, entities stay paused and disabled; an admin turns Active on."""
    registry = GatewayUserRegistry()
    records = registry.list_users()
    rec = _find_record(records, account_id, tenant_id)
    actor = f"person:{caller.user_id}"
    if rec is None or rec.principal_kind == "entity":
        if not _entity_is_archived(account_id, rec):
            if rec is None and account_id not in _entity_homes()[0]:
                raise AccountError(404, "account_not_found", f"There is no account named {account_id!r} on this gateway.")
            raise AccountError(409, "not_archived", REASON_NOT_ARCHIVED)
        _mark_entity_archived(account_id, archived=False, actor=actor)
        if rec is not None:
            registry.set_archived(user_id=rec.user_id, tenant_id=rec.tenant_id, archived=False, actor=actor)
    else:
        if not rec.archived:
            raise AccountError(409, "not_archived", REASON_NOT_ARCHIVED)
        registry.set_archived(user_id=rec.user_id, tenant_id=rec.tenant_id, archived=False, actor=actor)
    row = account_row(caller, account_id, rec.tenant_id if rec is not None else tenant_id)
    if row is None:
        raise AccountError(404, "account_not_found", f"There is no account named {account_id!r} on this gateway.")
    return row
