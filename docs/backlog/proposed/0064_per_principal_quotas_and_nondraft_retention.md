# Proposed: Per-principal quotas and non-draft retention

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 1 (make hosted multi-user safe)
- Effort: M

## ADR status
- Governing ADRs: None.

## Context

Hosted multi-user means untrusted (or semi-trusted) callers create durable state
on shared hardware. There is currently no cap on how much.

## Current code reality (investigated)

- `src/abstractgateway/run_retention.py` reclaims only ephemeral *draft* runs
  (`_is_ephemeral_draft_lifecycle`). Normal runs, ledgers, and per-run workspace
  directories grow unbounded.
- No per-user run-count or disk quota exists in source.
- Edge limits do exist (`security/gateway_security.py` ~211-220: body 256 KB,
  attachment 25 MB, bundle 75 MB, SSE 32, concurrency 64), but nothing caps how
  many runs/workspaces a single principal creates, nor total disk per tenant.
- Bridges (Telegram/email) can start one run per inbound message, so an open
  bridge is an amplification path.

## Problem or opportunity

One user (or a chatty bridge) can exhaust disk and threads for the whole host —
a trivial DoS and an unbounded cost liability for any hosted offering.

## What we might want to do

1. Per-principal quotas: max concurrent runs, max total retained runs, and a disk
   budget for runs+artifacts+workspaces, enforced at run-start with a clear
   structured error when exceeded.
2. Extend retention beyond drafts: a policy (age/count/size) for non-draft runs,
   ledgers, and per-run workspace dirs, using Runtime's optional deletion
   protocols (as draft purge already does).
3. Fair scheduling hook so one principal cannot monopolize shared execution
   capacity (couples with 0076).

## Dependency boundary

Gateway owns quotas/policy; deletion uses Runtime's optional deletion protocols.

## Why

Prerequisite for any safe hosted or commercial multi-user posture; prevents
single-tenant resource exhaustion.

## Promotion criteria

Promote together with, or immediately after, 0062 Tier 1 (safe multi-user).

## Validation to require on promotion

- Test: a principal exceeding its run/disk quota is rejected with a structured
  error while other principals are unaffected.
- Test: non-draft retention reclaims runs/artifacts/workspaces per policy.
