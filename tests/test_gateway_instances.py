"""Gateway instances: a second self-contained gateway on one machine (operator, 2026-10-29).

One name drives everything (instances.py): data dir, AbstractCore config, app port block (from a
registered slot, so two instances never collide), gateway port, service unit/label, console label."""

from __future__ import annotations

from pathlib import Path

import pytest

from abstractgateway import instances, os_service


@pytest.fixture()
def reg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv(instances.INSTANCE_ENV, raising=False)
    monkeypatch.delenv("ABSTRACTCORE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ABSTRACTCORE_CONFIG_FILE", raising=False)
    return tmp_path


@pytest.mark.parametrize("bad", ["1st", "-x", "a_b", "x" * 40, "a b", "fresh-"])
def test_names_are_validated(bad) -> None:
    with pytest.raises(instances.InstanceError):
        instances.validate_name(bad)
    assert instances.validate_name("fresh") == "fresh" and instances.validate_name("Fresh") == "fresh" and instances.validate_name("") == "default"


def test_the_cli_flag_is_stripped_and_exported(reg) -> None:
    env: dict = {}
    assert instances.apply_instance_arg(["--instance", "fresh", "service", "status"], env) == ["service", "status"]
    assert env[instances.INSTANCE_ENV] == "fresh"
    assert instances.apply_instance_arg(["--instance=default", "serve"], env) == ["serve"] and instances.INSTANCE_ENV not in env
    with pytest.raises(SystemExit):
        instances.apply_instance_arg(["--instance", "Bad Name", "serve"], {})


def test_slots_are_assigned_once_and_never_collide(reg) -> None:
    a, b = instances.slot("fresh"), instances.slot("lab")
    assert (a, b) == (1, 2) and instances.slot("fresh") == 1 and instances.slot("default") == 0
    blocks = {n: set(range(3001 + 200 * s, 3007 + 200 * s)) | set(range(*instances.app_port_range(n))) for n, s in (("default", 0), ("fresh", a), ("lab", b))}
    assert not (blocks["default"] & blocks["fresh"]) and not (blocks["fresh"] & blocks["lab"]) and not (blocks["default"] & blocks["lab"])
    assert instances.gateway_port("default") == 8080 and instances.gateway_port("fresh") == 8091 and instances.gateway_port("lab") == 8092
    assert instances.forget("fresh") and instances.slot("other") == 1  # a freed slot is reused


def test_a_named_instance_has_its_own_data_dir_core_config_and_service_names(reg, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.host_paths import user_data_dir

    home = reg / "home"
    monkeypatch.setenv(instances.INSTANCE_ENV, "fresh")
    assert user_data_dir(system="linux", env={"ABSTRACTGATEWAY_INSTANCE": "fresh"}, home=home) == home / ".local" / "share" / "abstractgateway-fresh"
    assert user_data_dir(system="darwin", env={"ABSTRACTGATEWAY_INSTANCE": "fresh"}, home=home).name == "AbstractGateway-fresh"
    assert user_data_dir(system="linux", env={}, home=home).name == "abstractgateway"  # the default instance is unchanged
    env = {"ABSTRACTGATEWAY_INSTANCE": "fresh", "XDG_CONFIG_HOME": str(reg / "config")}
    facts = instances.apply_instance_env(reg / "data-fresh", env)
    assert env["ABSTRACTCORE_CONFIG_DIR"] == str(reg / "data-fresh" / "core-config")
    assert facts["systemd_unit"] == "abstractgateway-fresh.service" and facts["app_ports"] == [3201, 3206]
    env2 = {"ABSTRACTGATEWAY_INSTANCE": "fresh", "XDG_CONFIG_HOME": str(reg / "config"), "ABSTRACTCORE_CONFIG_DIR": "/explicit"}
    instances.apply_instance_env(reg / "data-fresh", env2)
    assert env2["ABSTRACTCORE_CONFIG_DIR"] == "/explicit"  # an operator's choice wins
    assert os_service.systemd_unit() == "abstractgateway-fresh.service" and os_service.launchd_label() == "ai.abstractframework.gateway.fresh"
    unit = os_service.render_systemd_unit(exe_argv=["/x/abstractgateway"], host="127.0.0.1", port=8443, data_dir=reg / "d", home=reg / "home")
    assert "ExecStart=/x/abstractgateway --instance fresh serve" in unit
    assert os_service.service_file_path("linux", reg / "home", {}, linux_mechanism="systemd").name == "abstractgateway-fresh.service"
    monkeypatch.delenv(instances.INSTANCE_ENV)
    assert os_service.systemd_unit() == "abstractgateway.service"
    assert "ExecStart=/x/abstractgateway serve" in os_service.render_systemd_unit(exe_argv=["/x/abstractgateway"], host="127.0.0.1", port=8080, data_dir=reg / "d", home=reg / "home")


def test_a_named_instance_runs_its_apps_in_its_own_block(reg, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import apps_manager as am

    m = am.AppsManager(reg / "data", urlopen=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    # (conftest stubs the probe's port list; the offset it applies is what is checked here)
    assert m.port_range() == ((3100, 3199), False) and am.instance_app_offset() == 0
    monkeypatch.setenv(instances.INSTANCE_ENV, "fresh")
    assert m.port_range() == ((3300, 3399), False) and am.instance_app_offset() == 200
    assert am.APP_BY_ID["code"].default_port + am.instance_app_offset() == 3203


def test_the_console_and_about_name_the_instance(reg, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.console import gateway_console_html

    assert '<span class="shell_brand_instance"' not in gateway_console_html()
    monkeypatch.setenv(instances.INSTANCE_ENV, "fresh")
    html = gateway_console_html()
    assert "<title>AbstractGateway Console · fresh</title>" in html and 'class="shell_brand_instance"' in html
    from abstractgateway.routes.gateway import about_payload

    assert about_payload()["instance"] == "fresh"
