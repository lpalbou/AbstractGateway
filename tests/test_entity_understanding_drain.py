"""The gateway half of the understanding drain (runtime 0869 / gateway 1013; design note
untracked/release-2026-11-07/understanding-drain/DESIGN-drain-v1.md).

What these tests pin, on a hermetic gateway over a scratch data dir (never :8080):

- the capability LLM handler factory beside `_entity_llm_handler`: a client per capability from the
  home's cognition profile (`resolve_capability(home, cap, mind=...)`) and the door's endpoint
  resolution, with the entity patience kwargs and never the mind's speculation; the LLM_CALL-shaped
  handler forwards tools, media and params like the mind handler;
- every entity home the gateway opens has its memory task queue on (`MemorySystem(task_queue=True)`).
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")
pytest.importorskip("abstractruntime.identity.cognition_profile")

_TOKEN = "entity-drain-shared-secret"
ROUTE_MIND = {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None}


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    # The gateway text route stands in for the mind (round 3: no env vars).
    monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind", lambda: dict(ROUTE_MIND))


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _registry():
    from abstractgateway.service import get_gateway_service

    return get_gateway_service().entity_registry


def _home_dir(name: str = "Castor") -> Path:
    from abstractgateway.service import get_gateway_service

    return Path(get_gateway_service().config.data_dir) / "entities" / name.lower()


class _RecordingLLM:
    """A client that records every `generate` call and answers a fixed reply."""

    def __init__(self, reply: str = "ok") -> None:
        self.calls: list = []
        self.reply = reply

    def generate(self, **kwargs):
        self.calls.append(dict(kwargs))
        return SimpleNamespace(content=self.reply, finish_reason="stop", usage={"prompt_tokens": 3, "completion_tokens": 1},
                               model="recorded", tool_calls=None)


def _install_recording_factory(monkeypatch: pytest.MonkeyPatch):
    """Patch the late-bound factory; returns (built, llm): `built` collects (provider, kwargs) per build."""
    from abstractgateway import entity_chat

    built: list = []
    llm = _RecordingLLM()

    def factory(provider: str, **kwargs):
        built.append((provider, dict(kwargs)))
        return llm

    monkeypatch.setattr(entity_chat, "_default_llm_factory", factory)
    return built, llm


def _write_profile(home: Path, capabilities: dict, **rest) -> None:
    from abstractruntime.identity.cognition_profile import write_cognition_profile

    write_cognition_profile(home, {"capabilities": capabilities, **rest})


# --------------------------------------------------------------------------- 1. the capability factory


def test_capability_llm_follows_the_cognition_profile_with_patience_and_no_speculation(monkeypatch: pytest.MonkeyPatch):
    """`capability_llm_factory(slug)(cap)` builds the capability's client from `cognition.yaml`
    (its own choice, else the mind), through the same late-bound factory as the mind handler, with
    the entity patience kwargs — and never the mind's MTP speculation (no speculation on capability
    calls)."""
    from abstractgateway.entity_chat import write_entity_substrate
    from abstractruntime.identity.chat import ENTITY_LLM_READ_IDLE_TIMEOUT_S, _entity_patience_window

    built, _llm = _install_recording_factory(monkeypatch)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home = _home_dir()
        # The mind carries an MTP request; the capability clients must not.
        write_entity_substrate(home, provider="lmstudio", model="mind-model", thinking="high",
                               speculation={"mode": "native_mtp", "num_draft_tokens": 4})
        _write_profile(home, {
            "summaries": {"provider": "lmstudio", "model": "summ-model", "thinking": "low", "max_input_tokens": 8192},
        })

        llm_for = _registry().capability_llm_factory("castor")

        llm_for("summaries")
        provider, kw = built[-1]
        assert provider == "lmstudio"
        assert kw["model"] == "summ-model" and kw["thinking"] == "low"
        assert "speculation" not in kw, "a capability call never carries the mind's MTP request"
        timeout, budget = _entity_patience_window()
        assert kw["patience"] == {
            "timeout": timeout,
            "retry_wall_clock_budget_s": budget,
            "read_idle_timeout_s": ENTITY_LLM_READ_IDLE_TIMEOUT_S,
        }

        llm_for("understanding")  # inherits the mind: its model, its reasoning dial, still no speculation
        provider, kw = built[-1]
        assert provider == "lmstudio" and kw["model"] == "mind-model" and kw["thinking"] == "high"
        assert "speculation" not in kw

        from abstractruntime.identity.cognition_profile import CognitionProfileInvalid

        with pytest.raises(CognitionProfileInvalid, match="telepathy"):
            llm_for("telepathy")  # the runtime's own refusal, never a substitute client


def test_capability_handler_forwards_tools_media_and_params_like_the_mind_handler(monkeypatch: pytest.MonkeyPatch):
    """The LLM_CALL-shaped handler for a capability sends the payload's `tools`, `media` and
    `params` to the client and returns content/finish_reason/usage/model beside tool calls."""
    _built, llm = _install_recording_factory(monkeypatch)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        handler = _registry()._entity_capability_llm_handler("castor", "summaries")
        effect = SimpleNamespace(payload={
            "messages": [{"role": "user", "content": "rewrite this"}],
            "system_prompt": "You are the writer.",
            "tools": [{"name": "read_memory"}],
            "media": ["/abs/pic.png"],
            "params": {"thinking": "off", "response_format": {"type": "json_object"}},
        })
        outcome = handler(SimpleNamespace(run_id="r1", session_id="s1"), effect)

        sent = llm.calls[-1]
        assert sent["messages"] == effect.payload["messages"]
        assert sent["system_prompt"] == "You are the writer."
        assert sent["tools"] == [{"name": "read_memory"}]
        assert sent["media"] == ["/abs/pic.png"]
        assert sent["params"] == {"thinking": "off", "response_format": {"type": "json_object"}}
        result = outcome.result
        assert result["content"] == "ok"
        assert result["finish_reason"] == "stop" and result["usage"]["prompt_tokens"] == 3 and result["model"] == "recorded"


def test_entity_home_opens_its_memory_with_the_task_queue(monkeypatch: pytest.MonkeyPatch):
    """Correction 1 (memory owner): records enqueue rebuild tasks only in a home opened with
    `task_queue=True`; the door's `EntityHome` must open every home that way."""
    _install_recording_factory(monkeypatch)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home = _registry().get_home("castor")
        assert home.memory.tasks is not None, "the home's MemorySystem has no task queue (task_queue=True missing)"


