# Proposed: Official client SDKs (render kit)

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 3 (adoptability)
- Effort: M-L

## ADR status
- Governing ADRs: None. Pairs with 0072 (frozen event vocabulary).

## Context

The mission is to be a control plane for thin AI apps, but the hard part of
being a client — turning the ledger into UI — is unspecified and re-implemented
per client.

## Current code reality (investigated)

- The documented API is curl + SSE. Turning raw StepRecords, waits
  (`result.wait`), approval resumes (`{"approved": true}`), and
  `abstract.progress` events into UI state is documented nowhere.
- Each in-house client carries private mapping code: AbstractAssistant's gateway
  client (~1,880 lines), AbstractCode web client (~619 lines), AbstractFlow's
  `ledgerEvents.ts`. That is 2,500+ lines of duplicated client logic in one
  monorepo — the measured cost of no SDK.
- No external client author has a supported path; they must reverse-engineer the
  ledger semantics.

## Problem or opportunity

No outsider will hand-roll HTTP + SSE + ledger parsing. Without an SDK, the
"control plane for thin apps" positioning is unreachable beyond the in-house apps.

## What we might want to do

1. Extract a Python and a TypeScript SDK from the three in-house clients: run
   start/schedule, cursor-based SSE with reconnect (`Last-Event-ID`), wait and
   approval helpers, artifact upload/download, discovery/capability gating, and a
   typed ledger-event vocabulary (shared with 0072).
2. Dogfood by migrating at least one in-house client and the console (0068) onto
   the SDK — this forces the contract to be real and stable.

## Dependency boundary

Gateway-facing SDK; consumes the frozen event vocabulary (0072) and benefits from
event-driven streaming (0075) but does not require it.

## Why

The single biggest lever to let anyone outside the monorepo succeed on day one,
and the strongest forcing function for contract stability.

## Promotion criteria

Promote after 0072 (so the SDK encodes a frozen vocabulary) or jointly.

## Validation to require on promotion

- One in-house client and the console run on the SDK with no behavior loss.
- A quickstart "start a run and render its ledger in <20 lines" example works
  against a live gateway in both languages.
