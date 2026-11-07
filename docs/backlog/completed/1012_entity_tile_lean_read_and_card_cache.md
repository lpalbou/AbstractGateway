# 1012-abstractgateway: [IMPROVEMENT] A lean entity tile read, and the card cached until the life grows

> Created: 2026-11-07
> Type: improvement
> Priority: P2
> Labels: entities, performance, contracts
> Status: proposed

- Work-item id: abstractgateway-1012
- Owner: framework (intake; implementation belongs to Gateway; consumer AbstractObserver)
- Thread: Unavailable; agora not connected at intake.
- Hub refs: None.

## Summary

The Observer Board shows one small tile per entity (state, age, last moment, a few open questions and problems,
the latest lesson, the dreams brief) yet each tile reads the whole identity card. Operator, 2026-11-07: "it is absurd
for a tiny card about the entity to request so much data that you will never show." Serve a lean tile read, and cache
the card the way communities are cached, so a polling view costs nothing.

## Context and current code reality

- `routes/entities.py::entity_card` (`GET /entities/{name}/card`) calls `EntityRegistry.card()` (`entities.py` ~540,
  ~2449): the engine compositor runs over the whole home on every request, with no cache. Measured on the operator's
  gateway (0.20.2, 17 entities): most cards 0.01-0.02 s; ephemeral (570 MB home) 6.4 s on the first read after a restart,
  0.45-1.2 s warm, 88 KB (questions 32 KB, discoveries 28 KB, problems 14 KB, lessons 9 KB, likes 2 KB, the rest small);
  castor 0.1-0.4 s (46 KB).
- `top_n` and `current_window_events` do not shrink the expensive sections (top_n=1&current_window_events=1: 86 KB,
  0.45 s); 0 is a 400.
- The same file already caches `/{name}/communities` by home directory + journal high-water seq (`_COMMUNITIES_CACHE`).
- The roster (`GET /entities`, 0.02 s) already carries `state`, `created_at` and the identity ref. Observer 0.12.2 draws
  tiles from it at once, fills cards progressively and refetches a card only every few minutes or on a state change
  (client-side mitigation, no gateway change).

## Proposed direction and scope

1. `GET /entities/{name}/card?sections=a,b,c` (or a dedicated `GET /entities/{name}/tile`): the compositor builds only the
   requested sections; unknown sections are a 400; the default stays the full card (no client breaks).
2. A tile read returns exactly what a tile shows: state, age_days, the last moment {kind, at}, open questions/problems as
   counts plus the first N briefs, the latest lesson, the dreams brief, as_of_seq. Measure it on ephemeral (target: tens of
   milliseconds warm).
3. Cache the full card and the tile per (home directory, journal high-water seq, parameters), the communities rule, so a
   repeated read of an unchanged life is free; bound the cache (entities × parameter variants).
4. Advertise the capability in the client contract so Observer uses the tile read when present and falls back to the card.

Non-goals: changing what the card contains; client-side trimming of a full card (the bytes and the compute are the cost).

## Validation

- Tests: sections filter (known/unknown), tile shape, cache hit on unchanged seq and miss after a new journal record,
  tenant isolation of the cache key (the communities F3 rule).
- Timing before/after on a large home (a copy of ephemeral or a synthetic 500 MB home), warm and cold.
- Observer switches its Board tiles to the tile read; its test proves the fallback on an older gateway.

## Completion (2026-11-07, AbstractGateway 0.21.0)

Operator scope G5. `GET /entities/{name}/tile` serves exactly what an entity card shows (who + look, now:
phase/state/busy/since, trouble, into lately (newest, full statements), on its mind, latest lesson, last moment as a
fixed code, age, mind, non-zero counts), advertised on the roster as `reads.tile` and in discovery as
`common.entities.tile` so clients fall back to the card on older gateways; a failing source costs only its own fields
(labelled warning), never a 500. The engine card is cached per (home directory, memory database file identity,
journal seq, parameters), 64 entries, striped locks. Measured on APFS copies of the operator's homes: ephemeral
tile 5-9 ms warm / 2.2 KB (card was 552 ms / 86 KB), castor 3 ms / 1.6 KB (was 175 ms / 45 KB). Tests:
tests/test_gateway_entity_tile.py, 10 deliberate breakages each red. Consumer: AbstractUIC `EntityCard`
(ui-kit), adopted by AbstractObserver's Board; AbstractEntity's grid adoption is planned (owned by another agent).
Follow-ups: `/cognition` drives cache keyed by entity name only (tenant collision, same class as the communities
fix); an entity writing memory faster than the card computes never gets a cached card.
