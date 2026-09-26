"""Reverse proxy settings (mission Z): allowed browser origins + trust proxy.

Operator 2026-09-24: "i explicitly told you i don't like env vars. most should
be something one can configure from the consoles (wui+tui)". Pins, in order:
origin validation, the change door (both fields, 400 naming every bad origin,
nothing written on a refusal), the status payload (value / source /
overridden_by_env / effective / applies), live application through the REAL
security middleware (no restart: the next request sees the change), the env
override, admin-only writes and the audit-log lines, the warnings' wording
(never an env instruction), the run record the CLI reads, the CLI flags, and
the browser-apps settings (`apps.*` runtime-config keys).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from abstractgateway import network_exposure as ne

pytestmark = pytest.mark.basic

ENV_INSTRUCTION = re.compile(r"\b(set|export|unset)\s+(\$?ABSTRACT|[A-Z]+_[A-Z_]+=)", re.I)


def _discover():
    return [ne.IfaceAddr("en0", "192.168.1.23", "ipv4", True)], "fixture"


def _stored(data_dir: Path) -> dict:
    p = data_dir / "config" / "runtime_config.json"
    return json.loads(p.read_text()).get("network", {}) if p.exists() else {}


def _apply(data_dir: Path, env: Dict[str, str], **kw):
    return ne.apply_network_change(
        data_dir, actor="t", env=env, status_kwargs={"discover": _discover, "hostname_fn": lambda: None}, **kw
    )


def _status(data_dir: Path, env: Dict[str, str], **kw):
    return ne.network_status(data_dir, env=env, discover=_discover, hostname_fn=lambda: None, **kw)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,want",
    [
        ("https://gateway.example.com", "https://gateway.example.com"),
        ("HTTPS://Gateway.Example.COM", "https://gateway.example.com"),
        ("https://gateway.example.com:443", "https://gateway.example.com"),  # a browser never sends :443
        ("http://gateway.local:80", "http://gateway.local"),
        ("http://192.168.1.23:8080", "http://192.168.1.23:8080"),
        ("http://[fd00::1]:8080", "http://[fd00::1]:8080"),
        ("https://*.ngrok-free.app", "https://*.ngrok-free.app"),
        ("http://localhost:*", "http://localhost:*"),
        ("*", "*"),
    ],
)
def test_origin_normalization(raw, want) -> None:
    assert ne.normalize_origin(raw) == want


@pytest.mark.parametrize(
    "raw,needle",
    [
        ("https://gateway.example.com/", "no trailing slash"),
        ("https://gateway.example.com/console", "no path"),
        ("https://gateway.example.com?x=1", "no path"),
        ("gateway.example.com", "scheme://host"),
        ("ftp://gateway.example.com", "only http:// and https://"),
        ("https://user:pw@gateway.example.com", "no user name"),
        ("https://gate way.example.com", "no spaces"),
        ("https://gateway.example.com:99999", "port must be"),
        ("https://exa_mple.com", "not a valid host name"),
        ("http://fd00::1:8080", "IPv6 host in brackets"),
        ("", "cannot be empty"),
    ],
)
def test_origin_refusals_say_why(raw, needle) -> None:
    with pytest.raises(ne.NetworkSettingError, match=re.escape(needle)):
        ne.normalize_origin(raw)


def test_validate_origins_names_every_bad_entry_and_dedupes() -> None:
    assert ne.validate_origins(["https://a.example", "HTTPS://a.example:443", "https://b.example"]) == [
        "https://a.example",
        "https://b.example",
    ]
    assert ne.validate_origins("https://a.example, https://b.example") == ["https://a.example", "https://b.example"]
    with pytest.raises(ne.OriginsError) as info:
        ne.validate_origins(["https://ok.example", "https://bad.example/", "nope"])
    assert [e["value"] for e in info.value.errors] == ["https://bad.example/", "nope"]


def test_wildcards_and_plain_http_are_flagged() -> None:
    w = ne.origin_warnings(["*", "https://*.ngrok-free.app", "http://gateway.example.com", "http://localhost:8080"])
    assert any("ANY website" in x for x in w)
    assert any("pattern" in x and "ngrok" in x for x in w)
    assert any("plain http" in x and "gateway.example.com" in x for x in w)
    assert not any("localhost:8080" in x for x in w)  # loopback http is fine


# ---------------------------------------------------------------------------
# The change door
# ---------------------------------------------------------------------------


def test_door_stores_both_fields_without_touching_the_mode(tmp_path) -> None:
    st, body = _apply(tmp_path, {}, allowed_origins=["https://gateway.example.com"], trust_proxy=True)
    assert st == 200, body
    assert _stored(tmp_path) == {"allowed_origins": ["https://gateway.example.com"], "trust_proxy": True}
    rp = body["reverse_proxy"]
    assert rp["allowed_origins"]["value"] == ["https://gateway.example.com"]
    assert rp["allowed_origins"]["source"] == "setting" and rp["allowed_origins"]["applies"] == "live"
    assert rp["allowed_origins"]["effective"] == ["http://localhost:*", "http://127.0.0.1:*", "https://gateway.example.com"]
    assert rp["trust_proxy"] == {**rp["trust_proxy"], "value": True, "source": "setting", "effective": True,
                                 "overridden_by_env": False, "applies": "live"}
    assert body["changed"]["allowed_origins"] == {"from": [], "to": ["https://gateway.example.com"], "applies": "live"}
    assert body["changed"]["trust_proxy"]["applies"] == "live"
    assert "mode" not in body["changed"] and body["restart_required"] is False
    # Clearing: [] clears the list, off turns trust off.
    st, body = _apply(tmp_path, {}, allowed_origins=[], trust_proxy="off")
    assert st == 200 and _stored(tmp_path) == {"trust_proxy": False}
    assert body["reverse_proxy"]["allowed_origins"]["source"] == "default"


def test_door_refuses_invalid_input_and_writes_nothing(tmp_path) -> None:
    st, body = _apply(tmp_path, {}, allowed_origins=["https://ok.example", "https://bad.example/x"], trust_proxy=True)
    assert st == 400 and body["reason_code"] == "invalid_origins" and body["field"] == "allowed_origins"
    assert body["errors"] == [{"value": "https://bad.example/x", "error": "no path: an origin is scheme://host[:port] only (write https://bad.example)"}]
    assert _stored(tmp_path) == {}  # trust_proxy was valid but NOTHING lands
    st, body = _apply(tmp_path, {}, trust_proxy="maybe")
    assert st == 400 and "on or off" in body["refused_reason"]
    st, body = _apply(tmp_path, {})
    assert st == 400 and "nothing to change" in body["refused_reason"]
    # A mode refusal (acknowledgement) also keeps the proxy fields out.
    users = {"ABSTRACTGATEWAY_USER_AUTH": "1"}
    st, body = _apply(tmp_path, users, mode="internet", allowed_origins=["https://gateway.example.com"])
    assert st == 409 and _stored(tmp_path) == {}
    assert not (tmp_path / "config" / "runtime_config.json").exists()


def test_generic_runtime_config_door_refuses_the_proxy_keys(tmp_path) -> None:
    from abstractgateway.runtime_config import RuntimeConfigError, write_runtime_config

    for key in ("allowed_origins", "trust_proxy"):
        with pytest.raises(RuntimeConfigError, match="POST /api/gateway/network"):
            write_runtime_config(tmp_path, {key: "x"}, actor="t")


# ---------------------------------------------------------------------------
# Status: sources and the env override
# ---------------------------------------------------------------------------


def test_status_sources_default_setting_env(tmp_path) -> None:
    rp = _status(tmp_path, {})["reverse_proxy"]
    assert rp["allowed_origins"]["source"] == "default" and rp["trust_proxy"]["source"] == "default"
    assert rp["allowed_origins"]["effective"] == list(ne.BUILTIN_ORIGINS)

    _apply(tmp_path, {}, allowed_origins=["https://gateway.example.com"], trust_proxy=True)
    env = {"ABSTRACTGATEWAY_ALLOWED_ORIGINS": "https://pinned.example", "ABSTRACTGATEWAY_TRUST_PROXY": "0"}
    rp = _status(tmp_path, env)["reverse_proxy"]
    o, t = rp["allowed_origins"], rp["trust_proxy"]
    assert o["source"] == "env" and o["overridden_by_env"] is True
    assert o["value"] == ["https://gateway.example.com"] and o["effective"] == ["https://pinned.example"]
    assert o["env_name"] == "ABSTRACTGATEWAY_ALLOWED_ORIGINS" and "started with ABSTRACTGATEWAY_ALLOWED_ORIGINS" in o["note"]
    # Trust proxy: ONE precedence (resolve_trust_proxy): the saved switch wins
    # over the legacy launch environment, which only fills in when nothing is saved.
    assert t["source"] == "setting" and t["overridden_by_env"] is False and t["value"] is True and t["effective"] is True
    assert "wins" in t["note"]
    for note in (o["note"], t["note"]):
        assert not ENV_INSTRUCTION.search(note), note
    rp_env_only = _status(tmp_path / "fresh", {"ABSTRACTGATEWAY_TRUST_PROXY": "1"})["reverse_proxy"]["trust_proxy"]
    assert rp_env_only["source"] == "env" and rp_env_only["overridden_by_env"] is True and rp_env_only["effective"] is True


def test_serve_exported_origins_are_not_an_operator_override(tmp_path) -> None:
    """In a network mode `serve` exports the gateway's own LAN origins under
    the env name: that is the gateway's doing, never `overridden_by_env`."""
    _apply(tmp_path, {}, allowed_origins=["https://gateway.example.com"])
    env = {
        "ABSTRACTGATEWAY_ALLOWED_ORIGINS": "http://localhost:*,http://127.0.0.1:*,http://192.168.1.23:8080",
        ne.NETWORK_EXPORTS_ENV: "ABSTRACTGATEWAY_ALLOWED_ORIGINS",
    }
    o = _status(tmp_path, env)["reverse_proxy"]["allowed_origins"]
    assert o["overridden_by_env"] is False and o["source"] == "setting"
    assert o["self_origins"] == ["http://192.168.1.23:8080"]
    assert o["effective"] == ["http://localhost:*", "http://127.0.0.1:*", "http://192.168.1.23:8080", "https://gateway.example.com"]


