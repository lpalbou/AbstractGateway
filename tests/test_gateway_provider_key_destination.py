"""A stored provider key goes only to the address it was saved for (security, 0.21.0).

A fake OpenAI-compatible upstream records the Authorization header of every request it gets, so
these tests watch the real wire: the gateway's discovery and model calls through the AbstractRuntime
facade and AbstractCore's openai-compatible provider, not a stub.

- A keyless model check (Test) of an address the person typed never carries the gateway's
  configured key, neither the AbstractCore store's `openai_compatible` key nor `OPENAI_API_KEY`.
- A saved connection with an address and no key calls that address with no key (AbstractCore would
  otherwise fall back to `OPENAI_API_KEY`).
- A saved connection's key goes only to its saved address: testing it at another address runs
  keyless, and saving it with another address and no key typed drops the key.
- The configured connection at its own address still gets its key.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from test_gateway_provider_endpoint_profiles import _app_client

HEADERS = {"Authorization": "Bearer admin-token"}
CONFIG_KEY = "sk-config-secret-0123456789"
ENV_KEY = "sk-env-secret-0123456789"
PROFILE_KEY = "sk-profile-secret-0123456789"


class _Upstream:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None]] = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def _reply(self, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                upstream.seen.append((self.path, self.headers.get("Authorization")))
                self._reply({"object": "list", "data": [{"id": "fake-model", "object": "model"}]})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                upstream.seen.append((self.path, self.headers.get("Authorization")))
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    chunk = {"id": "c", "object": "chat.completion.chunk", "model": "fake-model",
                             "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())
                    return
                self._reply({"id": "c", "object": "chat.completion", "model": "fake-model",
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def auth(self) -> list[str | None]:
        return [a for _p, a in self.seen]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Upstream]:
    # The model listing goes through the runtime's real local listing (AbstractCore's provider on
    # the wire); only the facade lookup, which needs a running runtime, is bypassed.
    from abstractruntime.integrations.abstractcore.discovery_queries import local_list_provider_models
    import abstractgateway.routes.gateway as gateway_routes

    class _LocalDiscovery:
        def list_provider_models(self, provider_name: str, **kwargs: Any) -> dict[str, Any]:
            return local_list_provider_models(provider_name, **kwargs)

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (_LocalDiscovery(), None))
    up = _Upstream()
    try:
        yield up
    finally:
        up.close()


def _configured_key(monkeypatch: pytest.MonkeyPatch, *, base_url: str | None = None) -> None:
    """The gateway's configured openai-compatible connection: a key in the AbstractCore store (the
    test session's scratch config file) and OPENAI_API_KEY in the environment."""
    from abstractcore.config.manager import ConfigurationManager

    monkeypatch.setenv("OPENAI_API_KEY", ENV_KEY)
    if base_url is None:
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    manager = ConfigurationManager(config_file=Path(os.environ["ABSTRACTCORE_CONFIG_FILE"]), apply_env=False)
    assert manager.set_api_key("openai-compatible", CONFIG_KEY)


def test_a_keyless_check_of_a_typed_address_never_carries_the_configured_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: _Upstream
) -> None:
    _configured_key(monkeypatch, base_url="https://configured.example.test/v1")
    with _app_client(tmp_path, monkeypatch) as client:
        res = client.post(
            "/api/gateway/config/provider-endpoint-profiles/discover-models",
            headers=HEADERS,
            json={"provider_family": "openai-compatible", "base_url": upstream.url},
        )
    assert res.status_code == 200, res.text
    assert upstream.seen, ("the typed address was asked", res.json())
    assert upstream.auth() == [None] * len(upstream.seen), upstream.seen


def test_the_configured_connection_at_its_own_address_still_gets_its_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: _Upstream
) -> None:
    _configured_key(monkeypatch, base_url=upstream.url)
    with _app_client(tmp_path, monkeypatch) as client:
        res = client.post(
            "/api/gateway/config/provider-endpoint-profiles/discover-models",
            headers=HEADERS,
            json={"provider_family": "openai-compatible", "base_url": upstream.url + "/"},
        )
    assert res.status_code == 200, res.text
    # AbstractCore also makes one request of its own without a key; every key sent is this one.
    assert {a for a in upstream.auth() if a} == {f"Bearer {CONFIG_KEY}"}, upstream.seen


def test_a_saved_keyless_connection_calls_its_address_with_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: _Upstream
) -> None:
    _configured_key(monkeypatch)
    with _app_client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=HEADERS,
            json={"id": "lan", "display_name": "LAN server", "provider_family": "openai-compatible",
                  "base_url": upstream.url, "scope": "gateway"},
        )
        assert created.status_code == 200, created.text
        listed = client.post(
            "/api/gateway/config/provider-endpoint-profiles/discover-models",
            headers=HEADERS,
            json={"profile_id": "lan"},
        )
        assert listed.status_code == 200, listed.text
        res = client.post(
            "/api/gateway/sandbox/generate",
            headers=HEADERS,
            json={"capability": "output.text", "provider": "endpoint:lan", "model": "fake-model", "prompt": "hello"},
        )
    assert res.status_code == 200, res.text
    paths = [p for p, _a in upstream.seen]
    assert any(p.endswith("/models") for p in paths) and any(p.endswith("/chat/completions") for p in paths), upstream.seen
    assert upstream.auth() == [None] * len(upstream.seen), upstream.seen


