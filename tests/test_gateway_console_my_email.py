"""Web console: the account page (framework backlog 0992, DESIGN 2026-09-30 §5/§6) — the email
address, the Mailbox card, the notification and agent-tools switches, the admin's "Mailboxes for
users" switch and the sign-in page's recovery link are served, wired to the per-user routes, and
the page's JavaScript still parses (tests/test_gateway_console.py runs the parse and the login
harness over the whole page; test_gateway_console_state_toggles.py drives the behaviour)."""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.basic


def _html() -> str:
    from abstractgateway.console import gateway_console_html

    return gateway_console_html()


def test_account_page_carries_the_fields_and_words() -> None:
    html = _html()
    assert 'id="my-email-section"' in html
    for field in (
        "my-email-registered", "my-email-registered-save", "my-email-address", "my-email-password",
        "my-email-imap-host", "my-email-imap-port", "my-email-imap-security", "my-email-imap-folder",
        "my-email-smtp-host", "my-email-smtp-port", "my-email-smtp-security", "my-email-oauth-client-id",
        "my-email-oauth-client-secret", "my-email-oauth-flow", "my-email-policy-mode", "my-email-allow-list", "my-email-deny-list",
        "my-email-per-hour", "my-email-per-day", "my-email-notify-job-failed", "my-email-notify-approval",
        "my-email-notify-test", "my-email-agent-tools", "my-email-enabled", "my-email-connect-go",
    ):
        assert f'id="{field}"' in html, field
    # DESIGN-v2 §3: Advanced as plain sentences, servers visible, "Send a test" under Notifications.
    for words in ("Your agents may send to", "per hour and", "Watch folder", "Send a test", "Incoming mail (IMAP)", "Outgoing mail (SMTP)", "Sign in with Google"):
        assert words in html, words
    for gone in ("Server settings", "Display name", ">User name<", "Recipient rules", "Send limits"):
        assert gone not in html.split('id="my-email-section"', 1)[1].split('id="entities-list-section"', 1)[0], gone
    # The password field never pre-fills and is a password input.
    assert re.search(r'id="my-email-password" type="password"', html)
    # Disconnect confirms inline (the viewer has no confirm()).
    page = html.split('id="my-email-section"', 1)[1].split('id="entities-list-section"', 1)[0]
    assert "confirm(" not in page


def test_console_calls_only_the_callers_own_routes() -> None:
    html = _html()
    for route in (
        "/api/gateway/me/email", "/api/gateway/me/email/test",
        "/api/gateway/me/email/limits", "/api/gateway/me/email/enabled", "/api/gateway/me/email/agent-tools",
        "/api/gateway/me/email/address", "/api/gateway/me/email/discover", "/api/gateway/me/email/notifications",
        "/api/gateway/me/email/folder", "/api/gateway/me/email/oauth/start", "/api/gateway/me/email/oauth/finish",
        "/api/gateway/me/email/oauth/cancel", "/api/gateway/me/notifications/test",
    ):
        assert route in html, route
    # Recipient rules save through the API base they were rendered with (own or an entity's).
    assert 'const RECIPIENT_RULES_BASE = "/api/gateway/me/email";' in html
    assert "await api(`${base}/policy`, { method: \"PUT\"" in html
    # The only per-user admin call left is Reset (clears an old override).
    assert "/api/gateway/admin/users/${encodeURIComponent(u.user_id)}/email" in html
    assert 'JSON.stringify({ inherit: ["email", "email_agent_tools"] })' in html
    assert "<th>Mailbox</th>" in html


def test_sign_in_page_offers_recovery_only_when_available() -> None:
    html = _html()
    assert 'id="recovery-section" class="af-gateway-signin__recovery" hidden' in html
    assert "Forgot your token? Email me a sign-in code" in html
    assert "/api/gateway/session/recovery/request" in html and "/api/gateway/session/recovery/redeem" in html
    assert '$("recovery-section").hidden = !available' in html


def test_admin_sees_one_switch_and_advanced() -> None:
    html = _html()
    for field in ("email-caps-section", "email-cap-email", "email-cap-agent-tools", "email-cap-recovery", "email-caps-advanced"):
        assert f'id="{field}"' in html, field
    assert "/api/gateway/admin/email/capabilities" in html
    assert "Mailboxes are on for all users." in html
    assert "Whoever controls a user&#39;s mailbox can then sign in as that user." in html
