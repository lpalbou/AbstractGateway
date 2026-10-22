"""Deterministic OAuth approval/cancellation ordering without provider traffic."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
import time

import pytest

from abstractgateway.mail import accounts


pytestmark = pytest.mark.basic


@pytest.mark.parametrize("kind", ["device", "loopback"])
def test_cancel_during_approval_preserves_existing_mailbox(monkeypatch, kind):
    plane = SimpleNamespace(key="oauth-cancel-plane", tenant_id="default", user_id="owner")
    polling, release = Event(), Event()
    tokens = SimpleNamespace(refresh_token="new-refresh", access_token="new-access", expires_at=time.time() + 3600)

    def approve(*args, **kwargs):
        polling.set()
        assert release.wait(5), "approval was never released"
        return tokens

    stored = {"account": "existing-account", "secret": "existing-secret"}
    before = dict(stored)

    def connect(account, secret, **kwargs):
        stored.update(account=account, secret=secret)

    monkeypatch.setattr(accounts, "account_store", lambda _: SimpleNamespace(connect=connect))
    monkeypatch.setattr(accounts, "registered_address", lambda _: "owner@example.test")
    monkeypatch.setattr(accounts, "set_address_if_empty", lambda *a, **k: None)
    monkeypatch.setattr(accounts, "rebind_live_runtime", lambda *a: None)
    monkeypatch.setattr(accounts, "audit_email_event", lambda *a, **k: None)
    monkeypatch.setattr(accounts, "public_status", lambda _: {})
    entry = {
        "plane_key": plane.key, "kind": kind, "client_secret": "", "account": SimpleNamespace(address="new@example.test", oauth=None),
        "client": SimpleNamespace(poll_device_once=approve),
        "device": SimpleNamespace(expires_at=time.time() + 60),
        "loopback": SimpleNamespace(wait=approve, finish=lambda **k: tokens, close=lambda: None),
    }
    monkeypatch.setattr(accounts, "_FLOWS", {"flow": entry})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(accounts.oauth_finish, plane, "flow")
        try:
            assert polling.wait(5), "finish did not reach provider approval"
            assert accounts.oauth_cancel(plane, "flow") == {"ok": True, "cancelled": True}
        finally:
            release.set()
        with pytest.raises(accounts.EmailOAuthFailed, match="cancelled or expired"):
            future.result(timeout=5)
    assert stored == before


def test_cancel_reports_false_after_finish_claims_commit(monkeypatch):
    plane = SimpleNamespace(key="oauth-committing-plane", tenant_id="default", user_id="owner")
    committing, release = Event(), Event()
    tokens = SimpleNamespace(refresh_token="refresh", access_token="access", expires_at=time.time() + 3600)
    writes = []

    def connect(account, secret, **kwargs):
        committing.set()
        assert release.wait(5), "commit was never released"
        writes.append(account.address)

    monkeypatch.setattr(accounts, "account_store", lambda _: SimpleNamespace(connect=connect))
    monkeypatch.setattr(accounts, "registered_address", lambda _: "owner@example.test")
    monkeypatch.setattr(accounts, "set_address_if_empty", lambda *a, **k: None)
    monkeypatch.setattr(accounts, "rebind_live_runtime", lambda *a: None)
    monkeypatch.setattr(accounts, "audit_email_event", lambda *a, **k: None)
    monkeypatch.setattr(accounts, "public_status", lambda _: {"configured": True})
    entry = {
        "plane_key": plane.key, "kind": "device", "client_secret": "", "account": SimpleNamespace(address="new@example.test", oauth=None),
        "client": SimpleNamespace(poll_device_once=lambda _: tokens),
        "device": SimpleNamespace(expires_at=time.time() + 60),
    }
    monkeypatch.setattr(accounts, "_FLOWS", {"flow": entry})
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(accounts.oauth_finish, plane, "flow")
        try:
            assert committing.wait(5), "finish did not claim its commit"
            assert accounts.oauth_cancel(plane, "flow") == {"ok": True, "cancelled": False}
            with pytest.raises(accounts.EmailOAuthFailed, match="unknown or expired"):
                accounts.oauth_finish(plane, "flow")
        finally:
            release.set()
        assert future.result(timeout=5)["configured"] is True
    assert writes == ["new@example.test"]
