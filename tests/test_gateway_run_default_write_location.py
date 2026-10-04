"""Where a gateway run's file tools write when the run names no folder (R10 close, Z6.2).

The docs say it in one sentence (docs/security.md, docs/console.md,
docs/configuration.md): a run that names no `workspace_root` works in its
conversation's private session folder, so a relative path such as `out.txt`
lands THERE, not in the shared workspace; the shared workspace is reached by
its full path. This test is that sentence's evidence: it starts real runs
through `POST /api/gateway/runs/start` with no `workspace_root`, takes the run
vars the host stored, and sends a relative `write_file` through the runtime's
own workspace scope (the code path `TOOL_CALLS` uses) into AbstractCore's real
`write_file` tool.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from test_gateway_session_workspace_reuse import _client, _start, _workspace_of


def _write_like_a_tool_call(run_vars: dict, file_path: str, content: str) -> Path:
    """The runtime's TOOL_CALLS path: scope from the run vars, rewrite, then the real tool."""
    from abstractcore.tools.common_tools import write_file
    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import (
        WorkspaceScope,
        rewrite_tool_arguments,
    )

    scope = WorkspaceScope.from_input_data(run_vars)
    assert scope is not None, "a gateway run always has a workspace scope"
    args = rewrite_tool_arguments(
        tool_name="write_file", args={"file_path": file_path, "content": content}, scope=scope
    )
    write_file(**args)
    return Path(args["file_path"])


def _run_vars(run_id: str) -> dict:
    from abstractgateway.service import get_gateway_service

    run = get_gateway_service().host.run_store.load(run_id)
    assert run is not None
    return dict(run.vars or {})


def _shared_workspace(tmp_path: Path) -> Path:
    from abstractgateway.workspace_policy import effective_folder_paths

    return Path(effective_folder_paths(tmp_path / "runtime", tenant_id="default", user_id="admin").shared)


def test_a_relative_write_lands_in_the_private_session_folder_not_the_shared_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        run_id = _start(client, headers, session_id="chat-z62")
        session_folder = _workspace_of(client, headers, run_id).resolve()
        shared = _shared_workspace(tmp_path).resolve()

        written = _write_like_a_tool_call(_run_vars(run_id), "out.txt", "z62\n").resolve()

        assert written == session_folder / "out.txt"
        assert written.read_text(encoding="utf-8") == "z62\n"
        assert session_folder.parent == (tmp_path / "runtime" / "workspaces").resolve()
        assert session_folder.name.startswith("session-")
        assert session_folder != shared
        assert not (shared / "out.txt").exists()

        # The shared workspace is reachable, by its full path.
        in_shared = _write_like_a_tool_call(_run_vars(run_id), str(shared / "shared.txt"), "s\n").resolve()
        assert in_shared == shared / "shared.txt" and in_shared.is_file()


def test_without_a_session_the_relative_write_lands_in_the_runs_own_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        run_id = _start(client, headers)
        own = _workspace_of(client, headers, run_id).resolve()
        shared = _shared_workspace(tmp_path).resolve()

        written = _write_like_a_tool_call(_run_vars(run_id), "out.txt", "run\n").resolve()

        assert written == own / "out.txt"
        assert own.parent == (tmp_path / "runtime" / "workspaces").resolve()
        assert not own.name.startswith("session-")
        assert not (shared / "out.txt").exists()
