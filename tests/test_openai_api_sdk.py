"""OpenAI conformance of /v1, driven by the official `openai` Python SDK.

The SDK talks to the gateway app in-process (httpx ASGI transport): gateway
security middleware -> /v1 boundary -> AbstractCore's real serving app. Only
the model itself is a deterministic stub (Core's `create_llm`, its provider
listing and its embedding manager): no network, no provider keys, no model
loads. Covers models list/retrieve, chat (plain, streamed with and without
include_usage), a tool-call round trip, embeddings (base64, the SDK default,
and float), the standard error envelope and the /core/v1 alias."""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
import struct
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

openai = pytest.importorskip("openai")

from abstractgateway import core_endpoint as ce  # noqa: E402
from abstractgateway import network_exposure as ne  # noqa: E402

pytestmark = pytest.mark.basic

ADMIN_TOKEN = "admin-token-long-enough"
TOOLS = [{"type": "function", "function": {"name": "get_weather", "description": "Weather for a city",
                                            "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                                           "required": ["city"]}}}]


@pytest.fixture
def sdk(tmp_path, monkeypatch):
    data = tmp_path / "runtime"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "ABSTRACTCORE_AUTH_TOKEN",
                 "ABSTRACTGATEWAY_ALLOWED_ORIGINS", "ABSTRACTGATEWAY_TRUST_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ne, "discover_interfaces", lambda: ([], "stub"))
    monkeypatch.setattr(ne, "bonjour_hostname", lambda: None)
    monkeypatch.setattr(ne, "tailscale_status", lambda: None)

    core = importlib.import_module("abstractcore.server.app")
    from abstractcore.core.types import GenerateResponse

    seen = []

    class LLM:
        def generate(self, **kw):
            seen.append(kw)
            msgs = kw.get("messages") or []
            usage = {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
            if kw.get("tools") and not any(m.get("role") == "tool" for m in msgs):
                resp = GenerateResponse(content="", model="qwen3:4b", finish_reason="tool_calls", usage=usage,
                                        tool_calls=[{"name": "get_weather", "arguments": {"city": "Paris"}, "call_id": "call_w1"}])
                return iter([resp]) if kw.get("stream") else resp
            text = "Sunny in Paris." if any(m.get("role") == "tool" for m in msgs) else "Hello there."
            if kw.get("stream"):
                return iter([GenerateResponse(content="Hello ", model="qwen3:4b"),
                             GenerateResponse(content="there.", model="qwen3:4b", finish_reason="stop", usage=usage)])
            return GenerateResponse(content=text, model="qwen3:4b", finish_reason="stop", usage=usage)

    monkeypatch.setattr(core, "create_llm", lambda *a, **kw: LLM())
    registry = importlib.import_module("abstractcore.providers.registry")
    monkeypatch.setattr(registry, "list_available_providers", lambda: ["ollama"])
    monkeypatch.setattr(core, "get_models_from_provider", lambda prov, **kw: ["qwen3:4b"] if prov == "ollama" else [])

    class Embedder:
        def __init__(self, **kw):
            self.model_name = kw.get("model")

        def embed_batch(self, inputs):
            return [[0.5, -0.25, float(i)] for i, _ in enumerate(inputs)]

    manager = importlib.import_module("abstractcore.embeddings.manager")
    monkeypatch.setattr(manager, "EmbeddingManager", Embedder)

    from abstractgateway.routes import gateway_router
    from abstractgateway.routes.core_endpoint import router, user_router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    app.include_router(gateway_router, prefix="/api")
    app.include_router(router, prefix="/api")
    app.include_router(user_router, prefix="/api")
    app.mount("/v1", ce.CoreEndpoint())
    app.mount("/core", ce.LegacyCoreRedirect())
    ce.change_settings(data, enabled=True)

    def client(key=ADMIN_TOKEN):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)), base_url="http://127.0.0.1:8080")
        return openai.AsyncOpenAI(base_url="http://127.0.0.1:8080/v1", api_key=key, http_client=http, max_retries=0)

    return SimpleNamespace(client=client, seen=seen, app=app)


def run(coro):
    return asyncio.run(coro)


def test_models_list_and_retrieve(sdk):
    async def go():
        c = sdk.client()
        listed = await c.models.list()
        ids = [m.id for m in listed.data]
        assert ids == ["ollama/qwen3:4b"] and listed.data[0].object == "model" and listed.data[0].owned_by == "ollama"
        one = await c.models.retrieve("ollama/qwen3:4b")
        assert one.id == "ollama/qwen3:4b" and one.object == "model"
        with pytest.raises(openai.NotFoundError) as err:
            await c.models.retrieve("ollama/nope")
        assert err.value.body["code"] == "model_not_found"
    run(go())


def test_chat_completion_plain(sdk):
    async def go():
        r = await sdk.client().chat.completions.create(model="ollama/qwen3:4b", messages=[{"role": "user", "content": "hi"}],
                                                       max_completion_tokens=32, extra_headers={"OpenAI-Organization": "org-x"})
        assert r.object == "chat.completion" and r.id and r.created and r.model == "ollama/qwen3:4b"
        assert r.choices[0].message.role == "assistant" and r.choices[0].message.content == "Hello there."
        assert r.choices[0].finish_reason == "stop"
        assert (r.usage.prompt_tokens, r.usage.completion_tokens, r.usage.total_tokens) == (12, 4, 16)
        assert sdk.seen[-1]["max_tokens"] == 32  # max_completion_tokens mapped
    run(go())


def test_chat_stream_chunks_end_with_done_and_usage_only_on_request(sdk):
    async def go():
        c = sdk.client()
        stream = await c.chat.completions.create(model="ollama/qwen3:4b", messages=[{"role": "user", "content": "hi"}], stream=True)
        chunks = [ch async for ch in stream]
        assert all(ch.object == "chat.completion.chunk" for ch in chunks)
        assert chunks[0].choices[0].delta.role == "assistant"
        assert "".join(ch.choices[0].delta.content or "" for ch in chunks if ch.choices) == "Hello there."
        assert [ch.choices[0].finish_reason for ch in chunks if ch.choices][-1] == "stop"
        assert all(ch.usage is None for ch in chunks)
        stream = await c.chat.completions.create(model="ollama/qwen3:4b", messages=[{"role": "user", "content": "hi"}],
                                                 stream=True, stream_options={"include_usage": True})
        chunks = [ch async for ch in stream]
        assert chunks[-1].choices == [] and chunks[-1].usage.total_tokens == 16
        assert all(ch.usage is None for ch in chunks[:-1])
    run(go())


def test_raw_stream_is_sse_terminated_by_done(sdk):
    async def go():
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=sdk.app, client=("127.0.0.1", 1)), base_url="http://127.0.0.1:8080")
        r = await http.post("/v1/chat/completions", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                            json={"model": "ollama/qwen3:4b", "messages": [{"role": "user", "content": "hi"}], "stream": True})
        assert r.headers["content-type"].startswith("text/event-stream") and r.headers.get("x-request-id")
        events = [e for e in r.text.split("\n\n") if e.strip()]
        assert all(e.startswith("data: ") for e in events) and events[-1] == "data: [DONE]"
    run(go())


