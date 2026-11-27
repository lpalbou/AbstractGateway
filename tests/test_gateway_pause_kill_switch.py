"""The gateway pause is a kill switch (operator ruling 2026-11-19: "when the gateway is paused,
NOTHING moves forward. no automation, no entity, no computation, NOTHING ... that can be resumed
later on").

Incident (Windows, 2026-11-19): the gateway was paused from the tray; five minutes later the entity
sweep still ran an idle sleep, which loaded the embedding model again and sent generations to
LM Studio, which loaded a 27B model for them. Only the workflow runner read the pause.

Pins:
- `host_control`: a refused request raises `GatewayPaused`; a thread inside `parks_while_paused`
  waits at the model gate and continues at Resume; a background loop waits in `wait_while_paused`;
  the pause file's path is exported for child processes.
- the model host proxy sends NO model work while paused (generation, embedding, load), still answers
  reads and unloads, builds clients without warming a model, and applies at Resume the loading
  setters it recorded meanwhile.
- the entity sweeps (repair, need-check, idle sleep, authoring) start nothing while paused, and a
  sleep with its pass is refused before anything is written.
- the background workers do not tick while paused and tick again after Resume.
- HTTP: a door that starts an entity's cognition answers 409 `gateway_paused`.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from abstractgateway import host_control
from abstractgateway import model_host as mh

pytestmark = pytest.mark.basic


@pytest.fixture(autouse=True)
def _reset_host_control():
    host_control._reset_for_tests()
    yield
    host_control._reset_for_tests()


def _until(pred, timeout_s: float = 5.0) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


# ------------------------------------------------------------- host_control


def test_a_request_is_refused_while_paused_and_passes_after_resume(tmp_path: Path) -> None:
    host_control.configure(tmp_path)
    host_control.refuse_if_paused("a chat turn")  # not paused: nothing
    host_control.gate_model_work("generate")

    host_control.pause(by="default/admin")
    with pytest.raises(host_control.GatewayPaused) as refused:
        host_control.refuse_if_paused("a chat turn")
    assert "a chat turn" in str(refused.value) and "Resume" in str(refused.value)
    assert host_control.GatewayPaused.code == "gateway_paused"
    with pytest.raises(host_control.GatewayPaused):
        host_control.gate_model_work("generate")

    host_control.resume(by="default/admin")
    host_control.refuse_if_paused("a chat turn")
    host_control.gate_model_work("generate")


def test_a_parking_thread_waits_at_the_model_gate_and_continues_at_resume(tmp_path: Path) -> None:
    host_control.configure(tmp_path)
    host_control.pause(by="default/admin")
    passed = threading.Event()
    errors: List[BaseException] = []

    def work() -> None:
        try:
            with host_control.parks_while_paused():
                host_control.gate_model_work("a step in flight")
            passed.set()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    time.sleep(0.3)
    assert not passed.is_set() and not errors  # held, not refused

    host_control.resume(by="default/admin")
    assert passed.wait(5.0) and not errors
    thread.join(5.0)
    # Parking is the block's, not the thread's: outside it a paused gate refuses again.
    host_control.pause(by="default/admin")
    with pytest.raises(host_control.GatewayPaused):
        host_control.gate_model_work("after the block")


def test_wait_while_paused_ends_on_resume_or_on_the_callers_stop(tmp_path: Path) -> None:
    host_control.configure(tmp_path)
    assert host_control.wait_while_paused() is False  # not paused: no wait

    host_control.pause(by="default/admin")
    stop = threading.Event()
    stop.set()
    t0 = time.monotonic()
    host_control.wait_while_paused(stop)  # a stopping loop is never held by a pause
    assert time.monotonic() - t0 < 1.0 and host_control.is_paused()

    threading.Timer(0.2, lambda: host_control.resume(by="default/admin")).start()
    assert host_control.wait_while_paused(poll_s=0.05) is True
    assert host_control.is_paused() is False


def test_the_pause_file_is_named_to_child_processes(tmp_path: Path) -> None:
    host_control.configure(tmp_path)
    assert os.environ[host_control.PAUSE_FILE_ENV] == str(host_control.pause_file_path(tmp_path))
    host_control._reset_for_tests()
    assert host_control.PAUSE_FILE_ENV not in os.environ


# ------------------------------------------------------------- the model host proxy


class _FakeHost:
    """What `ModelHostClient` needs of a supervisor; `sent` is every message a proxy sent."""

    generation = 1

    def __init__(self) -> None:
        self.sent: List[Tuple[Any, ...]] = []
        self._clients: List[Any] = []

    def generation_alive(self) -> bool:
        return True

    def await_requested_restart(self) -> None:
        return None

    def _register(self, client: Any) -> None:
        self._clients.append(client)

    def _drop_later(self, client_id: str) -> None:
        return None


@pytest.fixture()
def host(monkeypatch: pytest.MonkeyPatch) -> _FakeHost:
    fake = _FakeHost()

    class _Exchange:
        def __init__(self, host_: Any, owner: Any, msg: Tuple[Any, ...], **_kw: Any) -> None:
            self.msg = msg

        def run(self, **_kw: Any) -> Any:
            fake.sent.append(self.msg)
            if self.msg[0] == "new":
                return {"attrs": {}}
            if self.msg[0] == "getattrs":
                return {}
            return {"ok": True, "sent": self.msg[2] if self.msg[0] == "call" else self.msg[0]}

    monkeypatch.setattr(mh, "_Exchange", _Exchange)
    return fake


def _calls(host: _FakeHost) -> List[str]:
    return [str(m[2]) for m in host.sent if m[0] == "call"]


def _ctor() -> Dict[str, Any]:
    return dict(provider="openai-compatible", model="fake-model", llm_kwargs={})


def test_model_work_names_are_everything_but_reads_and_releases() -> None:
    for name in ("generate", "embed_texts", "stream_tts", "load_model_residency", "lock_model_residency",
                 "prompt_cache_set", "prompt_cache_prepare_modules", "upsert_text_bloc",
                 "get_provider_instance", "a_method_added_later"):
        assert mh.is_model_work(name), name
    for name in ("list_providers", "list_model_residency", "get_model_capabilities", "lookup_model_capabilities",
                 "unload_model_residency", "unlock_model_residency", "unload", "residency", "residency_claims",
                 "set_model_idle_unload", "prompt_cache_clear", "clear_session_prompt_caches", "voice_library"):
        assert not mh.is_model_work(name), name


def test_a_paused_gateway_sends_no_model_work_to_the_model_host(tmp_path: Path, host: _FakeHost) -> None:
    host_control.configure(tmp_path)
    llm = mh.ModelHostClient(host, kind="local", ctor=_ctor())
    embedder = mh.ModelHostClient(host, kind="embeddings", ctor=dict(provider="huggingface", model="qwen3-embedding"))
    assert llm.generate(prompt="before")["sent"] == "generate"
    assert embedder.embed_texts(["built and loaded before the pause"])["sent"] == "embed_texts"

    host_control.pause(by="default/admin")
    host.sent.clear()
    with pytest.raises(host_control.GatewayPaused):
        llm.generate(prompt="while paused")
    with pytest.raises(host_control.GatewayPaused):
        llm.load_model_residency({"provider": "lmstudio", "model": "qwen/qwen3.8-27b"})
    with pytest.raises(host_control.GatewayPaused):
        embedder.embed_texts(["a memory"])
    assert host.sent == []  # nothing reached the child: no build, no call

    # Reading what is loaded and letting go of it stay possible (the person paused to free the GPU).
    assert llm.list_model_residency()["sent"] == "list_model_residency"
    assert llm.unload_model_residency({"provider": "lmstudio", "model": "qwen/qwen3.8-27b"})["sent"] == "unload_model_residency"
    assert embedder.unload()["sent"] == "unload"

    host_control.resume(by="default/admin")
    assert llm.generate(prompt="after")["sent"] == "generate"
    assert embedder.embed_texts(["a memory"])["sent"] == "embed_texts"


def test_a_step_in_flight_waits_at_the_proxy_and_generates_after_resume(tmp_path: Path, host: _FakeHost) -> None:
    host_control.configure(tmp_path)
    llm = mh.ModelHostClient(host, kind="local", ctor=_ctor())
    llm.list_providers()  # built
    host_control.pause(by="default/admin")
    host.sent.clear()
    out: List[Any] = []

    def tick() -> None:
        with host_control.parks_while_paused():  # what the runner's tick thread and a sweep's pass do
            out.append(llm.generate(prompt="second call of the step"))

    thread = threading.Thread(target=tick, daemon=True)
    thread.start()
    time.sleep(0.3)
    assert out == [] and _calls(host) == []

    host_control.resume(by="default/admin")
    assert _until(lambda: bool(out))
    assert _calls(host) == ["generate"]
    thread.join(5.0)


def test_a_client_built_while_paused_loads_no_model(tmp_path: Path, host: _FakeHost) -> None:
    host_control.configure(tmp_path)
    host_control.pause(by="default/admin")
    pool = mh.ModelHostClient(host, kind="multi", ctor=_ctor())
    pool.list_providers()
    new = [m for m in host.sent if m[0] == "new"]
    assert len(new) == 1 and new[0][4] == {"warm": False}  # the child loads no default model for it

    host_control.resume(by="default/admin")
    other = mh.ModelHostClient(host, kind="multi", ctor=_ctor())
    other.list_providers()
    assert len([m for m in host.sent if m[0] == "new"][-1]) == 4  # not paused: the usual build message


def test_an_embeddings_client_is_not_built_while_paused(tmp_path: Path, host: _FakeHost) -> None:
    """Building it in the child loads the embedding model (incident 2026-11-19: reloaded right after
    the person unloaded it, on a paused gateway). Paused, it is not built: reading its state and
    unloading it answer here, and its first use after Resume builds it."""
    host_control.configure(tmp_path)
    host_control.pause(by="default/admin")
    embedder = mh.ModelHostClient(host, kind="embeddings", ctor=dict(provider="huggingface", model="qwen3-embedding"))
    assert embedder._mh_held_by_pause()
    with pytest.raises(host_control.GatewayPaused):
        embedder._mh_ensure()
    assert embedder.provider == "huggingface" and embedder.model == "qwen3-embedding"
    state = embedder.residency()
    assert state["loaded"] is False and state["state"] == "not_loaded" and state["model"] == "qwen3-embedding"
    assert embedder.unload()["unloaded"] is False
    assert host.sent == []  # nothing reached the child: no build

    host_control.resume(by="default/admin")
    assert not embedder._mh_held_by_pause()
    assert embedder.embed_texts(["a memory"])["sent"] == "embed_texts"
    assert [m[0] for m in host.sent][0] == "new"


def test_the_warm_thread_holds_an_embeddings_build_until_resume(tmp_path: Path) -> None:
    built: List[str] = []

    class _Client:
        _mh_kind = "embeddings"
        _mh_deferred: set = set()

        def _mh_held_by_pause(self) -> bool:
            return host_control.is_paused()

        def _mh_ensure(self) -> None:
            built.append("built")

    host_control.configure(tmp_path)
    host_control.pause(by="default/admin")
    sup = mh.ModelHostSupervisor(tmp_path / "data")
    client = _Client()
    thread = threading.Thread(target=sup._warm_loop, daemon=True)
    thread.start()
    try:
        sup._warm_queue.append(client)
        sup._wake.set()
        assert _until(lambda: len(sup._held_builds) == 1)
        assert built == []
        host_control.resume(by="default/admin")
        sup.apply_deferred_setters()
        assert _until(lambda: built == ["built"])
    finally:
        sup._stopping = True
        sup._wake.set()
        thread.join(5.0)


def test_the_child_builds_an_unwarmed_pool_client_and_restores_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractruntime.integrations.abstractcore import llm_client as lc

    from abstractgateway import model_host_child as child

    seen: List[bool] = []

    class _Pool:
        def __init__(self, **_ctor: Any) -> None:
            seen.append(lc.default_model_warmup())

    monkeypatch.setattr(lc, "MultiLocalAbstractCoreLLMClient", _Pool)
    before = lc.default_model_warmup()
    lc.set_default_model_warmup(True)
    try:
        child._build_client("multi", {}, warm=False)
        assert seen == [False] and lc.default_model_warmup() is True  # off for that build only
        child._build_client("multi", {})
        assert seen == [False, True]
    finally:
        lc.set_default_model_warmup(before)


def test_setters_that_load_a_model_are_recorded_while_paused_and_applied_at_resume(tmp_path: Path, host: _FakeHost) -> None:
    host_control.configure(tmp_path)
    pool = mh.ModelHostClient(host, kind="multi", ctor=_ctor())
    pool.list_providers()  # built
    host_control.pause(by="default/admin")
    host.sent.clear()

    locks = [{"provider": "lmstudio", "model": "qwen/qwen3.8-27b"}]
    assert pool.set_model_residency_locks(locks) is None  # recorded, not sent: Keep loaded loads the model
    assert pool.set_model_idle_unload(600)["sent"] == "set_model_idle_unload"  # loads nothing: applied now
    assert _calls(host) == ["set_model_idle_unload"]
    assert pool._mh_deferred == {"set_model_residency_locks"}

    pool._mh_apply_deferred()  # still paused: nothing is applied
    assert _calls(host) == ["set_model_idle_unload"]

    host_control.resume(by="default/admin")
    pool._mh_apply_deferred()
    sent = [m for m in host.sent if m[0] == "call" and m[2] == "set_model_residency_locks"]
    assert len(sent) == 1 and sent[0][3] == (locks,)
    assert pool._mh_deferred == set()


def test_a_rebuild_while_paused_does_not_replay_the_loading_setters(tmp_path: Path, host: _FakeHost) -> None:
    host_control.configure(tmp_path)
    pool = mh.ModelHostClient(host, kind="multi", ctor=_ctor())
    locks = [{"provider": "lmstudio", "model": "qwen/qwen3.8-27b"}]
    pool.set_model_residency_locks(locks)  # not built yet: recorded for the build
    pool.set_model_idle_unload(600)

    host_control.pause(by="default/admin")
    pool.list_providers()  # the build, while paused
    assert _calls(host) == ["set_model_idle_unload", "list_providers"]
    assert pool._mh_deferred == {"set_model_residency_locks"}

    host_control.resume(by="default/admin")
    pool._mh_apply_deferred()
    assert _calls(host).count("set_model_residency_locks") == 1


# ------------------------------------------------------------- the entity sweeps


class _NoRegistry:
    """A registry no paused sweep may touch."""

    entities_dir = "unused"

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"a paused sweep read the registry ({name})")


def test_the_entity_sweeps_start_nothing_while_paused(tmp_path: Path) -> None:
    from abstractgateway.entity_authoring import drain_after_close
    from abstractgateway.entity_idle_sleep import idle_sweep, idle_sweep_one
    from abstractgateway.entity_repair import sweep_entity_repairs

    host_control.configure(tmp_path)
    host_control.pause(by="default/admin")
    registry = _NoRegistry()
    assert sweep_entity_repairs(registry) == []
    assert idle_sweep(registry, ["castor", "pollux"]) == []
    assert idle_sweep_one(registry, "castor") is None
    assert drain_after_close(registry, "castor") == {"skipped": "gateway_paused"}


def test_a_sleep_with_its_pass_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    from abstractgateway.entities import EntityRegistry

    host_control.configure(tmp_path)
    host_control.pause(by="default/admin")
    with pytest.raises(host_control.GatewayPaused):
        EntityRegistry.set_state(_NoRegistry(), name="castor", state="asleep", dream=True)


# ------------------------------------------------------------- the background workers


def test_the_mail_worker_does_not_tick_while_paused(tmp_path: Path) -> None:
    from abstractgateway.mail.worker import EmailWorker

    host_control.configure(tmp_path)
    host_control.pause(by="default/admin")
    worker = EmailWorker.__new__(EmailWorker)
    worker.tick_s = 0.02
    worker._stop = threading.Event()
    worker._wake = threading.Event()
    ticks: List[float] = []
    worker.tick = lambda: ticks.append(time.monotonic())  # type: ignore[method-assign]
    thread = threading.Thread(target=worker._loop, daemon=True)
    thread.start()
    try:
        time.sleep(0.4)
        assert ticks == []
        host_control.resume(by="default/admin")
        assert _until(lambda: len(ticks) >= 2)
    finally:
        worker._stop.set()
        worker._wake.set()
        thread.join(5.0)
    assert not thread.is_alive()


def test_a_paused_worker_still_stops(tmp_path: Path) -> None:
    from abstractgateway.mail.worker import EmailWorker

    host_control.configure(tmp_path)
    host_control.pause(by="default/admin")
    worker = EmailWorker.__new__(EmailWorker)
    worker.tick_s = 0.02
    worker._stop = threading.Event()
    worker._wake = threading.Event()
    worker.tick = lambda: None  # type: ignore[method-assign]
    thread = threading.Thread(target=worker._loop, daemon=True)
    thread.start()
    time.sleep(0.1)
    worker._stop.set()
    worker._wake.set()
    thread.join(5.0)
    assert not thread.is_alive()


# ------------------------------------------------------------- HTTP


def test_a_door_that_starts_cognition_answers_409_gateway_paused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from abstractgateway.app import _gateway_paused_409
    from abstractgateway.routes.entities import refuse_while_gateway_paused, router

    # The dependency is on every door that starts an entity's cognition.
    gated = {route.path for route in router.routes
             if any(getattr(dep, "dependency", None) is refuse_while_gateway_paused for dep in getattr(route, "dependencies", []))}
    for path in ("/{name}/visit/open", "/{name}/visit/{run_id}/turn", "/{name}/visit/{run_id}/tick", "/meets/open",
                 "/meets/{meet_id}/relay", "/{name}/reembed", "/{name}/chat/open", "/{name}/chat/{chat_id}/turn",
                 "/{name}/summon", "/{name}/voice/tts", "/{name}/voice/tts/stream"):
        assert "/gateway/entities" + path in gated, path
    # Letting go stays open while paused.
    for path in ("/{name}/visit/{run_id}/close", "/{name}/chat/{chat_id}/close", "/{name}/loop/stop", "/{name}/state"):
        assert "/gateway/entities" + path not in gated, path

    from fastapi import Depends

    app = FastAPI()
    app.add_exception_handler(host_control.GatewayPaused, _gateway_paused_409)

    @app.post("/door", dependencies=[Depends(refuse_while_gateway_paused)])
    def door() -> Dict[str, Any]:
        return {"started": True}

    host_control.configure(tmp_path)
    with TestClient(app) as client:
        assert client.post("/door").json() == {"started": True}
        host_control.pause(by="default/admin")
        refused = client.post("/door")
        assert refused.status_code == 409
        assert refused.json()["code"] == "gateway_paused" and "POST /door" in refused.json()["detail"]
        assert refused.headers["X-Gateway-Error-Code"] == "gateway_paused"
        host_control.resume(by="default/admin")
        assert client.post("/door").json() == {"started": True}
