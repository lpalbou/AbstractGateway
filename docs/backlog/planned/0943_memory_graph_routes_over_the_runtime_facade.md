# 0943-gateway: [FEATURE] Memory graph routes over the AbstractRuntime memory facade; AbstractMemory import boundary

> Package: abstractgateway (host); abstractruntime (the memory facade, its 0853 and ADR 0009); abstractmemory (the graph, its ADR 0003 and 0056)
> Type: feature
> Status: planned (implemented on `feat/memory-graph-routes`; ships after AbstractRuntime 0.14.0 and AbstractMemory 0.4.0 are on PyPI, root ADR-0034)
> Created: 2026-10-27
> Priority: P1
> Labels: memory, provenance, boundary, thin-client

## Summary

Serve the plane's memory graph to thin clients (AbstractNote, the AbstractEntity app) under
`/api/gateway/memory/*`, read through `abstractruntime.integrations.abstractmemory.memory_facade`, and
enforce gateway ADR 0001 for AbstractMemory: no direct `abstractmemory` import in gateway source except an
explicit burn-down list that only shrinks.

## Why

Memory transition (2026-10-27, `untracked/memory-transition/`): every derived memory names the ledger rows,
artifacts or diary entries it came from (AbstractMemory ADR 0003), and the runtime facade reconstructs the
verbatim. Thin clients need that over HTTP, plane-scoped and owner-scoped. The audit found 34 direct
AbstractMemory imports in 8 gateway files with no boundary check, `kg/query` copying runtime-private owner
derivations, and the memory contract descriptor naming the root store instead of the caller's.

## Current code reality (branch `feat/memory-graph-routes`)

- `routes/memory.py`: `POST /memory/query`, `GET /memory/{id}/history`, `GET /memory/{id}/sources`,
  `POST /memory/sources/reconstruct`, `GET /memory/derived?ref_key=` (shapes in docs/api.md "Memory graph").
- `memory_store.resolve_memory_home(config)`: `sqlite` backend -> the KG file itself; `lancedb`/`memory` ->
  `<plane>/abstractmemory/memory.sqlite3`. `open_memory_home(config, create=, **stores)` -> `MemoryFacade`.
- Store classes, `TripleQuery` and every entity-side name (the wake reads `open_questions`/`open_problems`/
  `open_ideas` included, runtime 9eea551) come from the facade; the burn-down list is empty (34 -> 0).
- `tests/test_gateway_import_boundary.py` (AbstractMemory rule + burn-down), `tests/test_gateway_memory_routes.py`,
  `tests/test_gateway_memory_owner_ids.py`, `tests/test_gateway_memory_contract_descriptor.py`.

## Scope

- In: the five routes, the owner rule (own owners unless admin), bounded lists (`limit`, `truncated`,
  `withheld`), the plane home rule, the boundary rule, the runtime/memory floors.
- Out: entity minds (`entities/<slug>/memory.sqlite3`: AbstractEntity follow-up through the same routes with
  an entity selector); the LanceDB -> SQLite default switch and the import of existing plane KG data
  (decision 2, AbstractMemory 0057); phase-3 reads (`search`, `entity/{id}/card`, `changes?after=`);
  `as_recorded` on `/memory/query`.

## Dependencies

- AbstractRuntime 0.14.0 (`memory_facade`, `LedgerStore.get_record`), released first.
- AbstractMemory 0.4.0 (`abstractmemory.graph`), released first.
- MEMORY_REVISE in `build_memory_kg_effect_handlers(store=, run_store=, now_iso=, journal=None)` (runtime
  2574881): with the `sqlite` backend the bundle host already hands it the plane home (the KG file IS the
  home; `tests/test_gateway_memory_routes.py` applies a revision through that handler and reads it back over
  `/memory/*`). With LanceDB the handler answers a failed outcome naming AbstractMemory 0057: the builder takes
  one store, so a LanceDB plane cannot revise until decision 2 moves the plane onto its SQLite home.

## Remaining follow-ups

- Gateway: the bundle host passes no `journal` to the builder, so closures applied by MEMORY_REVISE are in the
  revision log and history but not folded into ranked retrieval; passing `SQLiteJournal(home)` needs the
  host's reload/close lifecycle to own it.
- Runtime/gateway: a LanceDB plane revises only after decision 2 (or a builder that takes a separate home).
- Operator decision 5 (memory transition): the owner rule on `/kg/query` itself (still plane-wide).

## Validation

`tests/test_gateway_memory_routes.py` (real plane: run, ledger row, artifact, two revisions through the
facade; query, history, sources, reconstruct ok/source_gone/unsupported/422, derived, owner refusals, admin
reads), the boundary test red on any new direct import and on a stale exemption.
