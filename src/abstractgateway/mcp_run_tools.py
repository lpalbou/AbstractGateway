"""Tools of registered MCP servers in agent runs (round 3, DESIGN-v3 §6.2 follow-up).

WHICH SERVERS: the admin registry (`mcp_registry.py`, gateway root data dir) rows that are
`offered_to_agents`: **Enabled for agents** on, not archived, last connection test OK. Their tools are
the ones that test listed (name, description, input schema), named `mcp::<server>::<tool>`.

WHERE THEY APPEAR:
- the Agent nodes' tool registry (`hosts/bundle_host.py`: the host's ReActLogic reads
  `offered_tool_specs` live, so turning a server on or off needs no host rebuild);
- `/discovery/tools` rows (`toolset: "mcp:<server>"`, `mcp_server`), the source of the Code web and
  Assistant tool pickers;
- a run started without `tools` gets them after the start-node defaults (the email-tools rule).
A run is offered an MCP tool only when it is in the run's tool list, like every other tool.

HOW A CALL RUNS: `McpRoutingToolExecutor` sits UNDER the approval gate (`ApprovalToolExecutor`): an
MCP tool is on no auto-approve list, so each call asks unless the run's policy allows it ("allow all
tools"). At call time the registry is read again — a server disabled, archived or no longer listing
the tool refuses the call with a sentence — header values are unsealed here and only here (never in
run vars, specs, the ledger or the prompt), a client is opened (stdio: minimal environment, scratch
folder; http: the stored headers), `initialize` + `tools/call` run within CALL_TIMEOUT_S, and the
client is closed (a stdio server is killed).

RUN START (`prepare_run_mcp_tools`): MCP names of servers not offered leave the run's tool lists; each
offered server the run names is checked with `initialize` (PREFLIGHT_TIMEOUT_S); one that fails is
skipped — its tools leave the lists and `_runtime.mcp_notes` says why — and the run goes on. A run
that reads text written by other people (`_runtime.untrusted_input`, email-triggered occurrences) is
never offered an MCP tool (AbstractRuntime's controller also removes them and never grants them).

AbstractCore is reached only through AbstractRuntime's `mcp_facade` (the gateway import boundary).
"""
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PREFLIGHT_TIMEOUT_S = 5.0
CALL_TIMEOUT_S = 30.0
TOOLS_KEY = "tools"

_cache_lock = threading.Lock()
_spec_cache: Dict[str, Tuple[Any, List[Dict[str, Any]]]] = {}


def _facade() -> Any:
    from abstractruntime.integrations.abstractcore import mcp_facade

    return mcp_facade


def _registry_stamp(data_dir: Path) -> Any:
    from .mcp_registry import registry_path

    try:
        st = registry_path(data_dir).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def offered_servers(data_dir: Path) -> List[Dict[str, Any]]:
    """Registry rows whose tools agent runs are offered."""
    from .mcp_registry import offered_to_agents, read_registry

    return [row for row in read_registry(Path(data_dir))["servers"] if offered_to_agents(row)]


def offered_tool_specs(data_dir: Path) -> List[Dict[str, Any]]:
    """The agent-facing specs of every offered tool (cached on the registry file's stamp)."""
    key = str(Path(data_dir))
    stamp = _registry_stamp(Path(data_dir))
    with _cache_lock:
        hit = _spec_cache.get(key)
        if hit is not None and hit[0] == stamp:
            return list(hit[1])
    specs: List[Dict[str, Any]] = []
    if stamp is not None:
        facade = _facade()
        for row in offered_servers(Path(data_dir)):
            for tool in (row.get("last_test") or {}).get("tools") or []:
                entry = {"name": tool.get("name"), "description": tool.get("description") or "",
                         "inputSchema": tool.get("input_schema") or {}}
                try:
                    spec = facade.mcp_tool_spec(server_id=row["name"], tool=entry, transport=row["transport"])
                except Exception as exc:  # noqa: BLE001 - one bad tool must not hide the others
                    logger.warning("MCP tool %r of %s skipped: %s", tool.get("name"), row["name"], exc)
                    continue
                spec["toolset"] = f"mcp:{row['name']}"
                spec["mcp_server"] = row["name"]
                specs.append(spec)
    with _cache_lock:
        _spec_cache[key] = (stamp, specs)
    return list(specs)


def _split(name: Any) -> Optional[Tuple[str, str]]:
    return _facade().parse_namespaced_tool_name(str(name or ""))


