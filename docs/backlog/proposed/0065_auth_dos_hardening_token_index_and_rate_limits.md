# Proposed: Auth DoS hardening — token index and proxy-aware rate limits

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 1 (make hosted multi-user safe)
- Effort: M

## ADR status
- Governing ADRs: None.

## Context

The auth primitives are cryptographically sound (PBKDF2 token hashing,
constant-time compare), but the lookup and rate-limiting shapes create
availability risks under load or behind a proxy.

## Current code reality (investigated)

- `src/abstractgateway/users.py` (~662-673, `_PBKDF2_ITERATIONS=260_000`):
  `authenticate()` runs PBKDF2-HMAC-SHA256 against every enabled user until a
  match. A wrong token costs `N_users x 260k` iterations — an unauthenticated
  request amplifies into large CPU work as user count grows.
- `security/gateway_security.py` (~338-396): the auth lockout tracker is
  in-memory and IP-keyed; client IP is the first `X-Forwarded-For` hop only when
  `trust_proxy` is on, else the socket peer (~496-506). Behind a reverse proxy
  with `trust_proxy` off (default), all users share the proxy IP, so one
  attacker's failures lock out everyone and consume the shared concurrency
  semaphore. With `trust_proxy` on, an attacker rotates `X-Forwarded-For` to
  evade lockout entirely. No safe setting exists behind a proxy.

## Problem or opportunity

Cheap unauthenticated requests translate into expensive server work or
proxy-wide lockouts — a DoS with no attacker cost.

## What we might want to do

1. Index tokens by a non-secret fingerprint (already computed elsewhere) for
   O(1) candidate lookup, then run exactly one PBKDF2 verify against the matched
   record. Removes the O(N_users) amplification and speeds legitimate logins.
2. Make rate limiting proxy-aware and keyed on principal+route where a principal
   is resolvable, with a documented trusted-proxy configuration and a shared
   (not per-process) store when running multiple workers.

## Dependency boundary

Gateway-only.

## Why

Turns "one cheap request = large server cost / everyone locked out" into bounded,
per-caller cost.

## Promotion criteria

Promote alongside hosted multi-user hardening (0062/0064).

## Validation to require on promotion

- Test: an unknown token costs one PBKDF2 verify regardless of user count.
- Test: behind a simulated proxy, one abusive client does not lock out others;
  spoofed `X-Forwarded-For` does not evade limits under the documented config.
