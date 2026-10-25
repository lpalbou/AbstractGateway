"""`GET /api/gateway/me/email/autodiscover?address=` (the "address first" mailbox form).

From an address alone: the provider, the servers, the login, how one signs in and where to make
an app password. Resolution is AbstractCore's deterministic discovery (provider table, autoconfig
and ISPDB, SRV, MX) plus `_imap._tcp` SRV; cached per domain for a day. Every network seam is a
fake here (one live test is marked `network`)."""

from __future__ import annotations

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import BOB, PASSWORDS

pytestmark = pytest.mark.integration

URL = "/api/gateway/me/email/autodiscover"

ISP_XML = b"""<?xml version="1.0"?><clientConfig version="1.1"><emailProvider id="isp.test"><domain>isp.test</domain>
<incomingServer type="imap"><hostname>mail.isp.test</hostname><port>993</port><socketType>SSL</socketType><username>%EMAILLOCALPART%</username></incomingServer>
<outgoingServer type="smtp"><hostname>smtp.isp.test</hostname><port>587</port><socketType>STARTTLS</socketType><username>%EMAILLOCALPART%</username></outgoingServer>
</emailProvider></clientConfig>"""


class FakeNet:
    def __init__(self, *, docs=None, srv=None):
        self.docs, self.srv, self.calls = dict(docs or {}), dict(srv or {}), []

    def http_get(self, url, timeout):
        self.calls.append(url)
        return self.docs.get(url)

    def resolve_srv(self, name, timeout):
        self.calls.append(name)
        return self.srv.get(name, [])

    def resolve_mx(self, domain, timeout):
        self.calls.append(f"mx:{domain}")
        return []


@pytest.fixture()
def net(monkeypatch):
    from abstractgateway.mail import accounts

    fake = FakeNet()
    monkeypatch.setattr(accounts, "AUTODISCOVER_NET", {"http_get": fake.http_get, "resolve_srv": fake.resolve_srv, "resolve_mx": fake.resolve_mx})
    monkeypatch.setattr(accounts, "_AUTODISCOVER_CACHE", {})
    return fake


@pytest.mark.parametrize(
    ("address", "provider", "name", "imap_host", "sign_in", "app_url"),
    [
        ("me@gmail.com", "google", "Gmail", "imap.gmail.com", ["oauth", "app_password"], "https://myaccount.google.com/apppasswords"),
        ("me@outlook.com", "microsoft", "Outlook / Microsoft 365", "outlook.office365.com", ["oauth"], None),
        ("me@yahoo.com", "yahoo", "Yahoo Mail", "imap.mail.yahoo.com", ["app_password"], "https://login.yahoo.com/account/security"),
        ("me@icloud.com", "icloud", "iCloud Mail", "imap.mail.me.com", ["app_password"], "https://account.apple.com/account/manage"),
        ("me@fastmail.com", "fastmail", "Fastmail", "imap.fastmail.com", ["app_password"], None),
    ],
)
def test_known_providers_need_no_network(gateway, net, address, provider, name, imap_host, sign_in, app_url) -> None:
    r = gateway["client"].get(URL, params={"address": address}, headers=gateway["alice"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["provider"], out["display_name"], out["imap"]["host"], out["sign_in"], out["source"]) == (provider, name, imap_host, sign_in, "preset")
    assert out["imap"]["security"] in ("ssl", "starttls") and isinstance(out["smtp"], dict) and out["login"] == address
    assert out.get("app_password_url") == app_url and isinstance(out["oauth_available"], bool)
    assert net.calls == []  # the provider table answers: nothing reached


def test_ispdb_answer_cached_per_domain_with_the_login_per_address(gateway, net) -> None:
    net.docs["https://autoconfig.thunderbird.net/v1.1/isp.test"] = ISP_XML
    c = gateway["client"]
    out = c.get(URL, params={"address": "ann@isp.test"}, headers=gateway["alice"]).json()
    assert (out["provider"], out["source"], out["sign_in"], out["display_name"]) == ("other", "autoconfig", ["password"], "isp.test")
    assert out["imap"] == {"host": "mail.isp.test", "port": 993, "security": "ssl"}
    assert out["smtp"] == {"host": "smtp.isp.test", "port": 587, "security": "starttls"} and out["login"] == "ann"
    assert "app_password_url" not in out
    seen = len(net.calls)
    again = c.get(URL, params={"address": "bea@isp.test"}, headers=gateway["alice"]).json()
    assert len(net.calls) == seen  # cached for the domain
    assert again["login"] == "bea" and again["imap"] == out["imap"]


def test_imap_only_from_srv_is_enough_for_a_read_only_mailbox(gateway, net) -> None:
    net.srv["_imaps._tcp.srv.test"] = [(0, 1, 993, "imap.srv.test.")]
    out = gateway["client"].get(URL, params={"address": "me@srv.test"}, headers=gateway["alice"]).json()
    assert (out["source"], out["imap"], out["smtp"]) == ("srv", {"host": "imap.srv.test", "port": 993, "security": "ssl"}, None)


def test_imap_starttls_srv_is_queried_too(gateway, net) -> None:
    net.srv["_imap._tcp.plain.test"] = [(0, 1, 143, "mx.plain.test.")]
    out = gateway["client"].get(URL, params={"address": "me@plain.test"}, headers=gateway["alice"]).json()
    assert (out["source"], out["imap"], out["smtp"]) == ("srv", {"host": "mx.plain.test", "port": 143, "security": "starttls"}, None)


def test_nothing_found_and_a_bad_address(gateway, net) -> None:
    c = gateway["client"]
    out = c.get(URL, params={"address": "me@nowhere.test"}, headers=gateway["alice"]).json()
    assert (out["source"], out["imap"]["host"], out["smtp"], out["provider"], out["sign_in"]) == ("none", "", None, "other", ["password"])
    bad = c.get(URL, params={"address": "not an address"}, headers=gateway["alice"])
    assert bad.status_code == 400 and bad.json()["detail"]["reason_code"] == "email_invalid_settings"
    assert c.get(URL, params={"address": "me@gmail.com"}).status_code == 401


def test_put_takes_an_optional_name_and_login_defaults_to_the_address(gateway, imap_bob) -> None:
    from email_fixtures import plane_of

    from abstractgateway.mail.accounts import account_store

    c = gateway["client"]
    body = {"address": BOB, "password": PASSWORDS[BOB], "name": "Work", "imap": {"host": "localhost", "port": imap_bob.port, "security": "ssl"}}
    r = c.put("/api/gateway/me/email/accounts/work", headers=gateway["alice"], json=body)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Work"
    row = c.get("/api/gateway/me/email/accounts/work", headers=gateway["alice"]).json()
    assert row["name"] == "Work"
    assert account_store(plane_of("alice").for_account("work")).settings().account.username == BOB
    too_long = c.put("/api/gateway/me/email/accounts/work2", headers=gateway["alice"], json={**body, "name": "x" * 121})
    assert too_long.status_code == 422


@pytest.mark.network("live: the Thunderbird ISPDB answers for a domain outside AbstractCore's provider table")
def test_live_ispdb_lookup(gateway, monkeypatch) -> None:
    from abstractgateway.mail import accounts

    monkeypatch.setattr(accounts, "_AUTODISCOVER_CACHE", {})
    out = gateway["client"].get(URL, params={"address": "someone@posteo.de"}, headers=gateway["alice"]).json()
    assert out["source"] in ("autoconfig", "srv") and out["imap"]["host"]
