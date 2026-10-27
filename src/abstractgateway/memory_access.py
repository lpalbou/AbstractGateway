"""Who may read which memory owner (operator decision 5, memory transition, 2026-10-27).

A person owns their notes and their memory graph; an entity owns its graph; a person reads the graph
of the entities THEY created (entity -> creator delegation); nobody reads another person's graph; an
admin reads everything. The plane (a runtime's data folder) is a storage location, NOT a sharing
scope: two principals whose accounts share a runtime plane still read only their own owners.

ONE rule, `caller_owner_ids`, used by `/kg/query` and every `/memory/*` route:

- an admin principal, or a gateway WITHOUT user accounts (one operator, one world, like
  `entity_access.sees_every_entity`): None = every owner;
- anyone else: the set of owner ids that are theirs:
  * the plane's global owner (`global_memory_owner_id()`), when the plane serving the request is the
    principal's own (its tenant and runtime id are the service's). Planes are per account: the users
    registry refuses a second account on a runtime id (`users._require_runtime_available_unlocked`), and
    the default runtime is the operator's (admins only). So for an account, `global_memory` in its
    plane IS its own global memory; the check keeps that true if a plane is ever served to someone else;
  * for a `run_id` the request names: the run's run/session owners (and the global one, under the
    rule above), only when the run is THEIRS: its `_meta.origin.actor` (stamped by the host on
    `/runs/start`, never client-supplied) is `"<tenant>:<user>"` of the principal, read on the run or
    the nearest ancestor that carries one; a run that is not theirs, or carries no origin (a legacy
    run), is refused (403), a missing run is 404;
  * for a `session_id` the request names: its session owner, only when one of the session's runs
    (the run index, at most `SESSION_SCAN` rows) is theirs by the same origin test;
  * the owner id (`manifest.entity_id`) of every entity in the caller's plane whose manifest
    `created_by` is the principal (`entity_access.same_account`); a legacy entity with no recorded
    creator is the admin's only.

Anything else is 403 `owner_forbidden` (the message never names the owner it protects). `all_owners`
and an explicit `owner_id` outside the set are admin-only: refused, never silently narrowed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Set

from fastapi import HTTPException

from .memory_store import global_memory_owner_id, session_memory_owner_id
from .security.principal import GatewayPrincipal, safe_principal_component

#: How many of a session's runs are checked for ownership (newest first).
SESSION_SCAN = 200
#: How far up a run tree the origin is looked for.
_MAX_ANCESTORS = 64


def owner_forbidden(message: Optional[str] = None) -> HTTPException:
    """The refusal never names the owner it protects: the caller learns only that it is not theirs."""
    return HTTPException(
        status_code=403,
        detail={
            "reason": "owner_forbidden",
            "message": message
            or "not one of this caller's owners (your own runs, sessions and the entities you created; "
            "other owners need an admin principal)",
        },
    )


def principal_actor(principal: GatewayPrincipal) -> str:
    """`"<tenant>:<user>"`, exactly as `/runs/start` stamps `_meta.origin.actor` (routes/gateway.py
    `_stamp_run_origin`)."""
    tenant = str(principal.tenant_id or "").strip() or "default"
    user = str(principal.user_id or "").strip() or "local"
    return f"{tenant}:{user}"


def _origin_actor(run: Any) -> Optional[str]:
    variables = getattr(run, "vars", None)
    meta = variables.get("_meta") if isinstance(variables, dict) else None
    origin = meta.get("origin") if isinstance(meta, dict) else None
    actor = origin.get("actor") if isinstance(origin, dict) else None
    return actor.strip() if isinstance(actor, str) and actor.strip() else None


def run_origin_actor(run: Any, run_store: Any) -> Optional[str]:
    """The actor on whose behalf `run` started: its own `_meta.origin.actor`, else the nearest
    ancestor's (a child run inherits its root's starter). None for a run tree with no origin."""
    current, seen = run, set()
    for _ in range(_MAX_ANCESTORS):
        actor = _origin_actor(current)
        if actor:
            return actor
        parent_id = getattr(current, "parent_run_id", None)
        if not isinstance(parent_id, str) or not parent_id.strip() or parent_id in seen:
            return None
        seen.add(parent_id)
        current = run_store.load(parent_id)
        if current is None:
            return None
    return None


def run_belongs_to(principal: GatewayPrincipal, run: Any, run_store: Any) -> bool:
    return run_origin_actor(run, run_store) == principal_actor(principal)


def session_belongs_to(principal: GatewayPrincipal, session_id: str, run_store: Any) -> bool:
    """True when one of the session's runs (newest `SESSION_SCAN` in the run index) is the principal's."""
    rows = run_store.list_run_index(session_id=session_id, limit=SESSION_SCAN)
    for row in rows or []:
        run_id = row.get("run_id") if isinstance(row, dict) else None
        if not isinstance(run_id, str) or not run_id:
            continue
        run = run_store.load(run_id)
        if run is not None and run_belongs_to(principal, run, run_store):
            return True
    return False


def plane_is_principals_own(principal: GatewayPrincipal, svc: Any) -> bool:
    """The plane serving this request is the principal's own runtime: the service's tenant and runtime id
    are the principal's (the rule `service._config_for_principal` builds planes with)."""
    tenant = safe_principal_component(principal.tenant_id, default="default")
    runtime = safe_principal_component(principal.runtime_id or principal.user_id, default="")
    return bool(runtime) and svc.config.tenant_id == tenant and svc.config.runtime_id == runtime


