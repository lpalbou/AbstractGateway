# Proposed: Usage metering and analytics API

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 4 (hosted viability)
- Effort: M

## ADR status
- Governing ADRs: None.

## Context

Hosted multi-user (`1 user = 1 runtime`) is already shipped, but there is no way
to see or bound what it costs.

## Current code reality (verified)

- Token usage is present inside ledger records (e.g. `routes/gateway.py`
  references usage at ~5060 and ~7841), but there is no rollup API, no
  per-principal cost/usage view, and no enforceable usage quota.
- The only metrics endpoint is GPU hardware (`/host/metrics/gpu`); there is no
  general `/metrics` or `/usage`.

## Problem or opportunity

Hosting users without usage visibility or caps is an unbounded cost liability and
blocks any commercial/billing story.

## What we might want to do

1. Aggregate ledger `usage` per run/session/principal/model into a `/usage`
   (and optional Prometheus-style `/metrics`) surface.
2. Enforceable per-principal usage quotas (tokens/cost/time), coupled with the
   run/disk quotas in 0064.

## Dependency boundary

Gateway-owned aggregation over ledger data; usage tokens already recorded by
Runtime.

## Why

Prerequisite for safe hosted operation and any metering/billing; also gives
operators real cost observability.

## Promotion criteria

Promote when hosted multi-user is used beyond trusted teams, or when cost
visibility is needed operationally.

## Validation to require on promotion

- `/usage` returns correct per-principal/model rollups for a known run set; a
  principal exceeding a usage quota is throttled/blocked with a structured error.
