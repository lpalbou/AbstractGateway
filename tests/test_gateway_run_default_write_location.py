"""Where a gateway run writes when it names no folder, and what the agent is told (R10 close).

The docs say it (docs/security.md, docs/console.md, docs/configuration.md,
docs/api.md): a run that names no `workspace_root` works in its conversation's
private session folder, so a relative path such as `out.txt` lands THERE, not
in the shared workspace; the shared workspace and the allowed workspaces are
listed to the agent with their paths and modes.

Both kinds of door are covered:
- the HTTP door, `POST /api/gateway/runs/start` (it makes the session folder
  itself before the host sees the run);
- the in-process doors, which reach `run_workspace_guard.guard_run_vars` ->
  `ensure_run_workspace` with no folder: `host.start_run` (bridges, entity
  summons) and the automation occurrence inputs (`routes/automations.py`
  calls `guard_run_vars` on them directly).

A relative `write_file` goes through the runtime's own workspace scope (the
code path `TOOL_CALLS` uses) into AbstractCore's real `write_file` tool, and
the agent's context is the runtime's `describe_workspace_scope` text, the
block a tool-using LLM call receives.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from test_gateway_session_workspace_reuse import _client, _start, _workspace_of


def _scope(run_vars: dict):
    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import WorkspaceScope

    scope = WorkspaceScope.from_input_data(run_vars)
    assert scope is not None, "a gateway run always has a workspace scope"
    return scope


def _write_like_a_tool_call(run_vars: dict, file_path: str, content: str) -> Path:
    """The runtime's TOOL_CALLS path: scope from the run vars, rewrite, then the real tool."""
    from abstractcore.tools.common_tools import write_file
    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import rewrite_tool_arguments

    args = rewrite_tool_arguments(
        tool_name="write_file", args={"file_path": file_path, "content": content}, scope=_scope(run_vars)
    )
    write_file(**args)
    return Path(args["file_path"])


def _context(run_vars: dict) -> str:
    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import describe_workspace_scope

    return describe_workspace_scope(_scope(run_vars))


def _run_vars(run_id: str) -> dict:
    from abstractgateway.service import get_gateway_service

    run = get_gateway_service().host.run_store.load(run_id)
    assert run is not None
    return dict(run.vars or {})


def _data_dir(tmp_path: Path) -> Path:
    return tmp_path / "runtime"


def _shared_workspace(tmp_path: Path) -> Path:
    from abstractgateway.workspace_policy import effective_folder_paths

    return Path(effective_folder_paths(_data_dir(tmp_path), tenant_id="default", user_id="admin").shared).resolve()


# ------------------------------------------------------------------ the doors
# Each returns the stored (or frozen) run vars of a run started with NO workspace_root.


def _http_door(client, headers, tmp_path: Path, session_id: str | None) -> dict:
    return _run_vars(_start(client, headers, session_id=session_id))


def _host_door(client, headers, tmp_path: Path, session_id: str | None) -> dict:
    from abstractgateway.service import get_gateway_service

    host = get_gateway_service().host
    run_id = host.start_run(flow_id="root", bundle_id="bundle-ws", input_data={}, session_id=session_id)
    return _run_vars(run_id)


def _automation_door(client, headers, tmp_path: Path, session_id: str | None) -> dict:
    """The occurrence inputs an automation freezes: `guard_run_vars` with no folder."""
    from abstractgateway.run_workspace_guard import guard_run_vars

    data: dict = {}
    guard_run_vars(
        data,
        data_dir=_data_dir(tmp_path),
        root_data_dir=_data_dir(tmp_path),
        session_id=session_id,
        tenant_id="",
        user_id="",
    )
    return data


DOORS = [
    pytest.param(_http_door, id="http-runs-start"),
    pytest.param(_host_door, id="in-process-host-start-run"),
    pytest.param(_automation_door, id="in-process-automation-guard"),
]


