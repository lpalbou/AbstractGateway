# Proposed: Event-driven ledger streaming (fix the 250ms full re-parse)

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 3 (scale the signature feature)
- Effort: M (single-node) + S (multi-worker bridge)

## ADR status
- Governing ADRs: None. Consumes the durable-cursor contract + replay-equivalence
  invariants from 0082 (which must land first).

## Hard prerequisite: 0082

This item MUST NOT merge before 0082 (Ledger replay integrity and durable cursor)
establishes the durable cursor definition and a replay-equivalence test harness
(invariants I1-I5). Replay-first is the core contract; streaming is only an
optimization over replay. Every change here is gated on keeping the 0082 harness
green — SSE must deliver exactly the record set/order a replay would, never
inventing, dropping, or reordering records, and never leaving a client hung.

## Context

Replay-first SSE is the product's signature capability. Its current
implementation has an O(clients x ledger_size / interval) cost that collapses at
modest scale. This item is the synthesis of a three-way adversarial design
review (in-process pub/sub vs durable-log tail vs correctness referee).

## Current code reality (verified in gateway + runtime)

- `routes/gateway.py` `stream_ledger._gen()` (~7619-7665) calls
  `svc.host.ledger_store.list(run_id)` — a full read + JSON parse of the entire
  ledger file — inside a `while True` loop with `await asyncio.sleep(0.25)`. Full
  re-parse every 250ms per SSE client; 50 clients on a long run is ~200 full-file
  parses/second. `get_ledger` (~7541) also loads the full list then slices.
- The SSE cursor is a list index (position in the parsed list), not a durable
  record id; `Last-Event-ID` is not honored on reconnect.
- `stores.py` (~42) wraps the ledger in `ObservableLedgerStore`, whose
  `subscribe()` is never called anywhere in gateway source — the fix hook exists,
  unused.
- The SQLite ledger backend already implements an unused `list_after(seq)`
  cursor read; the JSONL backend has no incremental read.
- Terminal detection polls run status every ~0.75s; the terminal signal is
  run-store state, not a ledger record, creating a "last record emitted but close
  signal lost" hang risk.
- `migrate.py` (file->SQLite) renumbers records and can drop recovered lines,
  which invalidates any index-based cursor and is a data-loss vector regardless
  of streaming design.

## Problem or opportunity

The signature feature has a hard, embarrassing scale ceiling, and its cursor is
not reconnection- or migration-safe.

## What we might want to do (synthesis of the three reviews)

1. Stable cursor: embed a per-run monotonic sequence number in each ledger record
   and make the SSE cursor that seq (not a list index). It must survive reconnect
   AND the file->SQLite migration. Fix `migrate.py` to preserve seq and recovered
   records (do this regardless of the streaming change).
2. Incremental read: promote `read_after(run_id, seq)` / `last_seq(run_id)` into
   the AbstractRuntime store contract. SQLite: `WHERE seq > ? ORDER BY seq` (the
   `list_after` code already exists). JSONL: a byte-offset sidecar index or a
   stat-gated line-skip; consider deprecating JSONL for hosted/multi-writer use.
3. Wakeup-only pub/sub: wire `ObservableLedgerStore.subscribe` so `append` signals
   subscribers via `loop.call_soon_threadsafe` with a coalescing maxsize-1 dirty
   queue per run; the SSE generator then does a bounded incremental `read_after`.
   Push a signal, not records (the append path is multi-threaded with no seq in
   the callback), so correctness comes from cursor-gated reads, not from ordering
   pushed payloads. Keep a long fallback poll (2-5s) as a safety net.
4. Delivery + terminal close: at-least-once with an idempotent client (dedupe by
   seq); represent terminal as a ledger event or send an explicit `done` frame so
   a client never hangs waiting for a lost status transition.
5. Runner wakeups: replace the 250ms scan with a dirty-set + timer-heap; make
   `_apply_emit_event` (today scans up to 10,000 WAITING runs) a `wait_key`-indexed
   lookup, and resume subworkflow parents via the existing `parent_run_id` pointer
   instead of scanning.

## Dependency boundary

Steps 2 and the store cursor need AbstractRuntime store-contract additions
(Gateway's only boundary). Steps 3-5 are Gateway-local given the store API.

## Honest ceiling (multi-process)

In-process pub/sub only helps within one process. The shipped split-runner mode
(`serve --no-runner`) and any multi-worker/multi-pod deployment defeat in-memory
notify: a subscriber on process B won't see an append on process A. The
zero-dependency bridge is indexed tail polling (now O(new rows), not O(file)) per
process; reach for Postgres `LISTEN/NOTIFY` only if a Postgres store lands
(see 0076). Do not build a broker for the single-node default.

## Why

Removes the scale ceiling on the core demo and gives clients a
reconnection/migration-safe cursor — a prerequisite for the SDK (0071), webhooks
(0073), and the OpenAI facade (0074).

## Promotion criteria

Promote AFTER 0082 lands (durable cursor + replay-equivalence harness). Then the
single-node design is high value with bounded scope; the multi-worker bridge
follows demand (see 0076 triggers).

## Validation to require on promotion

- Benchmark: N SSE clients on a long run cost O(new records), not O(N x file).
- Test: reconnect with `Last-Event-ID` resumes exactly; no missed/duplicated
  records beyond idempotent dedupe; terminal always closes the stream.
- Test: cursor remains valid across a file->SQLite migration.
