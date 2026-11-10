"""A step's tools on the replay stream (root backlog 1204, the gateway half; the Entity app's ledger rows
read it, its backlog 0035).

One `step_tools` host marker per finished step that ran a tool: {run_id, turn_id, tools in call order,
tool_count, files_read, files_written}. The visit lane is pinned in test_gateway_entities_visits.py
(the door's TOOL_CALLS handler and the step's ANSWER_USER). These tests pin the home-direct lane: an
own-time step runs in the runtime's loop process (AbstractRuntime ChatSession, no run ledger), and its
episode carries `attributes.tools_used`; the gateway sweeps the formations since its cursor into the
host stream. Here the loop's session runs in-process on the same home, as the loop process opens it."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-step-tools-shared-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _registry() -> Any:
    from abstractgateway.service import get_gateway_service

    return get_gateway_service().entity_registry


class _ScriptedLLM:
    """The loop's mind, scripted: replies in order, loud when an unexpected call happens."""

    def __init__(self, replies: List[str]) -> None:
        self.replies = list(replies)

    def generate(self, **kwargs: Any) -> Any:
        if not self.replies:
            raise AssertionError("scripted LLM exhausted: an unexpected extra call happened")

        class _R:
            pass

        r = _R()
        r.content = self.replies.pop(0)
        return r


def _replay(client: TestClient) -> List[Dict[str, Any]]:
    resp = client.get("/api/gateway/entities/Castor/replay")
    assert resp.status_code == 200, resp.text
    return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def _step_tools(envelopes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e for e in envelopes if e.get("family") == "host" and (e.get("payload") or {}).get("kind") == "step_tools"]


class _SseRequest:
    async def is_disconnected(self) -> bool:
        return False


def _sse_until_quiet() -> List[Dict[str, Any]]:
    import asyncio

    from abstractgateway.routes.entity_replay import stream_entity_replay

    async def _drain() -> List[Dict[str, Any]]:
        response = await stream_entity_replay(
            _SseRequest(), "Castor", since_seq=0.0, families=None, enrich=True,
            heartbeat_s=0.3, poll_s=0.05, last_event_id=None,
        )
        gen = response.body_iterator
        buffer = ""
        try:
            while ": keep-alive" not in buffer:
                buffer += (await gen.__anext__()).decode("utf-8")
        finally:
            await gen.aclose()
        return [json.loads(block.split("data: ", 1)[1]) for block in buffer.split("\n\n") if "data: " in block]

    return asyncio.run(asyncio.wait_for(_drain(), timeout=60))


def test_the_counts_come_from_the_one_file_effect_table() -> None:
    """tool_count = every call that ran; files read / written through the one table (root 1204), so
    listing, searching and running a command count as tools only."""
    from abstractgateway.entity_step_tools import FILE_EFFECT_BY_TOOL, step_tools_details

    assert FILE_EFFECT_BY_TOOL == {"read_file": "read", "skim_files": "read", "write_file": "write", "edit_file": "write"}
    names = ["read_file", "skim_files", "edit_file", "write_file", "list_files", "execute_command", "read_file", "web_search"]
    assert step_tools_details("run-1", "t-0001", names) == {
        "run_id": "run-1", "turn_id": "t-0001", "tools": names,
        "tool_count": 8, "files_read": 3, "files_written": 2,
    }