@pytest.mark.parametrize("door", DOORS)
def test_a_relative_write_lands_in_the_private_session_folder_not_the_shared_workspace(
    door, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        run_vars = door(client, headers, tmp_path, "chat-z62")
        session_folder = Path(run_vars["workspace_root"]).resolve()
        shared = _shared_workspace(tmp_path)

        written = _write_like_a_tool_call(run_vars, "out.txt", "z62\n").resolve()

        assert written == session_folder / "out.txt"
        assert written.read_text(encoding="utf-8") == "z62\n"
        assert session_folder.parent == (_data_dir(tmp_path) / "workspaces").resolve()
        assert session_folder.name.startswith("session-")
        assert session_folder != shared
        assert not (shared / "out.txt").exists()

        # The shared workspace is reachable by the path the agent is given.
        in_shared = _write_like_a_tool_call(run_vars, str(shared / "shared.txt"), "s\n").resolve()
        assert in_shared == shared / "shared.txt" and in_shared.is_file()


@pytest.mark.parametrize("door", DOORS)
def test_without_a_session_the_relative_write_lands_in_the_runs_own_folder(
    door, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        run_vars = door(client, headers, tmp_path, None)
        own = Path(run_vars["workspace_root"]).resolve()
        shared = _shared_workspace(tmp_path)

        written = _write_like_a_tool_call(run_vars, "out.txt", "run\n").resolve()

        assert written == own / "out.txt"
        assert own.parent == (_data_dir(tmp_path) / "workspaces").resolve()
        assert not own.name.startswith("session-")
        assert not (shared / "out.txt").exists()


def _policy(client, headers, tmp_path: Path, *, posture: str, default_mode: str = "rw") -> dict:
    """Gateway: project rw, archive ro, secrets refused; the account lowers notes to read-only."""
    base = tmp_path / "workspaces-on-disk"
    paths = {name: (base / name) for name in ("project", "archive", "secrets", "notes")}
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)
    paths = {k: v.resolve() for k, v in paths.items()}
    body = {
        "posture": posture,
        "default_mode": default_mode,
        "folders": [
            {"path": str(paths["project"]), "mode": "rw"},
            {"path": str(paths["archive"]), "mode": "ro"},
            {"path": str(paths["secrets"]), "mode": "deny"},
            {"path": str(paths["notes"]), "mode": "rw"},
        ],
    }
    if posture == "any_except_denied":
        body["folders"] = [f for f in body["folders"] if f["mode"] != "rw" or f["path"] == str(paths["notes"])]
    res = client.put("/api/gateway/workspace/policy", json=body, headers=headers)
    assert res.status_code == 200, res.text
    res = client.put(
        "/api/gateway/workspace/policy/me", json={"folders": [{"path": str(paths["notes"]), "mode": "ro"}]}, headers=headers
    )
    assert res.status_code == 200, res.text
    return paths


@pytest.mark.parametrize("door", DOORS)
def test_the_agent_is_told_the_shared_and_allowed_workspaces_with_their_modes(
    door, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        paths = _policy(client, headers, tmp_path, posture="allowed_only")
        eff = client.get("/api/gateway/workspace/effective/me", headers=headers)
        assert eff.status_code == 200, eff.text
        run_vars = door(client, headers, tmp_path, "chat-ctx")
        text = _context(run_vars)
        shared = _shared_workspace(tmp_path)
        lines = text.splitlines()

        assert f'Default working directory: "{Path(run_vars["workspace_root"]).resolve()}"' in lines
        assert f'Shared workspace: "{shared}" (read & write)' in lines
        assert "Allowed workspaces:" in lines
        assert f'  "{paths["project"]}" (read & write)' in lines
        assert f'  "{paths["archive"]}" (read-only)' in lines
        # The account lowered notes to read-only: the agent sees the lowered mode.
        assert f'  "{paths["notes"]}" (read-only)' in lines
        assert f'  "{paths["notes"]}" (read & write)' not in lines
        # A refused workspace is never listed as allowed.
        assert not any(str(paths["secrets"]) in ln and ln.startswith("  ") and "(read" in ln for ln in lines)
        assert "Everything else" not in text
        # The listed modes are the ones the gateway's own summary states.
        summary = eff.json()["summary"] if "summary" in eff.json() else eff.json().get("effective", {}).get("summary", "")
        assert f"{paths['archive']} (ro)" in summary and f"{paths['notes']} (ro)" in summary


@pytest.mark.parametrize("door", DOORS)
def test_under_allow_everything_the_context_names_everything_else(
    door, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        paths = _policy(client, headers, tmp_path, posture="any_except_denied", default_mode="ro")
        run_vars = door(client, headers, tmp_path, "chat-any")
        lines = _context(run_vars).splitlines()
        shared = _shared_workspace(tmp_path)

        assert f'Shared workspace: "{shared}" (read & write)' in lines
        assert "Everything else: (read-only)" in lines
        assert not any(str(paths["secrets"]) in ln and "(read" in ln for ln in lines)


def test_a_client_cannot_name_another_shared_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        run_id = _start(client, headers, session_id="chat-forge", input_data={"workspace_shared_path": "/etc"})
        run_vars = _run_vars(run_id)
        assert Path(run_vars["workspace_shared_path"]).resolve() == _shared_workspace(tmp_path)
        assert 'Shared workspace: "/etc"' not in _context(run_vars)
