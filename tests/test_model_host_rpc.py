"""ADR 0002: the model host child and its proxy, with a real child process and a fake model server.

Each test starts `python -m abstractgateway.model_host_child` through `ModelHostSupervisor` and calls
AbstractRuntime's `LocalAbstractCoreLLMClient` in it through `ModelHostClient`; the provider is Core's
`openai-compatible` one, pointed at `FakeOpenAIServer` (no weights, no network).
"""
from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from abstractgateway import model_host as mh
from model_host_fakes import FakeOpenAIServer

pytestmark = pytest.mark.skipif(os.name != "posix", reason="kill -9 / SIGSTOP semantics")


@pytest.fixture()
def fake():
    with FakeOpenAIServer(chunks=4) as srv:
        yield srv


def _ctor(srv: FakeOpenAIServer) -> dict:
    return dict(provider="openai-compatible", model="fake-model",
                llm_kwargs={"base_url": srv.base_url, "api_key": "fake-key"})


def _sup(tmp_path: Path, **kw) -> mh.ModelHostSupervisor:
    kw.setdefault("nice", 0)
    kw.setdefault("backoff_s", (0.1,))
    return mh.ModelHostSupervisor(tmp_path / "data", **kw).start()


def _until(pred, timeout_s=20.0):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_generate_attributes_and_live_deltas_cross_the_boundary(tmp_path, fake):
    from abstractruntime.core.live_deltas import LiveDeltaEmitter

    sup = _sup(tmp_path)
    try:
        client = mh.ModelHostClient(sup, kind="local", ctor=_ctor(fake))
        out = client.generate(prompt="hello")
        assert "reply:" in out["content"] and "hello" in out["content"]
        # Attributes are the child's (the client lives there, not here).
        assert client._provider == "openai-compatible" and client._model == "fake-model"
        events = []
        emitter = LiveDeltaEmitter(sink=events.append, run_id="r", node_id="n", call_id="c", flush_interval_s=0.0)
        out = client.generate(prompt="streamed", params={"stream": True, "_on_delta": emitter})
        texts = "".join(e.get("text", "") for e in events if e.get("kind") == "llm.delta")
        assert "streamed" in out["content"] and texts and texts in out["content"], (texts, out["content"])
        assert all(e["run_id"] == "r" for e in events)
        assert sup.pid and sup.pid != os.getpid()
    finally:
        sup.stop()


def test_provider_exception_keeps_its_type_and_message(tmp_path, fake):
    sup = _sup(tmp_path)
    try:
        client = mh.ModelHostClient(sup, kind="local", ctor=dict(provider="no-such-provider", model="x"))
        with pytest.raises(Exception) as info:
            client.generate(prompt="x")
        assert type(info.value).__module__.startswith("abstractcore"), type(info.value)
        assert str(info.value).startswith("Unknown provider: no-such-provider")
        good = mh.ModelHostClient(sup, kind="local", ctor=_ctor(fake))
        with pytest.raises(AttributeError):
            good.no_such_attribute  # noqa: B018 - the child's AttributeError, raised here
        assert getattr(good, "no_such_attribute", None) is None
    finally:
        sup.stop()


def test_a_stop_reaches_the_generation_in_the_child(tmp_path, fake):
    fake.delay_s = 0.4  # 4 chunks: 1.6 s
    sup = _sup(tmp_path)
    try:
        client = mh.ModelHostClient(sup, kind="local", ctor=_ctor(fake))
        client.generate(prompt="warm")
        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        t0 = time.monotonic()
        try:
            client.generate(prompt="stop me", params={"stream": True, "cancel_event": stop})
        except Exception:  # noqa: BLE001 - Core may end a cancelled stream either way
            pass
        assert time.monotonic() - t0 < 1.3, "the Stop did not reach the child's generation"
    finally:
        sup.stop()


