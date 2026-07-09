# Proposed: Define "premium control plane" — SLOs, scale targets, deprecation policy

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 4 (mission definition)
- Effort: S

## ADR status
- Governing ADRs: None. This item largely *produces* ADRs.

## Context

The backlog's stated direction is to be a "premium control plane for thin AI
apps," but the phrase is defined nowhere in measurable terms.

## Current code reality (verified)

- `docs/` contains no SLOs, no scale/throughput targets, no compatibility
  guarantees, and no deprecation policy (searched: zero matches).
- `contracts.version` never changes despite behavior evolving across ~27 releases
  in six weeks, and legacy fields are promised "forever" with no removal clock.
- Versioning discipline is per-payload (`contracts.version=1`), with no `/api/v1`
  path discipline across 186 endpoints.

## Problem or opportunity

"Premium" is currently an aspiration, not a spec. Adopters cannot evaluate the
product against commitments, and the team cannot tell when scale-out work
(0076) is actually warranted.

## What we might want to do

1. Write down target SLOs (latency for run start, ledger tail, discovery),
   supported scale (concurrent runs, SSE clients, users per node) for the
   single-node default, and the compatibility/deprecation policy (when
   `contracts.version` bumps; how long legacy fields live).
2. Use those numbers as the promotion triggers for 0076 (scale-out) and as the
   conformance baseline for 0072.

## Dependency boundary

Documentation/policy (ADRs); measurement uses 0075/0077 outputs.

## Why

Turns the positioning into something evaluable and gives the roadmap objective
triggers instead of vibes.

## Promotion criteria

Promote early — it is cheap and it disciplines 0072/0076/0077. Best done once
0075 gives real single-node numbers to anchor the targets.

## Validation to require on promotion

- A published SLO/scale/deprecation document; `contracts.version` bump rules are
  encoded in the conformance kit (0072).
