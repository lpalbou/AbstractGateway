"""THE one seam between the gateway and AbstractCore's mail library (`abstractcore.comms.email`).

The gateway never talks IMAP/SMTP itself (framework backlog 0992, principle 1): every account,
connection, policy, limit, vault and OAuth operation is AbstractCore's, reached through
AbstractRuntime's email facade (the host -> Runtime -> Core boundary,
tests/test_gateway_import_boundary.py). Gateway modules import these names from HERE.
"""

from __future__ import annotations

from abstractruntime.integrations.abstractcore.email_facade import (  # noqa: F401 - re-exported
    EmailAccount,
    EmailAccountStore,
    EmailAgentToolsOff,
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
