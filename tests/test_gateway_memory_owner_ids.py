"""KG owner ids: the gateway's session/global derivation equals the runtime's.

`kg/query` (and `/memory/query`) resolve owners through the runtime's
`resolve_scope_owner_id(run, ...)` when a run is given. A caller holding only a
session id has no run, so `memory_store.session_memory_owner_id` /
`global_memory_owner_id` write the same public rule; this test pins the parity
on REAL RunStates in a real run store, so a runtime change goes red here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abstractgateway.memory_store import global_memory_owner_id, session_memory_owner_id

pytestmark = pytest.mark.basic


_SESSION_IDS = [
    "sess-1",
    "abc_DEF-123",
    "has space",
    "unicodé",
    "a/b:c",
    "x" * 300,
]


@pytest.mark.parametrize("session_id", _SESSION_IDS)
def test_session_owner_id_matches_the_runtime(tmp_path: Path, session_id: str) -> None:
    from abstractruntime import RunState
    from abstractruntime.integrations.abstractmemory.effect_handlers import resolve_scope_owner_id
    from abstractruntime.storage.json_files import JsonFileRunStore

    rs = JsonFileRunStore(tmp_path)
    run = RunState.new(workflow_id="wf", entry_node="start", session_id=session_id)
    rs.save(run)
    loaded = rs.load(run.run_id)
    assert loaded is not None and loaded.session_id == session_id
    assert session_memory_owner_id(session_id) == resolve_scope_owner_id(loaded, scope="session", run_store=rs)


def test_session_owner_id_refuses_an_empty_id() -> None:
    with pytest.raises(ValueError):
        session_memory_owner_id("  ")


@pytest.mark.parametrize("env_value", [None, "", "my_global-1", "not safe!"])
def test_global_owner_id_matches_the_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_value) -> None:
    from abstractruntime import RunState
    from abstractruntime.integrations.abstractmemory.effect_handlers import resolve_scope_owner_id
    from abstractruntime.storage.json_files import JsonFileRunStore

    if env_value is None:
        monkeypatch.delenv("ABSTRACTRUNTIME_GLOBAL_MEMORY_RUN_ID", raising=False)
    else:
        monkeypatch.setenv("ABSTRACTRUNTIME_GLOBAL_MEMORY_RUN_ID", env_value)
    rs = JsonFileRunStore(tmp_path)
    run = RunState.new(workflow_id="wf", entry_node="start")
    rs.save(run)
    assert global_memory_owner_id() == resolve_scope_owner_id(run, scope="global", run_store=rs)
