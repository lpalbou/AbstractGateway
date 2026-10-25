"""PUT/DELETE/GET /api/gateway/me/push/devices (framework backlog 1049), through the real app and
auth middleware with two real principals: principal scoping, CSRF on session writes, the topic
allowlist, a token moving between principals, the 7-day expiry, private files, no token returned."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from push_fakes import PHONE_TOPIC, REG, TOKEN_A, TOKEN_B, WATCH_TOPIC, FakeRelay
from test_gateway_principal_isolation_matrix import _client, _create_user

from abstractgateway.push import settings as push_settings

BASE = "/api/gateway/me/push/devices"


@pytest.fixture(autouse=True)
def _relay_off():
    push_settings._reset_for_tests()
    yield
    push_settings._reset_for_tests()


def _plane(tmp_path: Path, user: str) -> Path:
    return tmp_path / "runtime" / "users" / "default" / user / "runtime"


def _body(token: str = TOKEN_A, **over) -> dict:
    out = {"token": token, "topic": WATCH_TOPIC, "env": "sandbox", "app": "watch", "reg": REG, "name": "Wrist"}
    out.update(over)
    return out


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_devices_are_principal_scoped_and_tokens_never_returned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        alice, bob = _create_user(client, "alice"), _create_user(client, "bob")
        r = client.put(f"{BASE}/aw1", headers=alice, json=_body())
        assert r.status_code == 200, r.text
        assert r.json() == {"ok": True, "relay": "off"}  # --push-relay off (the packaged default)

        mine = client.get(BASE, headers=alice).json()
        assert [d["device_id"] for d in mine] == ["aw1"]
        assert set(mine[0]) == {"device_id", "app", "name", "refreshed_at", "last_sent_at", "last_error"}
        assert mine[0]["app"] == "watch" and mine[0]["name"] == "Wrist" and mine[0]["last_sent_at"] is None
        assert TOKEN_A not in json.dumps(mine)
        assert client.get(BASE, headers=bob).json() == []

        # Bob cannot delete Alice's device by naming its id.
        assert client.delete(f"{BASE}/aw1", headers=bob).json() == {"ok": True, "deleted": False}
        assert [d["device_id"] for d in client.get(BASE, headers=alice).json()] == ["aw1"]

        # Files: per plane, private.
        devices = _plane(tmp_path, "alice") / "push" / "devices.json"
        owners = tmp_path / "runtime" / "push" / "token_owners.json"
        assert _mode(devices) == 0o600 and _mode(owners) == 0o600
        raw = json.loads(devices.read_text())
        assert raw["schema_version"] == 1 and raw["devices"]["aw1"]["token"] == TOKEN_A
        assert not (_plane(tmp_path, "bob") / "push" / "devices.json").exists()
        # The first registration baselines the dispatcher's cursor at the stream head.
        assert json.loads((_plane(tmp_path, "alice") / "push" / "cursor.json").read_text()) == {"after": 0}

        assert client.delete(f"{BASE}/aw1", headers=alice).json() == {"ok": True, "deleted": True}
        assert client.get(BASE, headers=alice).json() == []
        assert json.loads(owners.read_text())["owners"] == {}
        assert client.get(BASE).status_code == 401


def test_a_token_moves_to_the_latest_principal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        alice, bob = _create_user(client, "alice"), _create_user(client, "bob")
        assert client.put(f"{BASE}/aw1", headers=alice, json=_body()).status_code == 200
        assert client.put(f"{BASE}/ap1", headers=alice, json=_body(TOKEN_B, topic=PHONE_TOPIC, app="phone")).status_code == 200
        # Bob signs in on the same watch app: the token leaves Alice.
        assert client.put(f"{BASE}/bw1", headers=bob, json=_body()).status_code == 200
        assert [d["device_id"] for d in client.get(BASE, headers=alice).json()] == ["ap1"]
        assert [d["device_id"] for d in client.get(BASE, headers=bob).json()] == ["bw1"]
        from abstractgateway.push.store import token_hash

        owners = json.loads((tmp_path / "runtime" / "push" / "token_owners.json").read_text())["owners"]
        assert owners[token_hash(TOKEN_A)]["principal"] == "default:bob"
        # A reinstalled app (new device id, same token) replaces its own older registration.
        assert client.put(f"{BASE}/bw2", headers=bob, json=_body()).status_code == 200
        assert [d["device_id"] for d in client.get(BASE, headers=bob).json()] == ["bw2"]


def test_session_writes_need_the_csrf_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        _create_user(client, "alice")
        from abstractgateway.security.sessions import (
            GatewaySessionStore,
            gateway_csrf_cookie_name,
            gateway_csrf_header_name,
            gateway_session_cookie_name,
        )
        from abstractgateway.users import GatewayUserRegistry

        cookie, csrf, _rec = GatewaySessionStore().create_session(GatewayUserRegistry().get_user("alice").to_principal())
        client.cookies.set(gateway_session_cookie_name(), cookie)
        client.cookies.set(gateway_csrf_cookie_name(), csrf)
        refused = client.put(f"{BASE}/aw1", json=_body())
        assert refused.status_code == 403 and refused.json()["reason_code"] == "csrf_required"
        assert client.delete(f"{BASE}/aw1").status_code == 403
        assert client.get(BASE).json() == []  # a read needs no CSRF
        ok = client.put(f"{BASE}/aw1", json=_body(), headers={gateway_csrf_header_name(): csrf})
        assert ok.status_code == 200, ok.text
        assert [d["device_id"] for d in client.get(BASE).json()] == ["aw1"]


def test_body_and_topic_are_strict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        alice = _create_user(client, "alice")
        for bad in (
            _body(topic="com.example.other"),
            _body(token="xyz"),
            _body(token="a" * 63),
            _body(env="dev"),
            _body(app="mac"),
            _body(reg="short"),
            _body(name="n" * 61),
            {**_body(), "title": "Your account is locked"},
        ):
            assert client.put(f"{BASE}/aw1", headers=alice, json=bad).status_code == 422, bad
        assert client.put(f"{BASE}/bad%20id", headers=alice, json=_body()).status_code == 422
        assert client.get(BASE, headers=alice).json() == []

        # A relay's own extra topics are allowed once the relay names them (GET /v1/info).
        with FakeRelay(topics=("com.example.selfhosted",)) as relay:
            push_settings.set_push_relay(relay.url, source="test")
            r = client.put(f"{BASE}/x1", headers=alice, json=_body(topic="com.example.selfhosted"))
            assert r.status_code == 200, r.text
            assert r.json()["relay"] == "on"


def test_a_registration_expires_seven_days_after_its_last_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        alice = _create_user(client, "alice")
        assert client.put(f"{BASE}/aw1", headers=alice, json=_body()).status_code == 200
        path = _plane(tmp_path, "alice") / "push" / "devices.json"
        raw = json.loads(path.read_text())
        raw["devices"]["aw1"]["refreshed_at"] -= 6.9 * 86400
        path.write_text(json.dumps(raw))
        assert [d["device_id"] for d in client.get(BASE, headers=alice).json()] == ["aw1"]
        raw["devices"]["aw1"]["refreshed_at"] -= 0.2 * 86400
        path.write_text(json.dumps(raw))
        assert client.get(BASE, headers=alice).json() == []
        # A refresh (the app re-PUTs) brings it back.
        assert client.put(f"{BASE}/aw1", headers=alice, json=_body()).status_code == 200
        assert [d["device_id"] for d in client.get(BASE, headers=alice).json()] == ["aw1"]
