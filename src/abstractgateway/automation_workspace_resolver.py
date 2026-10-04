"""Automations that follow their owner's default workspaces (round 13, R13-W2 follow-up).

An automation saved with "Use my default" stores ``target.input_data.workspace =
{"configured": false}`` (authoritative) — never a snapshot of the default as it was when it was
saved. Its workspaces are resolved at EACH occurrence's admission (its run start) through
AbstractRuntime's host hook ``Runtime.set_occurrence_input_resolver``: session > account >
gateway for the plane's owner, clamped to the eligible set and caps, exactly as every other door
does (``run_workspace_guard.guard_run_vars``). The runtime freezes the answer with the
occurrence's inputs, so a replayed dispatch is byte-identical.

A definition saved before this rule (round 11/12) followed the default too, but carries the
gateway-derived keys of that moment and ``_gateway_workspace.level`` "account"/"gateway" with no
``workspace`` payload; it is recognised as following and re-resolved the same way (nothing to
migrate, nothing narrowed by the old snapshot).

The save side (routes/automations.py `_guarded_input_data`) uses `follows_default` and
`strip_derived` so an echoed old snapshot in a revision is ignored: with ``configured: false``
the stored folders are the gateway's, never the client's.
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


def resolve_following_input(
    input_data: Mapping[str, Any],
    *,
    data_dir: Any,
    root_data_dir: Any,
    tenant_id: str,
    user_id: str,
    session_id: str,
) -> Dict[str, Any]:
    """The input with the owner's CURRENT default workspaces applied (and the marker kept)."""
    from .run_workspace_guard import guard_run_vars

    data = strip_derived(copy.deepcopy(dict(input_data)))
    guard_run_vars(
        data,
        data_dir=Path(str(data_dir)),
        root_data_dir=Path(str(root_data_dir)),
        session_id=session_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    data["workspace"] = dict(FOLLOW_DEFAULT)
    return data


def make_occurrence_input_resolver(
    *, data_dir: Any, root_data_dir: Any, tenant_id: str, user_id: str
) -> Callable[[Dict[str, Any], Dict[str, Any]], Dict[str, Any]]:
    """The runtime hook for ONE plane (one runtime = one owner): a definition that follows its
    owner's default gets the default as it is now; any other input is returned unchanged."""

    def resolve(definition: Dict[str, Any], input_data: Dict[str, Any]) -> Dict[str, Any]:
        if not follows_default(input_data):
            return input_data
        session_id = str(definition.get("session_id") or "")
        return resolve_following_input(
            input_data,
            data_dir=data_dir,
            root_data_dir=root_data_dir,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
        )

    return resolve


def wire_occurrence_workspaces(runtime: Any, *, data_dir: Any, root_data_dir: Any, tenant_id: str, user_id: str) -> None:
    """Register the resolver on a freshly built runtime (every host build, like the email wiring).

    Fails loudly on a runtime without the hook: an automation that follows its owner's default
    must never run on a snapshot silently."""
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
    "resolve_following_input",
    "strip_derived",
    "wire_occurrence_workspaces",
]
