"""The tool list of a run started WITHOUT `input_data.tools` (Agent email tools).

THE PROBLEM THIS SOLVES (0.7.0 Linux end-to-end, F2): a raw API start of the
default agent (`POST /runs/start {"bundle_id": "basic-agent", ...}`) that sends
no `input_data.tools` runs with the entry flow's start-node default list
(`on_flow_start` `pinDefaults.tools`: the nine file/web/command tools). The
email tools were never in that list, so a user whose "Agent email tools" are
active (`/me/email` `agent_tools.active: true`, `/discovery/tools` rows
`enabled: true`) got an agent that could not send mail and improvised with
`execute_command 'echo Email sent'` instead. Clients that seed the list from
`/discovery/tools` were unaffected.

THE RULE (facts only, no names guessed):
- it applies only when the caller sent NO `tools` key (any explicit value,
  an empty list included, is the caller's ceiling and is never widened);
- the entry flow must declare a `tools`-typed output pin on its entry
  `on_flow_start` node with a list default (`pinDefaults.tools`) — that list
  IS the default this module extends; a flow without one is untouched;
- this host's agent email tools must be active
  (`mail.accounts.agent_tools_active` on the host's own email plane: the
  administrator made them available, the account is connected and allowed,
  the user's toggle is on) — the same predicate the host's agent toolsets and
  `/discovery/tools` are built with, so the added names are exactly the email
  rows the caller sees enabled;
- the names added are AbstractRuntime's email kind
  (`comms_toolset_kinds()["email"]`), the catalog rows that already exist.

The host calls `apply_default_email_tools` once per start, after the
workflow id is resolved (hosts/bundle_host.py `start_run`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

#: The start-node output pin whose default is the run's tool allowlist, and its pin type.
TOOLS_PIN_ID = "tools"
TOOLS_PIN_TYPE = "tools"


def email_tool_names() -> List[str]:
    """AbstractRuntime's email tool names (the `email` comms kind), in catalog order."""

    from abstractruntime.integrations.abstractcore.default_tools import comms_toolset_kinds

    return list(comms_toolset_kinds()["email"])


def start_node_default_tools(visualflow: Mapping[str, Any]) -> Optional[List[str]]:
    """The `pinDefaults.tools` list of the flow's ENTRY `on_flow_start` node, or None
    when the entry node is not a flow start, declares no `tools`-typed output pin, or
    has no list default for it (the executor then never seeds a tool list from it)."""

    entry_id = visualflow.get("entryNode")
    nodes = visualflow.get("nodes")
    if not isinstance(entry_id, str) or not isinstance(nodes, list):
        return None
    entry = next((n for n in nodes if isinstance(n, dict) and n.get("id") == entry_id), None)
    if entry is None or str(entry.get("type") or "") != "on_flow_start":
        return None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None
    outputs = data.get("outputs")
    declares_tools_pin = isinstance(outputs, list) and any(
        isinstance(p, dict) and p.get("id") == TOOLS_PIN_ID and p.get("type") == TOOLS_PIN_TYPE for p in outputs
    )
    if not declares_tools_pin:
        return None
    defaults = data.get("pinDefaults")
    raw = defaults.get(TOOLS_PIN_ID) if isinstance(defaults, dict) else None
    if not isinstance(raw, list):
        return None
    return [t for t in raw if isinstance(t, str) and t]


def with_email_tools(defaults: List[str], email_names: List[str]) -> List[str]:
    """`defaults` followed by the email names it does not already carry (order kept)."""

    present = set(defaults)
    return list(defaults) + [n for n in email_names if n not in present]


def entry_visualflow(host: Any, workflow_id: str) -> Optional[Dict[str, Any]]:
    """The raw VisualFlow JSON of a resolved bundle workflow id (`bundle@version:flow`)
    loaded on `host`, or None for a workflow that is not a loaded bundle flow (dynamic
    and scheduled wrapper workflows, native loops)."""

    wid = str(workflow_id or "")
    if ":" not in wid:
        return None
    prefix, flow_id = wid.split(":", 1)
    if "@" not in prefix:
        return None
    bundle_id, version = prefix.split("@", 1)
    versions = host.bundles.get(bundle_id)
    bundle = versions.get(version) if isinstance(versions, dict) else None
    if bundle is None:
        return None
    rel = bundle.manifest.flow_path_for(flow_id)
    if not rel:
        return None
    raw = bundle.read_json(rel)
    return raw if isinstance(raw, dict) else None


def host_email_tools_active(host: Any) -> bool:
    """This host's agent email tools state, from the same plane the host's toolsets
    were built with (hosts/bundle_host.py `load_from_dir`)."""

    from .mail import accounts
    from .mail.runtime_wiring import plane_for_host

    plane = plane_for_host(
        data_root=Path(host.data_dir).expanduser().resolve(),
        tenant_id=host.catalog_tenant_id,
        user_id=host.catalog_user_id,
        runtime_id=host.catalog_runtime_id,
    )
    return bool(accounts.agent_tools_active(plane))


def apply_default_email_tools(host: Any, *, workflow_id: str, vars0: Dict[str, Any]) -> List[str]:
    """Give a run started without `tools` the start-node default list PLUS the email
    tools when this host's agent email tools are active. Mutates `vars0["tools"]` only
    in that case; returns the email tool names added (empty = nothing changed)."""

    if TOOLS_PIN_ID in vars0:
        return []
    visualflow = entry_visualflow(host, workflow_id)
    if visualflow is None:
        return []
    defaults = start_node_default_tools(visualflow)
    if defaults is None:
        return []
    if not host_email_tools_active(host):
        return []
    tools = with_email_tools(defaults, email_tool_names())
    vars0[TOOLS_PIN_ID] = tools
    return tools[len(defaults):]
