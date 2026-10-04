"""Structured outputs at /v1, driven by the official `openai` SDK: `response_format`
json_object and json_schema (and the SDK's `chat.completions.parse` helper with a
Pydantic model), through the gateway boundary into AbstractCore's real serving
app and its structured-output handler.

The model is a deterministic in-process provider (a real AbstractCore
BaseProvider) named like each provider family the gateway routes to, so Core
picks the same lane as in production: the caller's schema as a decoding
constraint (Ollama, LM Studio, llama.cpp/GGUF, MLX with Outlines, OpenAI,
OpenAI-compatible servers, Anthropic's forced tool) or in the prompt (a model
that cannot constrain), and in every case the answer validated against the
schema. No network, no provider keys, no model loads."""
from __future__ import annotations

import asyncio
import importlib
import json
from typing import List

import pytest
from pydantic import BaseModel

openai = pytest.importorskip("openai")

from abstractcore.core.types import GenerateResponse  # noqa: E402
from abstractcore.providers.base import BaseProvider  # noqa: E402
from test_openai_api_sdk import sdk  # noqa: E402,F401 - fixture

pytestmark = pytest.mark.basic

SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}, "temp_c": {"type": "number"},
                                           "sky": {"type": "string", "enum": ["clear", "cloudy", "rain"]}},
          "required": ["city", "temp_c", "sky"], "additionalProperties": False}
GOOD = {"city": "Paris", "temp_c": 21.5, "sky": "clear"}
RF = {"type": "json_schema", "json_schema": {"name": "weather", "schema": SCHEMA, "strict": True}}
ASK = [{"role": "user", "content": "The weather in Paris, as JSON."}]


class Weather(BaseModel):
    city: str
    temp_c: float
    sky: str


def _outlines() -> bool:
    try:
        import outlines  # noqa: F401
        return True
    except ImportError:
        return False


# route -> (provider class name, model capability structured_output or None, schema reaches the provider as a constraint)
FAMILIES = {
    "ollama": ("OllamaProvider", None, True),
    "lmstudio": ("LMStudioProvider", None, True),
    "huggingface": ("HuggingFaceProvider", None, True),  # GGUF through llama.cpp
    "mlx": ("MLXProvider", None, _outlines()),
    "openai": ("OpenAIProvider", "native", True),
    "openai-compatible": ("OpenAICompatibleProvider", "native", True),
    "anthropic": ("AnthropicProvider", "native", True),
    "openrouter": ("OpenRouterProvider", "prompted", False),
}


class _Fake(BaseProvider):
    script: List = []
    calls: List = []

    def __init__(self, model: str, **kwargs):
        super().__init__(model, **kwargs)
        self.provider = "fake"

    def get_capabilities(self):
        return ["chat"]

    def list_available_models(self):
        return []

    def unload_model(self, model_name):
        return None

    def _generate_internal(self, prompt, messages=None, system_prompt=None, tools=None, media=None, stream=False,
                           response_model=None, execute_tools=None, media_metadata=None, **kwargs):
        type(self).calls.append({"constrained": response_model is not None, "prompt": prompt,
                                 "schema": response_model.model_json_schema() if response_model is not None else None})
        answer = type(self).script.pop(0) if type(self).script else GOOD
        return GenerateResponse(content=answer if isinstance(answer, str) else json.dumps(answer), model=self.model,
                                finish_reason="stop")


@pytest.fixture
def families(sdk, monkeypatch):  # noqa: F811
    core = importlib.import_module("abstractcore.server.app")
    made = {}

    def create_llm(provider, model=None, **kw):
        name, capability, _ = FAMILIES[provider]
        if provider not in made:
            attrs = {"script": [], "calls": []}
            if name == "HuggingFaceProvider":
                attrs["model_type"] = "gguf"
            cls = type(name, (_Fake,), attrs)
            if capability is not None:
                base_init = cls.__init__

                def __init__(self, model, _base=base_init, _cap=capability, **kw2):
                    _base(self, model, **kw2)
                    self.model_capabilities = dict(self.model_capabilities or {}, structured_output=_cap)

                cls.__init__ = __init__
            made[provider] = cls
        return made[provider](model or "m")

    monkeypatch.setattr(core, "create_llm", create_llm)
    sdk.made = made
    return sdk


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("provider", sorted(FAMILIES))
def test_json_schema_through_the_sdk_for_every_provider_family(families, provider):
    async def go():
        r = await families.client().chat.completions.create(model=f"{provider}/m", messages=ASK, response_format=RF)
        assert r.object == "chat.completion" and r.choices[0].finish_reason == "stop"
        assert json.loads(r.choices[0].message.content) == GOOD
    run(go())
    call = families.made[provider].calls[0]
    assert call["constrained"] is FAMILIES[provider][2]
    if call["constrained"]:
        assert call["schema"] == SCHEMA
    else:
        assert '"additionalProperties": false' in call["prompt"]