def test_tool_call_round_trip(sdk):
    async def go():
        c = sdk.client()
        first = await c.chat.completions.create(model="ollama/qwen3:4b", tools=TOOLS, tool_choice="auto",
                                                messages=[{"role": "user", "content": "Weather in Paris?"}])
        choice = first.choices[0]
        assert choice.finish_reason == "tool_calls" and not choice.message.content
        call = choice.message.tool_calls[0]
        assert call.type == "function" and call.function.name == "get_weather" and json.loads(call.function.arguments) == {"city": "Paris"}
        final = await c.chat.completions.create(model="ollama/qwen3:4b", tools=TOOLS, messages=[
            {"role": "user", "content": "Weather in Paris?"},
            {"role": "assistant", "content": None, "tool_calls": [call.model_dump()]},
            {"role": "tool", "tool_call_id": call.id, "content": "{\"sky\": \"sunny\"}"}])
        assert final.choices[0].message.content == "Sunny in Paris." and final.choices[0].finish_reason == "stop"
        stream = await c.chat.completions.create(model="ollama/qwen3:4b", tools=TOOLS, stream=True,
                                                 messages=[{"role": "user", "content": "Weather in Paris?"}])
        chunks = [ch async for ch in stream]
        deltas = [tc for ch in chunks if ch.choices for tc in (ch.choices[0].delta.tool_calls or [])]
        assert deltas and deltas[0].function.name == "get_weather"
        reasons = [ch.choices[0].finish_reason for ch in chunks if ch.choices]
        assert reasons[-1] == "tool_calls" and all(r is None for r in reasons[:-1])
    run(go())


