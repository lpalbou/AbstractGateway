# Proposed: Ledger replay integrity and durable cursor (investigation + invariants)

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 3 (prerequisite spike for 0075/0076)
- Effort: S (investigation + invariants + test harness) before any streaming change

## ADR status
- Governing ADRs: None yet. This spike should PRODUCE an ADR: "Durable ledger
  cursor + replay-equivalence invariants" that 0075/0076 must comply with.

## Why this is a separate, investigation-first item

Replay-first is the product's core contract: the durable ledger is the source of
truth and SSE is only an optimization over replay. Changing the streaming/cursor
mechanism (0075) or the store backend (0076) touches that contract directly. This
item exists so that **no streaming change ships before replay integrity is
pinned down with invariants and a regression harness.** We must not break the
ledger or its replay; we do want clean, unambiguous replay.

## Current code reality (verified in abstractruntime + abstractgateway)

Two ledger backends with DIFFERENT cursor models:

- `abstractruntime/storage/sqlite.py` `SqliteLedgerStore` (line 545+):
  - Has a durable, monotonic per-run `seq` allocated atomically via a
    `ledger_heads` table, stored in a `ledger` table with
    `UNIQUE(run_id, seq)` (append at ~551-588). `list()` orders by `seq ASC`.
  - Already implements `list_after(*, run_id, after, limit) -> (records, next)`
    (line 711) where `after` is the last consumed `seq` — a correct, indexed,
    incremental read. Its docstring says cursor semantics "match the existing
    gateway API." It is UNUSED by the gateway SSE path (dead code for streaming).
- `abstractruntime/storage/json_files.py` `JsonlLedgerStore` (line 384+):
  - `append()` (392-397) opens the file in append mode and writes one JSON line.
    There is NO `fcntl`/`flock` and NO `fsync` — contrary to the AGENTS.md
    2026-02-22 "ledger lock + fsync" note, which is FALSE for this shipped store.
    The only concurrency mitigation is "write in one call."
  - `list()` (399-444) is positional (record index), has NO persisted `seq`, and
    performs best-effort recovery of concatenated JSON objects via `raw_decode`
    (emitting a `#FALLBACK` warning). There is NO `list_after`.

Gateway consumption:

- `routes/gateway.py` `stream_ledger._gen()` (~7619-7665) and `get_ledger`
  (~7541) call `ledger_store.list(run_id)` and use a LIST INDEX cursor
  (`after` = position). `Last-Event-ID` is not honored.
- `stores.py` (~42) wraps the ledger in `ObservableLedgerStore`
  (`abstractruntime/storage/observable.py`), which exposes `subscribe()` and
  notifies on `append()` — but gateway never calls `subscribe()`.

Migration:

- `abstractgateway/migrate.py` copies file→SQLite by re-appending records, which
  RE-ALLOCATES `seq`. If JSONL recovery merged/dropped lines, the SQLite `seq`
  sequence will not match the JSONL positional indices a client previously held.

## The core problems to resolve (investigation questions)

1. **Cursor identity.** Today the cursor is a positional list index (backend-
   dependent, migration-fragile). SQLite has a real `seq`; JSONL has none. What
   is the single, durable, backend-independent cursor definition?
2. **Migration safety.** How does a cursor survive `migrate.py` (file→SQLite)?
   Does `migrate.py` preserve record order and count exactly (no drops/renumber
   that would invalidate a held cursor)? Fix `migrate.py` if it can lose or
   reorder records — this is a data-loss/cursor-invalidation vector regardless of
   streaming.
3. **JSONL append durability + ordering.** Without lock/fsync, can concurrent
   writers to one run's ledger interleave or lose the last record on crash? Do we
   add a real lock + fsync, add a positional/offset index, or deprecate JSONL for
   any multi-writer/hosted use in favor of SQLite?
4. **Replay equivalence.** What exact guarantee do we promise: replaying the
   ledger reconstructs identical run/UI state, and streaming delivers the same
   record set as replay (SSE is a strict optimization over replay)?
5. **Ordering under multiple writers.** Can a single run's ledger receive appends
   from more than one writer (parent + subworkflow child records, wait-repair
   paths)? Is a monotonic per-run order actually guaranteed? SQLite's
   `UNIQUE(run_id, seq)` + atomic increment gives it; JSONL does not.

## Invariants any streaming/store change MUST preserve (to be encoded as tests)

- I1 — Replay completeness: `list(run_id)` returns every appended record for a
  terminal run, in append order, on both backends.
- I2 — Streaming ⊆ replay: the set/order of records delivered by SSE equals a
  replay from the same starting cursor (SSE never invents, drops, or reorders).
- I3 — Cursor monotonicity + stability: a cursor value denotes the same logical
  position before and after reconnect, AND after a file→SQLite migration.
- I4 — Recovery preserved: concatenated/truncated JSONL lines still recover
  (the `raw_decode` path) and recovery is observable (`#FALLBACK`).
- I5 — No terminal hang: a client always receives a definitive terminal/`done`
  signal (see 0075 terminal-as-event) and never blocks after the last record.

## What to do in this spike (no behavior change yet)

1. Write a replay-equivalence test harness: build a run with a known ledger,
   assert I1-I5 on BOTH backends and ACROSS a `migrate.py` migration. This harness
   is the guardrail that 0075/0076 must keep green.
2. Decide and document the durable cursor (recommendation: adopt the SQLite
   per-run `seq` as the canonical cursor; define how JSONL maps to it — persist a
   seq in JSONL records or a sidecar, or mark JSONL as replay-only/no-live-tail
   for hosted). Record in an ADR.
3. Fix `migrate.py` to preserve order+count exactly and to carry/reconstruct the
   cursor mapping (or refuse and warn if it cannot).
4. Decide the JSONL append durability posture (lock+fsync vs deprecate for
   hosted) and correct the false AGENTS.md note.

## Dependency boundary

The cursor + `list_after` promotion into the `LedgerStore` ABC lives in
AbstractRuntime (Gateway's only boundary). The test harness and cursor contract
can be authored from Gateway against the Runtime stores. `migrate.py` is
Gateway-local.

## Why

Turns "don't break replay" from a hope into an enforced invariant set with a
regression harness, and pins the cursor contract BEFORE the streaming redesign
touches it.

## Promotion criteria

Promote FIRST, ahead of 0075 implementation. 0075 and 0076 must not merge until
this harness exists and passes.

## Validation to require on promotion

- The replay-equivalence harness (I1-I5) passes on file and SQLite backends and
  across a migration; the durable-cursor ADR is written; `migrate.py` preserves
  order+count (test-proven); the false lock/fsync note is corrected.
