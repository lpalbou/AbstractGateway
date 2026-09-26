"""abstractgateway.run_workspace_guard -- the host's own protection of every run's tool sandbox.

ONE place, applied by `WorkflowBundleGatewayHost.start_run` to every run the
gateway starts (the HTTP routes, the Telegram / email / agora bridges, entity
summons, the sandbox routes, scheduled wrappers whose children inherit it):

- `ensure_run_workspace`: a run that names no `workspace_root` works in the
  gateway-owned folder of its session (or a per-run folder), exactly as
  `POST /runs/start` has always done. Without a root the runtime confines
  nothing, so a run without one would have unconfined file tools.
- `apply_builtin_tool_deny`: the data folder and the account's credential
  folders as whole-folder deny PREFIXES (`workspace_builtin_deny_prefixes`)
  with ONE exception, the run's own folder inside the data folder
  (`workspace_builtin_allow`). Never an enumeration of a folder's contents,
  never in `workspace_ignored_paths`, never rendered into the model's prompt
  (the runtime enforces the two keys silently; REVIEW/16). Whatever a client
  sent under these two keys is dropped first.

The client-facing policy check (`routes/gateway.py`
`_sanitize_run_workspace_policy`: a client `workspace_root` outside the
operator's roots or inside the data folder is refused) runs at every door
that takes a client's `input_data`, before the run reaches the host.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

BUILTIN_DENY_KEYS = ("workspace_builtin_deny_prefixes", "workspace_builtin_allow")


def strip_client_builtin_deny(vars0: Dict[str, Any]) -> None:
    """A client never sets the host's protection (an allow entry of "/" would lift it)."""
    for key in BUILTIN_DENY_KEYS:
        vars0.pop(key, None)


def _under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def ensure_run_workspace(
    vars0: Dict[str, Any],
    *,
    data_dir: Any,
    session_id: Optional[str],
    tenant_id: str = "",
    user_id: str = "",
) -> None:
    raw = vars0.get("workspace_root")
    if isinstance(raw, str) and raw.strip():
        return
    from .run_retention import resolve_gateway_run_workspace

    ws_dir, session_scoped = resolve_gateway_run_workspace(
        data_dir, session_id=session_id, tenant_id=tenant_id, user_id=user_id
    )
    vars0["workspace_root"] = str(ws_dir)
    vars0["_gateway_workspace"] = {"kind": "session" if session_scoped else "run", "path": str(ws_dir)}


def apply_builtin_tool_deny(vars0: Dict[str, Any], *, root_data_dir: Any) -> None:
    """See the module docstring. `root_data_dir` is the gateway's ROOT data
    folder (per-user data folders live inside it); the admin switch
    `workspace_builtin_deny` (stored there) turns the rule off for runs."""
    from .runtime_config import resolve_workspace_builtin_deny_enabled
    from .workspace_browse import BUILTIN_DENY_HOME_RELPATHS

    strip_client_builtin_deny(vars0)
    data_dir = Path(str(root_data_dir)).expanduser()
    if not resolve_workspace_builtin_deny_enabled(data_dir):
        return
    raw_root = vars0.get("workspace_root")
    root = Path(str(raw_root)).expanduser().resolve() if isinstance(raw_root, str) and raw_root.strip() else None
    home = Path.home()
    creds: List[Path] = [Path(os.path.realpath(str(home / rel))) for rel in BUILTIN_DENY_HOME_RELPATHS]
    if root is not None:
        # A credential folder that CONTAINS the run's workspace would deny the workspace itself.
        creds = [c for c in creds if not _under(root, c)]
    data_root = Path(os.path.realpath(str(data_dir)))
    vars0["workspace_builtin_deny_prefixes"] = list(dict.fromkeys([str(data_root)] + [str(c) for c in creds]))
    if root is not None and root != data_root and _under(root, data_root):
        vars0["workspace_builtin_allow"] = [str(root)]


def guard_run_vars(
    vars0: Dict[str, Any],
    *,
    data_dir: Any,
    root_data_dir: Any,
    session_id: Optional[str],
    tenant_id: str = "",
    user_id: str = "",
) -> None:
    """Both steps, in order: a workspace for every run, then the deny rule for it."""
    ensure_run_workspace(vars0, data_dir=data_dir, session_id=session_id, tenant_id=tenant_id, user_id=user_id)
    apply_builtin_tool_deny(vars0, root_data_dir=root_data_dir)
