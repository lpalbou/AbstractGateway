"""`automation_defaults` on VisualFlow documents and published bundles (contract C6).

A runnable flow may carry the defaults of the automations created from it:

    automation_defaults = {schema_version: 1, title?: str,
                           trigger: {source_id, source_version: int, config: object},
                           context: {mode: "independent"|"growing"}, input_data: object}

No binding_id, credentials or instantiated ids: those are server-owned and
minted when an automation is created. The gateway validates the field on
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
_TRIGGER_KEYS = frozenset({"source_id", "source_version", "config"})
_CONTEXT_KEYS = frozenset({"mode"})


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

    input_data = raw.get("input_data", {})
    if not isinstance(input_data, Mapping):
        raise AutomationDefaultsError("input_data must be an object", field="automation_defaults.input_data")
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
    try:
        adapter.validate(trigger["config"], now=now or datetime.datetime.now(datetime.timezone.utc).isoformat())
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


__all__ = [
    "AUTOMATION_DEFAULTS_SCHEMA_VERSION",
    "AutomationDefaultsError",
    "manifest_automation_defaults",
    "validate_flow_automation_defaults",
]
