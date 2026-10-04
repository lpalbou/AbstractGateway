"""abstractgateway.run_workspace_guard -- the host's own protection of every run's tool sandbox.

ONE place, applied by `WorkflowBundleGatewayHost.start_run` to every run the
gateway starts (the HTTP routes, the Telegram / email / agora bridges, entity
summons, the sandbox routes, scheduled wrappers whose children inherit it):

- `ensure_run_workspace`: a run that names no `workspace_root` works in the
  gateway-owned folder of its session (or a per-run folder), exactly as
  `POST /runs/start` has always done. Without a root the runtime confines
  nothing, so a run without one would have unconfined file tools.
- `apply_workspace_policy`: the account's effective workspace policy (round 9) — allowed
  folders, never-allowed folders, no any-folder mode — whatever door started the run.
- `apply_builtin_tool_deny`: the data folder and the account's credential
  folders as whole-folder deny PREFIXES (`workspace_builtin_deny_prefixes`)
  with ONE exception, the run's own folder inside the data folder
  (`workspace_builtin_allow`). Never an enumeration of a folder's contents,
  never in `workspace_ignored_paths`, never rendered into the model's prompt
  (the runtime enforces the two keys silently). Whatever a client
  sent under these two keys is dropped first.

The client-facing policy check (`routes/gateway.py`
`_sanitize_run_workspace_policy`: a client `workspace_root` outside the
operator's roots or inside the data folder is refused) runs at every door
that takes a client's `input_data`, before the run reaches the host.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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


def apply_builtin_tool_deny(vars0: Dict[str, Any], *, root_data_dir: Any, read_only_mounts: Sequence[str] = ()) -> None:
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
    allow: List[str] = []
    if root is not None and root != data_root and _under(root, data_root):
        allow.append(str(root))
    if read_only_mounts:
        # A host read-only mount inside the data folder is readable (the
        # runtime refuses every write under it: it must be declared read-only).
        from abstractruntime.utils.workspace_paths import read_only_paths

        declared = set(read_only_paths(vars0))
        for mount in read_only_mounts:
            real = os.path.realpath(str(mount))
            if real not in declared:
                raise ValueError(f"read-only mount {mount} is not declared in the run's workspace_read_only_paths")
            if _under(Path(real), data_root) and real not in allow:
                allow.append(real)
    if allow:
        vars0["workspace_builtin_allow"] = allow


def _path_list(raw: Any) -> List[str]:
    """A list, a JSON array or newline-separated text of paths -> list of non-empty strings."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if isinstance(x, str) and x.strip()]
    text = str(raw).strip()
    if text.startswith("["):
        import json

        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if isinstance(x, str) and x.strip()]
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def apply_workspace_policy(vars0: Dict[str, Any], *, root_data_dir: Any, tenant_id: str = "", user_id: str = "") -> None:
    """The account's EFFECTIVE workspace policy (round 9, workspace_policy.effective_policy) binds
    the run's tool sandbox, whichever door started it (HTTP, bridges, schedules, entities):

    - the access mode is "workspace_or_allowed" (or a narrower "workspace_only"); the removed
      any-folder mode "all_except_ignored" is never honoured here — it becomes "workspace_or_allowed";
    - workspace_allowed_paths: absent = the effective folders; present = only the entries inside an
      effective folder (the doors already refused wider entries with a 400; the host only narrows),
      plus the shared workspace, which is always present;
    - workspace_ignored_paths gains every never-allowed folder (never allowed wins).

    No account (a gateway without sign-in) = the operator, default:admin."""
    from .workspace_policy import effective_folder_paths

    data_dir = Path(str(root_data_dir)).expanduser()
    folders, never, _builtin, _trust = effective_folder_paths(
        data_dir, tenant_id=str(tenant_id or "") or "default", user_id=str(user_id or "") or "admin"
    )
    shared = folders[0]
    mode = str(vars0.get("workspace_access_mode") or vars0.get("workspaceAccessMode") or "").strip().lower()
    vars0.pop("workspaceAccessMode", None)
    raw_allowed = vars0.pop("workspaceAllowedPaths", None)
    if "workspace_allowed_paths" in vars0:
        raw_allowed = vars0.get("workspace_allowed_paths")
    if mode == "workspace_only":
        vars0["workspace_access_mode"] = "workspace_only"
        vars0.pop("workspace_allowed_paths", None)
    else:
        vars0["workspace_access_mode"] = "workspace_or_allowed"
        if raw_allowed is None:
            kept = [str(p) for p in folders]
        else:
            kept = [str(shared)]
            for item in _path_list(raw_allowed):
                p = Path(str(item)).expanduser()
                if not p.is_absolute():
                    continue
                real = Path(os.path.realpath(str(p)))
                if any(_under(real, Path(os.path.realpath(str(f)))) for f in folders) and str(real) not in kept:
                    kept.append(str(real))
        vars0["workspace_allowed_paths"] = kept
    raw_ignored = vars0.pop("workspaceIgnoredPaths", None)
    if "workspace_ignored_paths" in vars0:
        raw_ignored = vars0.get("workspace_ignored_paths")
    ignored: List[str] = []
    if raw_ignored is not None:
        ignored = _path_list(raw_ignored)
    merged = list(dict.fromkeys(ignored + [str(p) for p in never]))
    if merged:
        vars0["workspace_ignored_paths"] = "\n".join(merged)
    else:
        vars0.pop("workspace_ignored_paths", None)


def guard_run_vars(
    vars0: Dict[str, Any],
    *,
    data_dir: Any,
    root_data_dir: Any,
    session_id: Optional[str],
    tenant_id: str = "",
    user_id: str = "",
    read_only_mounts: Sequence[str] = (),
) -> None:
    """All steps, in order: a workspace for every run, the account's effective workspace policy,
    then the built-in deny rule for it.

    `read_only_mounts`: folders the HOST mounts read-only into this run (a
    discussion's automation workspace). They are passed explicitly by the
    caller, never read from `vars0`, so no client key can open the data folder.
    """
    ensure_run_workspace(vars0, data_dir=data_dir, session_id=session_id, tenant_id=tenant_id, user_id=user_id)
    apply_workspace_policy(vars0, root_data_dir=root_data_dir, tenant_id=tenant_id, user_id=user_id)
    apply_builtin_tool_deny(vars0, root_data_dir=root_data_dir, read_only_mounts=read_only_mounts)
