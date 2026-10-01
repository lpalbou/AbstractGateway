"""The MCP server registry: admin writes, encrypted header values, real handshake tests (DESIGN-v3 §6.2).

    <data_dir>/config/mcp_servers.json     the registry. Version 1 (hand-written, read-only rows
                                           {name, url?, description?, auth_required?, tags?}) is
                                           still read; version 2 is written the first time an
                                           admin edits it:
                                           {name, transport: "stdio"|"http", command, args[], cwd?,
                                            url?, headers: {<name>: {"fingerprint"}}, description,
                                            archived, last_test{ok, at, message, server_info,
                                            tools[{name, description}]}}
    <data_dir>/config/mcp_secrets/         the header VALUES, sealed with AbstractCore's
                                           SecretVault (AES-256-GCM; key in the OS keychain, a
                                           0600 key file when there is none) — never in the JSON
                                           file, never returned by the API (fingerprints only).

A server's tools are offered to agent runs only when an admin turned **Enabled for agents** on
(`enabled_for_agents`), the server is not archived and its last connection test succeeded (the test
stores each tool's name, description and input schema); `mcp_run_tools.py` does the rest. A test
connects with AbstractCore's MCP clients through AbstractRuntime's facade (initialize,
notifications/initialized, tools/list with pagination), within 10 seconds; a stdio server is started
in a scratch folder with a minimal environment and terminated afterwards.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REGISTRY_FILENAME = "mcp_servers.json"
SECRETS_DIRNAME = "mcp_secrets"
TEST_TIMEOUT_S = 10.0
TRANSPORTS = ("stdio", "http")
AGENTS_NOTE = (
    "Agents can't call MCP tools yet: registering a server records it and checks the connection; "
    "using its tools in runs comes in a later version."
)
# Served instead of AGENTS_NOTE once at least one server is offered to agents.
AGENTS_OFFERED_NOTE = (
    "Tools from enabled servers are offered to your agents. Each call asks for approval unless you allow all tools."
)
_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
_lock = threading.Lock()


class McpRegistryError(Exception):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def registry_path(data_dir: Path) -> Path:
    return Path(data_dir) / "config" / REGISTRY_FILENAME


def _vault(data_dir: Path) -> Any:
    from .mail.core_mail import SecretVault

    return SecretVault(Path(data_dir) / "config" / SECRETS_DIRNAME)


def fingerprint(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------------------ read


def _normalize_row(raw: Dict[str, Any]) -> Dict[str, Any]:
    """A registry row (v1 or v2) in the v2 shape, without secrets."""
    name = str(raw.get("name") or "").strip()
    url = str(raw.get("url") or "").strip()
    command = raw.get("command")
    if isinstance(command, list):  # factory-style list: first item is the command
        parts = [str(c) for c in command if str(c).strip()]
        command, args = (parts[0], parts[1:]) if parts else ("", [])
    else:
        command = str(command or "").strip()
        args = [str(a) for a in (raw.get("args") or []) if isinstance(a, (str, int, float))]
    transport = str(raw.get("transport") or "").strip().lower()
    if transport in ("streamable_http", "https"):
        transport = "http"
    if transport not in TRANSPORTS:
        transport = "stdio" if command and not url else "http"
    headers: Dict[str, Dict[str, str]] = {}
    raw_headers = raw.get("headers")
    if isinstance(raw_headers, dict):
        for key, val in raw_headers.items():
            k = str(key or "").strip()
            if not k:
                continue
            fp = val.get("fingerprint") if isinstance(val, dict) else None
            headers[k] = {"fingerprint": str(fp or "")}
    row: Dict[str, Any] = {
        "name": name,
        "transport": transport,
        "command": command if transport == "stdio" else "",
        "args": args if transport == "stdio" else [],
        "cwd": str(raw.get("cwd") or "").strip() or None,
        "url": url if transport == "http" else "",
        "headers": headers,
        "description": str(raw.get("description") or "").strip(),
        "archived": bool(raw.get("archived")),
        "enabled_for_agents": bool(raw.get("enabled_for_agents")),
        "last_test": raw.get("last_test") if isinstance(raw.get("last_test"), dict) else None,
    }
    if "auth_required" in raw:
        row["auth_required"] = bool(raw.get("auth_required"))
    tags = raw.get("tags")
    if isinstance(tags, list):
        clean = [str(t).strip() for t in tags if str(t or "").strip()]
        if clean:
            row["tags"] = clean
    return row


def read_registry(data_dir: Path) -> Dict[str, Any]:
    """{version, servers[v2 rows], source, warnings}. Malformed rows become warnings."""
    path = registry_path(data_dir)
    out: Dict[str, Any] = {"version": None, "servers": [], "source": None, "warnings": []}
    if not path.is_file():
        return out
    out["source"] = str(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        out["warnings"].append(f"The MCP server registry {path} could not be read: {e}.")
        return out
    rows = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        out["warnings"].append(f'The MCP server registry {path} must be {{"version": 2, "servers": [...]}}.')
        return out
    out["version"] = data.get("version")
    seen: set = set()
    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            out["warnings"].append(f"#FALLBACK servers[{i}] is not an object — skipped")
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            out["warnings"].append(f"#FALLBACK servers[{i}] has no name — skipped")
            continue
        if name in seen:
            out["warnings"].append(f"#FALLBACK duplicate MCP server name {name!r} — later row skipped")
            continue
        seen.add(name)
        out["servers"].append(_normalize_row(raw))
    return out


def _write_registry(data_dir: Path, servers: List[Dict[str, Any]]) -> None:
    path = registry_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"version": 2, "servers": servers}
    fd, tmp = tempfile.mkstemp(prefix=".mcp_servers.", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def _secrets(data_dir: Path) -> Dict[str, Dict[str, str]]:
    vault = _vault(data_dir)
    if not vault.exists():
        return {}
    payload = vault.load() or {}
    servers = payload.get("servers") if isinstance(payload, dict) else None
    return {str(k): {str(h): str(v) for h, v in (hv or {}).items()} for k, hv in (servers or {}).items() if isinstance(hv, dict)}


def _store_secrets(data_dir: Path, secrets: Dict[str, Dict[str, str]]) -> None:
    vault = _vault(data_dir)
    clean = {k: v for k, v in secrets.items() if v}
    if clean:
        vault.store({"servers": clean}, reuse_key=vault.exists())
    elif vault.exists():
        vault.store({"servers": {}}, reuse_key=True)


# ----------------------------------------------------------------------------- write


def _validate_name(name: Any) -> str:
    text = str(name or "").strip()
    if not text:
        raise McpRegistryError("Give the server a name.")
    if len(text) > 64 or not set(text) <= _NAME_CHARS:
        raise McpRegistryError(
            "A server name uses letters, digits, '-', '_' or '.', at most 64 characters (it names the server's tools later)."
        )
    return text


def _config_from_body(body: Dict[str, Any], *, existing: Optional[Dict[str, Any]], stored_headers: Dict[str, str]) -> tuple:
    """(row without name, header values) from a create/edit/test body."""
    transport = str(body.get("transport") or (existing or {}).get("transport") or "").strip().lower()
    if transport not in TRANSPORTS:
        raise McpRegistryError("Choose how to reach the server: a command (stdio) or a URL (http).")
    row: Dict[str, Any] = {"transport": transport, "description": str(body.get("description") or "").strip()}
    if transport == "stdio":
        command = str(body.get("command") or "").strip()
        if not command:
            raise McpRegistryError("Give the command that starts the server (for example npx -y @modelcontextprotocol/server-everything).")
        args = body.get("args") or []
        if not isinstance(args, list):
            raise McpRegistryError("args must be a list of strings.")
        row.update({"command": command, "args": [str(a) for a in args], "url": "", "cwd": str(body.get("cwd") or "").strip() or None})
        values: Dict[str, str] = {}
    else:
        url = str(body.get("url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            raise McpRegistryError("Give the server's URL, starting with http:// or https://.")
        row.update({"url": url, "command": "", "args": [], "cwd": None})
        raw_headers = body.get("headers") or {}
        if not isinstance(raw_headers, dict):
            raise McpRegistryError("headers must be an object of name: value.")
        values = {}
        for key, val in raw_headers.items():
            k = str(key or "").strip()
            if not k:
                continue
            if any(c in k for c in ":\r\n "):
                raise McpRegistryError(f"{k!r} is not a valid header name.")
            if val is None:
                # Masked row the admin did not retype: keep the stored value.
                if k not in stored_headers:
                    raise McpRegistryError(f"The header {k} has no value; type one.")
                values[k] = stored_headers[k]
            else:
                values[k] = str(val)
    row["headers"] = {k: {"fingerprint": fingerprint(v)} for k, v in values.items()}
    return row, values


def create_server(data_dir: Path, body: Dict[str, Any]) -> Dict[str, Any]:
    name = _validate_name(body.get("name"))
    with _lock:
        reg = read_registry(data_dir)
        if any(s["name"] == name for s in reg["servers"]):
            raise McpRegistryError(f"A server named {name!r} is already registered (archived ones count too).", status=409)
        row, values = _config_from_body(body, existing=None, stored_headers={})
        full = {"name": name, **row, "archived": False, "enabled_for_agents": False, "last_test": None}
        secrets = _secrets(data_dir)
        secrets[name] = values
        _store_secrets(data_dir, secrets)
        _write_registry(data_dir, reg["servers"] + [full])
    return _normalize_row(full)


def _find(reg: Dict[str, Any], name: str) -> Dict[str, Any]:
    for s in reg["servers"]:
        if s["name"] == name:
            return s
    raise McpRegistryError(f"There is no MCP server named {name!r}.", status=404)


def update_server(data_dir: Path, name: str, body: Dict[str, Any]) -> Dict[str, Any]:
    with _lock:
        reg = read_registry(data_dir)
        current = _find(reg, name)
        if body.get("name") not in (None, "", name):
            raise McpRegistryError("A server's name cannot change; register it again under the new name.")
        secrets = _secrets(data_dir)
        row, values = _config_from_body(body, existing=current, stored_headers=secrets.get(name, {}))
        changed_conn = any(current.get(k) != row.get(k) for k in ("transport", "command", "args", "url", "cwd")) or (
            values != secrets.get(name, {})
        )
        current.update(row)
        if changed_conn:
            current["last_test"] = None
        secrets[name] = values
        _store_secrets(data_dir, secrets)
        _write_registry(data_dir, reg["servers"])
    return _normalize_row(current)


def set_archived(data_dir: Path, name: str, archived: bool) -> Dict[str, Any]:
    with _lock:
        reg = read_registry(data_dir)
        current = _find(reg, name)
        current["archived"] = bool(archived)
        _write_registry(data_dir, reg["servers"])
    return _normalize_row(current)


def offered_to_agents(row: Dict[str, Any]) -> bool:
    """A server whose tools agent runs are offered: enabled for agents, not archived, last test OK."""
    last = row.get("last_test") if isinstance(row.get("last_test"), dict) else {}
    return bool(row.get("enabled_for_agents")) and not row.get("archived") and bool(last.get("ok"))


def agents_status(row: Dict[str, Any]) -> str:
    """The row's one-line agents status for the page."""
    if row.get("archived"):
        return "Not offered: archived"
    if not row.get("enabled_for_agents"):
        return "Not offered to agents"
    last = row.get("last_test") if isinstance(row.get("last_test"), dict) else {}
    if not last.get("ok"):
        return "Not offered: test the connection first"
    n = len(last.get("tools") or [])
    return f"Offered to agents · {n} tool{'s' if n != 1 else ''}"


