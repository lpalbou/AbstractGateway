# 0936-gateway: [FEATURE] Add per-user MCP access, credentials and activation defaults

> Package: abstractgateway
> Type: feature
> Status: planned
> Created: 2026-10-22
> Priority: P1
> Labels: mcp, credentials, multi-user, assistant

## Summary

Let administrators curate acceptable MCP servers and explicitly share them with selected users, either as endpoint-only configurations requiring each user's token, or with optional shared credentials. Each authorized user can supply/override their own token and set whether the server is active by default. Desktop Assistant, iPhone and Watch consume the same gateway-owned configuration and connection status.

## Why

The RAGnarok demo successfully discovers tools across all three clients, but the admin-configured bearer currently represents every user. The operator requests: “they should always be authorized to fill or override the token of a given MCP.” A user token must override shared credentials without allowing the user to change the admin-approved destination.

## Current code reality

- `src/abstractgateway/mcp_registry.py`: root-data-folder registry, encrypted header values, admin CRUD, saved handshake/tool catalog, archive and `enabled_for_agents` gates.
- `src/abstractgateway/mcp_run_tools.py`: `offered_tool_specs` caches by root registry stamp; `_open` loads shared headers by server name; `McpRoutingToolExecutor` checks live registry eligibility. Neither credential selection nor catalog cache is per-user.
- `src/abstractgateway/routes/gateway.py`: `/mcp/servers` inventory is admin-only; `/admin/mcp/*` handles configuration/test/enable. `/discovery/tools` exposes offered MCP tool specs.
- Assistant tool policy supports individual Off/Auto/Ask overrides. Phone and Watch already display MCP tool groups. This is not a per-user MCP connection settings surface.
- RAGnarok uses Streamable HTTP `/mcp/`, `Authorization: Bearer rmcp_…`, authenticated discovery and account/workspace grants. Shared credentials imply shared remote identity; local tool approval does not change that identity.

## Scope

### In scope

- Admin catalog: server destination/transport, availability, explicit user grants, default activation, and credential mode (user-required or optional shared credential). Server registration without a token must be possible even when authenticated discovery cannot yet succeed.
- Authorized users can always set, replace or remove a personal token. Personal credentials take precedence over shared credentials. Missing, rejected or revoked personal credentials must never silently fall back to the shared identity; removal may explicitly restore an available shared credential.
- User preferences distinguish inherit-admin-default from explicit on/off. Defaults apply to new runs; explicit run selection remains bounded by admin grants and current availability.
- Gateway-owned authenticated user endpoints for safe inventory, connection settings, token write/delete and real connection test. Show disabled, token-required, untested, connected, expired/rejected and unreachable states with last-tested time. Never equate cached tool visibility with a live connection.
- Enforce grants, active state and credential ownership at discovery and execution, including subruns, resumptions, automations and queued work. Preserve the existing external-email restriction unless separately authorized.
- Per-user discovery/schema cache isolation and invalidation on credential/grant/server changes; authenticated tools/resources may vary by remote identity. Resolve principal server-side, never from user-supplied owner IDs.
- Desktop/phone/watch settings expose admin-approved MCPs, activation defaults, personal credential entry and status. Users cannot redirect credentials to arbitrary URLs or modify the admin transport/command.
- Migration and audit events: make existing shared-credential behavior explicit and reviewable; decide migration grants before rollout. Keep tokens in the gateway's sealed stores, out of prompts, ledgers, logs, listings and returned settings.

### Out of scope

- Arbitrary user-registered servers, OAuth/browser authorization, an MCP marketplace, and changes to remote RAGnarok permissions.
- Fixing model tool-call formatting or streaming wire-name restoration in Runtime/Core.

## ADR status

Needs an ADR before implementation closure for credential precedence, access revocation, cache ownership and legacy migration. Inspect existing gateway identity/security decisions and `docs/security.md`; this item records intended work, not durable policy by itself.

## Dependencies and implementation guidance

Reuse existing authenticated principal resolution and sealed-secret storage. Start with HTTP bearer credentials for RAGnarok, keeping the server registry transport contract intact. Audit the runtime executor and agent catalog plumbing before choosing storage/schema changes; a UI-only token field cannot provide isolation. Related: proposed `0935_per_principal_provider_access_policy.md`, `0084` user RBAC, and existing MCP registry/run-tool tests.

## Acceptance Criteria

- [ ] Admin can register RAGnarok without a token and grant access only to chosen users; ungranted users cannot discover or execute it.
- [ ] Admin can optionally provide a shared credential; two users overriding it with distinct tokens see only their own remote grants and tools, including concurrent calls.
- [ ] Missing/invalid personal tokens produce an actionable reconnect state without shared-token fallback or secret leakage.
- [ ] Personal on/off defaults persist across sessions and devices; admin revocation/disable wins over user and run preferences.
- [ ] Credential changes, revocation, restart, resumed runs and discovery caches preserve user isolation.
- [ ] Desktop, phone and watch show truthful status and support required personal tokens, override/removal and default activation.
- [ ] Existing shared demo setup has a documented migration path; admin/user API contracts and the governing ADR are updated.

## Testing

Extend the existing suites: `python -m pytest tests/test_gateway_mcp_registry.py tests/test_gateway_mcp_runs.py`. Add route/authorization tests with two principals and a fake MCP server varying tools by bearer; cover concurrency, rejected token, deletion, revoke-after-discovery and resumed runs. Run each client's existing settings tests, then manually verify RAGnarok with two accounts from desktop/iPhone/Watch without putting tokens in test output.

## Receipts

- 2026-10-22: inspected registry, run router and discovery/admin routes; operator screenshots show successful discovery and calls on the shared token. Implementation intentionally deferred.