def test_kill_dash_nine_fails_the_request_records_an_incident_and_restarts(tmp_path, fake):
    sup = _sup(tmp_path)
    try:
        client = mh.ModelHostClient(sup, kind="local", ctor=_ctor(fake))
        assert "warm" in client.generate(prompt="warm")["content"]
        fake.delay_s = 1.0
        pid = sup.pid
        threading.Timer(0.5, lambda: os.kill(pid, signal.SIGKILL)).start()
        t0 = time.monotonic()
        with pytest.raises(mh.ModelHostCrashed) as info:
            client.generate(prompt="victim")
        assert time.monotonic() - t0 < 5.0
        assert "signal 9" in str(info.value) and info.value.status_code == 424
        incidents = mh.read_incidents(tmp_path / "data")
        assert incidents and incidents[0]["schema"] == mh.INCIDENT_SCHEMA
        first = incidents[0]
        assert first["kind"] == "exit" and first["signal"] == "SIGKILL" and first["pid"] == pid
        assert any(row.get("method") == "generate" for row in first["inflight"]), first["inflight"]
        assert first["restart"]["will_restart"] is True
        assert _until(lambda: sup.generation_alive() and sup.pid not in (None, pid))
        fake.delay_s = 0.0
        assert "after" in client.generate(prompt="after")["content"]  # rebuilt in the new child
        assert sup.snapshot()["last_incident"]["kind"] == "exit"
    finally:
        sup.stop()


def test_a_request_while_restarting_is_a_retryable_503(tmp_path, fake):
    sup = _sup(tmp_path, backoff_s=(3.0,))
    try:
        client = mh.ModelHostClient(sup, kind="local", ctor=_ctor(fake))
        client.generate(prompt="warm")
        os.kill(sup.pid, signal.SIGKILL)
        assert _until(lambda: not sup.generation_alive())
        with pytest.raises(mh.ModelHostUnavailable) as info:
            client.generate(prompt="during")
        assert info.value.status_code == 503 and "restarting" in str(info.value)
    finally:
        sup.stop()


def test_it_stops_restarting_after_the_limit(tmp_path):
    sup = _sup(tmp_path, max_restarts=1)
    try:
        os.kill(sup.pid, signal.SIGKILL)
        assert _until(lambda: sup.generation_alive() and sup.generation == 2)
        os.kill(sup.pid, signal.SIGKILL)
        assert _until(lambda: sup.state == "failed")
        kinds = [i["kind"] for i in mh.read_incidents(tmp_path / "data")]
        assert "gave_up" in kinds and kinds.count("exit") == 2, kinds
        with pytest.raises(mh.ModelHostUnavailable) as info:
            sup._acquire()
        assert "stopped restarting" in str(info.value)
    finally:
        sup.stop()


def test_the_memory_ceiling_kills_and_records_it(tmp_path):
    sup = _sup(tmp_path, memory_ceiling_bytes=1, memory_sample_s=0.1, max_restarts=0)
    try:
        assert _until(lambda: sup.state == "failed", timeout_s=20)
        kinds = [i["kind"] for i in mh.read_incidents(tmp_path / "data")]
        assert "memory_ceiling" in kinds, kinds
    finally:
        sup.stop()


def test_an_unresponsive_child_is_killed_and_recorded(tmp_path):
    sup = _sup(tmp_path, ping_interval_s=0.2, ping_timeout_s=0.3, ping_misses_to_kill=2, memory_sample_s=0.1)
    try:
        pid = sup.pid
        os.kill(pid, signal.SIGSTOP)
        assert _until(lambda: sup.generation >= 2 and sup.generation_alive(), timeout_s=20)
        kinds = [i["kind"] for i in mh.read_incidents(tmp_path / "data")]
        assert kinds and kinds[0] == "unresponsive", kinds
    finally:
        sup.stop()


def test_the_child_leaves_with_the_gateway(tmp_path):
    sup = _sup(tmp_path)
    pid = sup.pid
    sup._proc.stdin.close()  # what the gateway's death does to the pipe
    assert _until(lambda: sup._proc.poll() is not None, timeout_s=10)
    sup.stop()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_incident_files_are_private_and_bounded(tmp_path):
    data = tmp_path / "data"
    for i in range(mh.INCIDENT_KEEP + 5):
        mh.write_incident(data, {"schema": mh.INCIDENT_SCHEMA, "kind": "exit", "ended_at": 1_700_000_000 + i})
    files = list((data / "incidents").glob("model-host-*.json"))
    assert len(files) == mh.INCIDENT_KEEP
    assert oct(os.stat(files[0]).st_mode & 0o777) == "0o600"
    assert json.loads(files[0].read_text())["schema"] == mh.INCIDENT_SCHEMA


