"""Network exposure (localhost / lan / internet) + discovered addresses.

Pins, in order: address discovery (parsers + the address list, mocked
interfaces), the mode/auth refusal matrix, `serve`'s bind resolution (CLI
override reporting, network exports and their re-derivation at the next
start), the restart-required logic, the change door (400 / 409 / 200, nothing
written on a refusal), the generic runtime-config write refusing the key, and
the routes' authorization through the real security middleware.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from abstractgateway import network_exposure as ne

pytestmark = pytest.mark.basic

MAC_IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
\tinet6 fe80::1%lo0 prefixlen 64 scopeid 0x1
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether fc:b2:14:95:9c:03
\tinet6 fe80::1c2b:3aff:fe00:1%en0 prefixlen 64 secured scopeid 0xe
\tinet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255
\tinet6 2a01:e0a:d5e:e7f0::23 prefixlen 64 autoconf secured
en5: flags=8822<BROADCAST,SMART,SIMPLEX,MULTICAST> mtu 1500
\tinet 10.9.9.9 netmask 0xffffff00
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.101.102.103 --> 100.101.102.103 netmask 0xffffffff
awdl0: flags=8943<UP,BROADCAST,RUNNING,PROMISC,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::aa:bb%awdl0 prefixlen 64 scopeid 0x10
"""

