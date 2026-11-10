"""The gateway half of the Stop fence (STOP-LANE-DESIGN 2026-11-09 §2.2, §3 T9/T10; Codex review 3 of the gateway).
Every test lands the operator's Stop at the precise point the review names and asserts what must not happen after it:
no model call, no write, no live session, no restore over the Stop. Hermetic gateway, scripted minds; only the loop's
process creation is stubbed (conftest)."""

from __future__ import annotations

import copy
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "entity-stop-fence-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})


class _ScriptedLLM:
    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def __init__(self, replies) -> None:
        self._replies = list(replies)
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if not self._replies:
            raise AssertionError("scripted LLM exhausted: a model call happened after the Stop")
        return self._Reply(self._replies.pop(0))


def _install_llm(monkeypatch, replies) -> _ScriptedLLM:
    from abstractgateway import entity_chat

    llm = _ScriptedLLM(replies)
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)
    return llm


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _registry():
    from abstractgateway.routes.entities import _registry as reg

    return reg()


def _home(slug: str = "castor") -> Path:
    return _registry().entities_dir / slug


def _born(client: TestClient, *names: str) -> None:
    for name in names:
        assert client.post("/api/gateway/entities", json={"name": name, "spark": _spark(name)}).status_code == 201


def _stop(slug: str = "castor") -> dict:
    from abstractgateway import entity_phase
    from abstractgateway.routes.entities import _chat_host, _visit_host

    return entity_phase.stop(_registry(), slug, actor="person:admin", chat_host=_chat_host(), visit_host=_visit_host())


def _memory_dump(slug: str = "castor") -> str:
    """Every row of the home's memory as SQL (an UPDATE shows, not only a count)."""
    from abstractgateway.entities import MEMORY_FILENAME

    con = sqlite3.connect(str(_home(slug) / MEMORY_FILENAME))
    try:
        return "\n".join(con.iterdump())
    finally:
        con.close()


def _history_lines(slug: str = "castor") -> int:
    path = _home(slug) / "state_history.jsonl"
    return path.read_text(encoding="utf-8").count("\n") if path.exists() else 0


# ------------------------------------------------------------------------------------------------ finding 12
def test_two_concurrent_starts_create_one_loop_process(loop_spawns):
    """Codex review 3 #12: the tests replaced `spawn_loop_process` whole, so its admission (the spawn lock, the start
    refusal, the reservation) never ran in them. Only process creation is stubbed now: two concurrent starts through
    the gateway's own entry create ONE process; the runtime's admission refuses the other."""
    from abstractruntime.identity.life import read_loop_status, write_personal_grant
    from abstractruntime.identity.lifecycle_fence import LifecycleFence

    from abstractgateway.entity_loop import start_loop

    with _client() as client:
        _born(client, "Castor")
        write_personal_grant(_home(), mode="until_revoked", granted_by="person:admin", fence=LifecycleFence.admit(_home()))
        gate = threading.Barrier(2)
        results = []

        def _start():
            gate.wait()
            try:
                results.append(start_loop(_home(), provider="lmstudio", model="test-model"))
            except Exception as e:  # noqa: BLE001
                results.append(e)

        threads = [threading.Thread(target=_start) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert len(loop_spawns) == 1, loop_spawns
        started = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, Exception)]
        assert len(started) == 1 and len(refused) == 1, results
        assert "already" in str(refused[0]), refused[0]
        assert read_loop_status(_home())["phase"] == "starting"  # the reservation names the child, no clock


def test_the_spawn_admission_runs_a_job_starts_without_a_grant_nothing_without_either(loop_spawns):
    """Codex review 3 #12: a queued job starts the loop whatever the personal grant says (v3 D1); with neither a grant
    nor a job the runtime refuses synchronously and no process is created. Through the real spawn path."""
    from abstractgateway.entity_loop import start_loop

    with _client() as client:
        _born(client, "Castor")
        with pytest.raises(RuntimeError, match="no personal time"):
            start_loop(_home(), provider="lmstudio", model="test-model")
        assert loop_spawns == []
        (_home() / "work_order.md").write_text("Sort the archive.\n", encoding="utf-8")
        assert start_loop(_home(), provider="lmstudio", model="test-model")["pid"]
        assert len(loop_spawns) == 1