def owned_entity_owner_ids(principal: GatewayPrincipal, entities_dir: Path) -> Set[str]:
    """`manifest.entity_id` of every entity home under `entities_dir` whose `created_by` is the principal."""
    from .entities import EntityManifest
    from .entity_access import manifest_creator, same_account

    out: Set[str] = set()
    root = Path(entities_dir)
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        exists, created_by = manifest_creator(root, child.name)
        if not exists or not isinstance(created_by, dict) or not same_account(principal, created_by):
            continue
        try:
            manifest = EntityManifest.from_dict(json.loads((child / "manifest.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        if manifest.entity_id:
            out.add(manifest.entity_id)
    return out


def caller_owner_ids(
    principal: GatewayPrincipal,
    svc: Any,
    *,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Optional[Set[str]]:
    """None (every owner) for an admin or a gateway without user accounts; else the principal's own owner
    ids (see the module docstring). A named run/session that is not the principal's raises 403, a
    missing run 404, a blank session id 400."""
    from .service import gateway_multi_user_enabled

    if principal.is_admin() or not gateway_multi_user_enabled():
        return None
    from abstractruntime.integrations.abstractmemory.effect_handlers import resolve_scope_owner_id

    own_plane = plane_is_principals_own(principal, svc)
    # The plane's entity homes: `EntityRegistry(data_dir=cfg.data_dir).entities_dir` (service.py builds the
    # plane's registry on that folder).
    owners: Set[str] = set(owned_entity_owner_ids(principal, Path(svc.config.data_dir) / "entities"))
    if own_plane:
        owners.add(global_memory_owner_id())
    run_store = svc.host.run_store
    if run_id:
        run = run_store.load(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        if not run_belongs_to(principal, run, run_store):
            raise owner_forbidden("this run is not yours (a run you did not start, or one with no recorded starter)")
        owners.add(resolve_scope_owner_id(run, scope="run", run_store=run_store))
        owners.add(resolve_scope_owner_id(run, scope="session", run_store=run_store))
        if own_plane:
            owners.add(resolve_scope_owner_id(run, scope="global", run_store=run_store))
    if session_id:
        try:
            session_owner = session_memory_owner_id(session_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not session_belongs_to(principal, session_id, run_store):
            raise owner_forbidden("this session is not yours (none of its runs was started by you)")
        owners.add(session_owner)
    return owners

