"""`automation_defaults` on VisualFlow documents and published bundles (contract C6).

A runnable flow may carry the defaults of the automations created from it:

    automation_defaults = {schema_version: 1, title?: str,
                           trigger: {source_id, source_version: int, config: object,
                                     connection_id?: str},
                           context: {mode: "independent"|"growing",
                                     growing?: {max_tokens: positive int}  (growing mode only)},
                           input_data: object}

No binding_id, credentials or instantiated ids: those are server-owned and
minted when an automation is created. The one exception is `trigger.connection_id`
(framework backlog 1040/1047): REQUIRED when the source's descriptor says it needs a
trigger connection (`needs_connection` with the `trigger_connections` provider: a webhook,
a feed), refused otherwise. It is the AUTHOR's connection: an automation created from the
defaults by anyone else is refused (`connection_not_yours`, routes/automations.py) and that
person picks one of their own connections. The gateway validates the field on
save, stores it in the flow document, and on publish exports it as
`manifest.metadata.automation_defaults[<root flow_id>]`; the catalog, `/bundles`
and `/bundles/{id}` expose `automation_defaults` keyed by entrypoint flow id.

Validation = the same structural rules as the flow editor
(abstractflow `utils/triggerBindings.ts`), then the trigger source's own
`validate` from the runtime registry (unknown source -> unknown_trigger_source,
bad config -> invalid_definition / unsupported_feature).
# PART 2 / R2: when AbstractRuntime ships `validate_automation_defaults`, it
# replaces `_structural` below (one validator for every host).
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, Mapping, Optional

AUTOMATION_DEFAULTS_SCHEMA_VERSION = 1
CONTEXT_MODES = ("independent", "growing")
TITLE_MAX = 120

_TOP_KEYS = frozenset({"schema_version", "title", "trigger", "context", "input_data"})
_TRIGGER_KEYS = frozenset({"source_id", "source_version", "config", "connection_id"})
_CONTEXT_KEYS = frozenset({"mode", "growing"})
_GROWING_KEYS = frozenset({"max_tokens"})


#: The `_runtime` keys a client may set in an automation's target input
#: (review 52 R52-1): the per-run generation settings `/runs/start` accepts
#: from chat clients (`thinking`, `speculation`, `stream`, `provider`,
#: `model`) and `allowed_tools`, which only narrows the target's tools.
#: Everything else under `_runtime` is runtime/host state (`control` froze an
#: occurrence, `agora_agent` is an identity, `tool_policy` is set by
#: `policy.tool_approval`) and is dropped. Workspace knobs are top-level keys,
#: clamped separately by the gateway's workspace policy.
CLIENT_RUNTIME_KEYS: frozenset = frozenset({"allowed_tools", "provider", "model", "thinking", "speculation", "stream"})
#: Top-level input keys only the server writes.
SERVER_INPUT_KEYS: frozenset = frozenset({"_meta", "workspace_read_only", "_gateway_workspace"})


def strip_server_owned_input(input_data: dict) -> list:
    """Drop server-owned keys from a client input dict IN PLACE; returns the dropped key paths."""
    dropped = [k for k in SERVER_INPUT_KEYS if k in input_data]
    for key in dropped:
        input_data.pop(key, None)
    runtime_ns = input_data.get("_runtime")
    if runtime_ns is not None and not isinstance(runtime_ns, dict):
        input_data.pop("_runtime")
        dropped.append("_runtime")
    elif isinstance(runtime_ns, dict):
        for key in sorted(k for k in runtime_ns if k not in CLIENT_RUNTIME_KEYS):
            runtime_ns.pop(key)
            dropped.append(f"_runtime.{key}")
    return sorted(dropped)


class AutomationDefaultsError(ValueError):
    def __init__(self, message: str, *, field: str, reason_code: str = "invalid_definition") -> None:
        super().__init__(message)
        self.field = field
        self.reason_code = reason_code


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _structural(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AutomationDefaultsError("automation_defaults must be an object", field="automation_defaults")
    unknown = sorted(k for k in raw if k not in _TOP_KEYS)
    if unknown:
        raise AutomationDefaultsError(f"unknown field(s) {unknown}", field=f"automation_defaults.{unknown[0]}")
    if raw.get("schema_version") != AUTOMATION_DEFAULTS_SCHEMA_VERSION or not _is_int(raw.get("schema_version")):
        raise AutomationDefaultsError("schema_version must be 1", field="automation_defaults.schema_version")
    out: Dict[str, Any] = {"schema_version": AUTOMATION_DEFAULTS_SCHEMA_VERSION}

    title = raw.get("title")
    if title is not None:
        if not isinstance(title, str):
            raise AutomationDefaultsError("title must be a string", field="automation_defaults.title")
        if len(title.strip()) > TITLE_MAX:
            raise AutomationDefaultsError(f"title must be at most {TITLE_MAX} characters", field="automation_defaults.title")
        if title.strip():
            out["title"] = title.strip()

    trigger = raw.get("trigger")
    if not isinstance(trigger, Mapping):
        raise AutomationDefaultsError("trigger must be an object", field="automation_defaults.trigger")
    unknown = sorted(k for k in trigger if k not in _TRIGGER_KEYS)
    if unknown:
        raise AutomationDefaultsError(f"unknown trigger field(s) {unknown}", field=f"automation_defaults.trigger.{unknown[0]}")
    source_id = trigger.get("source_id")
    if not isinstance(source_id, str) or not source_id.strip():
        raise AutomationDefaultsError("trigger.source_id must be a non-empty string", field="automation_defaults.trigger.source_id")
    source_version = trigger.get("source_version")
    if not _is_int(source_version) or int(source_version) < 1:
        raise AutomationDefaultsError("trigger.source_version must be an integer >= 1", field="automation_defaults.trigger.source_version")
    config = trigger.get("config")
    if not isinstance(config, Mapping):
        raise AutomationDefaultsError("trigger.config must be an object", field="automation_defaults.trigger.config")
    out["trigger"] = {"source_id": source_id, "source_version": int(source_version), "config": dict(config)}
    if "connection_id" in trigger:
        cid = trigger.get("connection_id")
        from abstractruntime.triggers.connections import CONNECTION_ID_RE

        if not isinstance(cid, str) or not CONNECTION_ID_RE.match(cid):
            raise AutomationDefaultsError(
                "trigger.connection_id must be a connection id (letters, digits, '_' or '-', at most 64)",
                field="automation_defaults.trigger.connection_id",
            )
        out["trigger"]["connection_id"] = cid

    context = raw.get("context", {})
    if not isinstance(context, Mapping):
        raise AutomationDefaultsError("context must be an object", field="automation_defaults.context")
    unknown = sorted(k for k in context if k not in _CONTEXT_KEYS)
    if unknown:
        raise AutomationDefaultsError(f"unknown context field(s) {unknown}", field=f"automation_defaults.context.{unknown[0]}")
    mode = context.get("mode", "independent")
    if mode not in CONTEXT_MODES:
        raise AutomationDefaultsError(f"context.mode must be one of {'|'.join(CONTEXT_MODES)}", field="automation_defaults.context.mode")
    out["context"] = {"mode": mode}
    if "growing" in context:
        # The growing history budget of automations created from the flow (AbstractRuntime
        # `validate_context` takes `growing.max_tokens`); absent = the gateway's default
        # (`GET /api/gateway/automations/defaults` -> growing_max_tokens).
        growing = context["growing"]
        if mode != "growing":
            raise AutomationDefaultsError(
                "context.growing applies to the growing mode only", field="automation_defaults.context.growing"
            )
        if not isinstance(growing, Mapping):
            raise AutomationDefaultsError("context.growing must be an object", field="automation_defaults.context.growing")
        unknown = sorted(k for k in growing if k not in _GROWING_KEYS)
        if unknown:
            raise AutomationDefaultsError(
                f"unknown context.growing field(s) {unknown}", field=f"automation_defaults.context.growing.{unknown[0]}"
            )
        if "max_tokens" in growing:
            limit = growing["max_tokens"]
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise AutomationDefaultsError(
                    "context.growing.max_tokens must be a positive whole number of tokens",
                    field="automation_defaults.context.growing.max_tokens",
                )
            out["context"]["growing"] = {"max_tokens": int(limit)}

    input_data = raw.get("input_data", {})
    if not isinstance(input_data, Mapping):
        raise AutomationDefaultsError("input_data must be an object", field="automation_defaults.input_data")
    for key in sorted(k for k in input_data if k in SERVER_INPUT_KEYS):
        raise AutomationDefaultsError(f"input_data.{key} is set by the server, not by a workflow's defaults", field=f"automation_defaults.input_data.{key}")
    runtime_ns = input_data.get("_runtime")
    if runtime_ns is not None:
        if not isinstance(runtime_ns, Mapping):
            raise AutomationDefaultsError("input_data._runtime must be an object", field="automation_defaults.input_data._runtime")
        for key in sorted(k for k in runtime_ns if k not in CLIENT_RUNTIME_KEYS):
            raise AutomationDefaultsError(
                f"input_data._runtime.{key} cannot be set by a workflow's defaults (allowed: {', '.join(sorted(CLIENT_RUNTIME_KEYS))})",
                field=f"automation_defaults.input_data._runtime.{key}",
            )
    out["input_data"] = dict(input_data)
    return out


def validate_flow_automation_defaults(raw: Any, *, now: Optional[str] = None) -> Dict[str, Any]:
    """The normalized document value (defaults filled), or AutomationDefaultsError.

    The trigger config is checked by its source's adapter but stored as
    authored (the adapter's normalization fills `start_at = now`, which is
    only meaningful when an automation is created).
    """
    from abstractruntime.triggers.protocol import TriggerConfigError
    from abstractruntime.triggers.registry import UnknownTriggerSource, get_trigger_adapter

    value = _structural(raw)
    trigger = value["trigger"]
    try:
        adapter = get_trigger_adapter(trigger["source_id"], trigger["source_version"])
    except UnknownTriggerSource as exc:
        raise AutomationDefaultsError(str(exc), field="automation_defaults.trigger.source_id", reason_code="unknown_trigger_source") from exc
    from abstractruntime.triggers.connections import uses_connection_store

    config = dict(trigger["config"])
    needs = uses_connection_store(getattr(adapter, "descriptor", None) or {})
    cid = trigger.get("connection_id")
    if needs and not (cid or config.get("connection_id")):
        raise AutomationDefaultsError(
            f"trigger {trigger['source_id']}@{trigger['source_version']} listens to a connection: set trigger.connection_id",
            field="automation_defaults.trigger.connection_id",
            reason_code="connection_required",
        )
    if not needs and cid is not None:
        raise AutomationDefaultsError(
            f"trigger {trigger['source_id']}@{trigger['source_version']} takes no connection (remove trigger.connection_id)",
            field="automation_defaults.trigger.connection_id",
        )
    if needs and cid:
        if config.get("connection_id") not in (None, cid):
            raise AutomationDefaultsError(
                "trigger.connection_id and trigger.config.connection_id name different connections",
                field="automation_defaults.trigger.connection_id",
            )
        config["connection_id"] = cid
    try:
        adapter.validate(config, now=now or datetime.datetime.now(datetime.timezone.utc).isoformat())
    except TriggerConfigError as exc:
        raise AutomationDefaultsError(str(exc), field=f"automation_defaults.trigger.{exc.field}", reason_code=exc.reason_code) from exc
    return value


def manifest_automation_defaults(manifest: Any) -> Dict[str, Any]:
    """`{flow_id: defaults}` from a bundle manifest, for its ENTRYPOINT flows only."""
    metadata = getattr(manifest, "metadata", None)
    raw = metadata.get("automation_defaults") if isinstance(metadata, dict) else None
    if not isinstance(raw, dict):
        return {}
    entry_ids = {str(getattr(ep, "flow_id", "") or "").strip() for ep in list(getattr(manifest, "entrypoints", None) or [])}
    return {str(fid): value for fid, value in raw.items() if str(fid) in entry_ids and isinstance(value, dict)}


def fold_connection_id(trigger: Any) -> Any:
    """`trigger.connection_id` (the flow defaults' and a client's form) moved into
    `trigger.config.connection_id` (the binding's form, what the runtime reads). Raises
    AutomationDefaultsError when both are given and differ. Non-dict input is returned as is."""
    if not isinstance(trigger, Mapping) or "connection_id" not in trigger:
        return trigger
    out = {k: v for k, v in trigger.items() if k != "connection_id"}
    cid = trigger.get("connection_id")
    config = dict(out.get("config") or {}) if isinstance(out.get("config"), Mapping) else {}
    if config.get("connection_id") not in (None, cid):
        raise AutomationDefaultsError("trigger.connection_id and trigger.config.connection_id name different connections", field="trigger.connection_id")
    if cid is not None:
        config["connection_id"] = cid
    out["config"] = config
    return out


__all__ = [
    "AUTOMATION_DEFAULTS_SCHEMA_VERSION",
    "AutomationDefaultsError",
    "fold_connection_id",
    "manifest_automation_defaults",
    "validate_flow_automation_defaults",
]