def test_saving_trust_proxy_under_the_legacy_env_takes_effect(tmp_path) -> None:
    """The saved switch wins over ABSTRACTGATEWAY_TRUST_PROXY (one resolver)."""
    env = {"ABSTRACTGATEWAY_TRUST_PROXY": "1"}
    st, body = _apply(tmp_path, env, trust_proxy=False)
    assert st == 200 and body["changed"]["trust_proxy"]["applies"] == "live"
    assert not any(w.startswith("Saved, but not in effect:") and "trust" in w.lower() for w in body["warnings"])
    assert _status(tmp_path, env)["reverse_proxy"]["trust_proxy"]["effective"] is False


def test_saving_origins_under_an_env_override_says_it_is_not_in_effect(tmp_path) -> None:
    env = {"ABSTRACTGATEWAY_ALLOWED_ORIGINS": "https://pinned.example"}
    st, body = _apply(tmp_path, env, allowed_origins=["https://gateway.example.com"])
    assert st == 200 and body["changed"]["allowed_origins"]["applies"] == "overridden_by_env"
    assert any(w.startswith("Saved, but not in effect:") for w in body["warnings"])


def test_internet_warnings_point_at_the_controls_never_at_env_vars(tmp_path) -> None:
    for mode in ("lan", "internet"):
        for w in ne.mode_warnings(mode, 8080, lan_urls=[]):
            assert not ENV_INSTRUCTION.search(w), w
            assert "ABSTRACTGATEWAY_" not in w, w
    w = ne.mode_warnings("internet", 8080, lan_urls=[])
    assert any("under Reverse proxy below" in x and "https origin" in x for x in w)
    # Once an https origin is stored, the warning says it instead of asking.
    _apply(tmp_path, {"ABSTRACTGATEWAY_USER_AUTH": "1"}, mode="internet", acknowledge_internet=True,
           allowed_origins=["https://gateway.example.com"])
    ws = _status(tmp_path, {"ABSTRACTGATEWAY_USER_AUTH": "1"})["warnings"]
    assert any("Browsers may call this gateway from https://gateway.example.com" in x for x in ws)
    assert not any("Add your public https origin" in x for x in ws)
    for fix in (ne._FIX_USER_AUTH, ne._FIX_SECURITY_OFF):
        assert not ENV_INSTRUCTION.search(fix) and "This gateway was started with" in fix


