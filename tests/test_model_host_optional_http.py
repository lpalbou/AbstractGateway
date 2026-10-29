"""Real child/native RPC probes: optional HTTP never defines owner readiness."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import pytest

from abstractgateway import model_host as mh
from model_host_fakes import FakeOpenAIServer


def _settings(data: Path, *, legacy=False, direct=False):
    path = data / "config" / "core_endpoint.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"enabled": legacy, "direct_enabled": direct,
                               "access": "token", "reach": "machine", "token": "legacy-secret" if legacy else ""}))
    tmp.replace(path)


def _http(sup):
    return mh._Exchange(sup, None, ("ping",)).run(timeout_s=2)["http"]


def _until(pred, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(.05)
    pytest.fail("Optional HTTP state did not converge")


def _client(sup, fake):
    return mh.ModelHostClient(sup, kind="local", ctor={"provider": "openai-compatible", "model": "fake-model",
        "llm_kwargs": {"base_url": fake.base_url, "api_key": "fake", "enable_tracing": False}})


def test_disabled_http_does_not_import_listener_or_prepare_token(tmp_path):
    bootstrap = '''
import importlib.abc, sys
class NoHTTP(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'abstractgateway.split_mode' or fullname == 'uvicorn' or fullname.startswith('abstractcore.server'):
            raise RuntimeError('HTTP imported while disabled: ' + fullname)
sys.meta_path.insert(0, NoHTTP())
from abstractgateway.model_host_child import main
raise SystemExit(main())
'''
    data = tmp_path / "data"
    with FakeOpenAIServer() as fake:
        sup = mh.ModelHostSupervisor(data, nice=0, child_argv=[sys.executable, "-c", bootstrap]).start()
        try:
            assert "native" in _client(sup, fake).generate(prompt="native")["content"]
            assert sup.snapshot()["state"] == "up"
            assert _http(sup)["legacy"]["state"] == _http(sup)["direct"]["state"] == "disabled"
            assert not (data / "split" / "model_host.json").exists()
            assert not (data / "split" / "model_host_http.json").exists()
            assert not list(tmp_path.rglob("server-token"))
        finally:
            sup.stop()


def test_http_start_failure_does_not_break_native_rpc(tmp_path):
    bootstrap = '''
from abstractgateway.split_mode import ModelHost
def fail(*args, **kwargs): raise RuntimeError('forced HTTP startup failure')
ModelHost.start = fail
from abstractgateway.model_host_child import main
raise SystemExit(main())
'''
    data = tmp_path / "data"
    _settings(data, legacy=True)
    with FakeOpenAIServer() as fake:
        sup = mh.ModelHostSupervisor(data, nice=0, child_argv=[sys.executable, "-c", bootstrap]).start()
        try:
            client = _client(sup, fake)
            assert "native" in client.generate(prompt="native")["content"]
            _until(lambda: _http(sup)["legacy"]["state"] == "failed")
            assert "forced HTTP startup failure" in _http(sup)["legacy"]["error"]
            assert sup.snapshot()["state"] == "up" and not mh.read_incidents(data)
            assert "after" in client.generate(prompt="after")["content"]
            assert not (data / "split" / "model_host.json").exists()
        finally:
            sup.stop()


def test_dynamic_legacy_listener_keeps_native_provider_identity_and_private_auth(tmp_path):
    import httpx

    data = tmp_path / "data"
    with FakeOpenAIServer() as fake:
        sup = mh.ModelHostSupervisor(data, nice=0).start()
        try:
            client = _client(sup, fake)
            client.generate(prompt="warm")
            pid, provider = sup.pid, client._llm._mh_hid
            _settings(data, legacy=True)
            _until(lambda: _http(sup)["legacy"]["state"] == "up")
            url = _http(sup)["legacy"]["url"]
            # Knowing a Gateway credential alone does not bypass its admission proxy.
            assert httpx.get(url + "/v1/models", headers={"Authorization": "Bearer legacy-secret"}, trust_env=False).status_code == 403
            _settings(data)
            _until(lambda: _http(sup)["legacy"]["state"] == "disabled")
            assert not (data / "split" / "model_host.json").exists()
            assert "after" in client.generate(prompt="after")["content"]
            assert sup.pid == pid and client._llm._mh_hid == provider
        finally:
            sup.stop()


def test_dynamic_direct_core_listener_uses_core_auth_and_preserves_native_owner(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("ABSTRACTCORE_AUTH_TOKEN", "core-owned-test-token")
    monkeypatch.delenv("ABSTRACTCORE_SERVER_ALLOW_UNAUTHENTICATED", raising=False)
    # Explicit ephemeral test bind; normal managed serving uses Core's own config.
    bootstrap = '''
from abstractruntime.integrations.abstractcore import server_facade
original = server_facade.start_core_server
def start_core_server(): return original(host='127.0.0.1', port=0)
server_facade.start_core_server = start_core_server
from abstractgateway.model_host_child import main
raise SystemExit(main())
'''
    data = tmp_path / "data"
    with FakeOpenAIServer() as fake:
        sup = mh.ModelHostSupervisor(data, nice=0, child_argv=[sys.executable, "-c", bootstrap]).start()
        try:
            client = _client(sup, fake)
            client.generate(prompt="warm")
            pid, provider = sup.pid, client._llm._mh_hid
            _settings(data, legacy=True, direct=True)
            _until(lambda: _http(sup)["direct"]["state"] == "up")
            url = _http(sup)["direct"]["url"]
            path = url + "/__core_auth_probe__"
            assert httpx.get(path, trust_env=False).status_code == 401
            assert httpx.get(path, headers={"Authorization": "Bearer legacy-secret"}, trust_env=False).status_code == 401
            assert httpx.get(path, headers={"Authorization": "Bearer core-owned-test-token"}, trust_env=False).status_code == 404
            from abstractgateway.split_mode import read_address

            admitted = {"x-abstractgateway-split-secret": read_address(data)["secret"], "Authorization": "Bearer legacy-secret"}
            legacy_path = _http(sup)["legacy"]["url"] + "/__core_auth_probe__"
            assert httpx.get(legacy_path, headers=admitted, trust_env=False).status_code == 404
            admitted["Authorization"] = "Bearer core-owned-test-token"
            assert httpx.get(legacy_path, headers=admitted, trust_env=False).status_code == 401
            # Direct Core HTTP creates no Runtime run or ledger.
            assert not (data / "runs").exists()
            _settings(data)
            _until(lambda: _http(sup)["direct"]["state"] == "disabled")
            assert "after" in client.generate(prompt="after")["content"]
            assert sup.pid == pid and client._llm._mh_hid == provider
            assert not mh.read_incidents(data)
        finally:
            sup.stop()


def test_http_lifecycle_recovers_start_failure_and_listener_death_with_bounded_retries(tmp_path, monkeypatch):
    from abstractgateway import model_host_http as lifecycle, split_mode

    clock = [0.0]
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(lifecycle, "BACKOFF_S", (1.0,))
    monkeypatch.setattr(lifecycle, "MAX_RESTARTS", 3)
    data = tmp_path / "data"
    _settings(data, legacy=True)
    created = []

    class Listener:
        is_running = True
        url = "http://127.0.0.1:12345"
        def __init__(self, data):
            created.append(self)
        def start(self):
            if len(created) == 1:
                raise RuntimeError("transient bootstrap fault")
            return self
        def stop(self):
            self.is_running = False

    monkeypatch.setattr(split_mode, "ModelHost", Listener)
    owner = lifecycle.OptionalHTTP(data)
    owner.reconcile()
    assert owner.snapshot()["legacy"]["state"] == "failed"
    owner.reconcile()
    assert len(created) == 1, "backoff prevents a restart loop"
    clock[0] += 1
    owner.reconcile()
    assert owner.snapshot()["legacy"]["state"] == "up"
    created[-1].is_running = False
    owner.reconcile()
    assert owner.snapshot()["legacy"]["state"] == "failed"
    clock[0] += 1
    owner.reconcile()
    assert len(created) == 3 and owner.snapshot()["legacy"]["state"] == "up"
    created[-1].is_running = False
    owner.reconcile()
    clock[0] += 1
    owner.reconcile()
    assert len(created) == 3 and "restart limit" in owner.snapshot()["legacy"]["error"]
    clock[0] += lifecycle.RESTART_WINDOW_S + 1
    owner.reconcile()
    assert len(created) == 3, "giving up persists until an operator toggles the setting"
    _settings(data)
    owner.reconcile()
    _settings(data, legacy=True)
    owner.reconcile()
    assert len(created) == 4 and owner.snapshot()["legacy"]["state"] == "up"


def test_disable_retries_failed_stop_and_does_not_claim_disabled(tmp_path, monkeypatch):
    from abstractgateway import model_host_http as lifecycle, split_mode

    clock = [0.0]
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(lifecycle, "BACKOFF_S", (1.0,))
    data = tmp_path / "data"
    _settings(data, legacy=True)

    class Listener:
        is_running = True
        url = "http://127.0.0.1:12345"
        calls = 0
        def __init__(self, data): pass
        def start(self): return self
        def stop(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("not stopped yet")
            self.is_running = False

    monkeypatch.setattr(split_mode, "ModelHost", Listener)
    owner = lifecycle.OptionalHTTP(data)
    owner.reconcile()
    handle = owner._handles["legacy"]
    _settings(data)
    owner.reconcile()
    assert owner.snapshot()["legacy"]["state"] == "stopping" and handle.is_running
    clock[0] += 1
    owner.reconcile()
    assert owner.snapshot()["legacy"]["state"] == "disabled" and not handle.is_running
    assert "legacy" not in owner._handles


def test_direct_switch_is_additive_and_does_not_prepare_legacy_credentials(tmp_path):
    from abstractgateway.core_endpoint import change_settings
    from abstractgateway.endpoint_settings import read_settings

    data = tmp_path / "data"
    settings = change_settings(data, direct_enabled=True)
    assert settings.direct_enabled and not settings.enabled and not settings.token
    assert read_settings(data) == settings
    settings = change_settings(data, direct_enabled=False)
    assert not settings.direct_enabled and not settings.enabled and not settings.token


def test_http_shutdown_attempts_both_handles_even_when_one_stop_fails(tmp_path):
    from abstractgateway.model_host_http import OptionalHTTP

    stopped = []
    class Listener:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail
        def stop(self):
            stopped.append(self.name)
            if self.fail:
                raise RuntimeError("still stopping")

    owner = OptionalHTTP(tmp_path)
    owner._handles = {"legacy": Listener("legacy", fail=True), "direct": Listener("direct")}
    owner._stop.set()
    owner._run()
    assert stopped == ["legacy", "direct"]
    assert set(owner._handles) == {"legacy"}


def test_http_status_is_nonsecret_atomic_private_and_ignores_stale_owner_or_off(tmp_path):
    import os
    import stat
    from abstractgateway.model_host_http import OptionalHTTP, STATUS_FILE, read_http_status

    data = tmp_path / "data"
    owner = OptionalHTTP(data)
    _settings(data, direct=True)
    owner._requested["direct"] = True
    owner._record("direct", "up", url="http://127.0.0.1:8000")
    path = data / STATUS_FILE
    assert read_http_status(data)["direct"]["state"] == "up"
    raw = path.read_text()
    assert "token" not in raw and "secret" not in raw and "prompt" not in raw
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    doc = json.loads(raw)
    doc["pid"] = -1
    path.write_text(json.dumps(doc))
    assert read_http_status(data) == {}
    owner._publish()
    _settings(data)
    assert read_http_status(data) == {}, "off settings cannot show an old live-owner status"
    owner._requested["direct"] = False
    owner._publish()
    assert not path.exists()
    assert not list(path.parent.glob(".model-host-http-*"))
    _settings(data, direct=True)
    owner._requested["direct"] = True
    owner._publish()
    owner._stop.set()
    owner._run()
    assert not path.exists(), "clean shutdown removes the service status"
