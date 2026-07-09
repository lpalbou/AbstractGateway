# Proposed: Per-user RBAC and workspace grants (read/write/execute)

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 1 (make hosted multi-user safe) — foundation for 0062
- Effort: M

## ADR status
- Governing ADRs: None. Should produce an ADR: "Gateway RBAC model — roles,
  scopes, and per-user filesystem grants."

## Context

Hosted multi-user needs a durable, per-user answer to "which folders may this
user read / write / execute in, and what may they do on the system." Today that
answer is coarse and per-run, not per-user. This item adds the missing RBAC layer
that 0062 (execution isolation) depends on as its policy ceiling.

## Current code reality (investigated)

- Principal model is coarse: `security/principal.py` `GatewayPrincipal` has only
  `roles` (`admin`/`user`/`readonly`) and `scopes` (e.g. `*`, `gateway:read`).
  `is_admin()` = `"admin" in roles`. There are NO per-user filesystem grants.
- Workspace policy is PER-RUN, not per-user: `input_data` carries
  `workspace_root`, `workspace_access_mode`
  (`workspace_only|all_except_ignored|workspace_or_allowed`),
  `workspace_allowed_paths` (newline-separated roots, mounted), and
  `workspace_ignored_paths` (`routes/gateway.py:1691-1694`; normalization +
  mount building at `2846-2928`). So multi-workspace allowlisting already works
  for a single run.
- The gap: nothing binds a USER to a durable set of allowed folders. A regular
  user's run can request any `workspace_root`/`allowed_paths`/escaping mode; there
  is no per-user ceiling that a run can only narrow within. Admin gating exists
  for admin-only ROUTES (`security/authorization.py`) but not for filesystem
  grants.

## Problem or opportunity

Without per-user grants, "which folders can Alice touch, and can she run
commands there" is unanswerable and unenforceable — so execution isolation
(0062) has no policy to enforce, and operators cannot express "this user owns
these two project folders, read-write, execute allowed; nothing else."

## What we might want to do

1. Extend the principal/user record with a durable `grants` list: each grant is
   `{ path, read, write, execute }` (a folder + capability bits). Persist under
   the existing user registry (`users.py`), admin-managed via
   `/api/gateway/admin/users`.
2. Define role defaults: `admin` = full ownership (all paths, rwx, unsandboxed
   execution — admins own the machine); `user` = only their granted folders;
   `readonly` = read-only on granted folders, no execute.
3. Make the per-user grant the CEILING for every run: at run start, intersect the
   run's requested `workspace_root`/`workspace_allowed_paths`/`workspace_access_mode`
   with the user's grants. A run may narrow within the ceiling but never widen
   beyond it (widening is rejected or clamped, with a clear error). This is the
   enforcement hook 0062 Tier 1 needs.
4. Propagate the resolved (intersected) workspace policy + execution posture
   (sandboxed vs unsandboxed, from role) to the Runtime via run input/vars so it
   holds in split (non-co-located) deployments too.
5. Surface grants in discovery/console so operators can see and edit them, and so
   thin clients can show a user their accessible workspaces.

## Dependency boundary

Gateway owns the RBAC model, persistence, and the run-start intersection. The
Runtime enforces the resulting workspace policy + execution posture at tool-exec
time (it already consumes `workspace_*` from run input). The admin-vs-user
sandboxed/unsandboxed decision is Gateway policy, Runtime enforcement (0062
Tier 2/3).

## Why

It is the missing foundation that makes multi-user safe AND usable: users get
several authorized workspaces with explicit rwx, admins keep full ownership, and
0062's execution isolation finally has a per-user policy to enforce instead of
trusting per-run input.

## Promotion criteria

Promote with 0062 Tier 1 (RBAC is its prerequisite ceiling), ahead of Tier 2/3
sandboxing.

## Validation to require on promotion

- Tests: a regular user with a grant on folder X can read/write/execute in X per
  their bits; a run requesting a path outside the grant is rejected/clamped; a
  `readonly`-granted user cannot write or execute; an admin is unrestricted.
- Test: the resolved policy propagates to a (simulated) remote Runtime and is
  enforced there.
- An ADR documenting the roles, grant schema, and intersection semantics.