def set_enabled_for_agents(data_dir: Path, name: str, enabled: bool) -> Dict[str, Any]:
    """Turn **Enabled for agents** on or off. Turning it on needs a live, tested server."""
    with _lock:
        reg = read_registry(data_dir)
        current = _find(reg, name)
        if enabled and current.get("archived"):
            raise McpRegistryError(f"{name} is archived: unarchive it first, then enable it for agents.", status=409)
        last = current.get("last_test") if isinstance(current.get("last_test"), dict) else {}
        if enabled and not last.get("ok"):
            raise McpRegistryError(f"Test the connection to {name} first: agents are offered the tools a successful test listed.", status=409)
        current["enabled_for_agents"] = bool(enabled)
        _write_registry(data_dir, reg["servers"])
    return _normalize_row(current)


def _record_test(data_dir: Path, name: str, result: Dict[str, Any]) -> None:
    with _lock:
        reg = read_registry(data_dir)
        current = _find(reg, name)
        current["last_test"] = {k: result.get(k) for k in ("ok", "at", "message", "server_info", "tools")}
        _write_registry(data_dir, reg["servers"])


# ------------------------------------------------------------------------------ test


def _minimal_env() -> Dict[str, str]:
    keep = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA")
    return {k: v for k, v in os.environ.items() if k.upper() in keep}