LINUX_IP = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 192.168.1.50/24 brd 192.168.1.255 scope global dynamic eth0\\       valid_lft 1d
2: eth0    inet6 fe80::1/64 scope link \\       valid_lft forever preferred_lft forever
3: wlan0    inet 10.0.0.7/24 brd 10.0.0.255 scope global wlan0\\       valid_lft 1d
"""

HW_PORTS = """\
Hardware Port: Ethernet Adapter (en3)
Device: en3
Ethernet Address: ee:51:ac:37:31:82

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: fc:b2:14:95:9c:03
"""


def _ifaces():
    return ne.parse_ifconfig(MAC_IFCONFIG)


def _discover():
    return _ifaces(), "fixture"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_parse_ifconfig_keeps_up_interfaces_and_their_addresses() -> None:
    got = {(a.interface, a.address, a.family, a.up) for a in _ifaces()}
    assert ("en0", "192.168.1.23", "ipv4", True) in got
    assert ("en0", "2a01:e0a:d5e:e7f0::23", "ipv6", True) in got
    assert ("utun4", "100.101.102.103", "ipv4", True) in got
    # en5 is not UP+RUNNING: parsed, but flagged down (never listed).
    assert ("en5", "10.9.9.9", "ipv4", False) in got
    # zone ids are stripped from link-local IPv6
    assert ("lo0", "fe80::1", "ipv6", True) in got


def test_parse_ip_addr_linux() -> None:
    got = {(a.interface, a.address, a.family) for a in ne.parse_ip_addr(LINUX_IP)}
    assert got == {
        ("lo", "127.0.0.1", "ipv4"),
        ("eth0", "192.168.1.50", "ipv4"),
        ("eth0", "fe80::1", "ipv6"),
        ("wlan0", "10.0.0.7", "ipv4"),
    }


def test_parse_hardware_ports_macos() -> None:
    assert ne.parse_hardware_ports(HW_PORTS) == {"en3": "Ethernet Adapter (en3)", "en0": "Wi-Fi"}


def test_address_list_localhost_bind_lists_lan_addresses_as_not_reachable() -> None:
    addrs = ne.build_addresses(
        bind_host="127.0.0.1", port=8080, interfaces=_ifaces(), labels={"en0": "Wi-Fi"}, hostname="mymac.local"
    )
    assert addrs[0] == {
        "kind": "loopback", "host": "127.0.0.1", "port": 8080, "url": "http://127.0.0.1:8080",
        "family": "ipv4", "reachable": True, "note": "this machine only",
    }
    urls = [a["url"] for a in addrs]
    # loopback / link-local / down interfaces never listed
    assert not any("fe80" in u or "10.9.9.9" in u or "[::1]" in u for u in urls)
    lan = [a for a in addrs if a["kind"] == "lan"]
    assert [a["url"] for a in lan] == [
        "http://192.168.1.23:8080",  # labelled private IPv4 first
        "http://100.101.102.103:8080",  # VPN (cgnat) after
        "http://[2a01:e0a:d5e:e7f0::23]:8080",  # IPv6 last, bracketed
    ]
    assert lan[0]["interface"] == "en0" and lan[0]["interface_label"] == "Wi-Fi"
    assert lan[1]["interface_label"] == "VPN" and lan[1]["scope"] == "cgnat"
    assert all(a["reachable"] is False for a in lan)
    assert addrs[-1]["kind"] == "hostname" and addrs[-1]["url"] == "http://mymac.local:8080"
    assert addrs[-1]["reachable"] is False


def test_address_list_wildcard_bind_reaches_ipv4_only() -> None:
    addrs = ne.build_addresses(bind_host="0.0.0.0", port=18841, interfaces=_ifaces(), labels={}, hostname=None)
    by_url = {a["url"]: a for a in addrs}
    assert by_url["http://127.0.0.1:18841"]["reachable"] is True
    assert by_url["http://192.168.1.23:18841"]["reachable"] is True
    assert by_url["http://[2a01:e0a:d5e:e7f0::23]:18841"]["reachable"] is False
    assert "IPv4 only" in by_url["http://[2a01:e0a:d5e:e7f0::23]:18841"]["note"]
    dual = ne.build_addresses(bind_host="::", port=1, interfaces=_ifaces(), labels={}, hostname=None)
    assert all(a["reachable"] for a in dual if a["kind"] == "lan")


def test_address_list_public_row_only_when_given() -> None:
    no_pub = ne.build_addresses(bind_host="0.0.0.0", port=80, interfaces=[], labels={}, hostname=None)
    assert [a["kind"] for a in no_pub] == ["loopback"]
    pub = ne.build_addresses(
        bind_host="0.0.0.0", port=80, interfaces=[], labels={}, hostname=None,
        public={"ok": True, "address": "203.0.113.9", "service": "x"},
    )
    assert pub[-1]["kind"] == "public" and pub[-1]["url"] == "http://203.0.113.9:80"
    assert "forwards TCP 80" in pub[-1]["note"]


# ---------------------------------------------------------------------------
# Mode / auth matrix
# ---------------------------------------------------------------------------


def _posture(**env: str) -> Dict:
    return ne.auth_posture(dict(env))


@pytest.mark.parametrize(
    "env, mode, ok, reason_code, will_enable",
    [
        ({}, "localhost", True, None, False),
        ({}, "lan", True, None, True),  # nothing stated: serve turns user auth on
        ({"ABSTRACTGATEWAY_USER_AUTH": "1", "ABSTRACTGATEWAY_AUTH_MODE_SOURCE": "loopback_default"}, "lan", True, None, True),
        ({"ABSTRACTGATEWAY_USER_AUTH": "1"}, "lan", True, None, False),
        ({"ABSTRACTGATEWAY_USER_AUTH": "1", "ABSTRACTGATEWAY_AUTH_TOKEN": "t" * 20}, "internet", True, None, False),
        ({"ABSTRACTGATEWAY_AUTH_TOKEN": "t" * 20}, "lan", False, "user_auth_required", False),
        ({"ABSTRACTGATEWAY_AUTH_TOKEN": "t" * 20}, "internet", False, "user_auth_required", False),
        ({"ABSTRACTGATEWAY_USER_AUTH": "0"}, "lan", False, "user_auth_required", False),
        ({"ABSTRACTGATEWAY_SECURITY": "0"}, "lan", False, "auth_disabled", False),
        ({"ABSTRACTGATEWAY_USER_AUTH": "1", "ABSTRACTGATEWAY_PROTECT_WRITE": "0"}, "internet", False, "auth_disabled", False),
        ({"ABSTRACTGATEWAY_SECURITY": "0"}, "localhost", True, None, False),
    ],
)
def test_auth_matrix(env, mode, ok, reason_code, will_enable) -> None:
    check = ne.auth_check(mode, _posture(**env))
    assert check["ok"] is ok
    assert check.get("reason_code") == reason_code
    assert bool(check.get("will_enable_user_auth")) is will_enable
    if not ok:
        assert check["fix"] and "ABSTRACTGATEWAY_" in check["fix"]


def test_our_own_exports_are_not_operator_statements() -> None:
    """A relaunched process inherits serve's network exports: they must not
    read as an explicit posture (else the next start would never re-derive)."""
    env = {
        "ABSTRACTGATEWAY_USER_AUTH": "1",
        "ABSTRACTGATEWAY_AUTH_MODE_SOURCE": "network_setting",
        ne.NETWORK_EXPORTS_ENV: "ABSTRACTGATEWAY_USER_AUTH,ABSTRACTGATEWAY_AUTH_MODE_SOURCE",
    }
    p = ne.auth_posture(env)
    assert p["explicit"] is False and p["user_auth"] is False


# ---------------------------------------------------------------------------
# serve: bind resolution
# ---------------------------------------------------------------------------


def _store(data_dir: Path, mode: str, port=None) -> None:
    from abstractgateway.runtime_config import write_network_setting

    write_network_setting(data_dir, mode=mode, port=port, internet_acknowledged=None, actor="test")


def _prep(tmp_path, env, cli_host=None, cli_port=None):
    return ne.prepare_serve_bind(
        cli_host=cli_host, cli_port=cli_port, data_dir=tmp_path, env=env,
        discover=_discover, hostname_fn=lambda: "mymac.local",
    )


def test_serve_without_setting_keeps_the_historical_default(tmp_path) -> None:
    env: Dict[str, str] = {}
    b = _prep(tmp_path, env)
    assert (b.host, b.port, b.host_source, b.port_source) == ("127.0.0.1", 8080, "default", "default")
    env2 = {"ABSTRACTGATEWAY_USER_AUTH": "1"}
    assert _prep(tmp_path, env2).host == "0.0.0.0"  # configured deployments bind as before
    assert env[ne.BIND_SOURCE_ENV] == "host=default;port=default"
    assert ne.NETWORK_EXPORTS_ENV not in env


def test_serve_lan_setting_binds_all_and_exports_auth_and_origins(tmp_path) -> None:
    _store(tmp_path, "lan", 18841)
    env: Dict[str, str] = {}
    b = _prep(tmp_path, env)
    assert (b.host, b.port, b.host_source, b.port_source) == ("0.0.0.0", 18841, "setting", "setting")
    assert env["ABSTRACTGATEWAY_USER_AUTH"] == "1"
    assert env["ABSTRACTGATEWAY_AUTH_MODE_SOURCE"] == "network_setting"
    origins = env["ABSTRACTGATEWAY_ALLOWED_ORIGINS"].split(",")
    assert origins[:2] == ["http://localhost:*", "http://127.0.0.1:*"]
    assert "http://192.168.1.23:18841" in origins and "http://mymac.local:18841" in origins
    assert set(env[ne.NETWORK_EXPORTS_ENV].split(",")) == {
        "ABSTRACTGATEWAY_USER_AUTH", "ABSTRACTGATEWAY_AUTH_MODE_SOURCE", "ABSTRACTGATEWAY_ALLOWED_ORIGINS"
    }
    assert any("user auth enabled because network exposure 'lan'" in m for m in b.messages)


def test_serve_never_overrides_operator_origins_or_auth(tmp_path) -> None:
    _store(tmp_path, "lan")
    env = {"ABSTRACTGATEWAY_USER_AUTH": "1", "ABSTRACTGATEWAY_ALLOWED_ORIGINS": "https://mine.example"}
    b = _prep(tmp_path, env)
    assert b.host == "0.0.0.0" and b.exports == {}
    assert env["ABSTRACTGATEWAY_ALLOWED_ORIGINS"] == "https://mine.example"


def test_serve_next_start_forgets_previous_exports(tmp_path) -> None:
    """lan -> localhost across a relaunch (same environment inherited)."""
    _store(tmp_path, "lan")
    env: Dict[str, str] = {}
    _prep(tmp_path, env)
    assert env["ABSTRACTGATEWAY_USER_AUTH"] == "1"
    _store(tmp_path, "localhost")
    b = _prep(tmp_path, env)
    assert b.host == "127.0.0.1"
    for name in ("ABSTRACTGATEWAY_USER_AUTH", "ABSTRACTGATEWAY_ALLOWED_ORIGINS", "ABSTRACTGATEWAY_AUTH_MODE_SOURCE",
                 ne.NETWORK_EXPORTS_ENV):
        assert name not in env, name


def test_serve_cli_flags_win_and_are_reported(tmp_path) -> None:
    _store(tmp_path, "lan", 18841)
    env: Dict[str, str] = {}
    b = _prep(tmp_path, env, cli_host="127.0.0.1", cli_port=18842)
    assert (b.host, b.port, b.host_source, b.port_source) == ("127.0.0.1", 18842, "cli", "cli")
    assert env[ne.BIND_SOURCE_ENV] == "host=cli;port=cli"
    assert any("overrides it (reported as overridden_by_cli)" in m for m in b.messages)
    assert any("--port 18842" in m for m in b.messages)
    assert "ABSTRACTGATEWAY_USER_AUTH" not in env  # no network exports on a CLI bind


def test_serve_lan_setting_with_unmet_auth_falls_back_to_loopback_loudly(tmp_path) -> None:
    _store(tmp_path, "lan")
    env = {"ABSTRACTGATEWAY_AUTH_TOKEN": "a-strong-scratch-token-123"}
    b = _prep(tmp_path, env)
    assert b.host == "127.0.0.1" and b.blocked_reason
    assert any(m.startswith("[ERROR] Network exposure 'lan' cannot be applied") for m in b.messages)
    assert "ABSTRACTGATEWAY_USER_AUTH" not in env


# ---------------------------------------------------------------------------
# Status: restart-required logic
# ---------------------------------------------------------------------------


def _status(tmp_path, env, **kw):
    return ne.network_status(tmp_path, env=env, discover=_discover, hostname_fn=lambda: None, **kw)


def _running_env(host, port, source="host=setting;port=setting", **extra):
    from abstractgateway.runtime_config import BIND_HOST_ENV

    return {BIND_HOST_ENV: host, ne.BIND_PORT_ENV: str(port), ne.BIND_SOURCE_ENV: source, **extra}


def test_status_restart_required_until_the_bind_matches(tmp_path) -> None:
    _store(tmp_path, "localhost", 18841)
    env = _running_env("127.0.0.1", 18841, ABSTRACTGATEWAY_USER_AUTH="1")
    s = _status(tmp_path, env)
    assert s["schema"] == "gateway_network_v1"
    assert s["restart_required"] is False and s["effective"]["mode"] == "localhost"
    assert s["copy_hint"] == "http://127.0.0.1:18841"

    _store(tmp_path, "lan")
    s = _status(tmp_path, env)
    assert s["restart_required"] is True
    assert s["configured"]["mode"] == "lan" and s["effective"]["bind_host"] == "127.0.0.1"
    assert s["warnings"][0].startswith("Restart the gateway to apply 'lan'")
    assert s["restart"]["applies"] is True

    # port change alone also needs a restart
    _store(tmp_path, "lan", 18842)
    s = _status(tmp_path, _running_env("0.0.0.0", 18841, ABSTRACTGATEWAY_USER_AUTH="1"))
    assert s["restart_required"] is True

    s = _status(tmp_path, _running_env("0.0.0.0", 18842, ABSTRACTGATEWAY_USER_AUTH="1"))
    assert s["restart_required"] is False and s["effective"]["mode"] == "lan"
    assert s["copy_hint"] == "http://192.168.1.23:18842"  # the LAN URL, not loopback


def test_status_reports_cli_override_and_a_restart_that_cannot_apply(tmp_path) -> None:
    _store(tmp_path, "lan", 18841)
    s = _status(tmp_path, _running_env("127.0.0.1", 18841, "host=cli;port=setting", ABSTRACTGATEWAY_USER_AUTH="1"))
    assert s["effective"]["overridden_by_cli"] is True
    assert s["restart_required"] is True
    assert s["restart"]["applies"] is False and "--host/--port" in s["restart"]["reason"]
    # The way out is named; the pre-mission-T claim that the login service pins both is gone.
    assert "`abstractgateway service enable` rewrites a pinned registration" in s["restart"]["reason"]
    assert "passes both today" not in s["restart"]["reason"]


def test_status_modes_carry_the_refusal_for_ui_greying(tmp_path) -> None:
    s = _status(tmp_path, _running_env("127.0.0.1", 8080, "host=default;port=default",
                                       ABSTRACTGATEWAY_AUTH_TOKEN="a-strong-scratch-token-123"))
    modes = {m["id"]: m for m in s["modes"]}
    # Nothing stored + a token configured = the historical 0.0.0.0 default:
    # it reads as `lan` (source default), and says what `lan` is missing.
    assert s["configured"]["source"] == "default" and modes["lan"]["selected"] is True
    assert modes["localhost"]["allowed"] is True
    assert modes["lan"]["allowed"] is False and "started with accounts (user auth) off" in modes["lan"]["fix"]
    # Operator rule 2026-09-24: a fix describes the state, never "set <ENV>".
    assert not re.search(r"\b(set|export|unset)\s+ABSTRACT", modes["lan"]["fix"], re.I)
    assert s["auth"]["ok_for_mode"] is False and s["auth"]["fix"]
    assert modes["internet"]["requires_acknowledgement"] is True


def test_status_public_lookup_is_opt_in_and_internet_only(tmp_path) -> None:
    calls = []

    def fake_public():
        calls.append(1)
        return {"ok": True, "address": "203.0.113.9", "service": "fake"}

    env = _running_env("0.0.0.0", 8080, ABSTRACTGATEWAY_USER_AUTH="1")
    _store(tmp_path, "lan")
    _status(tmp_path, env, public_fn=fake_public)
    s = _status(tmp_path, env, public_fn=fake_public, lookup_public=True)
    assert calls == [] and "internet" in s["discovery"]["public_note"]
    _store(tmp_path, "internet")
    s = _status(tmp_path, env, public_fn=fake_public, lookup_public=True, is_admin=False)
    assert calls == [] and "admin" in s["discovery"]["public_note"]
    s = _status(tmp_path, env, public_fn=fake_public, lookup_public=True)
    assert calls == [1] and s["addresses"][-1]["url"] == "http://203.0.113.9:8080"


# ---------------------------------------------------------------------------
# The change door
# ---------------------------------------------------------------------------


def _apply(tmp_path, env, **kw):
    return ne.apply_network_change(
        tmp_path, actor="t", env=env, status_kwargs={"discover": _discover, "hostname_fn": lambda: None}, **kw
    )


def _stored(tmp_path) -> dict:
    p = tmp_path / "config" / "runtime_config.json"
    return json.loads(p.read_text()).get("network", {}) if p.exists() else {}


def test_change_refusals_write_nothing(tmp_path) -> None:
    token_only = _running_env("127.0.0.1", 8080, "host=default;port=default",
                              ABSTRACTGATEWAY_AUTH_TOKEN="a-strong-scratch-token-123")
    st, body = _apply(tmp_path, token_only, mode="lan")
    assert st == 409 and body["reason_code"] == "user_auth_required" and body["fix"]
    assert _stored(tmp_path) == {}

    users = _running_env("127.0.0.1", 8080, "host=default;port=default", ABSTRACTGATEWAY_USER_AUTH="1")
    st, body = _apply(tmp_path, users, mode="internet")
    assert st == 409 and body["reason_code"] == "acknowledgement_required"
    assert any("TLS" in w for w in body["warnings"])
    assert _stored(tmp_path) == {}

    for bad in ({"mode": "everywhere"}, {"mode": "lan", "port": 0}, {"mode": "lan", "port": "x"}):
        st, body = _apply(tmp_path, users, **bad)
        assert st == 400 and body["ok"] is False
    assert _stored(tmp_path) == {}


def test_change_stores_and_reports_restart_required(tmp_path) -> None:
    users = _running_env("127.0.0.1", 8080, "host=default;port=default", ABSTRACTGATEWAY_USER_AUTH="1")
    st, body = _apply(tmp_path, users, mode="lan", port=18841)
    assert st == 200 and body["ok"] is True and body["restart_required"] is True
    assert body["configured"]["mode"] == "lan" and body["configured"]["port"] == 18841
    assert _stored(tmp_path) == {"exposure": "lan", "port": 18841}

    st, body = _apply(tmp_path, users, mode="internet", acknowledge_internet=True)
    assert st == 200
    net = _stored(tmp_path)
    assert net["exposure"] == "internet" and net["port"] == 18841  # port kept
    assert net["internet_acknowledged"]["by"] == "t"
    assert body["configured"]["internet_acknowledged"]["by"] == "t"

    st, _ = _apply(tmp_path, users, mode="localhost")
    assert st == 200 and "internet_acknowledged" not in _stored(tmp_path)


def test_generic_runtime_config_write_refuses_network_keys(tmp_path) -> None:
    from abstractgateway.runtime_config import RuntimeConfigError, read_runtime_config, write_runtime_config

    for key in ("network", "network_exposure", "network_port"):
        with pytest.raises(RuntimeConfigError, match="POST /api/gateway/network"):
            write_runtime_config(tmp_path, {key: "lan"}, actor="t")
    assert read_runtime_config(tmp_path)["network"]["source"] == "default"


# ---------------------------------------------------------------------------
# Routes through the real middleware
# ---------------------------------------------------------------------------


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setattr(ne, "discover_interfaces", _discover)
    monkeypatch.setattr(ne, "bonjour_hostname", lambda: None)

    def _no_outbound():
        raise AssertionError("no outbound call in tests")

    monkeypatch.setattr(ne, "lookup_public_ip", _no_outbound)
    from abstractgateway.routes import gateway_router
    from abstractgateway.routes.network import router as network_router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    app.include_router(network_router, prefix="/api")
    app.include_router(gateway_router, prefix="/api")
    return TestClient(app)


def test_routes_read_is_user_level_writes_are_admin(tmp_path, monkeypatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        admin = {"Authorization": "Bearer admin-token"}
        created = client.post("/api/gateway/admin/users", headers=admin,
                              json={"user_id": "mallory", "tenant_id": "default", "roles": ["user"]})
        assert created.status_code == 200, created.text
        user = {"Authorization": f"Bearer {created.json()['token']}"}

        assert client.get("/api/gateway/network").status_code == 401
        r = client.get("/api/gateway/network", headers=user)
        assert r.status_code == 200 and r.json()["writable"] is False
        assert client.get("/api/gateway/network?lookup_public=1", headers=user).status_code == 403
        for path, body in (("/api/gateway/network", {"mode": "lan"}), ("/api/gateway/network/restart", {})):
            refused = client.post(path, headers=user, json=body)
            assert refused.status_code == 403, (path, refused.text)
        assert client.post("/api/gateway/network", json={"mode": "lan"}).status_code == 401

        r = client.get("/api/gateway/network", headers=admin)
        assert r.status_code == 200 and r.json()["writable"] is True
        ok = client.post("/api/gateway/network", headers=admin, json={"mode": "lan", "port": 18841})
        assert ok.status_code == 200, ok.text
        assert ok.json()["configured"]["mode"] == "lan"
        noack = client.post("/api/gateway/network", headers=admin, json={"mode": "internet"})
        assert noack.status_code == 409 and noack.json()["reason_code"] == "acknowledgement_required"
        bad = client.post("/api/gateway/network", headers=admin, json={"mode": "lan", "port": 70000})
        assert bad.status_code == 400
        extra = client.post("/api/gateway/network", headers=admin, json={"mode": "lan", "bogus": 1})
        assert extra.status_code == 422

        # Not started by `serve` here: the restart route refuses and says how.
        rs = client.post("/api/gateway/network/restart", headers=admin, json={"force": True})
        assert rs.status_code == 409 and rs.json()["refused_reason"]
