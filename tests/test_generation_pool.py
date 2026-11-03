"""R2 (2026-10-29 audit): gateway-side generations borrow the principal's runtime pool."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from abstractgateway import generation_pool as gp


class _Pool:
    _default_provider = "mlx"
    _llm_kwargs: dict = {}

    def __init__(self) -> None:
        self.calls = []

    def generate(self, **kw):
        self.calls.append(kw)
        return {"content": "ok"}


def test_a_generation_rides_the_pool_with_the_per_call_routing_keys() -> None:
    pool = _Pool()
    g = gp.PoolGeneration(pool, provider="openai-compatible", model="m", llm_kwargs={"base_url": "http://x/v1", "api_key": "k"})
    assert g.generate(prompt="hi", params={"thinking": "low"}) == {"content": "ok"}
    params = pool.calls[0]["params"]
    assert params == {"thinking": "low", "_provider": "openai-compatible", "_model": "m", "base_url": "http://x/v1", "api_key": "k"}
    # A caller's own per-call key wins over the resolved connection; the route always wins.
    g.generate(prompt="hi", params={"base_url": "http://y/v1", "_provider": "other"})
    assert pool.calls[1]["params"]["base_url"] == "http://y/v1" and pool.calls[1]["params"]["_provider"] == "openai-compatible"


def test_a_connection_key_the_pool_cannot_take_per_call_is_refused() -> None:
    with pytest.raises(ValueError, match="only base_url, api_key"):
        gp.PoolGeneration(_Pool(), provider="p", model="m", llm_kwargs={"timeout": 5})


def test_the_principals_pool_is_borrowed_and_only_a_poolless_host_builds_its_own(monkeypatch) -> None:
    pool = _Pool()
    svc = SimpleNamespace(host=SimpleNamespace(runtime=SimpleNamespace(_abstractcore_llm_client=pool)))
    import abstractgateway.service as service

    monkeypatch.setattr(service, "get_gateway_service", lambda: svc)
    client = gp.generation_client(provider="mlx", model="m")
    assert isinstance(client, gp.PoolGeneration) and client.pool is pool
    built = []
    monkeypatch.setattr(service, "get_gateway_service", lambda: SimpleNamespace(host=SimpleNamespace(runtime=None)))
    from abstractgateway import model_host

    monkeypatch.setattr(model_host, "local_client", lambda **kw: built.append(kw) or "own")
    assert gp.generation_client(provider="mlx", model="m", local_ctor={"provider": "mlx", "model": "m"}) == "own"
    assert built == [{"provider": "mlx", "model": "m"}]


def test_the_pools_own_connection_is_not_repeated_per_call() -> None:
    """A key equal to the pool's default connection would key a second client (another copy)."""
    pool = _Pool()
    pool._default_provider = "openai-compatible"
    pool._llm_kwargs = {"base_url": "http://x/v1", "api_key": "k", "timeout": 7200}
    gp.PoolGeneration(pool, provider="openai-compatible", model="m", llm_kwargs={"base_url": "http://x/v1", "api_key": "k"}).generate(prompt="p")
    assert pool.calls[0]["params"] == {"_provider": "openai-compatible", "_model": "m"}
