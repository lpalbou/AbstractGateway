# Shared IdentityContext across Gateway, Runtime, and Core

Status: proposed
Priority: unprioritized
Owner package: abstractgateway
Related packages: abstractruntime, abstractcore, abstractflow, abstractassistant

## Problem

AbstractFramework currently has authentication, execution provenance, and conversational session concepts, but no single trusted user abstraction.

Today:

- AbstractGateway validates bearer tokens and applies security limits, but does not derive a structured user/principal.
- AbstractRuntime persists `actor_id` and `session_id`, but those are provenance and correlation fields, not authorization boundaries.
- AbstractCore sessions can carry conversational metadata, but not an authenticated framework principal.
- Thin clients such as AbstractFlow and AbstractAssistant connect through Gateway, but they cannot reliably represent "which user is speaking to which runtime" beyond session ids.

This is acceptable for local single-user deployments, but it is not enough for shared runtimes, remote gateways, team workspaces, audit trails, or tenant isolation.

## Non-goals

- Do not replace existing bearer-token auth in v0.
- Do not make `session_id` an authorization boundary.
- Do not require OAuth/OIDC before the framework has a simple first-party identity contract.
- Do not block current local-first workflows.

## Proposed abstraction

Introduce a small shared identity payload that Gateway derives from trusted credentials and passes into Runtime.

```python
@dataclass(frozen=True)
class IdentityContext:
    principal_id: str
    principal_kind: str  # human | service | agent | anonymous
    actor_id: str
    session_id: str | None
    tenant_id: str | None = None
    auth_method: str | None = None
    scopes: tuple[str, ...] = ()
    claims: Mapping[str, JSONScalar] = field(default_factory=dict)
```

Rules:

- `principal_id` is the authenticated user/service identity.
- `actor_id` remains execution provenance and may be `gateway`, bridge-specific, or agent-specific.
- `session_id` remains durable conversation/memory scope only.
- `tenant_id` is optional, but all workspace/artifact/run filtering must use it server-side when present.
- `claims` must be sanitized and JSON-scalar only.

## Package responsibilities

### AbstractGateway

- Derive `IdentityContext` from bearer token configuration, future API keys, bridge bindings, or anonymous local mode.
- Never trust client-supplied identity fields unless an internal trusted bridge explicitly signs or owns them.
- Attach identity to run creation, subruns, artifacts, audit events, and workspace policy decisions.
- Map bearer tokens to named principals, scopes, and optional tenant ids.

Likely files:

- `abstractgateway/security/gateway_security.py`
- `abstractgateway/routes/gateway.py`
- `abstractgateway/hosts/bundle_host.py`
- `abstractgateway/hosts/visualflow_host.py`

### AbstractRuntime

- Own the durable identity schema because Runtime owns run state and ledger persistence.
- Preserve backward compatibility with `actor_id` and `session_id`.
- Store identity either as `RunState.identity` after migration, or initially under `_runtime.identity`.
- Inherit identity into subruns unless explicitly narrowed by Gateway policy.

Likely files:

- `abstractruntime/identity/*`
- `abstractruntime/core/models.py`
- `abstractruntime/integrations/abstractcore/effect_handlers.py`
- `abstractruntime/integrations/abstractcore/llm_client.py`

### AbstractCore

- Accept identity as request context from trusted Gateway/Runtime calls.
- Keep provider API keys separate from framework user identity.
- Use identity for session metadata, memory ownership, audit, and future per-user config.
- Treat identity headers as trace metadata unless they arrive through a trusted internal path.

Likely files:

- `abstractcore/server/app.py`
- `abstractcore/core/session.py`

### Thin clients

- AbstractFlow and AbstractAssistant should not own secrets directly in browser/UI state.
- They should authenticate to their local/server proxy, which stores gateway credentials server-side.
- Future user display should show principal/session clearly, but server-side Gateway remains the source of truth.

## Security constraints

- Never log raw tokens, provider keys, or sensitive claims.
- Log only stable fingerprints and non-sensitive principal ids.
- `session_id`, `actor_id`, `tenant_id`, and `claims` from client input are untrusted by default.
- Tenant/workspace/artifact filtering must happen server-side before run start and during artifact access.
- Subruns must inherit identity and cannot escalate scopes.
- Runtime/Core headers are not authorization unless protected by an internal trust boundary or signature.

## Suggested phases

1. Add `IdentityContext` dataclass and JSON schema in AbstractRuntime.
2. Add Gateway token-to-principal mapping config with local single-user default.
3. Persist identity on new runs while keeping `actor_id`/`session_id` fields unchanged.
4. Forward identity to AbstractCore as structured request context for memory/session/audit only.
5. Add thin-client display/logout semantics around principal and gateway session.
6. Add tenant/workspace enforcement once identity is stable.

## Acceptance criteria

- A run ledger can answer: who requested the run, what actor executed it, and which session it belongs to.
- Existing local workflows keep working with an anonymous/local principal.
- Gateway can map at least one bearer token to a principal id and scopes.
- Subruns preserve identity.
- Core can receive identity context without confusing it with provider credentials.
- Client UIs can show the current principal without storing gateway tokens in browser state.
