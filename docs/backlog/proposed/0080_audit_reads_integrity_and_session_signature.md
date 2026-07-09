# Proposed: Audit admin reads, audit-log integrity, and session-signature enforcement

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 1 (security posture)
- Effort: M

## ADR status
- Governing ADRs: None.

## Context

For a multi-user control plane, forensics and session integrity matter as much as
authentication.

## Current code reality (investigated)

- Audit covers writes only: `_finalize_audit`
  (`security/gateway_security.py` ~688-741) early-returns unless `is_write`, so
  admin reads (`GET /admin/users`, `/audit`, host metrics, artifact downloads)
  leave no record. The audit log is a process-local rotated JSONL with no
  integrity protection (no hash chain).
- Session signature is only partially enforced: `gateway_session_id_from_value`
  (`security/sessions.py` ~143-146) accepts a raw id (no `.`, length >=16)
  without verifying the HMAC signature on the header path. Session ids are
  256-bit random so guessing is infeasible, but a leaked id is directly
  replayable without a signature check — a deviation from the documented "opaque
  signed session id."

## Problem or opportunity

Admin snooping is invisible, the audit trail is tamper-un-evident, and a leaked
session id is replayable without signature verification.

## What we might want to do

1. Audit admin reads and auth decisions (not just writes), with a request-id
   already available in the middleware.
2. Add integrity to the audit log (hash chain) so tampering is detectable.
3. Enforce the session HMAC signature on all session-id acceptance paths,
   including the header path.

## Dependency boundary

Gateway-only.

## Why

Provides real accountability/forensics for a multi-user control plane and closes
the session-replay gap versus the documented model.

## Promotion criteria

Promote with hosted multi-user hardening (0062/0064).

## Validation to require on promotion

- Admin reads appear in the audit log; a modified audit entry is detectable via
  the chain; an unsigned session id is rejected on every path.