def _open(row: Dict[str, Any], data_dir: Path, *, timeout_s: float) -> Tuple[Any, Optional[str]]:
    """(client, scratch dir to delete) for one registry row; headers unsealed here only."""
    from .mcp_registry import _minimal_env, _secrets

    facade = _facade()
    if row["transport"] == "http":
        headers = _secrets(Path(data_dir)).get(row["name"], {})
        return facade.open_mcp_client(transport="http", url=row["url"], headers=headers, timeout_s=timeout_s), None
    scratch = None
    if not row.get("cwd"):
        parent = Path(data_dir) / "tmp" / "mcp-runs"
        parent.mkdir(parents=True, exist_ok=True)
        scratch = tempfile.mkdtemp(prefix="mcp-call-", dir=str(parent))
    try:
        client = facade.open_mcp_client(transport="stdio", command=row["command"], args=row.get("args") or [],
                                        cwd=row.get("cwd") or scratch, env=_minimal_env(), timeout_s=timeout_s)
    except BaseException:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
        raise
    return client, scratch


def _close(client: Any, scratch: Optional[str]) -> None:
    if client is not None:
        _facade().close_mcp_client(client)
    if scratch:
        shutil.rmtree(scratch, ignore_errors=True)


def _why(exc: BaseException, row: Dict[str, Any], stage: str) -> str:
    from .mcp_registry import _display_command, _sentence_for

    target = _display_command(row["command"], row.get("args") or []) if row["transport"] == "stdio" else row["url"]
    return _sentence_for(exc, transport=row["transport"], target=target, stage=stage)


def _find_row(data_dir: Path, server: str) -> Optional[Dict[str, Any]]:
    from .mcp_registry import read_registry

    return next((r for r in read_registry(Path(data_dir))["servers"] if r["name"] == server), None)


def call_refusal(row: Optional[Dict[str, Any]], server: str, tool: str) -> Optional[str]:
    """Why a call to `mcp::<server>::<tool>` is refused right now, or None."""
    from .mcp_registry import offered_to_agents

    if row is None:
        return f"There is no MCP server named {server}."
    if row.get("archived"):
        return f"The MCP server {server} is archived: its tools are not offered to agents."
    if not row.get("enabled_for_agents"):
        return f"The MCP server {server} is not enabled for agents."
    if not offered_to_agents(row):
        return f"The MCP server {server} failed its last connection test: an admin must test it again."
    if tool not in {t.get("name") for t in (row.get("last_test") or {}).get("tools") or []}:
        return f"The MCP server {server} does not list a tool named {tool} (test the connection again)."
    return None


class McpRoutingToolExecutor:
    """Routes `mcp::<server>::<tool>` calls to the registered server; everything else to `delegate`.

    Placed under the approval gate, never above it. `_delegate` keeps runtime's delegate-chain walks
    (endpoint-profile resolver attach) reaching the inner MappingToolExecutor."""

    def __init__(self, delegate: Any, *, data_dir: Path, timeout_s: float = CALL_TIMEOUT_S) -> None:
        self._delegate = delegate
        self._data_dir = Path(data_dir)
        self._timeout_s = float(timeout_s)

    def execute(self, *, tool_calls: List[Dict[str, Any]]) -> Dict[str, Any]:
        calls = list(tool_calls or [])
        mcp_idx = [i for i, c in enumerate(calls) if isinstance(c, dict) and _split(c.get("name")) is not None]
        if not mcp_idx:
            return self._delegate.execute(tool_calls=calls)
        results: List[Any] = [None] * len(calls)
        others = [(i, c) for i, c in enumerate(calls) if i not in mcp_idx]
        if others:
            out = self._delegate.execute(tool_calls=[c for _, c in others])
            if out.get("mode") != "executed":
                return out
            for (i, _), res in zip(others, out.get("results") or []):
                results[i] = res
        for i in mcp_idx:
            results[i] = self._call(calls[i])
        return {"mode": "executed", "results": results}

    def _call(self, tc: Dict[str, Any]) -> Dict[str, Any]:
        name = str(tc.get("name") or "")
        server, tool = _split(name)  # type: ignore[misc]
        rid = tc.get("runtime_call_id")
        base = {"call_id": str(tc.get("call_id") or ""), "runtime_call_id": (str(rid).strip() or None) if rid is not None else None,
                "name": name}
        args = tc.get("arguments") if isinstance(tc.get("arguments"), dict) else {}
        row = _find_row(self._data_dir, server)
        refusal = call_refusal(row, server, tool)
        if refusal is not None:
            return {**base, "success": False, "output": None, "error": refusal}
        client, scratch, stage = None, None, "start"
        try:
            client, scratch = _open(row, self._data_dir, timeout_s=self._timeout_s)  # type: ignore[arg-type]
            stage = "initialize"
            client.initialize()
            stage = "tools/call"
            ok, output, err = _facade().call_mcp_tool(client, tool_name=tool, arguments=dict(args))
        except BaseException as exc:  # noqa: BLE001 - every failure is a tool error sentence
            return {**base, "success": False, "output": None, "error": _why(exc, row, stage)}  # type: ignore[arg-type]
        finally:
            _close(client, scratch)
        return {**base, "success": ok, "output": output, "error": err}


