"""Per-account client preferences (round 14, R14.2).

    GET /api/gateway/accounts/{me|<id>|<tenant>:<id>}/preferences
    PUT /api/gateway/accounts/{me|<id>|<tenant>:<id>}/preferences

The GATEWAY defines the defaults and an account may override them for itself; a client
(the Assistant, AbstractCode, the consoles) holds none of this state. Only keys this module
DECLARES are stored; anything else is refused with a sentence.

Declared keys:

- ``default_workflow``: ``{<app interface>: "<[catalog:]bundle:flow>" | null}``. ``null`` (or
  absent) = follow the gateway's per-app default, which lives in the EXISTING admin setting
  ``agents.default_workflow.<interface>`` (agent_defaults.py, "Default workflow per app") and is
  never copied here. A chosen workflow is stored version-less, so it follows new versions.
  The interfaces are the app rows of ``agent_defaults.INTERFACE_TABLE`` (group "apps").

Storage: the runtime-config store (``runtime_config.json``), top-level key
``account_preferences`` = ``{"<tenant>:<user>": {"default_workflow": {iface: value}}}``, written
under the store lock; an account with nothing set has no entry.

This module holds the store and the payload rules; routes/account_preferences.py binds them to
the request (who may read/write which account, which workflows the account may run).
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from .agent_defaults import (
    INTERFACE_TABLE,
    DefaultWorkflowError,
    Resolved,
    format_workflow_ref,
    parse_workflow_ref,
)

STORE_KEY = "account_preferences"
DEFAULT_WORKFLOW = "default_workflow"
GATEWAY_DEFAULT_LABEL = "Gateway default ({name})"
GATEWAY_DEFAULT_UNAVAILABLE_LABEL = "Gateway default (unavailable)"

#: THE declared keys (anything else is refused): plain label + one sentence of help.
DECLARED: Dict[str, Dict[str, str]] = {
    DEFAULT_WORKFLOW: {
        "label": "Default workflow",
        "help": "The workflow each app runs for this account unless a conversation picks another. "
        "Gateway default follows the admin's Default workflow per app.",
    },
}


class PreferenceError(ValueError):
    """A refused preference write (the message is a user-readable sentence)."""

    def __init__(self, message: str, *, key: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.key = key

    def detail(self) -> Dict[str, Any]:
        return {"reason": "preference_refused", "message": self.message, "key": self.key}


def app_interfaces() -> List[str]:
    """The interfaces an app asks the gateway for as its default agent, in table order."""
    return [iface for iface, row in INTERFACE_TABLE.items() if row.get("group") == "apps"]


def account_key(tenant_id: Optional[str], user_id: Optional[str]) -> str:
    tenant = str(tenant_id or "default").strip() or "default"
    user = str(user_id or "").strip()
    if not user:
        raise PreferenceError("An account is required.")
    return f"{tenant}:{user}"


def _rc():
    from . import runtime_config

    return runtime_config


def _clean(entry: Any) -> Dict[str, Any]:
    """The declared part of one stored entry (a hand-edited store never leaks other keys)."""
    out: Dict[str, Any] = {}
    if not isinstance(entry, Mapping):
        return out
    mapping = entry.get(DEFAULT_WORKFLOW)
    if isinstance(mapping, Mapping):
        known = set(app_interfaces())
        dw = {str(k): str(v) for k, v in mapping.items() if str(k) in known and isinstance(v, str) and v.strip()}
        if dw:
            out[DEFAULT_WORKFLOW] = dw
    return out


def stored_preferences(data_dir: Path, *, tenant_id: str, user_id: str) -> Dict[str, Any]:
    """The account's saved overrides: ``{"default_workflow": {iface: value}}`` (only set ones)."""
    stored = _rc()._read_store(Path(data_dir))
    block = stored.get(STORE_KEY)
    if not isinstance(block, Mapping):
        return {}
    return _clean(block.get(account_key(tenant_id, user_id)))