def test_run_record_carries_the_running_gateways_proxy_env(tmp_path, monkeypatch) -> None:
    """The CLI reports the RUNNING gateway's environment, not its own shell's."""
    import os

    monkeypatch.setenv("ABSTRACTGATEWAY_TRUST_PROXY", "1")
    monkeypatch.delenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", raising=False)
    bind = ne.ServeBind(host="127.0.0.1", port=18901, host_source="setting", port_source="setting",
                        mode="localhost", mode_source="stored")
    ne.record_serve_bind(tmp_path, bind)
    rec = ne.read_run_record(tmp_path)
    assert rec["proxy_env"]["trust_proxy_env"]["value"] is True and rec["pid"] == os.getpid()
    monkeypatch.delenv("ABSTRACTGATEWAY_TRUST_PROXY")  # the CLI's shell has nothing
    t = _status(tmp_path, {}, in_process=False)["reverse_proxy"]["trust_proxy"]
    assert t["overridden_by_env"] is True and t["effective"] is True


# ---------------------------------------------------------------------------
# Live application through the real middleware + routes
# ---------------------------------------------------------------------------


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token-long-enough")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    for name in ("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "ABSTRACTGATEWAY_TRUST_PROXY", ne.NETWORK_EXPORTS_ENV):
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(ne, "discover_interfaces", _discover)
    monkeypatch.setattr(ne, "bonjour_hostname", lambda: None)
    from abstractgateway.routes import gateway_router
    from abstractgateway.routes.network import router as network_router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    app.include_router(network_router, prefix="/api")
    app.include_router(gateway_router, prefix="/api")
    return TestClient(app)


