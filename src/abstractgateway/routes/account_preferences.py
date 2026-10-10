"""`GET/PUT /api/gateway/accounts/{account}/preferences` (round 14, R14.2).

`account` = `me` (the caller), `<user>` or `<tenant>:<user>`. Who may read and write another
account's preferences: an admin (any account) or, for an entity, its creator — the rule of
`/workspace/policy/{account}`. The store and the payload rules are in account_preferences.py.

Which workflows an account may pick for an app: the latest, non-deprecated entrypoints that
declare the app's interface (agent_defaults.eligible_entrypoints) AND that the account may run
under the workflow governance (a gateway workflow an admin left available, the account's own
ones, never an archived one). For `me` the index is the caller's own host (exactly what its run
starts resolve against). For another account the index is the caller's host restricted to the
gateway's shared workflows (an admin never sees, and never picks, another user's private
workflows; that user picks those in their own apps).

Answers: see account_preferences.py and the COORD line "R14 PREFERENCES API — FINAL".
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request

from ..account_preferences import (
    DECLARED,
    DEFAULT_WORKFLOW,
    SPOKEN_LANGUAGE,
    TIME_ZONE,
    PreferenceError,
    app_interfaces,
    app_row,
    choice_labels,
    preferences_view,
    stored_preferences,
    write_preferences,
)

from ..automation_schedule import preferences_time_zone_block
from ..spoken_language import preferences_block as preferences_spoken_language_block

router = APIRouter(prefix="/gateway", tags=["accounts"])


async def _off_loop(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


def _gw():
    from . import gateway as gw

    return gw


def _refused(exc: PreferenceError, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=exc.detail())


def preference_edit_problem(principal: Any, tenant: str, user: str) -> Optional[str]:
    """Why ``principal`` may not read or change this account's preferences (None = it may).
    Humans their own; an entity's: admins and the entity's creator; admins: any account."""
    if principal is None or principal.is_admin():
        return None
    gw = _gw()
    from ..entity_access import same_account

    if gw._is_entity_account(tenant, user):
        created_by = gw._entity_creator(user)
        if isinstance(created_by, dict) and same_account(principal, created_by):
            return None
        return f"Only an admin or {user}'s creator can read or change its preferences."
    if tenant == str(principal.tenant_id or "default") and user == str(principal.user_id or ""):
        return None
    return "Only an admin or the account itself can read or change its preferences."


def _target(request: Request, account: str) -> Tuple[Any, str, str, bool]:
    """(caller, tenant, user, is_self). 403 with a sentence when the caller may not address it,
    404 when the account does not exist."""
    gw = _gw()
    principal = gw._principal_from_request(request)
    raw = str(account or "").strip()
    me_tenant, me_user = gw._principal_account(principal)
    if raw == "me":
        tenant, user = me_tenant, me_user
    else:
        from ..workspace_policy import WorkspacePolicyError, parse_account

        try:
            tenant, user = parse_account(raw)
        except WorkspacePolicyError as exc:
            raise _refused(PreferenceError(str(exc))) from exc
        tenant, user = gw._resolve_policy_target_user(tenant, user)
    is_self = (tenant, user) == (me_tenant, me_user)
    if not is_self:
        problem = preference_edit_problem(principal, tenant, user)
        if problem:
            raise HTTPException(status_code=403, detail={"reason": "preference_forbidden", "message": problem, "key": None})
    return principal, tenant, user, is_self


def _target_principal(caller: Any, tenant: str, user: str, is_self: bool) -> Any:
    if is_self:
        return caller
    from ..security.principal import local_admin_principal
    from ..users import GatewayUserRegistry

    rec = GatewayUserRegistry().get_user(user, tenant_id=tenant)
    if rec is None:
        return local_admin_principal()  # default:admin, the static-token operator (no record)
    return rec.to_principal()


class _Context:
    """Everything one answer needs, computed once per request: the index, the choices per app
    and the admin's per-app defaults."""

    def __init__(self, caller: Any, tenant: str, user: str, is_self: bool) -> None:
        gw = _gw()
        from ..agent_defaults import eligible_entrypoints, resolve_default_agent_workflow, stored_default_workflows
        from ..workflow_governance import OWNER_GATEWAY, workflow_visible

        self.tenant, self.user, self.is_self = tenant, user, is_self
        self.account_label = user
        self.data_dir = gw.gateway_data_dir_from_env()
        svc = gw.get_gateway_service()
        self.host = gw._require_bundle_host(svc)
        self.index = gw._agent_entrypoint_index(svc, caller)
        target = _target_principal(caller, tenant, user, is_self)
        admin_defaults = stored_default_workflows(self.data_dir)
        self.defaults: Dict[str, Any] = {}
        self.choices: Dict[str, List[Dict[str, Any]]] = {}
        for iface in app_interfaces():
            self.defaults[iface] = resolve_default_agent_workflow(iface, index=self.index, stored=admin_defaults)
            keep = []
            for e in eligible_entrypoints(self.index, iface):
                if e.get("registry_scope") == "private":
                    meta = gw._bundle_source_meta(self.host, e["bundle_id"], e["bundle_version"])
                    owner = gw._bundle_owner(self.host, caller, meta)
                    if not is_self and owner.get("kind") != OWNER_GATEWAY:
                        continue  # another account never gets the caller's own workflows
                    if not workflow_visible(target, owner, e["bundle_id"], data_dir=self.data_dir):
                        continue
                    if gw._bundle_archive_store(self.host, owner).is_archived(e["bundle_id"], e["bundle_version"]):
                        continue
                keep.append(e)
            self.choices[iface] = choice_labels(keep)

    def unavailable_reason(self, iface: str, value: str) -> str:
        from ..agent_defaults import Unavailable, resolve_ref

        res = resolve_ref(self.index, iface, value, source="stored")
        if isinstance(res, Unavailable):
            return str(res.reason)
        who = "you" if self.is_self else self.account_label
        return f"it is not among the workflows {who} may run for this app"

    def validate(self, iface: str, value: str) -> None:
        if any(c["value"] == value for c in self.choices.get(iface) or []):
            return
        raise PreferenceError(f"default_workflow.{iface} = {value!r} refused: {self.unavailable_reason(iface, value)}.", key=DEFAULT_WORKFLOW)

    def answer(self, caller: Any) -> Dict[str, Any]:
        stored = stored_preferences(self.data_dir, tenant_id=self.tenant, user_id=self.user)
        view = preferences_view(stored)
        apps = [
            app_row(
                iface,
                value=view[DEFAULT_WORKFLOW][iface],
                gateway_default=self.defaults[iface],
                choices=self.choices[iface],
                account_label=self.account_label,
                unavailable_reason=lambda v, i=iface: self.unavailable_reason(i, v),
            )
            for iface in app_interfaces()
        ]
        return {
            "ok": True,
            "account": f"{self.tenant}:{self.user}",
            "can_edit": self.is_self or preference_edit_problem(caller, self.tenant, self.user) is None,
            "preferences": view,
            "declared": {k: dict(v) for k, v in DECLARED.items()},
            "apps": apps,
            # R16.1: the account's time zone (null = this host's zone, the gateway default).
            "time_zone": preferences_time_zone_block(view[TIME_ZONE]),
            # Round 18: the account's spoken language ("auto" = the engine detects it), with the
            # served choices (AbstractVoice's list) — the control's whole truth.
            "spoken_language": preferences_spoken_language_block(
                None if view[SPOKEN_LANGUAGE] == "auto" else view[SPOKEN_LANGUAGE]
            ),
        }


