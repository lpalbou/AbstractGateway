"""THE one seam between the gateway and AbstractCore's mail library (`abstractcore.comms.email`).

The gateway never talks IMAP/SMTP itself (framework backlog 0992, principle 1): every account,
connection, policy, limit, vault and OAuth operation is AbstractCore's, reached through
AbstractRuntime's email facade (the host -> Runtime -> Core boundary,
tests/test_gateway_import_boundary.py). Gateway modules import these names from HERE.

`discover_servers` / `require_servers` / `EmailDiscoveryFailed` (mailbox server auto-discovery)
arrived in AbstractCore with the state-toggles work (abstractcore feat/state-toggles-api d9faa97);
the facade re-exports core's `__all__`, so an older core fails this import loudly. `server_defaults`
(the mailbox form's pre-filled servers, DESIGN-v2 round 2) arrived with abstractcore round2 fd71d04.
`ensure_key_file` / `read_key_file` / `key_fingerprint` (the sealing key file; SecretVault without
any OS keychain) arrived with AbstractCore 2.26.0 (round 16).
"""

from __future__ import annotations

from abstractruntime.integrations.abstractcore.email_facade import (  # noqa: F401 - re-exported
    EmailAccount,
    EmailAccountStore,
    EmailAgentToolsOff,
    EmailContext,
    EmailDisabled,
    EmailDiscoveryFailed,
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
    discover_servers,
    ensure_key_file,
    evaluate,
    guarded_send,
    key_fingerprint,
    legacy,
    normalize_address,
    parse_recipients,
    provider_preset,
    read_key_file,
    require_servers,
    resolve_oauth_client,
    server_defaults,
    tls_context,
)
