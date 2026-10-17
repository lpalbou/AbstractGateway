# 0935-abstractgateway: [FEATURE] Let admins grant provider access per principal

> Package: abstractgateway
> Type: feature
> Created: 2026-10-17
> Priority: P2
> Labels: providers, authorization, human-users, entities

## Summary

Add administrator-managed provider access rules for individual gateway principals,
covering human users and digital users/entities equally. Until this feature is
implemented, enabled administrator-configured gateway providers are shared with
every authenticated principal by default. A provider's API key stays server-side.

## Why

Operator request, 17 October 2026: "later on, we will want the admin to be able
to select which providers are accessbiles to which user". Provider configuration
and the right to use a provider are separate concerns; a private runtime must not
make the gateway's configured providers disappear accidentally.

## Current code reality

- `src/abstractgateway/provider_endpoint_profiles.py` owns effective profile
  inheritance and server-side resolution; historical root profiles can carry
  `scope: user`. The immediate fix treats root provider configuration as shared,
  including those historical records, without copying credentials to users.
- `src/abstractgateway/routes/gateway.py` supplies discovery/model catalogs and
  configuration CRUD. Admin-created connections belong in the shared root store;
  ordinary users' own connections remain in their private runtime.
- `src/abstractgateway/hosts/bundle_host.py` resolves endpoints for workflows;
  `entities.py` and `entity_loop.py` resolve entity cognition and spawned loops.
- `src/abstractgateway/provider_connections.py` also exposes configured built-in
  providers; policy must cover these as well as `endpoint:<id>` aliases.
- There is no per-principal provider grant policy today. A model allowlist on
  a provider is not a user-access policy.

## Scope

### In scope

- Persist a gateway-owned policy that an admin can view/edit by provider and
  principal; use stable tenant/principal identity, including entity accounts.
- Define inheritance, allow/deny precedence, admin recovery, new-account defaults,
  provider deletion/rename, revocation and active-run behavior before implementation.
- Keep existing gateways shared by default on upgrade until the admin explicitly
  narrows access. Preserve administrator-only connection management.
- Apply one policy decision to discovery and actual execution: workflow starts,
  explicit provider overrides, capability defaults, direct/media APIs, entity
  visits and autonomous loops, including already-instantiated runtime clients.
- Expose effective access and actionable denial reasons in the console/API;
  audit policy edits and denied use without logging credentials.

### Out of scope

- Implementing selective access as part of the immediate shared-provider fix.
- Billing/quotas, changing model defaults, exposing keys, sharing private user
  connections, or widening visibility of user/entity histories and files.

## Acceptance criteria

- [ ] Admin can grant/revoke a provider for one human or entity account without
  changing another principal's access or the provider's stored credentials.
- [ ] Existing and newly created accounts inherit shared access when no explicit
  policy is configured; migration tests cover historical root `scope: user` rows.
- [ ] A denied principal cannot use the provider through direct IDs, aliases,
  defaults, pre-existing clients, subruns or entity-loop respawn; discovery and
  execution agree and revocation behavior is documented.
- [ ] Private user endpoints remain private; non-admins cannot edit access policy
  or gateway provider connections; secrets never appear in catalogs or denials.
- [ ] Human/entity allow/deny matrices, admin recovery, API and console flows,
  and audit records have automated coverage.

## Dependencies and ADR state

Read `docs/security.md`, `docs/configuration.md`, the modules above and framework
backlog `1022_entities_are_digital_users.md`. Write or revise the authorization
ADR before implementing selective access; this proposed item records intent,
not a new authorization contract. Coordinate with existing principal/tenant
authorization so this does not become a separate identity system.

## Testing

Run `python -m pytest -q tests/test_gateway_provider_endpoint_profiles.py tests/test_gateway_entities_endpoint_provider.py`
as a baseline, then add human/entity policy matrices and execute them through
both discovery and runtime calls with fake providers. No real keys or paid model
requests are needed. Validate console controls in the existing browser harness.

## Promotion criteria

Promote when the operator prioritizes selective provider access and the policy
precedence/revocation decisions above are settled. Shared access remains the
default behavior until then.

## Receipts

- Operator request: 2026-10-17, shared providers now; per-principal controls later.
- Shared-access baseline verified on 2026-10-17: 96 backend tests passed across
  provider sharing, profiles, entity resolution, Core configuration, discovery
  and provider-key handling; both the full Providers page and focused
  admin/member sharing browser tests passed. Selective grants remain unimplemented.
