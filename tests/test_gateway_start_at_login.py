"""GET/PUT /api/gateway/host/start-at-login (`gateway_start_at_login_v1`), the
consoles' toggle over `abstractgateway.autostart` (wave 2, 2026-09-28).

Every OS mechanism runs on every OS with the recording doubles of
test_gateway_autostart (launchctl/systemctl runner, a dict registry, HOME =
tmp_path). Nothing here can register a real login item: the route test
injects the same doubles into the module functions the route calls.
"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from abstractgateway import autostart
from test_gateway_autostart import Registry, Runner, _exe

pytestmark = pytest.mark.basic


def _kw(tmp_path: Path, platform: str, runner=None, registry=None, env=None):
    return dict(platform=platform, home=tmp_path / "home", runner=runner or Runner(), registry=registry, uid=501, env=env or {})


def test_macos_launch_agent_toggle_reads_back(tmp_path: Path) -> None:
    data = tmp_path / "data"
    kw = _kw(tmp_path, "darwin", Runner({("launchctl", "print"): (113, "")}))
    st = autostart.start_at_login_status(data_dir=data, **kw)
    assert st["schema"] == "gateway_start_at_login_v1"
    assert (st["enabled"], st["state"], st["mechanism"], st["can_change"], st["reason"]) == (False, "off", "launchd-agent", True, None)

    code, body = autostart.set_start_at_login(data_dir=data, enabled=True, exe_argv=[str(_exe(tmp_path))], **kw)
    assert code == 200 and body["ok"] and body["changed"] is True
    assert body["start_at_login"]["enabled"] is True and body["start_at_login"]["mechanism_label"] == "a LaunchAgent"
    assert (tmp_path / "home" / "Library" / "LaunchAgents" / "ai.abstractframework.gateway.plist").exists()
    # Registered for the NEXT login: nothing started (no bootstrap).
    assert kw["runner"].mutating() == []

    # Idempotent: on -> on changes nothing.
    code, body = autostart.set_start_at_login(data_dir=data, enabled=True, exe_argv=[str(_exe(tmp_path))], **kw)
    assert code == 200 and body["changed"] is False

    code, body = autostart.set_start_at_login(data_dir=data, enabled=False, **kw)
    assert code == 200 and body["changed"] is True and body["start_at_login"]["enabled"] is False
    assert kw["runner"].mutating() == []  # disabling never stops the running gateway


def test_linux_systemd_user_unit_toggle(tmp_path: Path) -> None:
    data = tmp_path / "data"
    kw = _kw(tmp_path, "linux", Runner({("systemctl", "--user", "is-enabled"): (0, "enabled\n")}))
    st = autostart.start_at_login_status(data_dir=data, **kw)
    assert st["mechanism"] == "systemd-user" and st["can_change"] is True
    code, body = autostart.set_start_at_login(data_dir=data, enabled=True, exe_argv=[str(_exe(tmp_path))], linux_mechanism="systemd", **kw)
    assert code == 200 and body["start_at_login"]["enabled"] is True
    assert body["start_at_login"]["mechanism"] == "systemd-user"
    assert ["systemctl", "--user", "enable", "abstractgateway.service"] in kw["runner"].calls
    code, body = autostart.set_start_at_login(data_dir=data, enabled=False, **kw)
    assert code == 200 and body["start_at_login"]["enabled"] is False


def test_linux_without_systemd_uses_xdg_only_with_a_desktop_session(tmp_path: Path) -> None:
    data = tmp_path / "data"
    no_systemd = Runner({("systemctl",): None})
    desktop = _kw(tmp_path, "linux", no_systemd, env={"DISPLAY": ":0"})
    st = autostart.start_at_login_status(data_dir=data, **desktop)
    assert st["mechanism"] == "xdg-autostart" and st["can_change"] is True
    py = _exe(tmp_path, "python3")
    code, body = autostart.set_start_at_login(data_dir=data, enabled=True, exe_argv=[str(py), "-m", "abstractgateway"], **desktop)
    assert code == 200 and body["start_at_login"]["mechanism"] == "xdg-autostart" and body["start_at_login"]["enabled"]
    autostart.set_start_at_login(data_dir=data, enabled=False, **desktop)

    # Headless (SSH box, container): no systemd user manager, no desktop -> an
    # XDG entry would never run. The toggle says so and refuses; nothing written.
    headless = _kw(tmp_path, "linux", no_systemd, env={"SSH_CONNECTION": "1.2.3.4 5 6.7.8.9 22"})
    st = autostart.start_at_login_status(data_dir=data, **headless)
    assert st["can_change"] is False and "no systemd user manager" in st["reason"]
    code, body = autostart.set_start_at_login(data_dir=data, enabled=True, exe_argv=[str(py)], **headless)
    assert code == 409 and body["reason_code"] == "cannot_change" and body["refused_reason"] == st["reason"]
    assert not (tmp_path / "home" / ".config" / "autostart" / "abstractgateway.desktop").exists()


def test_windows_run_value_toggle_and_missing_registry(tmp_path: Path) -> None:
    data = tmp_path / "data"
    reg = Registry()
    kw = _kw(tmp_path, "windows", registry=reg)
    st = autostart.start_at_login_status(data_dir=data, **kw)
    assert st["mechanism"] == "registry-run" and st["experimental"] is True and st["can_change"] is True
    py = tmp_path / "py" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("")
    (py.parent / "pythonw.exe").write_text("")
    code, body = autostart.set_start_at_login(data_dir=data, enabled=True, exe_argv=[str(py), "-m", "abstractgateway"], **kw)
    assert code == 200 and body["start_at_login"]["enabled"] is True, body
    assert any(k[1] == "AbstractGateway" for k in reg.values)
    code, body = autostart.set_start_at_login(data_dir=data, enabled=False, **kw)
    assert code == 200 and not any(k[1] == "AbstractGateway" for k in reg.values)


def test_windows_without_a_registry_backend_cannot_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import os_service

    monkeypatch.setattr(os_service, "default_registry", lambda platform=None: None)
    st = autostart.start_at_login_status(data_dir=tmp_path / "d", platform="windows", home=tmp_path / "h", env={})
    assert st["can_change"] is False and "winreg" in st["reason"]


def test_another_gateways_registration_needs_replace_other(tmp_path: Path) -> None:
    kw = _kw(tmp_path, "darwin")
    exe = [str(_exe(tmp_path))]
    assert autostart.set_start_at_login(data_dir=tmp_path / "other", enabled=True, exe_argv=exe, **kw)[0] == 200
    st = autostart.start_at_login_status(data_dir=tmp_path / "mine", **kw)
    assert st["state"] == "other" and st["enabled"] is False and st["other_data_dir"] == str(tmp_path / "other")
    code, body = autostart.set_start_at_login(data_dir=tmp_path / "mine", enabled=True, exe_argv=exe, **kw)
    assert code == 409 and body["reason_code"] == "other_gateway_registered" and "replace_other" in body["refused_reason"]
    code, body = autostart.set_start_at_login(data_dir=tmp_path / "mine", enabled=True, replace_other=True, exe_argv=exe, **kw)
    assert code == 200 and body["start_at_login"]["enabled"] is True


def test_a_broken_registration_is_not_enabled_and_turning_on_repairs_it(tmp_path: Path) -> None:
    from abstractgateway import os_service

    data, home = tmp_path / "data", tmp_path / "home"
    gone = tmp_path / "old" / "bin" / "abstractgateway"
    plan = os_service.build_install_plan(platform="darwin", home=home, host="127.0.0.1", port=8080, data_dir=data, exe_argv=[str(gone)], uid=501, env={})
    Path(plan.files[0]["path"]).parent.mkdir(parents=True)
    Path(plan.files[0]["path"]).write_text(plan.files[0]["content"], encoding="utf-8")
    kw = _kw(tmp_path, "darwin")
    st = autostart.start_at_login_status(data_dir=data, **kw)
    assert st["state"] == "broken" and st["enabled"] is False and "gone" in st["summary"]
    code, body = autostart.set_start_at_login(data_dir=data, enabled=True, exe_argv=[str(_exe(tmp_path))], **kw)
    assert code == 200 and body["start_at_login"]["state"] == "on"


def test_route_is_admin_only_and_reads_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    runner = Runner({("systemctl", "--user", "is-enabled"): (0, "enabled\n")})
    doubles = dict(platform="linux", home=tmp_path / "home", runner=runner, env={}, uid=501)
    monkeypatch.setattr(autostart, "start_at_login_status", functools.partial(autostart.start_at_login_status, **doubles))
    monkeypatch.setattr(
        autostart, "set_start_at_login",
        functools.partial(autostart.set_start_at_login, exe_argv=[str(_exe(tmp_path))], linux_mechanism="systemd", **doubles),
    )
    from abstractgateway.routes import gateway_router
    from abstractgateway.routes.start_at_login import router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    app.include_router(router, prefix="/api")
    app.include_router(gateway_router, prefix="/api")
    path = "/api/gateway/host/start-at-login"
    # Admin in the ONE policy table too (not only the handler's check).
    from abstractgateway.security.authorization import GATEWAY_ROUTE_POLICIES

    for method in ("GET", "PUT"):
        assert any(p.matches(path, method) and p.admin_required for p in GATEWAY_ROUTE_POLICIES), method
    with TestClient(app) as client:
        admin = {"Authorization": "Bearer admin-token"}
        created = client.post("/api/gateway/admin/users", headers=admin, json={"user_id": "mallory", "tenant_id": "default", "roles": ["user"]})
        user = {"Authorization": f"Bearer {created.json()['token']}"}
        assert client.get(path).status_code == 401
        assert client.get(path, headers=user).status_code == 403
        assert client.put(path, headers=user, json={"enabled": True}).status_code == 403

        r = client.get(path, headers=admin)
        assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["mechanism"] == "systemd-user"
        assert client.put(path, headers=admin, json={"enabled": "yes"}).status_code == 422
        assert client.put(path, headers=admin, json={"enabled": True, "bogus": 1}).status_code == 422
        on = client.put(path, headers=admin, json={"enabled": True})
        assert on.status_code == 200 and on.json()["start_at_login"]["enabled"] is True, on.text
        assert client.get(path, headers=admin).json()["enabled"] is True  # verify by GET
        off = client.put(path, headers=admin, json={"enabled": False})
        assert off.status_code == 200 and client.get(path, headers=admin).json()["enabled"] is False