def test_embeddings_base64_default_and_float(sdk):
    async def go():
        c = sdk.client()
        r = await c.embeddings.create(model="huggingface/stub-embed", input=["a", "b"])  # SDK asks base64
        assert r.object == "list" and [d.index for d in r.data] == [0, 1]
        assert r.data[1].embedding == pytest.approx([0.5, -0.25, 1.0])
        raw = await c.embeddings.create(model="huggingface/stub-embed", input="a", encoding_format="float")
        assert raw.data[0].embedding == pytest.approx([0.5, -0.25, 0.0])
    run(go())


def test_errors_use_the_standard_envelope(sdk):
    async def go():
        with pytest.raises(openai.AuthenticationError) as err:
            await sdk.client("wrong-token").models.list()
        assert set(err.value.body) == {"message", "type", "param", "code"} and err.value.body["code"] == "invalid_api_key"
        c = sdk.client()
        with pytest.raises(openai.BadRequestError) as err:
            await c.chat.completions.create(model="ollama/qwen3:4b", messages=[{"role": "user", "content": "hi"}],
                                            response_format={"type": "json_object"})
        assert err.value.body["param"] == "response_format" and err.value.body["type"] == "invalid_request_error"
        with pytest.raises(openai.BadRequestError) as err:  # Core's validation (422) -> 400
            await c.chat.completions.create(model="ollama/qwen3:4b", messages=[{"role": "wizard", "content": "hi"}])
        assert set(err.value.body) == {"message", "type", "param", "code"}
        with pytest.raises(openai.NotFoundError) as err:
            await c.post("/files", cast_to=object, body={})
        assert err.value.body["type"] == "invalid_request_error"
    run(go())


def test_core_v1_alias_redirects_and_sdk_follows(sdk):
    async def go():
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=sdk.app, client=("127.0.0.1", 1)), base_url="http://127.0.0.1:8080")
        r = await http.get("/core/v1/models", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        assert r.status_code == 308 and r.headers["location"] == "/v1/models"
        legacy = openai.AsyncOpenAI(base_url="http://127.0.0.1:8080/core/v1", api_key=ADMIN_TOKEN, max_retries=0,
                                    http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=sdk.app, client=("127.0.0.1", 1)),
                                                                  base_url="http://127.0.0.1:8080", follow_redirects=True))
        assert [m.id for m in (await legacy.models.list()).data] == ["ollama/qwen3:4b"]
    run(go())


def test_embeddings_base64_encoding_is_float32_little_endian():
    out = json.loads(ce.embeddings_to_base64(json.dumps({"data": [{"embedding": [1.0, 2.5]}]}).encode()))
    assert struct.unpack("<2f", base64.b64decode(out["data"][0]["embedding"])) == (1.0, 2.5)