@router.get("/accounts/{account}/preferences", summary="An account's client preferences (default workflow per app, time zone, spoken language)")
async def get_account_preferences(request: Request, account: str) -> Dict[str, Any]:
    """`me`, or another account (an admin; an entity's creator). See the module docstring."""
    caller, tenant, user, is_self = _target(request, account)

    def work() -> Dict[str, Any]:
        return _Context(caller, tenant, user, is_self).answer(caller)

    return await _off_loop(work)


@router.put("/accounts/{account}/preferences", summary="Change an account's client preferences")
async def put_account_preferences(request: Request, account: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Body `{"default_workflow": {<interface>: "bundle:flow" | null}, "time_zone": "<IANA>" | null,
    "spoken_language": "auto" | "<code>" | null}` (each key optional): named interfaces replace (null =
    follow the gateway default), unnamed keep; `time_zone` null = this host's zone; `spoken_language`
    null/"auto" = the engine detects the language. Unknown keys, unknown interfaces, unknown language
    codes and workflows the account may not run are refused (400 preference_refused, a sentence)."""
    caller, tenant, user, is_self = _target(request, account)
    gw = _gw()
    from ..runtime_config import RuntimeConfigStoreCorrupt

    def work() -> Dict[str, Any]:
        ctx = _Context(caller, tenant, user, is_self)
        write_preferences(
            ctx.data_dir,
            tenant_id=tenant,
            user_id=user,
            changes=payload,
            validate=ctx.validate,
            actor=gw._workspace_actor(caller),
        )
        return ctx.answer(caller)

    try:
        out = await _off_loop(work)
    except PreferenceError as exc:
        raise _refused(exc) from exc
    except RuntimeConfigStoreCorrupt as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    gw._audit_account_change(request, user_id=user, tenant_id=tenant, changes={"preferences": sorted(k for k in (payload or {}) if k in DECLARED)})
    return out