ADMIN = {"Authorization": "Bearer admin-token-long-enough"}
PUBLIC = "https://gateway.example.com"


def _audit(tmp_path: Path) -> list:
    p = tmp_path / "runtime" / "audit_log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_origins_apply_to_the_next_request_without_a_restart(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        probe = {**ADMIN, "Origin": PUBLIC}
        assert client.get("/api/gateway/network", headers=probe).status_code == 403  # origin not allowed
        r = client.post("/api/gateway/network", headers=ADMIN, json={"allowed_origins": [PUBLIC]})
        assert r.status_code == 200, r.text
        # Same app object, same middleware instance: no restart happened.
        got = client.get("/api/gateway/network", headers=probe)
        assert got.status_code == 200, got.text
        o = got.json()["reverse_proxy"]["allowed_origins"]
        assert o["value"] == [PUBLIC] and o["applies"] == "live" and got.json()["restart_required"] is False
        # A CLI write (another process, same store) applies too.
        st, _ = ne.apply_network_change(tmp_path / "runtime", allowed_origins=[], actor="cli/t", in_process=False)
        assert st == 200
        assert client.get("/api/gateway/network", headers=probe).status_code == 403


def test_env_override_keeps_stored_origins_out_of_the_middleware(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch, ABSTRACTGATEWAY_ALLOWED_ORIGINS="https://pinned.example") as client:
        r = client.post("/api/gateway/network", headers=ADMIN, json={"allowed_origins": [PUBLIC]})
        assert r.status_code == 200 and r.json()["changed"]["allowed_origins"]["applies"] == "overridden_by_env"
        assert client.get("/api/gateway/network", headers={**ADMIN, "Origin": PUBLIC}).status_code == 403
        assert client.get("/api/gateway/network", headers={**ADMIN, "Origin": "https://pinned.example"}).status_code == 200