def _shell_words(command: str, args: List[str]) -> List[str]:
    return [command] + list(args)


def _display_command(command: str, args: List[str]) -> str:
    return " ".join([command] + list(args)).strip()


def _sentence_for(exc: BaseException, *, transport: str, target: str, stage: str) -> str:
    from abstractruntime.integrations.abstractcore.mcp_facade import McpHttpError, McpProtocolError, McpRpcError

    text = str(exc).strip()
    first = text.splitlines()[0] if text else type(exc).__name__
    if isinstance(exc, FileNotFoundError):
        return f"Couldn't start `{target}`: command not found."
    if isinstance(exc, PermissionError):
        return f"Couldn't start `{target}`: permission denied (is it executable?)."
    if isinstance(exc, NotADirectoryError) or (isinstance(exc, OSError) and getattr(exc, "filename", None) and stage == "start"):
        return f"Couldn't start `{target}`: {first}."
    if isinstance(exc, McpRpcError):
        if stage == "initialize":
            return f"The server answered but refused initialize: {exc.message}"
        return f"The server completed the handshake but tools/list failed: {exc.message}"
    if isinstance(exc, McpHttpError):
        if "MCP HTTP 401" in text or "MCP HTTP 403" in text:
            return f"The server refused the request ({first.replace('MCP ', '')}); check the headers (an Authorization header is often required)."
        if "MCP HTTP" in text:
            return f"The server at {target} answered with an error: {first.replace('MCP ', '')}"
        return f"Couldn't reach {target}: {first.replace('MCP request failed: ', '')}"
    if isinstance(exc, McpProtocolError):
        return f"The server's answer is not valid MCP ({stage}): {first}"
    if "closed unexpectedly" in text:
        tail = [ln for ln in text.splitlines()[1:] if ln.strip() and not ln.startswith("stderr tail")]
        why = tail[-1].strip() if tail else "no output"
        return f"`{target}` exited before answering {stage}: {why}"
    return f"The connection test failed during {stage}: {first}"