def test_mode_defaults():
    assert mh.default_mode(platform="linux") == mh.MODE_PROCESS
    assert mh.default_mode(platform="darwin") == mh.MODE_IN_PROCESS
    assert mh.default_mode(platform="win32") == mh.MODE_IN_PROCESS
    assert mh.active() is False  # nothing in the test process started one


class _OnlyHere:
    """Importable in the test process only: the child cannot unpickle it."""


def test_an_unreadable_request_fails_alone_and_the_connection_stays_usable(tmp_path, fake):
    sup = _sup(tmp_path)
    try:
        client = mh.ModelHostClient(sup, kind="local", ctor=_ctor(fake))
        client.generate(prompt="warm")
        pid = sup.pid
        with pytest.raises(mh.ModelHostRemoteError) as info:
            client.generate(prompt="x", params={"odd": _OnlyHere()})
        assert "could not read this request" in str(info.value)
        assert "after" in client.generate(prompt="after")["content"]
        assert sup.pid == pid and not mh.read_incidents(tmp_path / "data")
    finally:
        sup.stop()


def test_a_refused_setter_is_not_replayed_after_a_restart(tmp_path, fake):
    sup = _sup(tmp_path)
    try:
        client = mh.ModelHostClient(sup, kind="multi", ctor=_ctor(fake))
        client.generate(prompt="warm")
        try:
            client.set_default_provider_model(provider="no-such-provider", model="x")
        except Exception:  # noqa: BLE001 - refused or accepted lazily: either way it must not poison
            pass
        client.set_default_provider_model(provider="openai-compatible", model="fake-model",
                                          llm_kwargs={"base_url": fake.base_url, "api_key": "fake-key"})
        pid = sup.pid
        os.kill(pid, signal.SIGKILL)
        assert _until(lambda: sup.generation_alive() and sup.pid not in (None, pid))
        assert "after" in client.generate(prompt="after")["content"]
    finally:
        sup.stop()


def test_configuration_reads_and_fresh_setters_never_wait_for_the_child(tmp_path, fake):
    sup = _sup(tmp_path)
    try:
        client = mh.ModelHostClient(sup, kind="multi", ctor=_ctor(fake))
        client.generate(prompt="warm")
        os.kill(sup.pid, signal.SIGSTOP)  # a frozen child
        try:
            t0 = time.monotonic()
            assert client._llm_kwargs["base_url"] == fake.base_url
            assert client._default_provider == "openai-compatible"
            assert callable(client.voice_library) and callable(client.list_model_residency)
            fresh = mh.ModelHostClient(sup, kind="local", ctor=_ctor(fake))
            fresh.set_provider_endpoint_profile_resolver(lambda name: None)
            assert time.monotonic() - t0 < 0.5
        finally:
            os.kill(sup.pid, signal.SIGCONT)
    finally:
        sup.stop()


def test_stop_gives_the_process_back_its_in_process_defaults(tmp_path):
    from abstractruntime.integrations.abstractcore.llm_client import default_model_warmup

    before = default_model_warmup()
    mh.set_mode("process", source="test")
    mh.start(tmp_path / "data", nice=0)
    try:
        assert mh.active() and mh.mode() == mh.MODE_PROCESS and default_model_warmup() is False
    finally:
        mh.stop()
    assert not mh.active() and mh.mode() == mh.MODE_IN_PROCESS and default_model_warmup() is before


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="reads /proc/<pid>/fd")
def test_the_child_stdout_goes_to_its_log_after_ready(tmp_path):
    sup = _sup(tmp_path)
    try:
        pid = sup.pid
        assert os.readlink(f"/proc/{pid}/fd/1") == os.readlink(f"/proc/{pid}/fd/2")
    finally:
        sup.stop()


