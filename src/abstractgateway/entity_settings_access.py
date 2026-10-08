"""Who configures an entity (operator ruling 2026-10-08, R16.5: "the creator configures their
entity").

Roles are exactly two: an ADMIN and a MEMBER (a human account or an entity account). An entity's
SETTINGS are changed by:

- an admin (always), or
- the entity's CREATOR (the manifest's `created_by`, written by `POST /entities` from the
  authenticated principal — the same rule round 11 set for its workspaces and round 14 for its
  preferences), within what the admin authorised (the models/providers and voices the gateway
  offers, the workspace caps, the gateway's tool tiers).

Nobody else: another member gets the sentence "Only an admin or <name>'s creator can …", and the
entity itself never changes its own settings (an entity principal is refused even when its id
equals a creator's). An entity a member may not SEE answers like a missing one first (404,
`entity_access.require_entity_visible`), so names cannot be probed through these routes.

The settings routes are ONE row of the route table (`security/authorization.py`, required role
`admin_or_creator`); the middleware lets an admin through and leaves everyone else to
`entity_settings_guard`, a dependency of both entity routers, so no per-route check can be
forgotten. Every decision through the guard is recorded on the request's audit line
(`entity_settings: {entity, setting, actor, as: admin|creator|null, outcome}`).

Settings (creator-editable): mind (`substrate`), voice, tools per phase (`tool-policy`),
instructions (`prompt`), skills, workspaces (`/workspace/policy/{account}`), preferences
(`/accounts/{account}/preferences`), archive / unarchive and the Active switch
(`/me/accounts/{id}/…`). Lifecycle and maintenance acts stay admin-only: sleep/wake (`state`),
personal time (`loop/*`, `personal-grant`), the work order, tasks, memory review (`candidates`),
the memory index rebuild (`reembed`), maintenance windows, the capability map and host-folder
mounts (`workspace/mounts`).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from fastapi import Request

from .security.principal import GatewayPrincipal

ROLE_ADMIN = "admin"
ROLE_CREATOR = "creator"
# The route-table role of the settings row (security/authorization.py).
ADMIN_OR_CREATOR = "admin_or_creator"
REASON_CODE = "admin_or_creator_required"

# The last path segment of a settings route -> what the refusal sentence says the caller can't do.
SETTING_VERBS: dict[str, str] = {
    "substrate": "change its mind",
    "voice": "change its voice",
    "tool-policy": "change its tools",
    "prompt": "change its instructions",
    "skills": "change its skills",
    "unarchive": "unarchive it",
    "archive": "archive it",
    "active": "turn it on or off",
    "settings": "change its settings",
}


def refusal(name: str, setting: str = "settings") -> str:
    """The one sentence for a caller who may not change this entity's `setting` (the round-11
    workspace wording: "Only an admin or {name}'s creator can change its workspaces.")."""
    verb = SETTING_VERBS.get(str(setting), SETTING_VERBS["settings"])
    return f"Only an admin or {name}'s creator can {verb}."


def _is_entity_principal(principal: GatewayPrincipal) -> bool:
    return "entity" in {str(r).strip().lower() for r in (principal.roles or ())}


def configure_role(principal: Optional[GatewayPrincipal], created_by: Any) -> Optional[str]:
    """"admin" / "creator" when `principal` may change the settings of an entity whose manifest
    carries `created_by`; None otherwise. The entity itself is never its own configurer."""
    if principal is None:
        return None
    if principal.is_admin():
        return ROLE_ADMIN
    if _is_entity_principal(principal):
        return None
    if not isinstance(created_by, Mapping) or not str(created_by.get("user_id") or "").strip():
        return None  # legacy home without a recorded creator: admins only (never guessed)
    from .entity_access import same_account

    return ROLE_CREATOR if same_account(principal, created_by) else None


def can_configure_entity(principal: Optional[GatewayPrincipal], created_by: Any) -> bool:
    return configure_role(principal, created_by) is not None


def creator_of_slug(slug: str, *, registry: Any = None) -> Any:
    """The `created_by` of entity `slug` in the caller's plane (the registry the entity routes
    use), else over every runtime on this gateway (an admin's view). None when unknown."""
    from .entity_access import entities_dirs, manifest_creator

    if registry is not None:
        exists, created_by = manifest_creator(registry.entities_dir, slug)
        if exists:
            return created_by
    for entities_dir in entities_dirs():
        exists, created_by = manifest_creator(entities_dir, slug)
        if exists:
            return created_by
    return None


def access_view(principal: Optional[GatewayPrincipal], name: str, created_by: Any) -> dict:
    """What `GET /entities/{name}/access` answers: may the caller change its settings, as whom,
    and the sentence when not. Clients enable/disable their controls from this, never from a
    role they derive themselves."""
    role = configure_role(principal, created_by)
    return {
        "entity": name,
        "can_configure": role is not None,
        "as": role,
        "reason": None if role is not None else refusal(name),
        "admin_only": {
            "available": bool(principal is not None and principal.is_admin()),
            "reason": None
            if principal is not None and principal.is_admin()
            else "Only an admin can put it to sleep or wake it, run its personal time, give it work, review its memories or rebuild its memory index.",
        },
    }


def _route_setting(path: str) -> str:
    seg = str(path or "").rstrip("/").rsplit("/", 1)[-1]
    return seg or "settings"


def entity_settings_guard(request: Request) -> None:
    """Router dependency (both entity routers, after `entity_name_guard`): a route whose table
    row requires `admin_or_creator` runs only for an admin or the entity's creator; anyone else
    gets 403 with the sentence. Records the decision on the request's audit line."""
    from fastapi import HTTPException

    from .security.authorization import gateway_route_authorization_requirement
    from .security.principal import current_gateway_principal

    path = str(request.url.path)
    requirement = gateway_route_authorization_requirement(path, request.method)
    if requirement is None or requirement.required_role != ADMIN_OR_CREATOR:
        return
    name = request.path_params.get("name")
    if name is None:
        return
    principal = getattr(request.state, "gateway_principal", None) or current_gateway_principal()
    if principal is None:
        from .security.principal import local_admin_principal

        principal = local_admin_principal()  # auth disabled entirely: the local caller is the operator
    from .entities import entity_slug

    try:
        slug = entity_slug(str(name))
    except Exception:  # noqa: BLE001 - an invalid name is the route's own 400
        return
    from .entity_access import manifest_creator
    from .routes.entities import _registry

    exists, created_by = manifest_creator(_registry().entities_dir, slug)
    if not exists:
        return  # no such home in the caller's plane: the route answers its own 404
    role = configure_role(principal, created_by)
    setting = _route_setting(path)
    actor = f"person:{principal.user_id}" if not _is_entity_principal(principal) else f"entity:{principal.user_id}"
    record = {
        "entity": slug,
        "setting": setting,
        "actor": actor,
        "as": role,
        "outcome": "allowed" if role is not None else "refused",
    }
    try:
        request.state.audit_detail = {**(getattr(request.state, "audit_detail", None) or {}), "entity_settings": record}
    except Exception:  # noqa: BLE001 - auditing never breaks the request
        pass
    if role is None:
        raise HTTPException(status_code=403, detail={"reason_code": REASON_CODE, "message": refusal(slug, setting)})


# ------------------------------------------------------------------------------------------
# "Within what the admin authorised": the bounds a CREATOR's settings write obeys (admins are
# not bounded here). Each answers None (allowed) or the refusal sentence naming the offered set.
# The offered lists are the SAME answers the console/kit pickers show a member:
# `GET /discovery/providers/{provider}/models` (mind) and `GET /voice/voices` (voice).
# ------------------------------------------------------------------------------------------

REASON_NOT_OFFERED = "not_offered"
REASON_ADMIN_ONLY_TOOL = "admin_only_tool"
_LIST_CAP = 12


def _run_async(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run the async route function `fn` from a sync entity handler (a worker thread)."""
    import functools

    try:
        import anyio.from_thread

        return anyio.from_thread.run(functools.partial(fn, *args, **kwargs))
    except RuntimeError:
        import asyncio

        return asyncio.run(fn(*args, **kwargs))


def _bare_request() -> Any:
    from starlette.requests import Request as _Request

    return _Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})


def _listed(values: list) -> str:
    shown = [str(v) for v in values[:_LIST_CAP]]
    more = len(values) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def offered_text_models(provider: str) -> tuple[list, Optional[str]]:
    """(models, error) — what `GET /discovery/providers/{provider}/models` offers a member."""
    from fastapi import HTTPException

    from .routes.gateway import discovery_provider_models

    try:
        body = _run_async(
            discovery_provider_models,
            _bare_request(),
            provider_name=str(provider),
            base_url=None,
            input_type=None,
            output_type=None,
            capability_route=None,
        )
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, str) else str(e.detail)
        return [], detail
    except Exception as e:  # noqa: BLE001 - an unreachable provider offers nothing
        return [], str(e)
    models = [str(m) for m in (body.get("models") or []) if isinstance(m, str) and m.strip()]
    err = body.get("error") if isinstance(body.get("error"), str) else None
    return models, err


def mind_bound_problem(provider: str, model: str) -> Optional[str]:
    """None when (provider, model) is one the gateway offers; else the sentence."""
    models, err = offered_text_models(provider)
    if any(m.lower() == str(model).strip().lower() for m in models):
        return None
    if not models:
        why = f" ({err})" if err else ""
        return (
            f"This gateway offers no {provider} model right now{why}, so {model} can't be its mind. "
            "Choose a provider and model from the list, or the Gateway default."
        )
    return f"{model} isn't a {provider} model this gateway offers. Offered: {_listed(models)}."


def offered_voices(provider: str, model: Optional[str]) -> tuple[list, list, Optional[str]]:
    """(voice ids, speech models, error) — what `GET /voice/voices` offers a member for `provider`."""
    from fastapi import HTTPException

    from .routes.gateway import voice_voices_catalog

    try:
        body = _run_async(
            voice_voices_catalog,
            _bare_request(),
            base_url=None,
            provider=str(provider),
            model=None,
            providers_only=False,
            compact=True,
        )
    except HTTPException as e:
        return [], [], e.detail if isinstance(e.detail, str) else str(e.detail)
    except Exception as e:  # noqa: BLE001
        return [], [], str(e)
    prov = str(provider).strip().lower()
    voices: list = []
    for item in body.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("provider") or "").strip().lower() not in ("", prov):
            continue
        vid = str(item.get("id") or "").strip()
        if vid and vid not in voices:
            voices.append(vid)
    by_provider = body.get("tts_models_by_provider") if isinstance(body.get("tts_models_by_provider"), dict) else {}
    raw_models = by_provider.get(provider) or body.get("tts_models") or []
    models = [str(m if isinstance(m, str) else (m or {}).get("id") or (m or {}).get("name") or "") for m in raw_models]
    models = [m for m in models if m]
    err = body.get("error") if isinstance(body.get("error"), str) else None
    return voices, models, err


def voice_bound_problem(provider: str, model: str, voice: str) -> Optional[str]:
    voices, models, err = offered_voices(provider, model)
    if not voices:
        why = f" ({err})" if err else ""
        return (
            f"This gateway offers no {provider} voice right now{why}, so {voice} can't be its voice. "
            "Choose a voice from the list, or the Gateway default voice."
        )
    if not any(v.lower() == str(voice).strip().lower() for v in voices):
        return f"{voice} isn't a {provider} voice this gateway offers. Offered: {_listed(voices)}."
    if models and not any(m.lower() == str(model).strip().lower() for m in models):
        return f"{model} isn't a {provider} speech model this gateway offers. Offered: {_listed(models)}."
    return None


def tool_bound_problem(name: str, home_dir: Any, policy: Mapping[str, Any]) -> Optional[str]:
    """A creator may narrow or widen an entity's tools within the tier-1 and workspace tools; a
    tier-2 tool (world-effect, default off everywhere: "grantable only by the operator's explicit
    word", abstractruntime.identity.tool_policy) is added only by an admin. Removing one is fine."""
    from abstractruntime import resolve_tool_grant
    from abstractruntime.identity.tool_policy import TIER1_TOOL_NAMES, TIER2_TOOL_NAMES, WORKSPACE_TOOL_NAMES

    tier2 = set(TIER2_TOOL_NAMES)
    for phase, tools in (policy or {}).items():
        if tools is None or not isinstance(phase, str):
            continue
        try:
            current = set(resolve_tool_grant(home_dir, phase, enable_workspace=False).tools)
        except Exception:  # noqa: BLE001 - an unknown phase is the writer's own 400
            current = set()
        added = [t for t in tools if t in tier2 and t not in current]
        if added:
            offered = list(TIER1_TOOL_NAMES) + [t for t in WORKSPACE_TOOL_NAMES if t not in TIER1_TOOL_NAMES]
            return (
                f"Only an admin can give {name} {', '.join(added)}: a tier-2 tool acts on the world outside "
                f"its memory and workspace. Offered to you: {_listed(offered)}."
            )
    return None