def run_connection_test(config: Dict[str, Any], header_values: Dict[str, str], *, timeout_s: float = TEST_TIMEOUT_S, scratch_parent: Optional[Path] = None) -> Dict[str, Any]:
    """Connect, run the MCP handshake and list the tools; never raises.

    Returns {ok, message, server_info, tools[{name, description}], at, duration_ms}.
    """
    from abstractruntime.integrations.abstractcore.mcp_facade import open_mcp_client

    transport = config["transport"]
    started = time.monotonic()
    holder: Dict[str, Any] = {"stage": "start"}
    done = threading.Event()
    scratch: Optional[str] = None
    if transport == "stdio":
        target = _display_command(config["command"], config.get("args") or [])
        if not config.get("cwd"):
            if scratch_parent is not None:
                Path(scratch_parent).mkdir(parents=True, exist_ok=True)
            scratch = tempfile.mkdtemp(prefix="mcp-test-", dir=str(scratch_parent) if scratch_parent else None)
    else:
        target = config["url"]

    def work() -> None:
        try:
            if transport == "stdio":
                client = open_mcp_client(transport="stdio", command=config["command"], args=config.get("args") or [],
                                         cwd=config.get("cwd") or scratch, env=_minimal_env(), timeout_s=timeout_s)
            else:
                client = open_mcp_client(transport="http", url=config["url"], headers=dict(header_values), timeout_s=timeout_s)
            holder["client"] = client
            holder["stage"] = "initialize"
            info = client.initialize()
            holder["info"] = info
            holder["stage"] = "tools/list"
            holder["tools"] = client.list_tools()
        except BaseException as exc:  # noqa: BLE001 - every failure becomes a sentence
            holder["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=work, name="mcp-connection-test", daemon=True)
    thread.start()
    finished = done.wait(timeout_s)
    client = holder.get("client")
    if client is not None:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        proc = getattr(client, "_proc", None)
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001
                pass
    thread.join(timeout=2)
    if scratch:
        shutil.rmtree(scratch, ignore_errors=True)

    result: Dict[str, Any] = {"ok": False, "message": "", "server_info": None, "tools": [], "at": _now(),
                              "duration_ms": int((time.monotonic() - started) * 1000)}
    info = holder.get("info") or {}
    if isinstance(info, dict) and info:
        si = info.get("serverInfo") if isinstance(info.get("serverInfo"), dict) else {}
        result["server_info"] = {"name": str(si.get("name") or ""), "version": str(si.get("version") or ""),
                                 "protocol_version": str(info.get("protocolVersion") or "")}
    if not finished:
        result["message"] = f"The server did not answer {holder['stage']} within {int(timeout_s)} seconds."
        return result
    if holder.get("error") is not None:
        result["message"] = _sentence_for(holder["error"], transport=transport, target=target, stage=holder["stage"])
        return result
    tools = []
    for t in holder.get("tools") or []:
        name = str(t.get("name") or "").strip()
        if name:
            row = {"name": name, "description": str(t.get("description") or t.get("title") or "").strip()}
            if isinstance(t.get("inputSchema"), dict):
                row["input_schema"] = t["inputSchema"]  # the parameters agents are offered
            tools.append(row)
    result["tools"] = tools
    result["ok"] = True
    si = result["server_info"] or {}
    who = " ".join(x for x in (si.get("name"), si.get("version")) if x) or "the server"
    result["message"] = f"Connected to {who} · {len(tools)} tool{'s' if len(tools) != 1 else ''}."
    return result


def check_saved(data_dir: Path, name: str) -> Dict[str, Any]:
    reg = read_registry(data_dir)
    row = _find(reg, name)
    values = _secrets(data_dir).get(name, {})
    if row["transport"] == "http" and set(row.get("headers") or {}) - set(values):
        missing = ", ".join(sorted(set(row["headers"]) - set(values)))
        result = {"ok": False, "message": f"The stored value of {missing} is missing; edit the server and type it again.",
                  "server_info": None, "tools": [], "at": _now(), "duration_ms": 0}
    else:
        result = run_connection_test(row, values, scratch_parent=Path(data_dir) / "tmp" / "mcp-tests")
    _record_test(data_dir, name, result)
    return result


def check_unsaved(data_dir: Path, body: Dict[str, Any]) -> Dict[str, Any]:
    name = str(body.get("name") or "").strip()
    stored: Dict[str, str] = {}
    existing = None
    if name:
        reg = read_registry(data_dir)
        existing = next((s for s in reg["servers"] if s["name"] == name), None)
        if existing is not None:
            stored = _secrets(data_dir).get(name, {})
    row, values = _config_from_body(body, existing=existing, stored_headers=stored)
    return run_connection_test(row, values, scratch_parent=Path(data_dir) / "tmp" / "mcp-tests")


def public_inventory(data_dir: Path) -> Dict[str, Any]:
    """GET /mcp/servers: v2 rows (headers as fingerprints only) + the honest agents note."""
    reg = read_registry(data_dir)
    for row in reg["servers"]:
        row["offered_to_agents"] = offered_to_agents(row)
        row["agents_status"] = agents_status(row)
    offered = any(row["offered_to_agents"] for row in reg["servers"])
    out: Dict[str, Any] = {
        "servers": reg["servers"],
        "source": reg["source"],
        "version": reg["version"],
        "probed": False,
        "agents_can_call": offered,
        "agents_note": AGENTS_OFFERED_NOTE if offered else AGENTS_NOTE,
        "warnings": list(reg["warnings"]),
    }
    if reg["source"] is None:
        out["warnings"].append(
            f"no MCP server registry declared (add a server in the console, or create {registry_path(data_dir)})"
        )
    return out
