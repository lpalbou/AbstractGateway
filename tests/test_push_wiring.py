"""The push dispatcher wired into the gateway (framework backlog 1049).

- Through the real app: a registered watch and one approval in the owner's stream -> exactly one
  call at a fake relay on a scratch loopback port, whose JSON keys are the contract's seven. The
  dispatcher is started by `service._start_push_dispatcher` beside the email worker: unwiring it
  turns this test red.
- Split mode: an append made by ANOTHER process is dispatched within 3 s, and a split runner
  notices a registration it was not woken for.
- Idle: with `--push-relay off` no dispatcher exists; with a relay but no device the thread does no
  work and opens no socket (a socket guard records every connect)."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List

import pytest

from timing import timing_budget  # noqa: E402 - tests/timing.py (wall-clock budgets)

from push_fakes import REG, TOKEN_A, WATCH_TOPIC, FakeRelay, raise_approval
from test_gateway_principal_isolation_matrix import _client, _create_user

from abstractgateway.events_stream import stream_for
from abstractgateway.push import settings as push_settings
from abstractgateway.push.dispatcher import PushDispatcher
from abstractgateway.push.store import DeviceStore

BASE = "/api/gateway/me/push/devices"


@pytest.fixture(autouse=True)
def _relay_flag():
    push_settings._reset_for_tests()
    yield
    push_settings._reset_for_tests()


def _wait(pred, timeout: float = 15.0) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return time.monotonic() - t0
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def _body() -> dict:
    return {"token": TOKEN_A, "topic": WATCH_TOPIC, "env": "sandbox", "app": "watch", "reg": REG}


def test_an_approval_reaches_the_fake_relay_through_the_wired_gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with FakeRelay() as relay:
        push_settings.set_push_relay(relay.url, source="test")
        client = _client(tmp_path, monkeypatch)
        with client:
            alice = _create_user(client, "alice")
            r = client.put(f"{BASE}/aw1", headers=alice, json=_body())
            assert r.status_code == 200 and r.json()["relay"] == "on", r.text
            plane = tmp_path / "runtime" / "users" / "default" / "alice" / "runtime"
            item = raise_approval(stream_for(plane, "default", "alice"), 1)
            _wait(lambda: len(relay.calls) >= 1)
            time.sleep(0.5)
            assert len(relay.calls) == 1
            assert set(relay.calls[0]) == {"topic", "env", "token", "kind", "collapse", "seq", "reg"}
            assert relay.calls[0]["kind"] == "approval" and relay.calls[0]["token"] == TOKEN_A
            devices = client.get(BASE, headers=alice).json()
            _wait(lambda: client.get(BASE, headers=alice).json()[0]["last_sent_at"] is not None)
            events = client.get("/api/gateway/me/events", headers=alice).json()["events"]
            sent = [e["data"] for e in events if e["type"] == "notification.sent" and e["data"].get("via") == "push"]
            assert sent and sent[0]["record_key"] == item["record_key"] and sent[0]["channel"] == "watch"
            assert devices[0]["device_id"] == "aw1"


def test_split_mode_append_from_another_process_within_three_seconds(tmp_path: Path) -> None:
    plane = tmp_path / "plane"
    stream = stream_for(plane, "default", "alice")
    store = DeviceStore(plane, tmp_path)
    with FakeRelay() as relay:
        d = PushDispatcher(plane_dir=plane, root_dir=tmp_path, stream=stream, relay_url=relay.url, poll_when_idle=True)
        d.start()
        try:
            # A split runner is not woken by the API process's route: it notices the registration.
            store.put(principal="default:alice", device_id="aw1", token=TOKEN_A, topic=WATCH_TOPIC, env="sandbox", app="watch", reg=REG, name=None)
            _wait(lambda: (plane / "push" / "cursor.json").exists(), timeout=5)
            code = (
                "import sys; from abstractgateway.events_stream import UserEventStream, record_wait_raised;"
                "s = UserEventStream(sys.argv[1], 'default', 'alice');"
                "record_wait_raised(s, {'run_id': 'rx', 'wait_key': 'wx', 'step_id': 's1', 'kind': 'question',"
                " 'wait_kind': 'ask_user', 'record_key': 'step:rx:s1'}, session_id=None)"
            )
            t0 = time.monotonic()
            subprocess.run([sys.executable, "-c", code, str(plane)], check=True, timeout=60)
            appended = time.monotonic()
            _wait(lambda: len(relay.calls) >= 1, timeout=10)
            timing_budget(time.monotonic() - appended, 3.0, "split-mode append to relay call", inclusive=True)
            assert relay.calls[0]["kind"] == "question"
            del t0
        finally:
            d.stop()


class _SocketLog:
    """Every connect made while installed (any thread), recorded before the real call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: List[tuple] = []
        real = socket.socket.connect
        log = self

        def connect(sock, address):  # noqa: ANN001
            log.calls.append((threading.current_thread().name, address))
            return real(sock, address)

        monkeypatch.setattr(socket.socket, "connect", connect)


def test_off_or_no_devices_means_no_thread_work_and_no_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plane = tmp_path / "plane"
    stream = stream_for(plane, "default", "alice")
    sockets = _SocketLog(monkeypatch)

    # `--push-relay off`: the service builds no dispatcher at all.
    from abstractgateway import service as service_mod

    class _Svc:
        push_dispatcher = None

    svc = _Svc()
    service_mod._start_push_dispatcher(svc)  # type: ignore[arg-type]
    assert svc.push_dispatcher is None
    off = PushDispatcher(plane_dir=plane, root_dir=tmp_path, stream=stream, relay_url=None)
    off.start()
    assert not off.running and off.relay is None

    # A relay but no device: parked, no listener, no pass, no connect, whatever the stream does.
    with FakeRelay() as relay:
        sockets.calls.clear()
        d = PushDispatcher(plane_dir=plane, root_dir=tmp_path, stream=stream, relay_url=relay.url)
        d.start()
        try:
            assert d.running
            for i in range(20):
                raise_approval(stream, i)
            time.sleep(2.5)
            assert d.ticks == 0 and d._listener_off is None
            assert [c for c in sockets.calls if c[0].startswith("gateway-push")] == []
            assert relay.calls == []
            # The first registration wakes it (the route's kick): only then does it send.
            DeviceStore(plane, tmp_path).put(principal="default:alice", device_id="aw1", token=TOKEN_A, topic=WATCH_TOPIC,
                                             env="sandbox", app="watch", reg=REG, name=None)
            d.kick()
            _wait(lambda: d.ticks >= 1, timeout=5)
            assert relay.calls == []  # history is never pushed
            raise_approval(stream, 99)
            _wait(lambda: len(relay.calls) == 1, timeout=5)
            assert any(c[0].startswith("gateway-push") for c in sockets.calls)
        finally:
            d.stop()
