"""`GET /api/gateway/runs` tells the whole session behind a page (operator 2026-11-08).

Code's sidebar showed "≥4 turns": the listing page holds a few runs of a session, never all of
them, so the app could only say "at least". Every listing now carries `session_totals`: for each
`session_id` on the page, `{turns, runs}` for the WHOLE session — turn roots (runs no children set
names) and all runs — under the page's `session_kind` filter, read from the file store's session and
children indexes (no run file, no ledger), for the sessions on the page only.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, List

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, chat_run, controller_run, gateway_env, occurrence_run
from timing import patient  # noqa: E402 - tests/timing.py

pytestmark = pytest.mark.basic


def _at(i: int) -> str:
    return f"2026-10-{1 + i // 86400:02d}T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}+00:00"


def _boot(client: TestClient) -> None:
    deadline = time.time() + patient(60.0)
    while client.get("/api/health").json().get("status") == "starting":
        assert time.time() < deadline, "gateway never finished booting"
        time.sleep(0.05)


def _memory_run(session_id: str) -> Any:
    """The session's bookkeeping run (`session_memory_<sid>`, an internal workflow):
    the listing never shows it, so the totals must not count it."""
    from abstractgateway.artifact_scope import session_memory_run_id
    from automations_fixtures import _run

    return _run(run_id=session_memory_run_id(session_id), workflow_id="__session_memory__@0.0.0:root", vars={}, session_id=session_id)


def _seed(data_dir: Path) -> List[str]:
    """Session chat-A: 7 turn roots, 2 chat children (follow-ups of the first two
    roots), one occurrence run of an automation that woke into this session
    (a child of its controller, session_kind occurrence) and its session-memory
    run. Session chat-B: 2 roots. Session chat-C: one turn and its memory run."""
    from abstractruntime import JsonFileRunStore

    store = JsonFileRunStore(data_dir)
    n = 0

    def save(run: Any) -> Any:
        nonlocal n
        run.created_at = run.updated_at = _at(n)
        n += 1
        store.save(run)
        return run

    roots: List[str] = []
    for _ in range(7):
        roots.append(save(chat_run(session_id="chat-A")).run_id)
    save(chat_run(session_id="chat-A", parent_run_id=roots[0]))
    save(chat_run(session_id="chat-A", parent_run_id=roots[1]))
    controller = save(controller_run(title="Monitor"))
    save(occurrence_run(controller.run_id, index=1, session_id="chat-A"))
    save(_memory_run("chat-A"))
    for _ in range(2):
        save(chat_run(session_id="chat-B"))
    save(chat_run(session_id="chat-C"))
    save(_memory_run("chat-C"))
    return roots


def test_session_totals_count_the_whole_session_behind_the_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = gateway_env(monkeypatch, tmp_path)
    _seed(data_dir)
    from abstractgateway.app import app

    with TestClient(app) as client:
        _boot(client)
        # A page of 3 of a 9-run conversation (the chat kinds): the totals say the whole session.
        page = client.get(
            "/api/gateway/runs?limit=3&root_only=true&session_id=chat-A&session_kind=chat", headers=HEADERS
        ).json()
        assert page["count"] == 3 and page["has_more"] is True, page
        assert page["session_totals"] == {"chat-A": {"turns": 7, "runs": 9}}, page.get("session_totals")

        # Without the kind filter the occurrence run that woke into the session counts as a
        # run AND as a turn — exactly what the listing itself shows (an occurrence is a turn
        # root, `_is_turn_root_summary`); the session-memory run still counts in neither.
        page = client.get("/api/gateway/runs?limit=3&root_only=true&session_id=chat-A", headers=HEADERS).json()
        assert page["session_totals"] == {"chat-A": {"turns": 8, "runs": 10}}, page.get("session_totals")

        # A page across sessions: one entry per session on the page, those only.
        page = client.get("/api/gateway/runs?limit=50&root_only=true&session_kind=chat", headers=HEADERS).json()
        on_page = {str(it.get("session_id")) for it in page["items"] if it.get("session_id")}
        assert on_page == {"chat-A", "chat-B", "chat-C"}, on_page
        assert set(page["session_totals"]) == on_page
        assert page["session_totals"]["chat-B"] == {"turns": 2, "runs": 2}
        # A one-turn conversation reads as ONE turn: its session-memory run (the internal
        # `session_memory_<sid>` workflow the listing never shows) is neither a turn nor a run here.
        assert page["session_totals"]["chat-C"] == {"turns": 1, "runs": 1}, page["session_totals"]["chat-C"]
        assert page["session_totals"]["chat-A"] == {"turns": 7, "runs": 9}

        # A page that names no session carries no key (never an empty guess).
        page = client.get("/api/gateway/runs?limit=3&query=nothing-matches-this", headers=HEADERS).json()
        assert page["items"] == [] and "session_totals" not in page, page


def test_the_store_indexes_the_totals_read_from_exist(tmp_path: Path) -> None:
    """The seam the totals read (named, so a rename in abstractruntime shows here, not as a
    500 on the live listing): the file store's lazy session index (`session_id -> {run id:
    session_kind}`) and run->parent index, built by its own `_ensure_*` methods."""
    from abstractruntime import JsonFileRunStore

    store = JsonFileRunStore(tmp_path / "runs")
    store.save(chat_run(session_id="s1"))
    store._ensure_session_index()  # noqa: SLF001
    store._ensure_children_index()  # noqa: SLF001
    assert isinstance(store._session_index, dict) and "s1" in store._session_index  # noqa: SLF001
    assert isinstance(store._run_parent_index, dict)  # noqa: SLF001
    assert store._session_index_lock is not None and store._index_lock is not None  # noqa: SLF001
