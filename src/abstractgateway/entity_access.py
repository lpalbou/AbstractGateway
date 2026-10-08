"""Who may see which entity (operator ruling 2026-10-01, RBAC).

    An ADMIN sees every user and every entity. A NON-ADMIN sees only themself and the entities
    THEY created.

The creator is the manifest's `created_by` ({tenant_id, user_id}), written by `POST /entities`
from the authenticated principal. An entity whose manifest has no `created_by` (every home born
before the field existed) is visible to admins only: a creator is never guessed and no existing
manifest is rewritten.

Enforced in the API, not only in the UI:

- `GET /entities` lists only the visible homes;
- every `/entities/{name}/...` route (both entity routers) runs `require_entity_visible` first,
  as a router dependency, so no per-route check can be forgotten;
- `POST /entities/meets/open` checks both entities;
- `GET /me/accounts` and `GET /me/accounts/{id}/activity` (routes/gateway.py) answer self + own
  entities.

A hidden entity answers EXACTLY like a missing one (404, the registry's own not-found sentence),
so a non-admin cannot probe which names exist through these routes. Creating an entity under a
name another account already holds is the one place a name's existence shows (names are unique
per gateway): 409 "That name is taken". Entity names are DOOR-GLOBAL principals (users file)
while homes live per runtime plane, so the check is two-part (`name_taken_detail`): a home with
that name in the caller's plane that the caller may not see, OR no home in the caller's plane but
a door-wide user record with that name (an entity living in another plane, or a human account):
creating there would adopt that principal.

A gateway without user accounts (the single-operator gateway, one shared world) shows everything
to every caller, as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from fastapi import Request

from .security.principal import GatewayPrincipal, current_gateway_principal, safe_principal_component

NAME_TAKEN = "That name is taken: another account already has an entity called {slug!r}. Pick another name."
ACCOUNT_NAME_TAKEN = "That name is taken: an account is already called {slug!r}. Pick another name."


def creator_of(principal: Optional[GatewayPrincipal]) -> Optional[dict]:
    """The `created_by` value for an entity this principal creates (None without a principal)."""
    if principal is None or not str(principal.user_id or "").strip():
        return None
    return {
        "tenant_id": safe_principal_component(principal.tenant_id or "default", default="default"),
        "user_id": str(principal.user_id).strip(),
    }


def sees_every_entity(principal: Optional[GatewayPrincipal]) -> bool:
    """Admins see every entity; so does everyone on a gateway WITHOUT user accounts (one shared
    world: the operator's, as before — e.g. the loopback dev-read principal)."""
    if principal is None or principal.is_admin():
        return True
    from .service import gateway_multi_user_enabled

    return not gateway_multi_user_enabled()


def same_account(principal: GatewayPrincipal, created_by: Mapping[str, Any]) -> bool:
    return str(created_by.get("user_id") or "").strip() == str(principal.user_id or "").strip() and (
        safe_principal_component(created_by.get("tenant_id") or "default", default="default")
        == safe_principal_component(principal.tenant_id or "default", default="default")
    )


def entity_visible_to(principal: Optional[GatewayPrincipal], slug: str, created_by: Any) -> bool:
    """True when `principal` may see the entity `slug` whose manifest carries `created_by`."""
    if sees_every_entity(principal):
        return True
    assert principal is not None
    # An entity principal is an account too: it sees itself.
    if "entity" in {str(r).strip().lower() for r in principal.roles} and str(principal.user_id) == str(slug):
        return True
    if not isinstance(created_by, Mapping) or not str(created_by.get("user_id") or "").strip():
        return False  # legacy / unknown creator: admins only
    return same_account(principal, created_by)


def manifest_creator(entities_dir: Path, slug: str) -> tuple[bool, Any]:
    """(home exists, its manifest's created_by). A home with an unreadable manifest counts as
    existing with no creator (admins only)."""
    path = Path(entities_dir) / slug / "manifest.json"
    if not path.exists():
        return False, None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, None
    return True, (doc.get("created_by") if isinstance(doc, dict) else None)


def not_found_detail(entities_dir: Path, slug: str) -> str:
    # The registry's own sentence (entities.EntityRegistry.manifest_for): hidden == missing.
    return f"entity {slug!r} not found under {entities_dir}"


def require_entity_visible(name: str, *, registry: Any = None) -> None:
    """Raise 404 (the missing-entity answer) when the caller may not see entity `name`.
    A name with no home passes: the route answers its own 404/400."""
    from fastapi import HTTPException

    principal = current_gateway_principal()
    if sees_every_entity(principal):
        return
    from .entities import entity_slug

    try:
        slug = entity_slug(name)
    except Exception:  # noqa: BLE001 - an invalid name is the route's own 400
        return
    if registry is None:
        from .routes.entities import _registry

        registry = _registry()
    exists, created_by = manifest_creator(registry.entities_dir, slug)
    if exists and not entity_visible_to(principal, slug, created_by):
        raise HTTPException(status_code=404, detail=not_found_detail(registry.entities_dir, slug))


def entity_plane_owner(slug: str) -> Optional[GatewayPrincipal]:
    """The principal whose runtime plane holds entity `slug`'s home (R16.5, "admins always"):
    the default plane's operator for `<data_dir>/entities`, else the account whose runtime is
    `<data_dir>/users/<tenant>/<runtime>`. None when no plane holds it."""
    from .users import GatewayUserRegistry, gateway_data_dir_from_env

    root = gateway_data_dir_from_env()
    for entities_dir in entities_dirs():
        exists, _created_by = manifest_creator(entities_dir, slug)
        if not exists:
            continue
        if entities_dir == root / "entities":
            return GatewayPrincipal(
                user_id="admin", tenant_id="default", roles=("admin", "user"), scopes=("*",), runtime_id="default", source="entity-plane"
            )
        runtime_root = entities_dir.parent.parent  # <root>/users/<tenant>/<runtime>
        tenant, runtime = runtime_root.parent.name, runtime_root.name
        for rec in GatewayUserRegistry().list_users():
            if rec.principal_kind == "entity":
                continue
            if safe_principal_component(rec.tenant_id or "default", default="default") == tenant and (
                safe_principal_component(rec.runtime_id or rec.user_id, default=rec.user_id) == runtime
            ):
                return rec.to_principal()
        return None
    return None


def _admin_plane_owner_for(principal: Optional[GatewayPrincipal], name: str) -> Optional[GatewayPrincipal]:
    """For an ADMIN on a multi-user gateway: the owner of the plane holding entity `name` when it
    is not the admin's own plane; else None (the admin's own service serves it)."""
    from .service import gateway_multi_user_enabled

    if principal is None or not principal.is_admin() or not gateway_multi_user_enabled():
        return None
    from .entities import entity_slug

    try:
        slug = entity_slug(name)
    except Exception:  # noqa: BLE001 - an invalid name is the route's own 400
        return None
    from .routes.entities import _registry

    if manifest_creator(_registry().entities_dir, slug)[0]:
        return None  # the admin's own plane holds it
    return entity_plane_owner(slug)


async def entity_plane_resolver(request: Request) -> None:
    """Router dependency, FIRST on both entity routers (async, so what it sets reaches the
    sync dependencies and handlers of this request): an admin acting on an entity that lives in
    another account's plane is served by that plane (R16.5, operator ruling 2026-10-08: "admins
    always"); everyone else is untouched."""
    name = request.path_params.get("name")
    if name is None:
        return
    principal = getattr(request.state, "gateway_principal", None) or current_gateway_principal()
    import asyncio

    owner = await asyncio.to_thread(_admin_plane_owner_for, principal, str(name))
    if owner is not None:
        from .service import set_entity_plane_owner

        set_entity_plane_owner(owner)


def entity_plane_for(slug: str):
    """Context manager for in-process entity acts outside the entity routers (Accounts'
    Active / archive / unarchive): an admin acts in the plane that holds `slug`."""
    from contextlib import nullcontext

    from .service import using_entity_plane

    owner = _admin_plane_owner_for(current_gateway_principal(), slug)
    return using_entity_plane(owner) if owner is not None else nullcontext()


def entity_name_guard(request: Request) -> None:
    """Router dependency for both entity routers: every route with a `{name}` path parameter is
    checked before its handler runs."""
    name = request.path_params.get("name")
    if name is None:
        return
    require_entity_visible(str(name))


def name_taken_detail(caller: Optional[GatewayPrincipal], registry: Any, slug: str) -> Optional[str]:
    """The 409 sentence when `caller` may not create entity `slug`, else None.

    - A home in the caller's plane: taken unless the caller may see it (its creator, an admin, a
      single-user gateway) — re-creating your own entity stays the idempotent re-summon.
    - No home in the caller's plane but a door-wide user record with that name: taken, for
      everyone (an admin included). The record is an entity whose home is in another plane, or a
      human account; `create` would adopt it as this entity's principal.
    - Neither: free (a legacy home whose principal was never minted mints it on create)."""
    exists, created_by = manifest_creator(registry.entities_dir, slug)
    if exists:
        return None if entity_visible_to(caller, slug, created_by) else NAME_TAKEN.format(slug=slug)
    record = registry.door_principal(slug)
    if record is None:
        return None
    if record.principal_kind == "entity":
        return NAME_TAKEN.format(slug=slug)
    return ACCOUNT_NAME_TAKEN.format(slug=slug)


# ---------------------------------------------------------------------------------------
# Archived entities (round 3: accounts are archived, never deleted)
# ---------------------------------------------------------------------------------------

ENTITY_ARCHIVED = "{slug} is archived: it can't act or wake. An admin can unarchive it from Accounts."


class EntityArchivedError(RuntimeError):
    """An archived entity was asked to wake, act, be summoned, visited or run its loop."""

    def __init__(self, slug: str) -> None:
        self.slug = str(slug)
        self.message = ENTITY_ARCHIVED.format(slug=self.slug)
        super().__init__(self.message)


def entity_archived(slug: str, *, users_path: Optional[Path] = None) -> bool:
    """THE predicate (one rule for every wake entry point): True when the entity `slug` is
    archived. The door's users file is the record (`archived` on the entity principal); an
    entity whose home predates principals has no record, so its archive mark lives in the
    Accounts suspend store (`auth/entity_suspended.json`, `archived: true`). A users file that
    cannot be read raises (an unknown answer never lets an archived entity wake)."""
    from .users import GatewayUserRegistry

    key = str(slug or "").strip()
    if not key:
        return False
    rec = GatewayUserRegistry(path=users_path).get_user(key)
    if rec is not None and rec.principal_kind == "entity" and rec.archived:
        return True
    from .admin_accounts import suspended_record

    mark = suspended_record(key)
    return bool(mark and mark.get("archived"))


def refuse_if_entity_archived(slug: str, *, users_path: Optional[Path] = None) -> None:
    if entity_archived(slug, users_path=users_path):
        raise EntityArchivedError(slug)


def users_path_of(registry: Any) -> Optional[Path]:
    """The users file an EntityRegistry authenticates against (`_principal_registry_path`), or
    None (= the gateway's users file) for a registry stand-in that has none."""
    fn = getattr(registry, "_principal_registry_path", None)
    return fn() if callable(fn) else None


def entities_dirs() -> list[Path]:
    """Every runtime's `entities/` dir on this gateway: the default runtime's
    (`<data_dir>/entities`) and each user runtime's (`<data_dir>/users/<tenant>/<runtime>/runtime/entities`).
    ONE listing for the Accounts census and the entity mailbox resolver."""
    from .users import gateway_data_dir_from_env

    root = gateway_data_dir_from_env()
    dirs: list[Path] = [root / "entities"]
    users = root / "users"
    if users.is_dir():
        dirs.extend(sorted(users.glob("*/*/runtime/entities")))
    return dirs


def entity_home_dir(slug: str) -> Optional[Path]:
    """The home of entity `slug` on this gateway (`<creator runtime data_dir>/entities/<slug>/`,
    the first runtime holding a manifest for it), or None. The entity's mailbox plane is rooted
    here (mail/accounts.py `plane_for_principal`)."""
    key = safe_principal_component(slug, default="")
    if not key:
        return None
    for entities_dir in entities_dirs():
        home = entities_dir / key
        if (home / "manifest.json").is_file():
            return home
    return None
