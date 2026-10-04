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


def apply_builtin_tool_deny(
    vars0: Dict[str, Any], *, root_data_dir: Any, read_only_mounts: Sequence[str] = (), policy_allow: Sequence[str] = ()
) -> None:
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
    for extra in policy_allow:
        # Folders of the account's OWN data plane its workspace policy allows (apply_workspace_policy
        # computed them for this account only); never anything outside the data folder's planes.
        real = os.path.realpath(str(extra))
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


def apply_workspace_policy(
    vars0: Dict[str, Any], *, root_data_dir: Any, tenant_id: str = "", user_id: str = ""
) -> List[str]:
    """The account's EFFECTIVE workspace policy (round 9, workspace_policy) binds the run's tool
    sandbox, whichever door started it (HTTP, bridges, schedules, entities). Two dimensions only:

    - WHAT (the posture): "Deny everything, allow listed workspaces" -> `workspace_or_allowed` with the ro/rw folders;
      "Allow everything, refuse listed workspaces" -> `all_except_ignored` (set here, never by a client); denied rows ->
      `workspace_ignored_paths`. A client's narrower `workspace_only`, or its own allowed list (only the
      entries the posture reaches, plus the shared workspace), narrows.
    - HOW (per folder): read-only folders -> `workspace_read_only_paths`; with a read-only default
      ("Allow everything, refuse listed workspaces (ro)") every folder is read-only ("/") except the run's own folder,
      the shared workspace and the read & write folders (`workspace_writable_paths`). A client never
      sets writable exceptions (dropped first).

    Returns the reachable folders inside this account's OWN data plane: the caller lifts the built-in
    data-folder deny for exactly those (never another account's plane). No account (a gateway without
    sign-in) = the operator, default:admin."""
    from abstractruntime.utils.workspace_paths import WRITABLE_PATHS_KEY

    from .workspace_policy import effective_folder_paths

    data_dir = Path(str(root_data_dir)).expanduser()
    eff = effective_folder_paths(data_dir, tenant_id=str(tenant_id or "") or "default", user_id=str(user_id or "") or "admin")
    vars0.pop(WRITABLE_PATHS_KEY, None)
    if isinstance(vars0.get("_runtime"), dict):
        vars0["_runtime"] = {k: v for k, v in vars0["_runtime"].items() if k != WRITABLE_PATHS_KEY}
    shared = eff.shared
    mode = str(vars0.get("workspace_access_mode") or vars0.get("workspaceAccessMode") or "").strip().lower()
    vars0.pop("workspaceAccessMode", None)
    raw_allowed = vars0.pop("workspaceAllowedPaths", None)
    if "workspace_allowed_paths" in vars0:
        raw_allowed = vars0.get("workspace_allowed_paths")
    plane = [Path(os.path.realpath(str(p))) for p in eff.plane_allow]

    def _reachable(real: Path) -> bool:
        if any(_under(real, p) for p in plane):
            return True
        if any(_under(real, Path(os.path.realpath(str(b)))) for b in eff.builtin):
            return False
        return eff.mode(real) != "deny"

    reach: List[Path] = list(eff.reach)
    if mode == "workspace_only":
        vars0["workspace_access_mode"] = "workspace_only"
        vars0.pop("workspace_allowed_paths", None)
        reach = []
    elif raw_allowed is None or (mode == "all_except_ignored" and eff.any_folder):
        vars0["workspace_access_mode"] = "all_except_ignored" if eff.any_folder else "workspace_or_allowed"
        vars0["workspace_allowed_paths"] = [str(p) for p in eff.reach]
    else:
        vars0["workspace_access_mode"] = "workspace_or_allowed"
        kept = [str(shared)]
        reach = [shared]
        for item in _path_list(raw_allowed):
            p = Path(str(item)).expanduser()
            if not p.is_absolute():
                continue
            real = Path(os.path.realpath(str(p)))
            if _reachable(real) and str(real) not in kept:
                kept.append(str(real))
                reach.append(real)
        vars0["workspace_allowed_paths"] = kept

    # HOW: read-only folders, and the read-only default with its writable exceptions.
    raw_root = vars0.get("workspace_root")
    root = os.path.realpath(str(Path(str(raw_root)).expanduser())) if isinstance(raw_root, str) and raw_root.strip() else None
    ro = [str(p) for p in eff.read_only]
    if eff.any_folder and eff.default_mode == "ro" and vars0["workspace_access_mode"] == "all_except_ignored":
        ro = ["/"] + ro
    if ro:
        existing_ro = _path_list(vars0.get("workspace_read_only_paths"))
        vars0["workspace_read_only_paths"] = list(dict.fromkeys(existing_ro + ro))
        writable = ([root] if root else []) + [str(p) for p in eff.writable]
        vars0[WRITABLE_PATHS_KEY] = list(dict.fromkeys(writable))

    raw_ignored = vars0.pop("workspaceIgnoredPaths", None)
    if "workspace_ignored_paths" in vars0:
        raw_ignored = vars0.get("workspace_ignored_paths")
    ignored: List[str] = []
    if raw_ignored is not None:
        ignored = _path_list(raw_ignored)
    merged = list(dict.fromkeys(ignored + [str(p) for p in eff.deny]))
    if merged:
        vars0["workspace_ignored_paths"] = "\n".join(merged)
    else:
        vars0.pop("workspace_ignored_paths", None)
    return [str(p) for p in plane if any(_under(p, r) or _under(r, p) for r in reach)]


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
    plane_allow = apply_workspace_policy(vars0, root_data_dir=root_data_dir, tenant_id=tenant_id, user_id=user_id)
    apply_builtin_tool_deny(vars0, root_data_dir=root_data_dir, read_only_mounts=read_only_mounts, policy_allow=plane_allow)
