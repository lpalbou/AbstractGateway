"""Keep the Mac awake while the gateway runs is a USER SETTING (backlog 1149, operator 2026-11-08).

- `host.keep_awake` round-trips through the runtime-config store; default on (existing installs
  behave the same); `serve --keep-awake on|off` wins for one run (the launch record), the stored
  value applies at the next start.
- `keep_awake.apply(False)` RELEASES the assertion at once (IOKit mocked), `apply(True)` HOLDS it
  again; idempotent; nothing is ever held off macOS.
- `GET /host/runner` carries `keep_awake: {value, source, held, platform_supported}`;
  `PUT /host/keep-awake {enabled}` (admin) stores + applies live and returns the same shape; a
  non-admin is refused.
The manual check on a Mac is `pmset -g assertions` (AbstractGateway listed under
PreventUserIdleSystemSleep while held) — documented in docs/configuration.md, never run here.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Any, List

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, gateway_env

pytestmark = pytest.mark.basic


class _FakeIOKit:
    """Records IOPMAssertionCreateWithName / Release like a Mac would answer."""

    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.created: List[int] = []
        self.released: List[int] = []
        self._next = 41

    def IOPMAssertionCreateWithName(self, kind: Any, level: Any, name: Any, out_ref: Any) -> int:  # noqa: N802
        if self.rc != 0:
            return self.rc
        self._next += 1
        out_ref._obj.value = self._next
        self.created.append(self._next)
        return 0

    def IOPMAssertionRelease(self, assertion_id: Any) -> int:  # noqa: N802
        self.released.append(int(getattr(assertion_id, "value", assertion_id)))
        return 0


class _FakeCF:
    def CFStringCreateWithCString(self, alloc: Any, text: Any, enc: Any) -> int:  # noqa: N802
        return 7

    def CFRelease(self, ref: Any) -> None:  # noqa: N802
        return None


@pytest.fixture()
def fake_mac(monkeypatch: pytest.MonkeyPatch) -> _FakeIOKit:
    from abstractgateway import keep_awake

    iokit = _FakeIOKit()
    monkeypatch.setattr(keep_awake, "_libraries", lambda: (iokit, _FakeCF()))
    monkeypatch.setattr(keep_awake, "_platform", lambda: "darwin")
    monkeypatch.setattr(keep_awake, "_assertion_id", None)
    return iokit


def test_setting_round_trips_default_on_and_the_flag_wins_for_one_run(tmp_path: Path) -> None:
    from abstractgateway.runtime_config import (
        KEEP_AWAKE_KEY,
        clear_launch_settings,
        read_runtime_config,
        record_launch_settings,
        resolve_keep_awake,
        write_runtime_config,
    )

    data = tmp_path / "data"
    data.mkdir()
    assert resolve_keep_awake(data) == {"value": True, "source": "default"}
    out = write_runtime_config(data, {KEEP_AWAKE_KEY: False}, actor="person:test")
    assert out["applied"][KEEP_AWAKE_KEY] is False
    assert resolve_keep_awake(data) == {"value": False, "source": "stored"}
    assert read_runtime_config(data)[KEEP_AWAKE_KEY]["value"] is False
    assert read_runtime_config(data)[KEEP_AWAKE_KEY]["label"] == "Keep the Mac awake while the gateway runs"
    # on/off words through the door; garbage refuses in words.
    write_runtime_config(data, {KEEP_AWAKE_KEY: "on"}, actor="person:test")
    assert resolve_keep_awake(data)["value"] is True
    with pytest.raises(Exception, match="on or off"):
        write_runtime_config(data, {KEEP_AWAKE_KEY: "maybe"}, actor="person:test")
    # `serve --keep-awake off` for this run: the flag wins, the saved value is named.
    record_launch_settings(data, {"keep_awake": False})
    assert resolve_keep_awake(data) == {"value": False, "source": "flag", "stored_value": True}
    clear_launch_settings(data)
    assert resolve_keep_awake(data) == {"value": True, "source": "stored"}
    # Clearing the key = the default again.
    write_runtime_config(data, {KEEP_AWAKE_KEY: None}, actor="person:test")
    assert resolve_keep_awake(data) == {"value": True, "source": "default"}


def test_apply_releases_and_holds_live_and_is_idempotent(fake_mac: _FakeIOKit) -> None:
    from abstractgateway import keep_awake

    assert keep_awake.held() is False
    assert keep_awake.apply(True) is True and keep_awake.held() is True
    assert fake_mac.created == [42]
    assert keep_awake.apply(True) is True and fake_mac.created == [42], "on → on holds nothing new"
    assert keep_awake.apply(False) is False and keep_awake.held() is False
    assert fake_mac.released == [42]
    assert keep_awake.apply(False) is False and fake_mac.released == [42], "off → off releases nothing"
    assert keep_awake.apply(True) is True and fake_mac.created == [42, 43]
    keep_awake.apply(False)


def test_nothing_is_held_off_macos_and_the_status_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from abstractgateway import keep_awake

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(keep_awake, "_platform", lambda: "linux")
    monkeypatch.setattr(keep_awake, "_assertion_id", None)
    monkeypatch.setattr(keep_awake, "_libraries", lambda: (_ for _ in ()).throw(AssertionError("IOKit must not load off macOS")))
    assert keep_awake.apply(True) is False and keep_awake.held() is False
    st = keep_awake.status(data)
    assert st == {"value": True, "source": "default", "held": False, "platform_supported": False}


def test_route_reports_and_changes_the_setting_live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_mac: _FakeIOKit) -> None:
    from abstractgateway import keep_awake

    gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    keep_awake.apply(True)  # what `serve` does at start with the default setting
    with TestClient(app) as client:
        runner = client.get("/api/gateway/host/runner", headers=HEADERS).json()
        assert runner["keep_awake"] == {"value": True, "source": "default", "held": True, "platform_supported": True}

        off = client.put("/api/gateway/host/keep-awake", json={"enabled": False}, headers=HEADERS)
        assert off.status_code == 200, off.text
        assert off.json()["keep_awake"] == {"value": False, "source": "stored", "held": False, "platform_supported": True}
        assert fake_mac.released == [42], "turning it off released the assertion at once"
        assert client.get("/api/gateway/host/runner", headers=HEADERS).json()["keep_awake"]["held"] is False

        on = client.put("/api/gateway/host/keep-awake", json={"enabled": True}, headers=HEADERS)
        assert on.status_code == 200, on.text
        assert on.json()["keep_awake"] == {"value": True, "source": "stored", "held": True, "platform_supported": True}
        assert fake_mac.created == [42, 43], "turning it on held it again"

        # The body is strict: a non-boolean or an unknown field is refused.
        assert client.put("/api/gateway/host/keep-awake", json={"enabled": "off"}, headers=HEADERS).status_code == 422
        assert client.put("/api/gateway/host/keep-awake", json={"enabled": True, "x": 1}, headers=HEADERS).status_code == 422
        # Not an admin: refused (the policy row), nothing changes.
        anon = client.put("/api/gateway/host/keep-awake", json={"enabled": False})
        assert anon.status_code in (401, 403), anon.text
        assert keep_awake.held() is True
    keep_awake.apply(False)


def test_route_under_a_launch_flag_stores_but_the_flag_wins_for_the_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_mac: _FakeIOKit) -> None:
    from abstractgateway import keep_awake
    from abstractgateway.runtime_config import record_launch_settings

    data = gateway_env(monkeypatch, tmp_path)
    record_launch_settings(data, {"keep_awake": False})  # `serve --keep-awake off`
    from abstractgateway.app import app

    keep_awake.apply(False)
    with TestClient(app) as client:
        before = client.get("/api/gateway/host/runner", headers=HEADERS).json()["keep_awake"]
        assert before == {"value": False, "source": "flag", "held": False, "platform_supported": True}
        out = client.put("/api/gateway/host/keep-awake", json={"enabled": True}, headers=HEADERS).json()["keep_awake"]
        # Saved for the next start; this run stays as the flag says, and the shape names both.
        assert out == {"value": False, "source": "flag", "stored_value": True, "held": False, "platform_supported": True}
        assert fake_mac.created == []
