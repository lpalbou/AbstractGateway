# Proposed: Client conformance kit and frozen event vocabulary

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 3 (adoptability)
- Effort: S-M

## ADR status
- Governing ADRs: None. Should produce a deprecation-policy ADR.

## Context

The pitch is "stable versioned contracts clients can trust," but versioning is
currently decorative and the event semantics are undocumented.

## Current code reality (verified)

- `contracts.version` has stayed at 1 while behavior evolved across ~27 releases
  in six weeks, including breaking changes inside the 0.2.x patch line.
- Catalog routes carry legacy fields AND a `gateway_catalog_v1` envelope AND
  canonical `items`, with no deprecation clock (README states legacy fields
  "remain in place for compatibility").
- The ledger event vocabulary (StepRecord shapes, wait reasons, approval resume
  contract, `abstract.progress`, terminal/done semantics) is implied by code and
  in-house client mappings, not published as a versioned contract.

## Problem or opportunity

Without a frozen, tested vocabulary and a real version/deprecation discipline,
external clients (0071) break silently on the current release cadence, and every
envelope addition without a removal date is permanent double-maintenance.

## What we might want to do

1. Publish a versioned event/contract vocabulary: StepRecord/wait/approval/
   media-event/terminal semantics, with an explicit cursor contract (see 0075).
2. Ship a conformance kit: a test suite a third-party client runs against a live
   gateway to verify it implements the contract; run it in CI against the SDK
   (0071).
3. Adopt a deprecation policy: bump `contracts.version` when semantics change;
   give legacy fields a removal timeline instead of "forever."

## Dependency boundary

Gateway-owned contract; the cursor semantics interlock with 0075.

## Why

Converts "trust our contracts" from an assertion into a checkable property and
stops silent breakage of external clients.

## Promotion criteria

Promote with or just before 0071.

## Validation to require on promotion

- The conformance kit passes for the reference SDK and fails when a contract
  field/semantic is changed without a version bump.
