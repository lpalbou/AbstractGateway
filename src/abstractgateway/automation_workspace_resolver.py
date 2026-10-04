"""Automation workspaces resolved at EACH run (round 13, R13-W2 follow-up + adversary F1).

Occurrences are started by AbstractRuntime from inputs frozen at admission, never through a gateway
door, so the gateway resolves them through the runtime's host hook
``Runtime.set_occurrence_input_resolver`` (wired per plane by the bundle host): at every occurrence's
admission (its run start) EVERY automation's workspaces are guarded again against the policy as it
is NOW (``run_workspace_guard.guard_run_vars``: session > account > gateway, the eligible set and
caps, the built-in denies). The runtime freezes the answer with the occurrence's inputs, so a
replayed dispatch is byte-identical. The keys the gateway derived when the definition was SAVED are
never trusted: they are stripped first, and only the authoritative choice is kept:

- "Use my default" — ``target.input_data.workspace = {"configured": false}`` (never a snapshot of
  the default): the owner's default as it is now; a wider default applies from the next run.
- An explicit choice — ``workspace: {posture, default_mode, folders}``: re-clamped to the CURRENT
  eligible set and caps; a row the admin refused since is dropped, a cap lowered since lowers the
  row, each recorded with the gateway's sentence under ``_gateway_workspace.clamped``.
- A pre-round-11 definition with only a client ``workspace_allowed_paths`` list: the list still
  only narrows (entries the policy no longer reaches are dropped).

A definition saved before this rule that followed the default (no payload, gateway-derived keys at
the account/gateway level) is recognised as following. The save side (routes/automations.py
`_guarded_input_data`) uses `follows_default` and `strip_derived` so an echoed old snapshot in a
revision is ignored.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Mapping

logger = logging.getLogger("abstractgateway.automations")

#: The stored marker: follow the owner's default at each run.
FOLLOW_DEFAULT: Dict[str, Any] = {"configured": False}

#: Keys the gateway derives from the effective workspaces when it guards a run's inputs. With
#: ``configured: false`` none of them is ever taken from a stored definition or a client.
DERIVED_KEYS = (
    "workspace_access_mode",
    "workspace_allowed_paths",
    "workspace_read_only_paths",
    "workspace_writable_paths",
    "workspace_ignored_paths",
    "workspace_builtin_deny_prefixes",
    "workspace_builtin_allow",
    "_gateway_workspace",
    "workspaceAccessMode",
    "workspaceAllowedPaths",
    "workspaceIgnoredPaths",
)

#: `_gateway_workspace.level` values meaning "the gateway resolved the owner's default here".
_DEFAULT_LEVELS = ("account", "gateway")


def follows_default(input_data: Mapping[str, Any]) -> bool:
    """Does this automation input follow its owner's default workspaces?

    Yes for ``workspace: {"configured": false}``, and for a pre-rule definition with no workspace
    payload whose derived keys the gateway wrote at the account/gateway level."""
    workspace = input_data.get("workspace")
    if isinstance(workspace, Mapping):
        return workspace.get("configured") is False
    if workspace is not None:
        return False
    record = input_data.get("_gateway_workspace")
    return isinstance(record, Mapping) and record.get("level") in _DEFAULT_LEVELS


def strip_derived(data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop every gateway-derived workspace key (and the payload): the guard derives them again."""
    for key in DERIVED_KEYS:
        data.pop(key, None)
    data.pop("workspace", None)
    runtime_ns = data.get("_runtime")
    if isinstance(runtime_ns, dict):
        from abstractruntime.utils.workspace_paths import WRITABLE_PATHS_KEY

        runtime_ns.pop(WRITABLE_PATHS_KEY, None)
    return data


def resolve_occurrence_input(
    input_data: Mapping[str, Any],
    *,
    data_dir: Any,
    root_data_dir: Any,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> Dict[str, Any]:
    """One automation occurrence's input with its workspaces guarded against the policy NOW."""
    from .run_workspace_guard import guard_run_vars

    data = copy.deepcopy(dict(input_data))
    following = follows_default(data)
    chosen = data.get("workspace") if not following and isinstance(data.get("workspace"), Mapping) else None
    legacy: Dict[str, Any] = {}
    if not following and chosen is None:
        # A pre-round-11 client list (no payload, not gateway-derived): it still only narrows.
        for key in ("workspace_allowed_paths", "workspace_access_mode"):
            if key in data:
                legacy[key] = data[key]
    strip_derived(data)
    data.update(legacy)
    if chosen is not None:
        data["workspace"] = copy.deepcopy(dict(chosen))  # the one-off level: clamped by the guard
    guard_run_vars(
        data,
        data_dir=Path(str(data_dir)),
        root_data_dir=Path(str(root_data_dir)),
        session_id=session_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    if following:
        data["workspace"] = dict(FOLLOW_DEFAULT)
    elif chosen is not None:
        data["workspace"] = copy.deepcopy(dict(chosen))  # kept as chosen; what applied is recorded
    return data


def make_occurrence_input_resolver(
    *, data_dir: Any, root_data_dir: Any, tenant_id: str, user_id: str
) -> Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]:
    """The runtime hook for ONE plane (one runtime = one owner): every occurrence's workspaces
    are guarded against the policy as it is at its admission."""

    def resolve(definition: Dict[str, Any], input_data: Dict[str, Any]) -> Dict[str, Any]:
        return resolve_occurrence_input(
            input_data,
            data_dir=data_dir,
            root_data_dir=root_data_dir,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=str(definition.get("session_id") or ""),
        )

    return resolve


def wire_occurrence_workspaces(runtime: Any, *, data_dir: Any, root_data_dir: Any, tenant_id: str, user_id: str) -> None:
    """Register the resolver on a freshly built runtime (every host build, like the email wiring).

    Fails loudly on a runtime without the hook: no automation may run on workspaces frozen at save
    time (a refused or lowered workspace would stay reachable)."""
    setter = getattr(runtime, "set_occurrence_input_resolver", None)
    if not callable(setter):
        raise RuntimeError(
            "This AbstractRuntime has no Runtime.set_occurrence_input_resolver: automations that use "
            "their owner's default workspaces cannot be resolved at each run. Upgrade abstractruntime."
        )
    setter(make_occurrence_input_resolver(data_dir=data_dir, root_data_dir=root_data_dir, tenant_id=tenant_id, user_id=user_id))


__all__ = [
    "DERIVED_KEYS",
    "FOLLOW_DEFAULT",
    "follows_default",
    "make_occurrence_input_resolver",
    "resolve_occurrence_input",
    "strip_derived",
    "wire_occurrence_workspaces",
]