def preferences_view(stored: Mapping[str, Any]) -> Dict[str, Any]:
    """Every declared key with every app interface: ``{"default_workflow": {iface: value|null}}``."""
    dw = stored.get(DEFAULT_WORKFLOW) if isinstance(stored.get(DEFAULT_WORKFLOW), Mapping) else {}
    return {DEFAULT_WORKFLOW: {iface: (dw.get(iface) or None) for iface in app_interfaces()}}


def normalize_workflow_value(iface: str, value: Any) -> Optional[str]:
    """``None``/"" -> None (gateway default); otherwise the canonical VERSION-LESS
    ``[catalog:]bundle:flow`` (a version in the input is dropped: the preference names a workflow
    and always runs its latest published version, like the admin's per-app default)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise PreferenceError(
            f"default_workflow.{iface} must be a workflow written bundle:flow, or null for the gateway default.",
            key=DEFAULT_WORKFLOW,
        )
    text = value.strip()
    if not text:
        return None
    try:
        scope, bid, ver, fid = parse_workflow_ref(text)
    except DefaultWorkflowError as exc:
        raise PreferenceError(f"default_workflow.{iface} = {text!r} refused: {exc}", key=DEFAULT_WORKFLOW) from exc
    return format_workflow_ref(bid, None, fid, scope)


def check_changes(changes: Any) -> Dict[str, Dict[str, Optional[str]]]:
    """Shape check of a PUT body (no workflow resolution): only declared keys, only app
    interfaces, string-or-null values. Returns ``{"default_workflow": {iface: value|None}}``."""
    if not isinstance(changes, Mapping):
        raise PreferenceError("Send an object, e.g. {\"default_workflow\": {\"abstractcode.agent.v1\": null}}.")
    body = {k: v for k, v in changes.items() if k not in ("ok", "account")}
    unknown = sorted(str(k) for k in body if k not in DECLARED)
    if unknown:
        declared = ", ".join(sorted(DECLARED))
        what = f"Unknown preference '{unknown[0]}'" if len(unknown) == 1 else "Unknown preferences " + ", ".join(f"'{k}'" for k in unknown)
        raise PreferenceError(f"{what}: this gateway declares {declared}.", key=unknown[0])
    out: Dict[str, Dict[str, Optional[str]]] = {}
    if DEFAULT_WORKFLOW in body:
        mapping = body[DEFAULT_WORKFLOW]
        if not isinstance(mapping, Mapping):
            raise PreferenceError(
                "default_workflow is one entry per app: {\"<interface>\": \"bundle:flow\" | null}.", key=DEFAULT_WORKFLOW
            )
        known = app_interfaces()
        dw: Dict[str, Optional[str]] = {}
        for iface, value in mapping.items():
            iface = str(iface)
            if iface not in known:
                apps = ", ".join(f"{i} ({INTERFACE_TABLE[i]['label']})" for i in known)
                raise PreferenceError(
                    f"'{iface}' is not an app this gateway sets a default workflow for: default_workflow accepts {apps}.",
                    key=DEFAULT_WORKFLOW,
                )
            dw[iface] = normalize_workflow_value(iface, value)
        out[DEFAULT_WORKFLOW] = dw
    return out


def write_preferences(
    data_dir: Path,
    *,
    tenant_id: str,
    user_id: str,
    changes: Mapping[str, Any],
    validate: Callable[[str, str], None],
    actor: str,
) -> Dict[str, Any]:
    """Apply a checked PUT: named interfaces replace (None clears), unnamed keep. ``validate(iface,
    value)`` raises PreferenceError when the account may not run that workflow for that app; it
    runs BEFORE the store is touched, so a refusal changes nothing."""
    checked = check_changes(changes)
    for iface, value in (checked.get(DEFAULT_WORKFLOW) or {}).items():
        if value is not None:
            validate(iface, value)
    rc = _rc()
    data_dir = Path(data_dir)
    key = account_key(tenant_id, user_id)
    with rc.store_lock(data_dir):
        stored = rc._read_store(data_dir, strict=True)
        block = dict(stored.get(STORE_KEY) or {}) if isinstance(stored.get(STORE_KEY), Mapping) else {}
        entry = _clean(block.get(key))
        if DEFAULT_WORKFLOW in checked:
            dw = dict(entry.get(DEFAULT_WORKFLOW) or {})
            for iface, value in checked[DEFAULT_WORKFLOW].items():
                if value is None:
                    dw.pop(iface, None)
                else:
                    dw[iface] = value
            if dw:
                entry[DEFAULT_WORKFLOW] = dw
            else:
                entry.pop(DEFAULT_WORKFLOW, None)
        if entry:
            block[key] = entry
        else:
            block.pop(key, None)
        if block:
            stored[STORE_KEY] = block
        else:
            stored.pop(STORE_KEY, None)
        stored["_last_changed_by"] = str(actor)
        stored["_last_changed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rc._write_store(data_dir, stored)
    return entry


# ----------------------------------------------------------------------- the GET answer


def choice_labels(choices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Each choice with its display ``label``: the workflow's name, plus `` — <bundle id>`` when two
    choices share a name, plus `` (tenant catalog)`` where the scope disambiguates."""
    names: Dict[str, int] = {}
    for c in choices:
        names[str(c.get("name") or "")] = names.get(str(c.get("name") or ""), 0) + 1
    out = []
    for c in choices:
        name = str(c.get("name") or c.get("flow_id") or "")
        label = name if names.get(name, 0) <= 1 else f"{name} — {c.get('bundle_id')}"
        if c.get("show_scope") and c.get("registry_scope") != "private":
            label += " (tenant catalog)"
        out.append(
            {
                "value": c["value"],
                "label": label,
                "name": name,
                "workflow_id": c.get("workflow_id"),
                "bundle_id": c.get("bundle_id"),
                "bundle_version": c.get("bundle_version"),
                "flow_id": c.get("flow_id"),
                "registry_scope": c.get("registry_scope"),
            }
        )
    return out


