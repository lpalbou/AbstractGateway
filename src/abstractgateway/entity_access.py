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