def test_an_own_time_step_carries_its_tools_from_its_episode(monkeypatch: pytest.MonkeyPatch) -> None:
    """An own-time step that ran tools gets ONE marker, right after its episode's formation and before the
    next step's rows, on the live SSE tail and in the replay export (each sweeps on its own: the tail is
    the first reader after step 2, the export the first after step 4); a step with no tool gets none. The
    sweep starts at the home's first stream read (no backfill: a step from before it stays unmarked). The
    marker is no moment of the life (the card's and the console's moments)."""
    from abstractruntime.identity.chat import ChatSession, open_home

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home_dir = Path(_registry().entities_dir) / "castor"
        workspace = home_dir / "workspace" / "notes"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "a.txt").write_text("Beavers build dams.\n", encoding="utf-8")
        (workspace / "b.txt").write_text("Of wood and mud.\n", encoding="utf-8")

        llm = _ScriptedLLM([
            # t-0001, before the home's first stream read: one write.
            "```tool name=write_file path=notes/early.md\nbefore the first read\n```",
            "Noted early.",
            # t-0002: a search, two reads and a write, in that order.
            "Let me look.\n```tool name=web_search\nbeaver dams\n```\n```tool name=read_file\nnotes/a.txt\n```\n"
            "```tool name=read_file\nnotes/b.txt\n```\n```tool name=write_file path=notes/summary.md\n# Dams\n```",
            "Read both notes and wrote the summary.",
            # t-0003: no tool.
            "Resting my eyes on the river.",
            # t-0004: one read.
            "```tool name=read_file\nnotes/summary.md\n```",
            "My summary still reads well.",
        ])
        home = open_home(home_dir, fence=None)
        try:
            session = ChatSession(
                home, llm, participants=["agent:tester"], context_window=20000, phase="personal",
                web_search_fn=lambda q: f"Search results for: {q}", out=lambda s: None, fence=None,
            )
            run_id = str(session.run.run_id)
            assert session.turn("Your own time begins.")[1].tools == ["write_file"]
            assert _step_tools(_replay(client)) == [], "the sweep starts at the first read: no backfill"

            assert session.turn("Your own time continues.")[1].tools == ["web_search", "read_file", "read_file", "write_file"]
            step2 = {
                "kind": "step_tools", "session_id": None, "run_id": run_id, "turn_id": "t-0002",
                "tools": ["web_search", "read_file", "read_file", "write_file"],
                "tool_count": 4, "files_read": 2, "files_written": 1,
            }
            # The live SSE tail is the first reader: its own sweep sends the marker, after the step's formation.
            live = _sse_until_quiet()
            assert [m["payload"] for m in _step_tools(live)] == [step2]
            wire = [i for i, e in enumerate(live) if e.get("family") == "binding" and e.get("turn_id") == "t-0002"]
            assert wire and max(wire) < next(i for i, e in enumerate(live) if e in _step_tools(live))

            assert session.turn("Your own time continues.")[1].tools == []
            before_step4 = int(_registry().get_home("castor").memory.current_seq())
            assert session.turn("Your own time continues.")[1].tools == ["read_file"]
        finally:
            home.close()

        # The live tail's bound: a sweep up to what a reader already received never marks a later step
        # (its marker would arrive before the step's rows).
        from abstractgateway.entity_step_tools import sweep_step_tools

        gw_home = _registry().get_home("castor")
        assert sweep_step_tools(_registry().entities_dir, "castor", gw_home, until_seq=before_step4) == 0

        step4 = {
            "kind": "step_tools", "session_id": None, "run_id": run_id, "turn_id": "t-0004",
            "tools": ["read_file"], "tool_count": 1, "files_read": 1, "files_written": 0,
        }
        # The replay export is the first reader after step 4: its own sweep records step 4's marker.
        replay = _replay(client)
        marked = _step_tools(replay)
        assert [m["payload"] for m in marked] == [step2, step4], marked  # none for t-0001 (before) or t-0003
        formation = [e["seq"] for e in replay if e.get("family") == "binding" and e.get("run_id") == run_id
                     and e.get("turn_id") == "t-0002" and (e.get("payload") or {}).get("source") == "remember"]
        next_rows = [e["seq"] for e in replay if e.get("family") != "host" and e.get("run_id") == run_id
                     and e.get("turn_id") == "t-0003"]
        assert formation and next_rows
        assert max(formation) < marked[0]["seq"] < min(next_rows)
        # Reading again records nothing new (the cursor moved; the markers dedup on run and turn).
        assert [m["payload"] for m in _step_tools(_replay(client))] == [step2, step4]

        # Per-step observability, never a moment of the life (the card and the console's recent moments).
        assert "step_tools" not in {m.get("kind") for m in _registry().life_moments("castor")}
