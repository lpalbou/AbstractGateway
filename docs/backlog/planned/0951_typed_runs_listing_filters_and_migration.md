# 0951-gateway: [FEATURE] Typed runs: stamp `run_type` on every start path, filter `/runs` by it, migrate existing runs

> Package: abstractgateway (start paths, `/runs` and search filters, `session_kinds.json` retirement, backfill); abstractruntime 0858 (enum, stamp, index, view field)
> Type: feature
> Status: planned (not implemented)
> Created: 2026-10-28
> Priority: P1 (high)
> Labels: runs, sessions, ledger, replay, search, migration
> Root link: AbstractFramework backlog 1063 (`docs/backlog/planned/1063_typed_runs_and_sessions.md` in the AbstractFramework root; [relative link in a monorepo checkout](../../../../docs/backlog/planned/1063_typed_runs_and_sessions.md)); links root 0993, 1059

## Summary

Clients cannot filter, search or route runs by what they are: the gateway serves `session_kind`/`role`
(automations v1, derived by the runtime), a separate session purpose (`kind=conversation|docs` from
`session_kinds.json`) and an unindexed `_meta.origin` whose `client` is a workflow interface id, not a
device. Stamp the runtime's `run_type` and `origin.surface` (runtime 0858) on every start path the gateway
owns, filter on them, and backfill existing runs once.

## Current code reality (origin/main 8369e5fb, 0.17.0)

- `routes/gateway.py` `_stamp_run_origin` (kind `person` at `/runs/start`, `system` for schedule wrappers; `client=req.interface`).
- `routes/gateway.py` `_LIST_RUNS_KNOWN_PARAMS` (`session_kind`, `kind`, `root_only`, `archived_only`...), `_parse_session_kind_filter` (400 on unknown), `_is_turn_root_summary` (role-based).
- `session_kinds.py`: `KINDS = ("conversation", "docs")`, `<data_dir>/session_kinds.json`, docs-qa migration `docs_qa_bundle_v1`; `/runs/start` marks docs sessions (`routes/gateway.py` ~9285-9306).
- `routes/automations.py` returns `session_kind: "automation" | "discussion"`; `routes/conversation_search.py` filters `session_kind` (default `chat,discussion`).
- `events_stream.run_origin`, `memory_access.run_origin_actor` read the origin.
- Entity visits and the life loop (`entity_visits.py`, `entity_chat.py`) start runs in the entity's home runtime without `_meta.origin`; Note importers run outside `/runs/start`.

## Chosen route

1. Start paths stamp `run_type` + `origin.surface`: `/runs/start` (`conversation`, or `docs` for the docs-qa bundle; `surface` from a validated `X-Abstract-Surface` header or body field), schedule (`scheduled`), automations (controller/occurrence/discussion via the runtime), entity visit/life hosts, bridges, Note import runs, correction runs (memory decision 8).
2. `GET /runs?run_type=a,b&surface=x` on the index columns; `/conversations/search` takes the same filter; `session_kind` and `kind` stay accepted for one release as aliases mapped onto `run_type`.
3. `session_kinds.json` folds into the index (`docs` becomes `run_type=docs`); the file is read once by the migration and kept read-only.
4. Migration: `abstractgateway migrate run-types` (also run at boot, resumable, idempotent, off the event loop), using the runtime's `run_type_of` derivation; rows carry `run_type_source`.
5. Docs: `docs/api.md` (runs listing), the thin-client capability contract advertises `run_types`.

## Acceptance criteria

- [ ] Each start path has a test that fails when its stamp is removed.
- [ ] `/runs?run_type=` and `surface=` filter correctly on JSONL and SQLite; unknown values 400.
- [ ] Migration on a copy of a real data dir: every run typed, docs sessions preserved, second run a no-op.