def _preflight(row: Dict[str, Any], data_dir: Path) -> Optional[str]:
    """None when the server completes `initialize` within PREFLIGHT_TIMEOUT_S, else why not."""
    holder: Dict[str, Any] = {}
    done = threading.Event()

    def work() -> None:
        stage = "start"
        try:
            holder["client"], holder["scratch"] = _open(row, data_dir, timeout_s=PREFLIGHT_TIMEOUT_S)
            stage = "initialize"
            holder["client"].initialize()
        except BaseException as exc:  # noqa: BLE001
            holder["error"] = _why(exc, row, stage)
        finally:
            done.set()

    thread = threading.Thread(target=work, name=f"mcp-preflight-{row['name']}", daemon=True)
    thread.start()
    finished = done.wait(PREFLIGHT_TIMEOUT_S)
    _close(holder.get("client"), holder.get("scratch"))
    if not finished:
        return f"The server did not answer initialize within {int(PREFLIGHT_TIMEOUT_S)} seconds."
    return holder.get("error")


def _untrusted(vars0: Dict[str, Any]) -> bool:
    rt = vars0.get("_runtime") if isinstance(vars0.get("_runtime"), dict) else {}
    pol = rt.get("tool_policy") if isinstance(rt.get("tool_policy"), dict) else {}
    return rt.get("untrusted_input") is True or pol.get("untrusted_input") is True


def prepare_run_mcp_tools(host: Any, *, workflow_id: str, vars0: Dict[str, Any], caller_sent_tools: bool) -> List[str]:
    """Run-start rules above. Mutates `vars0` (`tools`, `_runtime.allowed_tools`, `_runtime.mcp_notes`);
    returns the MCP tool names the run is offered."""
    from .run_default_tools import entry_visualflow, start_node_default_tools

    data_dir = Path(getattr(host, "catalog_root_data_dir", None) or host.data_dir)
    rt = vars0.setdefault("_runtime", {}) if isinstance(vars0.get("_runtime", {}), dict) else {}
    holders = [(vars0, TOOLS_KEY), (rt, "allowed_tools")]
    if _untrusted(vars0):
        for holder, key in holders:
            if isinstance(holder.get(key), list):
                holder[key] = [t for t in holder[key] if _split(t) is None]
        return []
    servers = {row["name"]: row for row in offered_servers(data_dir)}
    if not caller_sent_tools and servers:
        names = [s["name"] for s in offered_tool_specs(data_dir)]
        if isinstance(vars0.get(TOOLS_KEY), list):  # the email-tools rule already set the defaults
            vars0[TOOLS_KEY] = list(vars0[TOOLS_KEY]) + [n for n in names if n not in vars0[TOOLS_KEY]]
        else:
            visualflow = entry_visualflow(host, workflow_id)
            defaults = start_node_default_tools(visualflow) if visualflow is not None else None
            if defaults is not None:
                vars0[TOOLS_KEY] = list(defaults) + [n for n in names if n not in defaults]
    named = {sp[0] for holder, key in holders if isinstance(holder.get(key), list)
             for sp in (_split(t) for t in holder[key]) if sp is not None}
    notes: List[str] = [f"MCP server {name} is not offered to agents: its tools were removed from this run."
                        for name in sorted(set(named) - set(servers))]
    skipped = set(named) - set(servers)
    for server in sorted(named & set(servers)):
        why = _preflight(servers[server], data_dir)
        if why is not None:
            skipped.add(server)
            notes.append(f"MCP server {server} skipped: {why}")
            logger.warning("run start: MCP server %s skipped: %s", server, why)
    if skipped:
        for holder, key in holders:
            if isinstance(holder.get(key), list):
                holder[key] = [t for t in holder[key] if (_split(t) or ("",))[0] not in skipped]
    if notes:
        rt["mcp_notes"] = notes
    tools = vars0.get(TOOLS_KEY)
    return [t for t in tools if _split(t) is not None] if isinstance(tools, list) else []
