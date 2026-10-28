# 0948-gateway: [FEATURE] Memory graph routes over the AbstractRuntime memory facade; AbstractMemory import boundary

> Package: abstractgateway (host); abstractruntime (the memory facade, its 0856 and ADR 0011); abstractmemory (the graph, its ADR 0003 and 0056)
> Type: feature
> Status: completed (2026-10-28, 0.17.0)
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

- In: the five routes, the ownership rule of operator decision 5 on `/memory/*` and `/kg/query`
  (`memory_access.caller_owner_ids`: own runs by origin actor, own sessions, created entities, own plane's
  global owner; admin all), bounded lists (`limit`, `truncated`,
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

- Gateway (done in review G3): the bundle host passes the store's `SQLiteJournal`, the plane's `runtime_id` and
  artifact store to the builder (`hosts/bundle_host.build_gateway_memory_kg_handlers`); the host closes the
  journal with the store on reload.
- Done (architect C1): the home is opened once per service (`memory_store.PlaneMemoryHome`, closed at
  shutdown); a read no longer waits behind a writer's lock to open it (tested: < 100 ms under a held write lock).
- Done (architect B6): the reconstruct owner gate is AbstractMemory's `MemoryGraph.names_ref(ref, owner_ids)`
  (one covering-index probe, AbstractMemory 1116ac7): right and < 50 ms with 1,201 memories naming one ref
  (`tests/test_gateway_memory_scale.py`; the old 1000-memory walk refused the 1,201st owner).
- Note: AbstractMemory now refuses a cross-owner `derived_from` at write (lineage never crosses owners); the
  gateway's owner-filtered lineage walk and history id filter remain as a second layer for rows written before.
- Open (operator decision pending): `owner_id` is not a principal (session/run/global/entity ids derived from
  runs); `memory_access.caller_owner_ids` is the single place that changes when owner = principal.
- Done (operator decision 5): ownership is strict, not plane-wide. A named run or session must be the
  caller's (`_meta.origin.actor`); the global owner is the account's own because planes are per account;
  entities delegate to their creator. Runs with no recorded origin (before backlog 1038) are admin-only.
  Future: sharing a graph with another person or a topic-scoped grant (no route yet).
- Runtime/gateway: a LanceDB plane revises only after decision 2 (or a builder that takes a separate home).
- Session ownership reads up to 200 of the session's newest runs from the run index (a session whose own
  runs are all older than that window is refused); an owner index (session -> starter) would make it exact.

## Validation

`tests/test_gateway_memory_routes.py` (real plane: run, ledger row, artifact, two revisions through the
facade; query, history, sources, reconstruct ok/source_gone/unsupported/422, derived, owner refusals, admin
reads), the boundary test red on any new direct import and on a stale exemption.

Completed 2026-10-28: PR #7 merged (`f349cebc`) with CI green (4214 passed on 3.10-3.13); released in 0.17.0 on
AbstractRuntime 0.15.0 and AbstractMemory 0.4.0. Direct `abstractmemory` imports: 34 (8 files) -> 0.
