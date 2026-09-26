"""The one "caller on this machine" rule (CONTRACTS A-2, A-8).

X-Forwarded-For is believed only from a loopback peer (an app-server proxy on
this machine, which overwrites the header with the browser's real address),
exactly like uvicorn with `forwarded_allow_ips` pinned to loopback; `serve`
passes that pin explicitly.
"""
from __future__ import annotations

from typing import Dict, Optional

import pytest

from abstractgateway.security import same_machine as sm

OWN = ["127.0.0.1", "::1", "192.168.1.175"]
SESSION = "x-abstractgateway-session"
PROXY = "X-AbstractFramework-App-Proxy"


class _Req:
    def __init__(self, peer: str, headers: Optional[Dict[str, str]] = None) -> None:
        self.client = type("C", (), {"host": peer})()
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


def _local(peer: str, headers: Optional[Dict[str, str]] = None, *, trust_proxy: bool = False) -> bool:
    return sm.request_is_from_this_machine(_Req(peer, headers), addresses=OWN, trust_proxy=trust_proxy)


def test_lan_browser_through_the_loopback_proxy_is_not_this_machine() -> None:
    assert not _local("127.0.0.1", {"X-Forwarded-For": "192.168.1.50", PROXY: "code"})
    assert not _local("127.0.0.1", {"X-Forwarded-For": "192.168.1.50"})


def test_local_browser_through_the_proxy_is_this_machine() -> None:
    assert _local("127.0.0.1", {"X-Forwarded-For": "127.0.0.1", PROXY: "code"})
    assert _local("127.0.0.1", {"X-Forwarded-For": "192.168.1.175", PROXY: "code"}), "own LAN address"


def test_forwarded_headers_from_an_untrusted_peer_are_never_local() -> None:
    assert not _local("192.168.1.50", {"X-Forwarded-For": "127.0.0.1"})
    # A reverse proxy on THIS host reaching the gateway over its LAN address:
    # the peer is an own address, but the visitor behind it is not local.
    for h in ("X-Forwarded-For", "Forwarded", "X-Real-IP", "X-Forwarded-Host"):
        assert not _local("192.168.1.175", {h: "203.0.113.9"}), h


def test_direct_requests_use_the_peer() -> None:
    assert _local("127.0.0.1") and _local("192.168.1.175")
    assert not _local("192.168.1.50") and not _local("testclient")


def test_unreadable_proxies_are_never_this_machine() -> None:
    for h in ("Forwarded", "X-Forwarded-Host", "X-Real-IP"):
        assert not _local("127.0.0.1", {h: "127.0.0.1"}), h


def test_app_proxy_request_without_forwarded_for_is_not_this_machine() -> None:
    """A-8 (1), keyed on the app-proxy marker: a proxy that drops
    X-Forwarded-For must not make a LAN browser look local."""
    assert not _local("127.0.0.1", {PROXY: "code"})


def test_a_native_client_with_a_session_header_stays_local() -> None:
    """The Assistant on this machine sends the session header directly."""
    assert _local("127.0.0.1", {SESSION: "s"})
    assert _local("127.0.0.1", {SESSION: "s"}, trust_proxy=True)


def test_app_proxy_requests_are_never_local_behind_a_trusted_reverse_proxy() -> None:
    """A-8 (2): in trust-proxy mode only direct requests use the derived peer."""
    assert not _local("127.0.0.1", {"X-Forwarded-For": "127.0.0.1", PROXY: "code"}, trust_proxy=True)
    assert _local("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}, trust_proxy=True), "a direct console request"


def test_trust_proxy_mode_reads_the_network_setting(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ABSTRACTGATEWAY_TRUST_PROXY", raising=False)
    monkeypatch.delenv("ABSTRACTFLOW_GATEWAY_TRUST_PROXY", raising=False)
    assert sm.trust_proxy_mode() is False
    monkeypatch.setenv("ABSTRACTGATEWAY_TRUST_PROXY", "1")
    assert sm.trust_proxy_mode() is True, "legacy environment when nothing is stored"
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "runtime_config.json").write_text(json.dumps({"network": {"trust_proxy": False}}))
    assert sm.trust_proxy_mode() is False, "the stored setting wins over the environment"
    (tmp_path / "config" / "runtime_config.json").write_text(json.dumps({"network": {"trust_proxy": True}}))
    monkeypatch.delenv("ABSTRACTGATEWAY_TRUST_PROXY")
    assert sm.trust_proxy_mode() is True


def test_serve_pins_forwarded_allow_ips_to_loopback(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`serve` hands uvicorn forwarded_allow_ips = loopback explicitly (an
    environment FORWARDED_ALLOW_IPS=* is ignored) and --no-tray keeps the
    tray off."""
    from abstractgateway import cli

    seen: dict = {}

    def fake_serve(*, uvicorn, args, run_kwargs, argv):
        seen.update(run_kwargs)
        seen["no_tray"] = args.no_tray

    monkeypatch.setattr(cli, "_serve_with_host_controls", fake_serve)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    try:
        cli.main(["serve", "--no-tray", "--port", "18899", "--data-dir", str(tmp_path / "data")])
    except SystemExit:
        pass
    assert seen.get("forwarded_allow_ips") == "127.0.0.1,::1" and seen.get("proxy_headers") is True
    assert seen.get("no_tray") is True


def test_no_tray_flag_decision() -> None:
    from abstractgateway.tray_supervisor import tray_decision

    d = tray_decision(no_tray=True, dependencies=(True, None), probes={})
    assert d.start is False and d.reason == "no_tray_flag"


@pytest.mark.parametrize("stored,env,want", [
    (None, "1", True),    # nothing saved: the legacy environment decides
    (False, "1", False),  # the saved switch wins over the environment
    (True, "0", True),
    (None, None, False),
])
def test_ip_attribution_and_the_same_machine_rule_share_one_trust_proxy_answer(
    tmp_path, monkeypatch: pytest.MonkeyPatch, stored, env, want
) -> None:
    """One resolver (network_exposure.resolve_trust_proxy): the middleware's
    client IP (sign-in lockouts, audit) and the same-machine rule agree."""
    import json

    from abstractgateway import network_exposure as ne
    from abstractgateway.security import same_machine as sm
    from abstractgateway.security.gateway_security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    if stored is not None:
        (data / "config" / "runtime_config.json").write_text(json.dumps({"network": {"trust_proxy": stored}}))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    if env is None:
        monkeypatch.delenv("ABSTRACTGATEWAY_TRUST_PROXY", raising=False)
    else:
        monkeypatch.setenv("ABSTRACTGATEWAY_TRUST_PROXY", env)
    monkeypatch.delenv("ABSTRACTFLOW_GATEWAY_TRUST_PROXY", raising=False)
    ne._LIVE_CACHE.update({"key": None, "value": None})

    mw = GatewaySecurityMiddleware(lambda *_: None, policy=load_gateway_auth_policy_from_env())
    scope = {"type": "http", "client": ("10.0.0.9", 5000), "headers": [(b"x-forwarded-for", b"203.0.113.7")]}
    assert mw._client_ip(scope) == ("203.0.113.7" if want else "10.0.0.9")
    assert sm.trust_proxy_mode() is want
    assert ne.trust_proxy_now()["value"] is want
