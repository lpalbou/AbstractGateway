"""The durable command types the gateway accepts: ONE source of truth.

`POST /api/gateway/commands` (the door), the runner (`_apply_command`, the
applier) and `GET /api/gateway/discovery/capabilities` (`runs.commands.types`,
`automations.command_types`) all read these tuples, so a type can never be
accepted at the door and then refused by the runner, or advertised without
being accepted (`tests/test_automation_command_types.py` keeps them equal).

Automation commands (`automation.*`) target an automation root run
(`run_id = automation_id`); the runner hands them to AbstractRuntime's
`apply_automation_command`, which records the applied/rejected outcome in the
automation's ledger.
"""

from __future__ import annotations

LEGACY_COMMAND_TYPES: tuple[str, ...] = (
    "pause",
    "resume",
    "cancel",
    "conclude",
    "emit_event",
    "update_schedule",
    "compact_memory",
    "inject_guidance",
)

AUTOMATION_COMMAND_TYPES: tuple[str, ...] = (
    "automation.revise",
    "automation.pause",
    "automation.resume",
    "automation.run_now",
    "automation.stop_current",
    "automation.archive",
    "automation.unarchive",
)

COMMAND_TYPES: tuple[str, ...] = LEGACY_COMMAND_TYPES + AUTOMATION_COMMAND_TYPES

#: What `AutomationSummary.capabilities` lists for a non-legacy automation:
#: the command suffixes plus `discuss`.
AUTOMATION_SUMMARY_CAPABILITIES: tuple[str, ...] = tuple(t.split(".", 1)[1] for t in AUTOMATION_COMMAND_TYPES) + ("discuss",)


def is_automation_command_type(value: str) -> bool:
    return str(value or "") in AUTOMATION_COMMAND_TYPES


__all__ = [
    "AUTOMATION_COMMAND_TYPES",
    "AUTOMATION_SUMMARY_CAPABILITIES",
    "COMMAND_TYPES",
    "LEGACY_COMMAND_TYPES",
    "is_automation_command_type",
]