def test_trust_proxy_applies_live_to_the_client_address(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        xff = {**ADMIN, "X-Forwarded-For": "203.0.113.77"}
        assert client.post("/api/gateway/network", headers=xff, json={"trust_proxy": True}).status_code == 200
        # The POST that turned it on was attributed to the socket peer...
        first = _audit(tmp_path)[-1]
        assert first["ip"] == "testclient"
        # ...the next request to the forwarded address.
        assert client.post("/api/gateway/network", headers=xff, json={"trust_proxy": False}).status_code == 200
        second = _audit(tmp_path)[-1]
        assert second["ip"] == "203.0.113.77"
        # Off again: the socket peer.
        assert client.post("/api/gateway/network", headers=xff, json={"trust_proxy": False}).status_code == 200
        assert _audit(tmp_path)[-1]["ip"] == "testclient"


def test_writes_are_admin_only_and_audit_logged(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        created = client.post("/api/gateway/admin/users", headers=ADMIN,
                              json={"user_id": "mallory", "tenant_id": "default", "roles": ["user"]})
        assert created.status_code == 200, created.text
        user = {"Authorization": f"Bearer {created.json()['token']}"}
        for body in ({"allowed_origins": [PUBLIC]}, {"trust_proxy": True}):
            assert client.post("/api/gateway/network", json=body).status_code == 401
            assert client.post("/api/gateway/network", headers=user, json=body).status_code == 403
        assert _stored(tmp_path / "runtime") == {}
        # A non-admin reads the posture (read-only).
        assert client.get("/api/gateway/network", headers=user).json()["writable"] is False
        # Strict bool: "yes" is not a boolean on the wire.
        assert client.post("/api/gateway/network", headers=ADMIN, json={"trust_proxy": "yes"}).status_code == 422
        bad = client.post("/api/gateway/network", headers=ADMIN, json={"allowed_origins": ["https://x.example/"]})
        assert bad.status_code == 400 and bad.json()["errors"][0]["value"] == "https://x.example/"
        ok = client.post("/api/gateway/network", headers=ADMIN, json={"allowed_origins": [PUBLIC], "trust_proxy": True})
        assert ok.status_code == 200
    lines = [e for e in _audit(tmp_path) if e.get("path") == "/api/gateway/network" and e.get("method") == "POST"]
    refused = [e for e in lines if e["status"] == 400]
    assert refused and refused[-1]["setting_change"]["ok"] is False
    assert refused[-1]["setting_change"]["refused"]["reason_code"] == "invalid_origins"
    done = [e for e in lines if e["status"] == 200][-1]
    sc = done["setting_change"]
    assert sc["setting"] == "network" and sc["ok"] is True and sc["actor"]
    assert sc["changed"]["allowed_origins"]["to"] == [PUBLIC] and sc["changed"]["trust_proxy"]["to"] is True
    assert done["principal_user_id"]
    denied = [e for e in lines if e["status"] == 403]
    assert denied and "setting_change" not in denied[-1]  # refused before the route ran


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_network_set_flags_and_show(tmp_path, capsys) -> None:
    args = SimpleNamespace(network_cmd="set", mode=None, port=None, acknowledge_internet=False,
                           allowed_origins="https://a.example,https://b.example:8443", trust_proxy="on",
                           json=False, data_dir=str(tmp_path))
    assert ne.run_network_command(args) == 0
    out = capsys.readouterr().out
    assert "changed: allowed_origins" in out and "applies now" in out
    assert _stored(tmp_path) == {"allowed_origins": ["https://a.example", "https://b.example:8443"], "trust_proxy": True}
    bad = SimpleNamespace(**{**vars(args), "allowed_origins": "https://a.example/"})
    assert ne.run_network_command(bad) == 1
    assert "no trailing slash" in capsys.readouterr().err
    show = SimpleNamespace(network_cmd="show", json=True, data_dir=str(tmp_path), public=False)
    assert ne.run_network_command(show) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reverse_proxy"]["trust_proxy"]["value"] is True


def test_cli_parser_accepts_the_new_flags() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    ne.add_network_subparser(sub)
    a = parser.parse_args(["network", "set", "--allowed-origins", "https://a.example", "--trust-proxy", "off"])
    assert a.mode is None and a.allowed_origins == "https://a.example" and a.trust_proxy == "off"
    a = parser.parse_args(["network", "set", "lan", "--port", "18901"])
    assert a.mode == "lan" and a.allowed_origins is None and a.trust_proxy is None
    assert parser.parse_args(["network", "show"]).network_cmd == "show"
    with pytest.raises(SystemExit):
        parser.parse_args(["network", "set", "--trust-proxy", "maybe"])


# ---------------------------------------------------------------------------
# Browser apps settings (apps.*)
# ---------------------------------------------------------------------------


def test_apps_settings_registry_and_door(tmp_path, monkeypatch) -> None:
    from abstractgateway.runtime_config import (
        APPS_SETTINGS,
        RuntimeConfigError,
        read_runtime_config,
        resolve_apps_setting,
        write_runtime_config,
    )

    for row in APPS_SETTINGS:
        monkeypatch.delenv(row["env"], raising=False)
        assert row["label"] and row["help"] and row["key"] == f"apps.{row['name']}"
    assert {r["name"] for r in APPS_SETTINGS} == {"node", "ports", "host", "npm_registry", "pypi_url"}
    apps = read_runtime_config(tmp_path)["apps"]
    assert apps["host"]["value"] == "127.0.0.1" and apps["host"]["source"] == "default"

    out = write_runtime_config(tmp_path, {"apps.host": "0.0.0.0", "apps.ports": "3200 - 3299",
                                          "apps": {"npm_registry": "https://npm.example.com/"}}, actor="t")
    assert out["applied"] == {"apps.host": "0.0.0.0", "apps.ports": "3200-3299", "apps.npm_registry": "https://npm.example.com"}
    assert resolve_apps_setting(tmp_path, "host") == {**resolve_apps_setting(tmp_path, "host"), "value": "0.0.0.0", "source": "stored"}

    # env = labeled fallback below the stored value (dm#194), shadowing reported.
    monkeypatch.setenv("ABSTRACTGATEWAY_APPS_HOST", "10.0.0.5")
    monkeypatch.setenv("ABSTRACTGATEWAY_APPS_NODE", "system")
    host = resolve_apps_setting(tmp_path, "host")
    assert host["value"] == "0.0.0.0" and host["env_shadowed"] is True
    node = resolve_apps_setting(tmp_path, "node")
    assert node["value"] == "system" and node["source"] == "env" and "a saved value replaces it" in node["note"]
    monkeypatch.setenv("ABSTRACTGATEWAY_APPS_PORTS", "not-a-range")
    write_runtime_config(tmp_path, {"apps.ports": ""}, actor="t")  # clear
    ports = resolve_apps_setting(tmp_path, "ports")
    assert ports["source"] == "default" and "ABSTRACTGATEWAY_APPS_PORTS='not-a-range'" in ports["invalid_env"]

    for bad in ({"apps.ports": "70000"}, {"apps.host": "exa mple"}, {"apps.node": "relative/node"},
                {"apps.npm_registry": "ftp://x"}, {"apps.bogus": "1"}, {"apps": "x"}):
        with pytest.raises(RuntimeConfigError):
            write_runtime_config(tmp_path, bad, actor="t")
    with pytest.raises(KeyError):
        resolve_apps_setting(tmp_path, "hots")  # a typo fails loudly, never reads as a default


def test_apps_settings_through_the_route_admin_only_and_audited(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        created = client.post("/api/gateway/admin/users", headers=ADMIN,
                              json={"user_id": "eve", "tenant_id": "default", "roles": ["user"]})
        user = {"Authorization": f"Bearer {created.json()['token']}"}
        assert client.post("/api/gateway/admin/runtime-config", headers=user, json={"apps.host": "0.0.0.0"}).status_code == 403
        r = client.post("/api/gateway/admin/runtime-config", headers=ADMIN, json={"apps.host": "0.0.0.0"})
        assert r.status_code == 200, r.text
        assert r.json()["apps"]["host"]["value"] == "0.0.0.0"
        got = client.get("/api/gateway/admin/runtime-config", headers=ADMIN).json()["apps"]["host"]
        assert got["source"] == "stored" and got["label"] == "Where apps listen"
        bad = client.post("/api/gateway/admin/runtime-config", headers=ADMIN, json={"apps.ports": "x"})
        assert bad.status_code == 400
    lines = [e for e in _audit(tmp_path) if e.get("path") == "/api/gateway/admin/runtime-config" and e.get("method") == "POST"]
    ok = [e for e in lines if e["status"] == 200][-1]["setting_change"]
    assert ok["ok"] is True and ok["applied"] == {"apps.host": "0.0.0.0"}
    refused = [e for e in lines if e["status"] == 400][-1]["setting_change"]
    assert refused["ok"] is False and refused["keys"] == ["apps.ports"]


def test_env_registry_rows_name_their_setting() -> None:
    from abstractgateway.env_registry import registry_rows

    rows = {r["name"]: r for r in registry_rows([
        "ABSTRACTGATEWAY_ALLOWED_ORIGINS", "ABSTRACTGATEWAY_TRUST_PROXY", "ABSTRACTGATEWAY_APPS_HOST",
        "ABSTRACTGATEWAY_APPS_NODE", "ABSTRACTGATEWAY_APPS_PORTS", "ABSTRACTGATEWAY_APPS_NPM_REGISTRY",
        "ABSTRACTGATEWAY_APPS_PYPI_URL",
    ])}
    assert rows["ABSTRACTGATEWAY_ALLOWED_ORIGINS"]["superseded_by"].startswith("network.allowed_origins")
    assert rows["ABSTRACTGATEWAY_TRUST_PROXY"]["superseded_by"].startswith("network.trust_proxy")
    for name in ("HOST", "NODE", "PORTS", "NPM_REGISTRY", "PYPI_URL"):
        assert rows[f"ABSTRACTGATEWAY_APPS_{name}"]["superseded_by"].startswith(f"runtime-config apps.{name.lower()}")


def test_apps_config_cli(tmp_path, capsys) -> None:
    from abstractgateway.apps_cli import run_apps_command

    base = dict(apps_cmd="config", json=False, data_dir=str(tmp_path))
    assert run_apps_command(SimpleNamespace(**base, apps_config_cmd="set", name="host", value="0.0.0.0")) == 0
    assert "apps.host" in capsys.readouterr().out
    assert run_apps_command(SimpleNamespace(**base, apps_config_cmd="set", name="ports", value="0")) == 2
    assert "refused:" in capsys.readouterr().err
    assert run_apps_command(SimpleNamespace(**{**base, "json": True}, apps_config_cmd="get", name="apps.host")) == 0
    assert json.loads(capsys.readouterr().out)["host"]["value"] == "0.0.0.0"
    assert run_apps_command(SimpleNamespace(**base, apps_config_cmd="get", name="nope")) == 1


# ---------------------------------------------------------------------------
# Terminal parity (operator: "a more technical user must continue to be able
# to do that setup from terminal too"): the web console and the TUI send the
# HTTP door (bodies pinned in console-tui/tests/headless_ui.rs), the CLI
# writes the store directly. For every setting the doors must store the SAME
# value, read back the SAME source, and refuse with the SAME sentence.
# ---------------------------------------------------------------------------


def _cli(argv, capsys):
    from abstractgateway.cli import main

    with pytest.raises(SystemExit) as info:
        main(argv)
    out = capsys.readouterr()
    return int(info.value.code or 0), out.out, out.err


def _raw_store(data_dir: Path) -> dict:
    p = data_dir / "config" / "runtime_config.json"
    return {k: v for k, v in json.loads(p.read_text()).items() if not k.startswith("_")}


@pytest.mark.parametrize(
    "http_body,cli_args,good_field",
    [
        ({"allowed_origins": ["https://gateway.example.com", "https://b.example:8443"]},
         ["--allowed-origins", "https://gateway.example.com,https://b.example:8443"], "allowed_origins"),
        ({"trust_proxy": True}, ["--trust-proxy", "on"], "trust_proxy"),
        ({"trust_proxy": False}, ["--trust-proxy", "off"], "trust_proxy"),
    ],
)
def test_three_doors_store_the_same_reverse_proxy_value(tmp_path, monkeypatch, capsys, http_body, cli_args, good_field) -> None:
    with _client(tmp_path, monkeypatch) as client:
        r = client.post("/api/gateway/network", headers=ADMIN, json=http_body)
        assert r.status_code == 200, r.text
        via_http = _raw_store(tmp_path / "runtime")
        http_source = client.get("/api/gateway/network", headers=ADMIN).json()["reverse_proxy"][good_field]["source"]
    cli_dir = tmp_path / "cli"
    code, out, err = _cli(["network", "set", *cli_args, "--data-dir", str(cli_dir)], capsys)
    assert code == 0, err
    assert _raw_store(cli_dir) == via_http
    code, out, _ = _cli(["network", "show", "--json", "--data-dir", str(cli_dir)], capsys)
    assert json.loads(out)["reverse_proxy"][good_field]["source"] == http_source == "setting"


def test_three_doors_refuse_a_bad_origin_with_the_same_sentence(tmp_path, monkeypatch, capsys) -> None:
    with _client(tmp_path, monkeypatch) as client:
        r = client.post("/api/gateway/network", headers=ADMIN, json={"allowed_origins": ["https://x.example/"]})
        assert r.status_code == 400
        sentence = r.json()["refused_reason"]
    assert "no trailing slash" in sentence
    code, _, err = _cli(["network", "set", "--allowed-origins", "https://x.example/", "--data-dir", str(tmp_path / "cli")], capsys)
    assert code == 1 and f"refused: {sentence}" in err
    assert not (tmp_path / "cli" / "config" / "runtime_config.json").exists()


@pytest.mark.parametrize(
    "name,good,bad",
    [
        ("host", "0.0.0.0", "exa mple"),
        ("ports", "3200-3299", "70000"),
        ("node", "managed", "relative/node"),
        ("npm_registry", "https://npm.example.com", "ftp://npm.example.com"),
        ("pypi_url", "https://pypi.example.com/pypi", "not a url"),
    ],
)
def test_three_doors_store_the_same_apps_value(tmp_path, monkeypatch, capsys, name, good, bad) -> None:
    with _client(tmp_path, monkeypatch) as client:
        r = client.post("/api/gateway/admin/runtime-config", headers=ADMIN, json={f"apps.{name}": good})
        assert r.status_code == 200, r.text
        via_http = _raw_store(tmp_path / "runtime")
        http_row = client.get("/api/gateway/admin/runtime-config", headers=ADMIN).json()["apps"][name]
        refused = client.post("/api/gateway/admin/runtime-config", headers=ADMIN, json={f"apps.{name}": bad})
        assert refused.status_code == 400
        sentence = refused.json()["detail"]
    cli_dir = tmp_path / "cli"
    code, _, err = _cli(["apps", "config", "set", name, good, "--data-dir", str(cli_dir)], capsys)
    assert code == 0, err
    assert _raw_store(cli_dir) == via_http
    code, out, _ = _cli(["apps", "config", "get", name, "--json", "--data-dir", str(cli_dir)], capsys)
    cli_row = json.loads(out)[name]
    assert (cli_row["value"], cli_row["source"]) == (http_row["value"], http_row["source"]) == (via_http["apps"][name], "stored")
    code, _, err = _cli(["apps", "config", "set", name, bad, "--data-dir", str(cli_dir)], capsys)
    assert code == 2 and f"refused: {sentence}" in err
    assert _raw_store(cli_dir) == via_http  # the refusal wrote nothing


# ---------------------------------------------------------------------------
# Read protection off (mission AA finding): never on a network
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["ABSTRACTGATEWAY_PROTECT_READ", "ABSTRACTFLOW_GATEWAY_PROTECT_READ"])
def test_read_protection_off_refuses_network_modes(tmp_path, name) -> None:
    env = {"ABSTRACTGATEWAY_USER_AUTH": "1", name: "0"}
    posture = ne.auth_posture(env)
    assert posture["read_protected"] is False and posture["explicit"] is True
    assert ne.auth_check("localhost", posture)["ok"] is True
    for mode in ("lan", "internet"):
        c = ne.auth_check(mode, posture)
        assert c["ok"] is False and c["reason_code"] == "auth_disabled"
        assert "This gateway was started with read protection off" in c["fix"]
        assert not ENV_INSTRUCTION.search(c["fix"])
    st, body = _apply(tmp_path, env, mode="lan")
    assert st == 409 and body["reason_code"] == "auth_disabled" and _stored(tmp_path) == {}
    # `serve` with a stored lan setting falls back to loopback, loudly.
    from abstractgateway.runtime_config import write_network_setting

    write_network_setting(tmp_path, mode="lan", port=None, internet_acknowledged=None, actor="t")
    serve_env = dict(env)
    b = ne.prepare_serve_bind(cli_host=None, cli_port=None, data_dir=tmp_path, env=serve_env,
                              discover=_discover, hostname_fn=lambda: None)
    assert b.host == "127.0.0.1" and "read protection off" in (b.blocked_reason or "")
    s = _status(tmp_path, env)
    assert {m["id"]: m["allowed"] for m in s["modes"]} == {"localhost": True, "lan": False, "internet": False}


# ---------------------------------------------------------------------------
# SEAM with apps_manager.py (owner: mission Y). Mission Z changed its READS to
# the settings door (AppsManager._setting -> runtime_config.resolve_apps_setting;
# untracked/missionZ/apps_manager_callsites.patch is the exact diff). This test
# goes RED if a later rewrite of apps_manager.py drops that change: a saved
# apps setting must never be "saved but inactive".
# ---------------------------------------------------------------------------


def test_apps_manager_reads_the_settings_door(tmp_path, monkeypatch) -> None:
    from abstractgateway.apps_manager import AppsManager
    from abstractgateway.runtime_config import write_runtime_config

    for name in ("HOST", "PORTS", "NODE", "NPM_REGISTRY", "PYPI_URL"):
        monkeypatch.delenv(f"ABSTRACTGATEWAY_APPS_{name}", raising=False)
    write_runtime_config(tmp_path, {"apps.host": "0.0.0.0", "apps.ports": "3200-3299",
                                    "apps.npm_registry": "https://npm.example.com"}, actor="t")
    m = AppsManager(tmp_path)
    assert m.bind_host == "0.0.0.0"
    assert m.port_range() == ((3200, 3299), True)
    assert m.registry_url == "https://npm.example.com"
    assert m.pypi_url == "https://pypi.org/pypi"  # default rung
    monkeypatch.setenv("ABSTRACTGATEWAY_APPS_HOST", "10.9.9.9")
    assert m.bind_host == "0.0.0.0"  # stored beats env (dm#194)
    write_runtime_config(tmp_path, {"apps.host": ""}, actor="t")
    assert m.bind_host == "10.9.9.9"  # cleared: the env fallback rung, read at use