def _resolved_view(res: Any) -> Dict[str, Any]:
    if isinstance(res, Resolved):
        return {"available": True, "value": res.value, "name": res.name, "reason": None, **res.resolved_dict()}
    return {
        "available": False,
        "value": getattr(res, "value", None),
        "name": None,
        "reason": getattr(res, "reason", None),
        "workflow_id": None,
        "bundle_id": None,
        "bundle_version": None,
        "flow_id": None,
        "registry_scope": None,
    }


def app_row(
    iface: str,
    *,
    value: Optional[str],
    gateway_default: Any,
    choices: List[Dict[str, Any]],
    account_label: str,
    unavailable_reason: Callable[[str], str],
) -> Dict[str, Any]:
    """One app's row of the GET answer. ``gateway_default`` = the agent_defaults Resolution of the
    admin per-app default; ``choices`` = the eligible workflows the account may run (labelled);
    ``unavailable_reason(value)`` says why a saved value is not among them."""
    info = INTERFACE_TABLE.get(iface) or {"label": iface, "app": None, "help": ""}
    gd = _resolved_view(gateway_default)
    gd_label = GATEWAY_DEFAULT_LABEL.format(name=gd["name"]) if gd["available"] else GATEWAY_DEFAULT_UNAVAILABLE_LABEL
    if value is None:
        state, reason = "default", None
        effective = {"source": "gateway", **gd}
    else:
        match = next((c for c in choices if c["value"] == value), None)
        if match is not None:
            state, reason = "set", None
            effective = {
                "source": "account",
                "available": True,
                "value": value,
                "name": match["name"],
                "reason": None,
                "workflow_id": match["workflow_id"],
                "bundle_id": match["bundle_id"],
                "bundle_version": match["bundle_version"],
                "flow_id": match["flow_id"],
                "registry_scope": match["registry_scope"],
            }
        else:
            why = unavailable_reason(value)
            state = "broken"
            reason = f"{value} no longer runs for {account_label}: {why}. Pick another workflow or Gateway default."
            effective = {"source": "account", **_resolved_view(None), "value": value, "reason": why}
    return {
        "interface": iface,
        "label": info.get("label") or iface,
        "app": info.get("app"),
        "help": info.get("help") or "",
        "value": value,
        "state": state,
        "reason": reason,
        "gateway_default": {k: gd[k] for k in ("available", "name", "value", "workflow_id", "reason")},
        "gateway_default_label": gd_label,
        "effective": effective,
        "choices": choices,
    }