def _child_attr(sup, client, name):
    return mh._Exchange(sup, client, ("getattr", ("c", client._mh_id), name)).run()


def test_an_attribute_the_host_assigns_reaches_the_child_and_survives_a_restart(tmp_path, fake):
    """bundle_host sets `client.residency_owner_label = "the runtime of account default/bob"` (the
    Models page's held-by names): on the proxy it must land on the child's client, again after a
    restart, and before the first build too."""
    sup = _sup(tmp_path)
    try:
        client = mh.ModelHostClient(sup, kind="multi", ctor=_ctor(fake))
        client.residency_owner_label = "the runtime of account default/bob"  # maybe before the build
        client.generate(prompt="warm")
        assert _child_attr(sup, client, "residency_owner_label") == "the runtime of account default/bob"
        assert client.residency_owner_label == "the runtime of account default/bob"
        client.residency_owner_label = "the runtime of account default/alice"
        assert _child_attr(sup, client, "residency_owner_label") == "the runtime of account default/alice"
        pid = sup.pid
        os.kill(pid, signal.SIGKILL)
        assert _until(lambda: sup.generation_alive() and sup.pid not in (None, pid))
        client.generate(prompt="after")
        assert _child_attr(sup, client, "residency_owner_label") == "the runtime of account default/alice"
        # Assigned while the child is down, with the proxy still believing it built: no exception,
        # and the rebuild delivers it.
        pid = sup.pid
        os.kill(pid, signal.SIGKILL)
        assert _until(lambda: not sup.generation_alive())
        client._mh_built = lambda: True  # the instant before the proxy notices the restart
        client.residency_owner_label = "the runtime of account default/carol"
        del client._mh_built
        assert _until(lambda: sup.generation_alive() and sup.pid not in (None, pid))
        client.generate(prompt="again")
        assert _child_attr(sup, client, "residency_owner_label") == "the runtime of account default/carol"
    finally:
        sup.stop()


def test_after_a_restart_only_the_pool_clients_are_rebuilt_at_once(tmp_path, fake):
    """Pass three (2026-10-29): a gateway-side client rebuilt eagerly loaded the model as an
    instance of its own before the runtime's pool could; the pool's copy was then a second one.
    Pool clients (`multi`) rebuild at once (their default warms); `local` ones at their next call."""
    sup = _sup(tmp_path)
    try:
        pool = mh.ModelHostClient(sup, kind="multi", ctor=_ctor(fake))
        side = mh.ModelHostClient(sup, kind="local", ctor=_ctor(fake))
        pool.generate(prompt="warm")
        side.generate(prompt="warm")
        pid = sup.pid
        os.kill(pid, signal.SIGKILL)
        assert _until(lambda: sup.generation_alive() and sup.pid not in (None, pid))
        assert _until(lambda: pool._mh_built_generation == sup.generation), "the pool client was not rebuilt"
        time.sleep(1.0)
        assert side._mh_built_generation != sup.generation, "a gateway-side client was rebuilt before its call"
        assert "again" in side.generate(prompt="again")["content"]
        assert side._mh_built_generation == sup.generation
    finally:
        sup.stop()


def test_a_pool_client_rebuilds_with_its_current_default_and_does_not_replay_the_switch(tmp_path, fake):
    sup = _sup(tmp_path)
    try:
        pool = mh.ModelHostClient(sup, kind="multi", ctor=dict(provider="lmstudio", model="boot-default",
                                                               llm_kwargs={"base_url": "http://127.0.0.1:9/v1"}))
        assert _until(lambda: pool._mh_built_generation == sup.generation)
        pool.set_default_provider_model(provider="openai-compatible", model="fake-model",
                                        llm_kwargs={"base_url": fake.base_url, "api_key": "fake-key"})
        assert pool._mh_ctor["provider"] == "openai-compatible" and pool._mh_ctor["model"] == "fake-model"
        assert "set_default_provider_model" not in pool._mh_setters
    finally:
        sup.stop()