# ------------------------------------------------------------------- 2. the visit-turn drain

import threading  # noqa: E402
import time  # noqa: E402


class _ScriptedLLM:
    """Replies in order; raises when exhausted (a silent fallback would hide an extra call)."""

    def __init__(self, replies) -> None:
        self._replies = list(replies)

    def generate(self, **kwargs):
        if not self._replies:
            raise AssertionError("scripted LLM exhausted — an unexpected extra call happened")
        return SimpleNamespace(content=self._replies.pop(0))


def _install_scripted_llm(monkeypatch: pytest.MonkeyPatch, replies) -> None:
    from abstractgateway import entity_chat

    llm = _ScriptedLLM(replies)
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)


def _report(*, trigger: str, window: str, rebuilt=(), ended_by: str = "empty") -> dict:
    """A DrainReport in the contract's shape (DESIGN-drain-v1.md)."""
    rebuilt = [dict(r) for r in rebuilt]
    return {
        "trigger": trigger, "window": window, "run_id": "run-drain-1", "selected": len(rebuilt),
        "applied": len(rebuilt), "unchanged": 0, "superseded": 0, "deferred": 0, "skipped": 0, "failed": 0,
        "left_out_counts": {}, "calls": len(rebuilt), "tokens": {"prompt": 10 * len(rebuilt), "completion": 5 * len(rebuilt)},
        "seconds": 0.5, "rebuilt": rebuilt, "ended_by": ended_by,
    }


ONE_REBUILT = {"task_id": "digest.rewrite:rec-1:v1", "kind": "digest.rewrite", "record_id": "rec-1",
               "outcome": "applied", "version": 2, "model": "summ-model"}


class _FakeDrain:
    """Stands in for `abstractruntime.identity.understanding.drain` with the contract's exact
    keyword-only signature (a wrong call from the gateway is a TypeError, which the host records
    as a failed drain — and the test then sees no `rebuilt`). Blocks on `gate` so a test can prove
    the reply never waited for it."""

    def __init__(self, reports) -> None:
        self.reports = list(reports)
        self.calls: list = []
        self.gate = threading.Event()
        self.started = threading.Event()

    def __call__(self, home_dir, *, trigger, window, budget_seconds, hard_cap, parent, llm_for_capability,
                 should_continue, holder="memory-tasks", now=None):
        self.calls.append({
            "home_dir": Path(home_dir), "trigger": trigger, "window": window, "budget_seconds": budget_seconds,
            "hard_cap": hard_cap, "parent": dict(parent), "llm_for_capability": llm_for_capability,
            "should_continue": should_continue, "holder": holder, "continue_at_start": bool(should_continue()),
        })
        self.started.set()
        if not self.gate.wait(timeout=15):
            raise AssertionError("the test never released the fake drain")
        if self.reports:
            return self.reports.pop(0)
        return _report(trigger=trigger, window=window)


def _install_fake_drain(monkeypatch: pytest.MonkeyPatch, reports) -> _FakeDrain:
    from abstractgateway import entity_drain

    fake = _FakeDrain(reports)
    monkeypatch.setattr(entity_drain, "resolve_drain", lambda: fake)
    return fake


def _visit_host():
    from abstractgateway.routes.entities import _visit_host

    return _visit_host()


