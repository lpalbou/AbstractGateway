# Proposed: OpenAI-compatible facade

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 3 (adoptability)
- Effort: M

## ADR status
- Governing ADRs: None.

## Context

The largest existing client ecosystem speaks the OpenAI Chat Completions
protocol. Nothing lets those clients talk to a gateway-hosted agent/flow.

## Current code reality (verified)

- Gateway executes only `.flow` bundles started via `POST /api/gateway/runs/start`
  and observed via the ledger. There is no OpenAI-compatible route family.
- MCP appears only as a Runtime worker extra; there is no inbound interop facade.

## Problem or opportunity

An OpenAI-compatible endpoint is the cheapest possible "SDK": every OpenAI client
library, playground, and tool works immediately, and it makes a compelling
minutes-long demo.

## What we might want to do

1. Add a `/v1/chat/completions` (and optionally `/v1/responses`) facade that maps
   a request onto starting a designated flow/agent bundle, streams tokens/results
   by mapping ledger records to OpenAI streaming chunks, and returns usage.
2. Configure which bundle/entrypoint backs a given model name; keep it opt-in.

## Dependency boundary

Gateway-owned mapping over the existing run + ledger machinery; benefits from
0075 (efficient streaming) and 0072 (event vocabulary).

## Why

Instant compatibility with a huge client ecosystem for one route family — the
highest reach-per-effort adoption lever after the SDK.

## Promotion criteria

Promote after 0071/0072, or standalone as a demo-driven spike.

## Validation to require on promotion

- An unmodified OpenAI client library completes a streaming chat against a
  gateway-hosted flow, including usage reporting.
