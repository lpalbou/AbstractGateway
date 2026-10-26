"""The saved Push forwarder address (framework backlog 1049): precedence (the `--push-relay` flag of
this run > the saved setting > the packaged default), `GET|PUT /api/gateway/push/settings` (admin
only, 422 on a bad address, https except on this computer, one /v1/info probe, saved either way),
and the dispatcher following a change without a restart (serve: the PUT starts/wakes it; split
runner: its poll sees the settings file move)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from push_fakes import REG, TOKEN_A, WATCH_TOPIC, FakeRelay, free_port, raise_approval
from test_gateway_principal_isolation_matrix import _client, _create_user

from abstractgateway.events_stream import stream_for
from abstractgateway.push import settings as push_settings
from abstractgateway.push.dispatcher import PushDispatcher, collapse_id
from abstractgateway.push.store import DeviceStore

SETTINGS = "/api/gateway/push/settings"
DEVICES = "/api/gateway/me/push/devices"
ADMIN = {"Authorization": "Bearer admin-token"}


@pytest.fixture(autouse=True)
def _no_flag():
    push_settings._reset_for_tests()
    yield
    push_settings._reset_for_tests()


def _wait(pred, timeout: float = 15.0) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_precedence_flag_over_setting_over_packaged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    eff = push_settings.effective_forwarder
    assert eff(tmp_path) == (None, "packaged")  # nothing saved, nothing built in yet: off
    monkeypatch.setattr(push_settings, "PUBLISHER_RELAY_URL", "https://push.publisher.example")
    assert eff(tmp_path) == ("https://push.publisher.example", "packaged")

    push_settings.save_forwarder(tmp_path, "url", "http://127.0.0.1:8095/")
    assert eff(tmp_path) == ("http://127.0.0.1:8095", "setting")
    push_settings.save_forwarder(tmp_path, "off")
    assert eff(tmp_path) == (None, "setting")
    push_settings.save_forwarder(tmp_path, "default")
    assert eff(tmp_path) == ("https://push.publisher.example", "packaged")

    push_settings.save_forwarder(tmp_path, "url", "https://mine.example.org")
    push_settings.set_push_relay("off", source="test")
    assert eff(tmp_path) == (None, "flag")
    push_settings.set_push_relay("http://10.0.0.5:9000", source="test")
    assert eff(tmp_path) == ("http://10.0.0.5:9000", "flag")
    push_settings.set_push_relay(None, source="test")  # no flag at the next start
    assert eff(tmp_path) == ("https://mine.example.org", "setting")

    doc = json.loads(push_settings.settings_path(tmp_path).read_text())
    assert doc["forwarder"]["mode"] == "url" and doc["forwarder"]["url"] == "https://mine.example.org"
    # A hand-broken file is the default, never a crash.
    push_settings.settings_path(tmp_path).write_text('{"forwarder": {"mode": "url", "url": "ftp://x"}, "pad": 1}')
    assert eff(tmp_path) == ("https://push.publisher.example", "packaged")


@pytest.mark.parametrize("url", ["http://127.0.0.1:8095", "http://localhost:8095", "http://[::1]:8095", "https://push.example.org/base/"])
def test_addresses_accepted(url: str) -> None:
    assert push_settings.parse_forwarder_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", ["", "push.example.org", "ftp://push.example.org", "http://push.example.org",
                                 "http://192.168.1.2:8095", "https://u:p@push.example.org", "https://push.example.org/?a=1",
                                 "https://push.example.org/#x", "https://push.example.org:99999"])
def test_addresses_refused(url: str) -> None:
    with pytest.raises(ValueError):
        push_settings.parse_forwarder_url(url)


def test_settings_routes_admin_only_validation_and_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        alice = _create_user(client, "alice")
        assert client.get(SETTINGS, headers=alice).status_code == 403
        assert client.put(SETTINGS, headers=alice, json={"mode": "off"}).status_code == 403
        assert client.get(SETTINGS).status_code == 401

        r = client.get(SETTINGS, headers=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json() == {"forwarder": {"mode": "default", "url": None, "effective": None, "source": "packaged", "packaged": None}}

        for bad in ({"mode": "url"}, {"mode": "url", "url": "http://push.example.org"}, {"mode": "url", "url": "nope"},
                    {"mode": "off", "url": "https://push.example.org"}, {"mode": "sometimes"}, {"mode": "off", "extra": 1}):
            assert client.put(SETTINGS, headers=ADMIN, json=bad).status_code == 422, bad
        assert not (tmp_path / "runtime" / "push" / "settings.json").exists()

        with FakeRelay(topics=("org.example.mywatch",)) as relay:
            r = client.put(SETTINGS, headers=ADMIN, json={"mode": "url", "url": relay.url + "/"})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["forwarder"] == {"mode": "url", "url": relay.url, "effective": relay.url, "source": "setting", "packaged": None}
            assert body["probe"] == {"reachable": True, "topics": ["org.example.mywatch"], "reason": None}
            # The probe primes the topic allowlist: a device of the forwarder's own app registers.
            r = client.put(f"{DEVICES}/aw1", headers=alice, json={"token": TOKEN_A, "topic": "org.example.mywatch", "env": "sandbox",
                                                                 "app": "watch", "reg": REG})
            assert r.status_code == 200 and r.json()["relay"] == "on", r.text

        # Nobody listening: the reason in words, and the setting is saved anyway.
        dead = f"http://127.0.0.1:{free_port()}"
        r = client.put(SETTINGS, headers=ADMIN, json={"mode": "url", "url": dead})
        assert r.status_code == 200, r.text
        assert r.json()["probe"]["reachable"] is False and r.json()["probe"]["reason"].startswith("Could not reach it")
        assert client.get(SETTINGS, headers=ADMIN).json()["forwarder"]["url"] == dead
        settings_file = tmp_path / "runtime" / "push" / "settings.json"
        saved = json.loads(settings_file.read_text())
        assert saved["schema_version"] == 1 and saved["forwarder"]["by"] and saved["forwarder"]["at"]

        # Off: no probe at all.
        r = client.put(SETTINGS, headers=ADMIN, json={"mode": "off"})
        assert r.json() == {"forwarder": {"mode": "off", "url": None, "effective": None, "source": "setting", "packaged": None}}

        # The launch flag wins for this run; the saved setting is kept and shown.
        push_settings.set_push_relay("http://127.0.0.1:1", source="test")
        r = client.get(SETTINGS, headers=ADMIN).json()["forwarder"]
        assert r == {"mode": "off", "url": None, "effective": "http://127.0.0.1:1", "source": "flag", "packaged": None}


def _approval_reaches(stream, relay: FakeRelay, n: int) -> None:
    before = len(relay.calls)
    raise_approval(stream, n)
    _wait(lambda: len(relay.calls) == before + 1, timeout=10)


def test_the_running_gateway_follows_a_saved_change_without_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with FakeRelay() as first, FakeRelay() as second, client:
        alice = _create_user(client, "alice")
        r = client.put(f"{DEVICES}/aw1", headers=alice, json={"token": TOKEN_A, "topic": WATCH_TOPIC, "env": "sandbox", "app": "watch", "reg": REG})
        assert r.json()["relay"] == "off"  # nothing saved, nothing built in: no dispatcher
        plane = tmp_path / "runtime" / "users" / "default" / "alice" / "runtime"
        stream = stream_for(plane, "default", "alice")
        raise_approval(stream, 0)  # while off: never pushed, even once it is turned on

        assert client.put(SETTINGS, headers=ADMIN, json={"mode": "url", "url": first.url}).status_code == 200
        _approval_reaches(stream, first, 1)
        time.sleep(0.5)
        assert len(first.calls) == 1 and first.calls[0]["collapse"] == collapse_id("step:r1:s1")
        assert first.calls[0]["kind"] == "approval" and first.calls[0]["token"] == TOKEN_A

        assert client.put(SETTINGS, headers=ADMIN, json={"mode": "url", "url": second.url}).status_code == 200
        _approval_reaches(stream, second, 2)
        assert len(first.calls) == 1

        assert client.put(SETTINGS, headers=ADMIN, json={"mode": "off"}).status_code == 200
        assert client.get(f"{DEVICES}", headers=alice).status_code == 200
        raise_approval(stream, 3)
        time.sleep(1.5)
        assert len(first.calls) == 1 and len(second.calls) == 1


def test_a_split_runner_sees_the_settings_file_move(tmp_path: Path) -> None:
    plane = tmp_path / "plane"
    stream = stream_for(plane, "default", "alice")
    DeviceStore(plane, tmp_path).put(principal="default:alice", device_id="aw1", token=TOKEN_A, topic=WATCH_TOPIC,
                                     env="sandbox", app="watch", reg=REG, name=None)
    with FakeRelay() as relay:
        d = PushDispatcher(plane_dir=plane, root_dir=tmp_path, stream=stream, relay_url=None,
                           resolve_relay=lambda: push_settings.push_relay_url(tmp_path),
                           settings_token=lambda: push_settings.settings_change_token(tmp_path), poll_when_idle=True)
        d.start()
        try:
            assert d.running and d.relay is None
            raise_approval(stream, 1)
            time.sleep(0.5)
            assert d.ticks == 0 and relay.calls == []
            # Another process (the API's settings PUT) saves an address: nobody kicks this one.
            push_settings.save_forwarder(tmp_path, "url", relay.url)
            _wait(lambda: d.relay_url == relay.url, timeout=5)
            _approval_reaches(stream, relay, 2)
            assert relay.calls[0]["kind"] == "approval" and len(relay.calls) == 1  # history before the address: not pushed
            push_settings.save_forwarder(tmp_path, "off")
            _wait(lambda: d.relay is None, timeout=5)
        finally:
            d.stop()