def test_sdk_parse_helper_with_a_pydantic_model(families):
    async def go():
        done = await families.client().beta.chat.completions.parse(model="ollama/m", messages=ASK, response_format=Weather)
        parsed = done.choices[0].message.parsed
        assert isinstance(parsed, Weather) and parsed.city == "Paris" and parsed.temp_c == 21.5
    run(go())
    sent = families.made["ollama"].calls[0]["schema"]
    assert sent["required"] == ["city", "temp_c", "sky"] and sent["additionalProperties"] is False


def test_json_object_through_the_sdk(families):
    async def go():
        r = await families.client().chat.completions.create(model="lmstudio/m", messages=ASK,
                                                            response_format={"type": "json_object"})
        assert isinstance(json.loads(r.choices[0].message.content), dict)
    run(go())


def test_a_wrong_answer_is_retried_then_validated(families):
    async def go():
        c = families.client()
        await c.chat.completions.create(model="openrouter/m", messages=ASK, response_format=RF)
        families.made["openrouter"].calls.clear()
        families.made["openrouter"].script[:] = ['{"city": "Paris", "temp_c": "warm", "sky": "clear"}', GOOD]
        r = await c.chat.completions.create(model="openrouter/m", messages=ASK, response_format=RF)
        assert json.loads(r.choices[0].message.content) == GOOD
    run(go())
    calls = families.made["openrouter"].calls
    assert len(calls) == 2 and "temp_c" in calls[1]["prompt"] and "must be of type number" in calls[1]["prompt"]


@pytest.mark.parametrize("provider", ["ollama", "openrouter"])
def test_an_answer_that_never_matches_is_a_standard_error(families, provider):
    async def go():
        c = families.client()
        await c.chat.completions.create(model=f"{provider}/m", messages=ASK, response_format=RF)
        families.made[provider].script[:] = [{"city": "Paris"}] * 20
        with pytest.raises(openai.InternalServerError) as err:
            await c.chat.completions.create(model=f"{provider}/m", messages=ASK, response_format=RF)
        body = err.value.body
        assert set(body) == {"message", "type", "param", "code"}
        assert body["code"] == "structured_output_invalid" and body["param"] == "response_format"
    run(go())


def test_streamed_structured_answer(families):
    async def go():
        stream = await families.client().chat.completions.create(model="ollama/m", messages=ASK, response_format=RF,
                                                                 stream=True)
        chunks = [ch async for ch in stream]
        assert chunks[0].choices[0].delta.role == "assistant"
        text = "".join(ch.choices[0].delta.content or "" for ch in chunks if ch.choices)
        assert json.loads(text) == GOOD and chunks[-1].choices[0].finish_reason == "stop"
    run(go())


def test_bad_schema_is_refused_at_the_boundary_before_core(families):
    async def go():
        c = families.client()
        with pytest.raises(openai.BadRequestError) as err:
            await c.chat.completions.create(model="ollama/m", messages=ASK, response_format={
                "type": "json_schema", "json_schema": {"name": "w", "schema": {"type": "array"}}})
        assert err.value.body["code"] == "invalid_response_format"
        assert err.value.body["param"] == "response_format.json_schema.schema"
        with pytest.raises(openai.BadRequestError) as err:
            await c.chat.completions.create(model="ollama/m", messages=ASK, response_format=RF, tools=[
                {"type": "function", "function": {"name": "f", "parameters": {"type": "object", "properties": {}}}}])
        assert err.value.body["param"] == "response_format"
    run(go())
    assert "ollama" not in families.made  # no model was created
