"""ADR 0002: a runtime built by `create_local_runtime` is moved onto the model host.

`model_host.move_runtime_client_to_host` reads AbstractRuntime attributes (`_handlers`,
`_abstractcore_llm_client`, `_chat_summarizer._llm_resolver` and the pooled client's constructor
fields); this test is the tripwire for a runtime release that moves them (ADR 0002, Consequences).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from abstractgateway import model_host as mh
from model_host_fakes import FakeOpenAIServer

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Unix socket transport in this test")


def test_runtime_model_work_runs_in_the_model_host(tmp_path: Path) -> None:
    from abstractruntime import FileArtifactStore
    from abstractruntime.core.models import EffectType
    from abstractruntime.integrations.abstractcore import get_abstractcore_host_facade
    from abstractruntime.integrations.abstractcore.factory import create_local_runtime
    from abstractruntime.integrations.abstractcore.llm_client import default_model_warmup, set_default_model_warmup
    from abstractruntime.integrations.abstractcore.tool_executor import PassthroughToolExecutor

    with FakeOpenAIServer() as fake:
        sup = mh.ModelHostSupervisor(tmp_path / "data", nice=0).start()
        before = default_model_warmup()
        set_default_model_warmup(False)
        try:
            store = FileArtifactStore(tmp_path / "artifacts")
            tools = PassthroughToolExecutor(mode="delegated")
            runtime = create_local_runtime(provider="openai-compatible", model="fake-model",
                                           llm_kwargs={"base_url": fake.base_url, "api_key": "k"},
                                           artifact_store=store, tool_executor=tools)
            built = runtime._abstractcore_llm_client
            for _arg, attr in mh._MULTI_CLIENT_FIELDS:
                assert hasattr(built, attr), attr
            sentinel = object()
            from abstractgateway.model_host import ModelHostClient, install_runtime_client, mirror_multi_client

            proxy = mirror_multi_client(built, capability_defaults=None, host=sup)
            assert proxy._mh_ctor["llm_kwargs"]["timeout"] and proxy._mh_ctor["artifact_store"] is store
            install_runtime_client(runtime, proxy, tools=tools, artifact_store=store, run_store=None,
                                   extra_effect_handlers={EffectType.EMIT_EVENT: sentinel})
            assert runtime._abstractcore_llm_client is proxy
            assert runtime._handlers[EffectType.EMIT_EVENT] is sentinel  # the host's own handlers win again
            assert isinstance(get_abstractcore_host_facade(runtime).list_model_residency(), dict)
            # The pooled client (and its default model) lives in the child.
            assert proxy._default_provider == "openai-compatible"
            out = proxy.generate(prompt="through the runtime's client")
            assert "through the runtime" in out["content"]
            # Chat summaries resolve a Core provider instance that stays in the child.
            llm = runtime._chat_summarizer._llm_resolver("openai-compatible", "fake-model")
            assert isinstance(llm, mh._RemoteHandle)
            assert "summary please" in llm.generate("summary please").content
            assert isinstance(proxy, ModelHostClient)
        finally:
            set_default_model_warmup(before)
            sup.stop()
