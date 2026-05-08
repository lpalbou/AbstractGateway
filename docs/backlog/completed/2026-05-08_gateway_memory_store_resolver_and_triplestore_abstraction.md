# Proposed: Gateway Memory Store Resolver And TripleStore Abstraction

## Metadata
- Created: 2026-05-08
- Status: Completed
- Completed: 2026-05-08

## Context

Gateway hosts durable workflows and wires `memory_kg_*` effects to AbstractMemory. AbstractMemory
already exposes store contracts and multiple store implementations. AbstractRuntime's memory effect
handlers are store-protocol based and only require a store with `add(...)` and `query(...)`.

Gateway currently bypasses that abstraction by constructing `LanceDBTripleStore` directly.

## Current Code Reality

- `src/abstractgateway/hosts/bundle_host.py` imports `LanceDBTripleStore` when a bundle uses
  `memory_kg_*` nodes.
- `src/abstractgateway/routes/gateway.py` imports `LanceDBTripleStore` for `/kg/query`.
- `abstractruntime.integrations.abstractmemory.build_memory_kg_effect_handlers(...)` accepts a
  generic store object with `add(...)` and `query(...)`.
- AbstractMemory's TripleStore API includes, depending on the installed build:
  - `InMemoryTripleStore`: dependency-free, volatile;
  - `SQLiteTripleStore`: dependency-free, persistent, structured-query only;
  - `LanceDBTripleStore`: optional dependency, persistent, vector-capable.
- AbstractMemory planned item `002_sqlite_database_compatibility_and_store_capabilities.md` already
  proposes explicit store capability metadata.

## Problem

Gateway's hardcoded LanceDB dependency makes the implementation say "memory requires LanceDB" even
though the real contract is "Gateway's current persistent/vector memory backend is LanceDB."

That creates three issues:

- structured-only persistent KG memory cannot use SQLite through Gateway;
- install/profile docs overstate LanceDB as a universal requirement;
- future image memory is conflated with vector storage, even though image bytes should remain
  artifact-backed and Memory should store references/provenance.

## Proposed Direction

Add a Gateway-owned memory store resolver that returns an AbstractMemory TripleStore plus explicit
capability metadata.

Suggested config surface:

- `ABSTRACTGATEWAY_MEMORY_STORE_BACKEND`: `lancedb`, `memory`/`inmemory`, or
  `sqlite` when the installed AbstractMemory build exposes `SQLiteTripleStore`.
- `ABSTRACTGATEWAY_MEMORY_STORE_PATH`: optional explicit store path.
- `ABSTRACTGATEWAY_MEMORY_REQUIRE_VECTOR`: optional strict gate for deployments that promise
  semantic recall.

Initial defaults:

- default Gateway `memory` profile may still resolve to LanceDB because it is the only current
  durable vector-capable AbstractMemory backend;
- structured-only deployments may select SQLite explicitly;
- in-memory should remain test/dev only and emit a readiness warning.

Gateway should use the resolver in both:

- bundle host setup for `memory_kg_*` effects;
- `/kg/query`.

## Capability Rules

Gateway should ask the resolver/store capability layer:

- is the store persistent?
- does it support structured KG queries?
- does it support semantic `query_text`?
- does it support direct `query_vector`?
- does it have an embedder configured?

Then Gateway should enforce behavior:

- `memory_kg_assert` can use any store with `add(...)`.
- structured `memory_kg_query` can use any store with `query(...)`.
- semantic `query_text` requires a vector-capable store and configured embedder.
- `/kg/query?query_text=...` should fail clearly on SQLite or missing embeddings.
- image/media memory should store artifact refs and metadata without requiring vectors.
- image/media semantic search should require a vector-capable backend.

## Non-Goals

- Do not implement a new memory store in Gateway.
- Do not move AbstractMemory store classes into Gateway.
- Do not make SQLite pretend to support semantic/vector search.
- Do not store image/audio bytes in the triple store.
- Do not make LanceDB a dependency of the base Gateway install unless the chosen Gateway profile
  explicitly promises persistent vector memory.

## Dependencies And Related Work

- AbstractMemory planned item:
  - `docs/backlog/planned/002_sqlite_database_compatibility_and_store_capabilities.md`
- AbstractMemory proposed item:
  - `docs/backlog/proposed/2026-05-08_gateway_memory_install_and_config_boundary.md`
- Gateway install/config proposal:
  - `docs/backlog/proposed/2026-05-08_gateway_install_profiles_and_config_entrypoint.md`

## Promotion Criteria

Promote before Gateway advertises persistent KG memory as part of a default server profile, or before
SQLite-backed structured Gateway memory is expected to work.

## Validation Ideas

- Unit test resolver selects LanceDB, SQLite, and in-memory from env/config.
- Bundle host test uses a fake TripleStore without importing LanceDB.
- `/kg/query` structured query works with SQLite.
- `/kg/query` semantic query fails clearly with SQLite.
- Semantic query works with LanceDB plus a stub embedder.
- Readiness output reports store backend, path, persistence, vector support, and embedder status.
- Install-profile tests prove base Gateway does not import LanceDB unless the memory/vector profile is
  selected.

## Guidance For Implementing Agents

Start with the resolver boundary, not a broad memory refactor. Preserve the current LanceDB behavior
as the default for vector memory, but make that a selected backend rather than an implicit import.

## Completion Report

Implemented in Gateway only.

- Added `src/abstractgateway/memory_store.py`, a Gateway-owned resolver for
  `lancedb`, `memory`, and SQLite-capable AbstractMemory TripleStore backends.
- Bundle `memory_kg_*` wiring and `/api/gateway/kg/query` now use the same
  resolver and capability metadata.
- SQLite-backed structured KG queries now work through Gateway when the
  installed AbstractMemory build exposes `SQLiteTripleStore`.
- Semantic `query_text` fails clearly when the selected backend/embedder cannot
  support vector retrieval.
- Capability discovery now reports memory backend, persistence, vector support,
  and embedder status.
- Added resolver/endpoint tests covering default LanceDB config, selected-backend
  class availability, SQLite structured queries when available, and SQLite
  semantic-query rejection.