def test_visit_turn_drains_after_the_reply_and_the_next_probe_lists_what_was_rebuilt(monkeypatch: pytest.MonkeyPatch):
    """Acceptance (design §gateway 2, 6): with cap_per_turn 1 the turn schedules exactly one
    drain(trigger="turn", window="visit", hard_cap=1, budget ≥ 60 s, parent={visit_run_id, turn_id})
    on the per-home worker; the reply returns while the drain is still blocked (never delayed); the
    report lands in `_visit.last_drain`; the next turn's probe carries `rebuilt` from it; a newer
    turn turns `should_continue` false."""
    fake = _install_fake_drain(monkeypatch, [_report(trigger="turn", window="visit", rebuilt=[ONE_REBUILT], ended_by="cap")])
    _install_scripted_llm(monkeypatch, ["Hello.", "Second.", "Reflection."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        _write_profile(_home_dir(), {}, rebuild={"cap_per_turn": 1})
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]

        turned = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "hello"})
        assert turned.status_code == 200, turned.text
        first = turned.json()
        assert first["reply"] == "Hello."
        assert first["rebuilt"] == [], "nothing has been rebuilt when the reply returns"
        assert fake.started.wait(5), "the drain was not scheduled after the park"
        assert not fake.gate.is_set() and fake.calls, "the reply came back while the drain was still running"

        call = fake.calls[0]
        assert call["home_dir"] == _home_dir()
        assert call["trigger"] == "turn" and call["window"] == "visit" and call["hard_cap"] == 1
        assert call["budget_seconds"] >= 60.0, "a task costs 60 s by default: the budget is at least cap x 60"
        assert call["parent"] == {"visit_run_id": run_id, "turn_id": first["turn_id"]}
        assert call["holder"] == "memory-tasks"
        assert call["continue_at_start"] is True
        assert callable(call["llm_for_capability"])

        fake.gate.set()
        assert _visit_host()._drain_worker.wait_idle(timeout=10), "the drain job did not finish"
        assert len(fake.calls) == 1, "cap 1 = one drain window per turn, never a loop"

        # Durable: the report rests in the run's own _visit namespace.
        er = _registry().get_entity_runtime("castor")
        stored = ((er.run_store.load(run_id).vars or {}).get("_visit") or {}).get("last_drain")
        assert stored and stored["rebuilt"] == [ONE_REBUILT] and stored["ended_by"] == "cap"

        second = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "again"})
        assert second.status_code == 200, second.text
        assert second.json()["rebuilt"] == [ONE_REBUILT], "the next probe lists what the previous turn rebuilt"
        assert call["should_continue"]() is False, "a newer turn ends the earlier window at its boundary"
        assert _visit_host()._drain_worker.wait_idle(timeout=10)
        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={"closed_by": "operator"})


def test_cap_zero_schedules_no_drain_and_the_probe_says_so(monkeypatch: pytest.MonkeyPatch):
    """cap_per_turn 0 = rebuilds only while it rests: no drain run is started on a turn, and the
    probe still reports a fresh empty `rebuilt` (never a stale list from an earlier turn)."""
    fake = _install_fake_drain(monkeypatch, [])
    fake.gate.set()
    _install_scripted_llm(monkeypatch, ["Hello.", "Reflection."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        _write_profile(_home_dir(), {}, rebuild={"cap_per_turn": 0})
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        turned = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "hello"})
        assert turned.status_code == 200, turned.text
        assert _visit_host()._drain_worker.wait_idle(timeout=10)
        assert fake.calls == [], "cap 0 starts no drain run"
        er = _registry().get_entity_runtime("castor")
        stored = ((er.run_store.load(run_id).vars or {}).get("_visit") or {}).get("last_drain")
        assert stored["rebuilt"] == [] and stored["selected"] == 0 and stored["ended_by"] == "cap"
        assert turned.json()["rebuilt"] == []
        assert not any("rebuild" in n for n in turned.json()["notices"])
        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={"closed_by": "operator"})


def test_probe_reads_rebuilt_from_last_drain_and_names_a_failed_drain():
    from abstractgateway.entity_visits import _compose_turn_probe

    ok = _compose_turn_probe({"_visit": {"last_drain": _report(trigger="turn", window="visit", rebuilt=[ONE_REBUILT])}, "_turn": {}})
    assert ok["rebuilt"] == [ONE_REBUILT]
    # An in-memory report (the newest) wins over the durable copy.
    newer = _compose_turn_probe({"_visit": {"last_drain": _report(trigger="turn", window="visit")}, "_turn": {}},
                                last_drain=_report(trigger="turn", window="visit", rebuilt=[ONE_REBUILT]))
    assert newer["rebuilt"] == [ONE_REBUILT]
    failed = _compose_turn_probe({"_visit": {}, "_turn": {"notices": ["kept"]}},
                                 last_drain={"trigger": "turn", "window": "visit", "rebuilt": [],
                                             "error": "ImportError: cannot import name 'drain'"})
    assert failed["rebuilt"] == []
    assert failed["notices"][0] == "kept"
    assert any(n.startswith("#FALLBACK memory rebuild did not run: ImportError") for n in failed["notices"])
    none = _compose_turn_probe({"_visit": {}, "_turn": {}})
    assert none["rebuilt"] == [] and none["notices"] == []
