# 0986-gateway: [IMPROVEMENT] Bound model catalog fetch latency while keeping all versions reachable

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: proposed
> Priority: P2
> Labels: catalog, performance, seat-framework
> Work-item id: abstractgateway-0986
> Owner: framework
> Thread: unavailable (Agora connection refused)
> Hub refs: none

## Summary and evidence

0984 improves initial DOM work (4-bit and five versions per card), but the
client still requests the complete `hub=true&extended=true` catalog. The first
post-restart live request completed with 96 models/1,798 versions; subsequent
fresh browser reads waited beyond 30s and an authenticated HTTP probe timed out
at 12s, while health stayed healthy and the new spinner remained visible.
Do not mistake the frontend precision filter for server-side quant projection.

## Current code reality

`console_catalog.py:MC_CATALOG_PATH` always asks the extended catalog;
`mcLoad` also waits for the parallel installed-model inventory before painting.
`routes/gateway.py:models_catalog_get` delegates to `core_model_catalog` through
`_core_host_call`; AbstractCore `config/model_catalog.py:catalog` owns catalog
assembly, Hub caching, fit and presence semantics. The exact latency cause has
not been isolated; this is a proposal, not permission to bypass these owners.

## Possible next work

Measure catalog vs installed-inventory latency and in-flight request behavior.
Evaluate rendering the first truthful inventory as soon as it arrives, safe
cache/coalescing improvements, or a documented progressive/server-filtered
contract. Keep every precision and matching version accessible through All and
expand; preserve accurate incomplete/loading labels, provider truth and errors.
Do not introduce silent caps, fabricated sizes or background polling services.

## Validation expectations

A delayed/failing provider fixture must not hold useful catalog data behind an
unrelated inventory. Measure cold/warm p50/p95; repeated refreshes must not create
unbounded duplicate work. Preserve full reachability, filters, downloads and
explicit failure/retry behavior. Architecture changes require owner review/ADR
assessment before implementation. Related: completed 0984/0985.
