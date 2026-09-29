"""THE one seam between the gateway and AbstractCore's mail library (`abstractcore.comms.email`).

The gateway never talks IMAP/SMTP itself (framework backlog 0992, principle 1): every account,
connection, policy, limit, vault and OAuth operation is AbstractCore's. Gateway modules import
these names from HERE, never from `abstractcore` directly, so the host -> Runtime -> Core
boundary (tests/test_gateway_import_boundary.py) has exactly one named exemption.

Pending seam (WP3 -> WP2 ask): AbstractRuntime has no email facade yet
(`abstractruntime.integrations.abstractcore.email_facade`, mirroring `config_facade.py`).
When it lands, the import below points at it and the exemption is removed.
"""

from __future__ import annotations

from abstractcore.comms.email import (  # noqa: F401 - re-exported
    EmailAccount,
    EmailAccountStore,
    EmailContext,
    EmailDisabled,
    EmailError,
    EmailInvalidMessage,
    EmailInvalidSettings,
    EmailNotConfigured,
    EmailOAuthFailed,
    EmailOAuthPending,
    EmailRateLimited,
    EmailSecret,
    ImapSettings,
    LoopbackAuthorization,
    MailCursor,
    OAuthSettings,
    OAuthTokenClient,
    OutgoingMessage,
    SearchCriteria,
    SecretVault,
    SmtpSettings,
    builtin_client,
    evaluate,
    guarded_send,
    legacy,
    parse_recipients,
    provider_preset,
    resolve_oauth_client,
    tls_context,
)
