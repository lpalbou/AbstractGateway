"""Logs modal wording (adversary pass 2, F3): the footer note is plain words (no HTTP verbs) and a
"Mailbox connected" event says how in plain words, from an explicit table."""

from __future__ import annotations


def test_note_is_plain_words() -> None:
    from abstractgateway.account_activity import NOTE

    assert NOTE.startswith("The gateway records sign-ins, changes, runs started and email events. Page views and reads are not recorded")
    for jargon in ("POST", "PUT", "PATCH", "DELETE", "GET", "request"):
        assert jargon not in NOTE, jargon


def test_mailbox_connected_detail_says_how() -> None:
    from abstractgateway.account_activity import _email_event

    spec = ("email", "Mailbox connected")
    detail = lambda **doc: _email_event({"ts": "2026-10-01T09:00:00+00:00", **doc}, "email.connected", spec)["detail"]  # noqa: E731
    assert detail(auth_kind="password") == "IMAP · password sign-in"
    assert detail(auth_kind="oauth2", provider="google") == "Google sign-in"
    assert detail(auth_kind="oauth2", provider="microsoft") == "Microsoft sign-in"
    # Not in the table: the raw values stay visible, never hidden or guessed.
    assert detail(auth_kind="oauth2", provider="fastmail") == "oauth2 · fastmail"
    assert detail() is None
