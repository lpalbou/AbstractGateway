# Proposed: Fail-closed auth and network defaults

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 0 (truth and footguns)
- Effort: S

## ADR status
- Governing ADRs: None. Candidate for a small security-defaults ADR.

## Context

Gateway's README leads with a security baseline (token + origin allowlist +
limits). Two defaults invert that baseline under plausible operator
misconfiguration, and one CORS setting is broader than the enforcement path.

## Current code reality

- Fail-open-to-admin: in `src/abstractgateway/security/gateway_security.py`
  (~lines 815-821), when auth is not required and no principal is set, an
  unauthenticated caller is assigned `local_admin_principal()` — not a read-only
  principal. Disabling read protection therefore makes every unauthenticated
  reader a full admin (admin-only routes like `/admin/users`, `/audit` become
  world-accessible). `routes/gateway.py:164-165` similarly returns admin when
  security is disabled.
- Public bind by default: `src/abstractgateway/cli.py:226` sets
  `--host` default `0.0.0.0`, while `docs/security.md` (~line 300) tells
  operators to bind `127.0.0.1`. Startup only warns on public bind (`cli.py`
  ~338-351) and proceeds unless a weak/absent token is combined with it.
- CORS breadth: `src/abstractgateway/app.py:46-53` sets `allow_origins=["*"]`
  with `allow_credentials=True`; real origin enforcement lives only in the
  custom middleware and only for `/api/gateway/*` when an `Origin` header is
  present. Any future sensitive route added outside that prefix silently gets
  credentialed any-origin CORS.

## Problem or opportunity

A one-line env change (`ABSTRACTGATEWAY_PROTECT_READ=0`) or a bare
`abstractgateway serve` exposes an admin surface on the LAN. These are
catastrophic-downside, near-zero-cost-to-fix footguns.

## What we might want to do

1. Map "protection disabled / no principal" to `local_readonly_principal()` (or
   deny), never `local_admin_principal()`. Admin must require an explicit
   authenticated admin principal.
2. Default `--host` to `127.0.0.1`; require an explicit `--host 0.0.0.0` (or an
   env opt-in) with a loud confirmation for public binds.
3. Scope CORS to the enforced prefix, or unify the middleware allowlist so no
   route can receive credentialed any-origin CORS by omission.

## Dependency boundary

Gateway-only.

## Why

Removes the two highest-downside misconfiguration paths and closes the latent
CORS gap before it is exercised by a new route.

## Promotion criteria

Promote now; this is cheap and purely protective.

## Validation to require on promotion

- Tests: unauthenticated request with protection disabled resolves a read-only
  principal and is denied on admin routes; default serve binds loopback; a
  cross-origin credentialed request to a non-gateway route is not reflected.
- `docs/security.md` and README updated so docs and defaults agree.
