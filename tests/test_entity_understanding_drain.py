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
