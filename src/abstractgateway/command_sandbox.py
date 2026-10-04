"""abstractgateway.command_sandbox -- the host side of the command sandbox (round 12, R12.1).

Every process-spawning tool (execute_command, the shell session, script runners) runs inside an
OS-level sandbox built by ``abstractcore.tools.sandbox`` from the run's effective workspace set —
the same flattened keys the file tools read (``run_workspace_guard.apply_workspace_policy``). The
gateway owns two HOST-level facts, set ONCE per process at boot, never per run and never from an
environment variable (DESIGN R12.1, "R12 SANDBOX SPEC — FINAL"):

- the base environment of every command: the gateway's SCRUBBED environment, computed by the very
  function the gateway uses for the apps it starts (``apps_manager._scrubbed_child_env``: no token,
  secret, password or key, nothing named ABSTRACTGATEWAY_* / ABSTRACTCORE_*);
- ``unsandboxed_commands_allowed``: off unless ``abstractgateway serve --unsandboxed-commands`` (or
  the split ``runner`` started with the same flag). It only matters on a host with no sandbox; it
  is audited at boot (``command_sandbox_configured``) and shown on the console as a state.

``configure_at_boot`` runs at the top of ``service.start_gateway_runner`` (the serve boot thread and
the split ``abstractgateway runner`` process). ``state`` is what the console and the TUI show.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

FLAG = "--unsandboxed-commands"

_LOCK = threading.Lock()
_flag: bool = False
_flag_source: Optional[str] = None
_configured: Optional[Dict[str, Any]] = None


def set_unsandboxed_commands(allowed: bool, *, source: str) -> None:
    """Record the CLI flag for this process (``serve`` / ``runner`` main, before the boot)."""
    global _flag, _flag_source
    with _LOCK:
        _flag = bool(allowed)
        _flag_source = str(source)


def _reset_for_tests() -> None:
    global _flag, _flag_source, _configured
    from abstractcore.tools.sandbox import _reset_host_for_tests

    with _LOCK:
        _flag, _flag_source, _configured = False, None, None
    _reset_host_for_tests()


def scrubbed_environment() -> Dict[str, str]:
    """The base environment of every command: the apps' scrub, applied to this process's env."""
    from .apps_manager import _scrubbed_child_env

    return _scrubbed_child_env(dict(os.environ))


def _audit(entry: Dict[str, Any]) -> None:
    try:
        from .security.gateway_security import _AUDIT_LOCK, _audit_data_dir_from_env, _audit_log_enabled

        if not _audit_log_enabled(default=True):
            return
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _AUDIT_LOCK:
            path = (_audit_data_dir_from_env() / "audit_log.jsonl").resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as fh:
                fh.write(line.encode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - auditing never breaks the boot
        return


def configure_at_boot() -> Dict[str, Any]:
    """Configure the core's host policy once for this process and audit it. A second call is a
    no-op that returns the first record with ``first: false`` (the env is frozen at the first boot
    of the process)."""
    global _configured
    from abstractcore.tools.sandbox import configure_host

    with _LOCK:
        if _configured is not None:
            return {**_configured, "first": False}
        env = scrubbed_environment()
        configure_host(env=env, unsandboxed_commands_allowed=_flag)
        st = _state_locked()
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": "command_sandbox_configured",
            "source": _flag_source or "default",
            "kind": st["kind"],
            "state": st["state"],
            "line": st["line"],
            "unsandboxed_commands_allowed": _flag,
            "env_keys": len(env),
        }
        _configured = record
    _audit(record)
    return {**record, "first": True}


def _kinds() -> Dict[str, str]:
    from abstractcore.tools.sandbox import host_sandbox_kind

    return {"allowed_only": host_sandbox_kind("allowed_only"), "any_except_denied": host_sandbox_kind("any_except_denied")}


def _state_locked() -> Dict[str, Any]:
    from abstractcore.tools.sandbox import KIND_LABELS, KIND_NONE, host_policy

    kinds = _kinds()
    policy = host_policy()
    allowed = bool(policy.get("configured") and policy.get("unsandboxed_commands_allowed")) if policy.get("configured") else _flag
    k_allow, k_any = kinds["allowed_only"], kinds["any_except_denied"]
    if k_allow != KIND_NONE and k_any != KIND_NONE:
        state, kind = "sandboxed", k_any
        line = f"Commands sandboxed: {KIND_LABELS[kind]}"
        sentence = "Every command a run starts is confined by the operating system to that run's workspaces."
    elif k_allow != KIND_NONE:
        # Landlock without bubblewrap: only the allow-list posture can be expressed.
        state, kind = "partial", k_allow
        if allowed:
            line = f"Commands sandboxed: {KIND_LABELS[kind]} (Deny everything, allow listed workspaces); otherwise unsandboxed commands allowed (flag)"
        else:
            line = f"Commands sandboxed: {KIND_LABELS[kind]} (Deny everything, allow listed workspaces); otherwise commands refused"
        sentence = "Landlock cannot express \"Allow everything, refuse listed workspaces\"; install bubblewrap to sandbox those runs too."
    elif allowed:
        state, kind = "unsandboxed", KIND_NONE
        line = "Unsandboxed commands allowed (flag)"
        sentence = (
            "This host has no command sandbox and the gateway was started with --unsandboxed-commands: commands run "
            "with the gateway's own file access (their environment is still scrubbed)."
        )
    else:
        state, kind = "refused", KIND_NONE
        line = "Commands refused: no sandbox on this host"
        sentence = (
            "Commands are refused because this host has no command sandbox (macOS sandbox-exec, Linux bubblewrap or "
            "Landlock); restart the gateway with --unsandboxed-commands to allow them unsandboxed."
        )
    return {
        "state": state,
        "kind": kind,
        "kinds": kinds,
        "line": line,
        "sentence": sentence,
        "unsandboxed_commands_allowed": allowed,
        "configured": bool(policy.get("configured")),
        "flag": FLAG,
    }


def state() -> Dict[str, Any]:
    """The host's command-sandbox state for the console Security/Tools surfaces and the TUI:
    ``{state: sandboxed|partial|unsandboxed|refused, kind, line, sentence, unsandboxed_commands_allowed,
    configured, flag}`` — ``line`` is shown verbatim, ``sentence`` is its tooltip."""
    with _LOCK:
        return _state_locked()


SANDBOXED_SENTENCE = "Sandboxed to this run's workspaces"


def process_tools() -> frozenset:
    """The process-spawning tools: the runtime's own list (the tools it stamps with the run's
    sandbox), never a second list kept here."""
    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import SANDBOXED_TOOL_NAMES

    return frozenset(SANDBOXED_TOOL_NAMES)


def tool_sandbox_fields(tool_name: str) -> Optional[Dict[str, Any]]:
    """``{sandboxed, sandbox}`` for a process-spawning tool (the Tools inventory marks them so the
    tool cards can show the state), None for any other tool."""
    if tool_name not in process_tools():
        return None
    st = state()
    if st["state"] == "sandboxed":
        return {"sandboxed": True, "sandbox": SANDBOXED_SENTENCE}
    if st["state"] == "partial":
        return {"sandboxed": True, "sandbox": f"{SANDBOXED_SENTENCE} (Deny everything, allow listed workspaces); {st['line'].split('; ', 1)[1]}"}
    if st["state"] == "unsandboxed":
        return {"sandboxed": False, "sandbox": "Not sandboxed: unsandboxed commands allowed (flag)"}
    return {"sandboxed": False, "sandbox": "Refused: no command sandbox on this host"}