def test_a_saved_key_goes_only_to_its_saved_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, upstream: _Upstream
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    with _app_client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=HEADERS,
            json={"id": "office", "display_name": "Office", "provider_family": "openai-compatible",
                  "base_url": "https://office.example.test/v1", "api_key": PROFILE_KEY, "scope": "gateway"},
        )
        assert created.status_code == 200, created.text
        # Edit: another address, key not typed again. The Test runs keyless...
        tested = client.post(
            "/api/gateway/config/provider-endpoint-profiles/discover-models",
            headers=HEADERS,
            json={"profile_id": "office", "base_url": upstream.url},
        )
        assert tested.status_code == 200, tested.text
        assert upstream.seen and upstream.auth() == [None] * len(upstream.seen), upstream.seen
        # ...and the save drops the key: it belonged to the old address.
        saved = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=HEADERS,
            json={"id": "office", "display_name": "Office", "base_url": upstream.url, "scope": "gateway"},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["profile"]["api_key_set"] is False, saved.json()
        # The same address (trailing slash aside) keeps it.
        again = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=HEADERS,
            json={"id": "office", "display_name": "Office", "base_url": upstream.url + "/", "api_key": PROFILE_KEY, "scope": "gateway"},
        )
        assert again.json()["profile"]["api_key_set"] is True
        kept = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=HEADERS,
            json={"id": "office", "display_name": "Office", "base_url": upstream.url, "scope": "gateway"},
        )
        assert kept.json()["profile"]["api_key_set"] is True, kept.json()
        upstream.seen.clear()
        at_home = client.post(
            "/api/gateway/config/provider-endpoint-profiles/discover-models",
            headers=HEADERS,
            json={"profile_id": "office", "base_url": upstream.url + "/"},
        )
        assert at_home.status_code == 200, at_home.text
    assert {a for a in upstream.auth() if a} == {f"Bearer {PROFILE_KEY}"}, upstream.seen


def test_a_run_on_a_saved_keyless_connection_is_explicitly_keyless(tmp_path: Path) -> None:
    """Runs reach a connection through the bundle host (client kwargs, and the endpoint resolver the
    runtime applies to each call): a keyless connection with an address carries AbstractCore's
    explicit no-key value, so AbstractCore never falls back to OPENAI_API_KEY or the configured key."""
    from abstractgateway.hosts.bundle_host import (
        _attach_provider_endpoint_profile_resolver,
        _resolve_gateway_default_endpoint_profile,
    )
    from abstractgateway.provider_endpoint_profiles import NO_PROVIDER_KEY, ProviderEndpointProfileStore
    from abstractruntime.integrations.abstractcore.effect_handlers import _apply_provider_endpoint_profile_resolution

    data_root, root_data = tmp_path / "runtime", tmp_path / "root"
    ProviderEndpointProfileStore(base_dir=root_data).upsert_profile(
        profile_id="lan", display_name="LAN", provider_family="openai-compatible",
        base_url="http://192.168.1.20:8000/v1", scope="gateway",
    )
    provider, kwargs, _override = _resolve_gateway_default_endpoint_profile(
        provider="endpoint:lan", data_root=data_root, catalog_root=root_data,
    )
    assert (provider, kwargs) == ("openai-compatible", {"base_url": "http://192.168.1.20:8000/v1", "api_key": NO_PROVIDER_KEY})

    class FakeLLM:
        pass

    class FakeRuntime:
        pass

    runtime = FakeRuntime()
    runtime._abstractcore_llm_client = FakeLLM()
    _attach_provider_endpoint_profile_resolver(runtime=runtime, data_root=data_root, catalog_root=root_data)  # type: ignore[arg-type]
    params: dict[str, Any] = {"_provider": "endpoint:lan"}
    _apply_provider_endpoint_profile_resolution(llm=runtime._abstractcore_llm_client, params=params)  # type: ignore[arg-type]
    assert params["provider_api_key"] == NO_PROVIDER_KEY
    # AbstractCore's provider sends no Authorization header for it.
    from abstractcore.providers.openai_compatible_provider import OpenAICompatibleProvider

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("OPENAI_API_KEY", ENV_KEY)
        assert OpenAICompatibleProvider._resolve_api_key(object.__new__(OpenAICompatibleProvider), NO_PROVIDER_KEY) == NO_PROVIDER_KEY
