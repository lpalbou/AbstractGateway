"""Web console: "My email" (framework backlog 0992) — the section, the admin mailbox column and
switch, and the sign-in page's recovery options are served, wired to the per-user routes, and
the page's JavaScript still parses (tests/test_gateway_console.py runs the parse and the login
harness over the whole page)."""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.basic


def _html() -> str:
    from abstractgateway.console import gateway_console_html

    return gateway_console_html()


def test_my_email_section_carries_the_web_fields_and_words() -> None:
    html = _html()
    assert 'id="my-email-section"' in html
    for field in (
        "my-email-address", "my-email-password", "my-email-imap-host", "my-email-imap-port", "my-email-imap-security",
        "my-email-imap-folder", "my-email-smtp-host", "my-email-smtp-port", "my-email-smtp-security",
        "my-email-oauth-provider", "my-email-oauth-client-id", "my-email-oauth-client-secret", "my-email-oauth-flow",
        "my-email-policy-mode", "my-email-policy-entries", "my-email-per-hour", "my-email-per-day",
        "my-email-notify-events", "my-email-notify-test", "my-email-agent-tools",
    ):
        assert f'id="{field}"' in html, field
    # Same words as AbstractCore's Email page and the terminal console.
    for words in ("Save and test", "Disconnect now", "Sign in with OAuth2", "Recipient policy", "Send limits", "Send test notification"):
        assert words in html, words
    # The password field never pre-fills and is a password input.
    assert re.search(r'id="my-email-password" type="password"', html)
    # Disconnect confirms inline (the viewer has no confirm()).
    assert "confirm(" not in html.split('id="my-email-section"', 1)[1].split("</details>", 1)[0]


def test_console_calls_only_the_callers_own_routes() -> None:
    html = _html()
    for route in (
        "/api/gateway/me/email", "/api/gateway/me/email/test", "/api/gateway/me/email/policy",
        "/api/gateway/me/email/limits", "/api/gateway/me/email/enabled", "/api/gateway/me/email/agent-tools", "/api/gateway/me/email/oauth/start",
        "/api/gateway/me/email/oauth/finish", "/api/gateway/me/email/oauth/cancel", "/api/gateway/me/notifications",
        "/api/gateway/me/notifications/test",
    ):
        assert route in html, route
    # The admin switch is status + on/off only.
    assert "/api/gateway/admin/users/${encodeURIComponent(u.user_id)}/email" in html
    assert "<th>Mailbox</th>" in html


def test_sign_in_page_offers_recovery_only_when_available() -> None:
    html = _html()
    assert 'id="recovery-section" class="hidden"' in html
    assert "Forgot your token?" in html and "Email me a sign-in code" in html
    assert "/api/gateway/session/recovery/request" in html and "/api/gateway/session/recovery/redeem" in html
    assert 'toggle("hidden", !(out && out.available))' in html


def test_admin_sees_email_defaults_and_per_user_agent_tools() -> None:
    html = _html()
    for field in ("email-caps-section", "email-cap-email", "email-cap-agent-tools", "email-cap-recovery", "email-caps-save"):
        assert f'id="{field}"' in html, field
    assert "/api/gateway/admin/email/capabilities" in html
    assert "setUserAgentToolsAvailable(u, !toolsOn)" in html
    assert "whoever controls a user's mailbox can sign in as that user" in html
