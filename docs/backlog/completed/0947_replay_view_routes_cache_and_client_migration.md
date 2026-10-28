# 0947-gateway: [FEATURE] Serve the replay view: `/runs/{id}/view`, its stream, the per-run view cache, and the client migration

> Package: abstractgateway (routes, cache, fan-out); abstractruntime (the reducer `abstractruntime.replay`, its backlog 0853 and ADR 0009)
> Type: feature
> Status: completed (2026-10-28, 0.16.0)
> Created: 2026-10-27
> Priority: P0
> Labels: replay, ledger, sse, cache, performance, thin-clients

## Summary

Serve AbstractRuntime's replay view of a run tree to every client. There are two routes:
`GET /api/gateway/runs/{run_id}/view` returns the whole view, with an ETag set to the cursor
token, a 304 on `If-None-Match`, and `?at=<token>` for the view at a cursor.
`GET /api/gateway/runs/{run_id}/view/stream` is SSE: one `view` event, then a `delta` event
for each fold, with `Last-Event-ID` set to the cursor token. Behind both routes there is one
cached view per run tree. Each appended record is folded once, and the same delta goes to every
subscriber. The plan below moves the clients onto the view, panel-chat first.

## Why

- Framework root backlog 0993 (master,
  `abstractframework/docs/backlog/planned/0993_one_ledger_replay_many_views.md`): one replay of
  the ledger, many views. Operator: "logic should not be in the thin client". "Each client decides
  what to show and how to forward requests from users and entities".
- Framework root backlog 1055 (release wave,
  `abstractframework/docs/backlog/planned/1055_release_wave_2026_10_27_voice_library_replay_memory.md`):
  the gateway's 0.16.0 release waits for this item, and the apps release after the gateway.
- AbstractRuntime backlog 0853 (`abstractruntime/docs/backlog/planned/0853_replay_view.md`) and
  ADR 0009 (`abstractruntime/docs/adr/0009_one_replay_many_views.md`) put the reducer in the
  runtime. Backlog 0853 also holds the inventory of every client's private reconstruction, with
  file:line anchors.
- Operator: "special attention to performance (caching, indexing, leveraging the runtime/ledger,
  the file<->SQL coupling)". Root recurrent 1060 makes latency a hard rule, so this item ships
  with before and after numbers.

## Current code reality (before this item)

- `routes/gateway.py` `/runs/{id}/ledger/stream` is already event-driven. It wakes on
  `ObservableLedgerStore.subscribe` and reads through `ledger_tail.py` (a JSONL byte-offset tail
  or SQLite `list_after`). The 250 ms full re-parse that 0075 describes is gone, so 0075's text
  is stale. The stream's `id:` is a count of records consumed. Because every ledger record now
  carries a dense per-run `seq`, that count equals the record's `seq`.
- Clients get run state by combining `/ledger`, `/ledger/stream`, `/history_bundle` (windowed,
  `$slim` left in) and `/runs/{id}` polls, then folding the result themselves. Backlog 0853 lists
  the divergences.
- `migrate.py` re-appended records and so renumbered `seq`. Commit `c5666390` fixed it:
  records are read by the JSONL store, then imported with `SqliteLedgerStore.import_records`,
  which keeps `seq`.

## What this item adds

- `src/abstractgateway/replay_view.py`:
  - `ViewHub` is an LRU of `ViewEntry` (64 entries). The key is the caller's ledger store (the
    plane) plus the root run. An entry with live subscribers is never evicted.
  - A refresh reads only the records after each run's folded `seq` (`list_after`: an indexed
    read on SQLite, a seek to the remembered byte offset on JSONL).
  - The refresh folds them once (`ReplayView.apply`) and broadcasts the delta to every
    subscriber.
  - The serialized view is cached per cursor token.
  - A read that changes nothing (an unchanged snapshot) produces no delta and no broadcast.
  - Reconnect: a known older token gets one delta, computed as `diff_views` between a rebuild at
    that token and now. Rows that come from snapshots are re-sent whole.
  - `?at=` rebuilds the view as the ledger stood at the token. Only the static fields of the
    snapshots are kept.
- `src/abstractgateway/routes/replay_view.py`, the two routes:
  - Authorization follows `GET /runs/{id}`: the caller's own plane, otherwise 404. The principal
    isolation matrix covers this.
  - The stream wakes on the ledger append subscription and never runs a timed re-parse.
  - When the appender may be another process (split mode, or no observable store), it checks
    `list_after` every 0.25 s. No new bytes means nothing is parsed.
  - A 5 s safety check re-reads the snapshots of open runs, for legacy ledgers without terminal
    records.
  - When the tree is terminal and drained, the stream sends `event: done`.
- `security/gateway_security.py`: `/view/stream` is counted in the SSE concurrency pool.

## Client migration plan (each client in its own repo and backlog)

Each client deletes its private reconstruction once it passes the runtime's conformance
fixtures. The order is:

1. **panel-chat** (`@abstractframework/panel-chat`, used by AbstractCode web and any embedding):
   - `WorkflowSessionController` reads `/view`, then follows `/view/stream`.
   - It keeps `apply_delta` (a TypeScript port of `abstractruntime.replay.delta`, about 40 lines)
     as its only state logic, and answers `interaction.command` through `/commands`.
   - It deletes the history-bundle fold, the `resolvedWaits` patch from 0.1.20, and the `$slim`
     handling.
   - Its conformance check: `scripts/check_approval_sync.mjs` against the runtime fixtures as
     served by the gateway.
2. **AbstractCode web** activity rows read `view.activities`, one row per execution. The dead
   modules that 0853 lists are deleted.
3. **AbstractAssistant desktop** reads the view instead of `history_seed.py` and the ledger
   follow (Assistant backlog 0008). Its approval sheet closes when `interaction` changes,
   whichever client answered.
4. **iPhone and watch:**
   - They read `interaction` (one person-facing wait).
   - Wait identity is (run_id, wait_key, step_id), because the watch's reused-key stall needs
     the step.
   - Activities are one row per execution.
5. **AbstractObserver** reads `interaction`, which the view already resolves from the child, so
   it no longer reads only the root's last record. The 2 s polls go.
6. **AbstractCode terminal client** and the **gateway consoles** (web, and the Rust terminal
   crate). The console renders `runs[]`, so the tree is visible.
7. **AbstractFlow run views** read `activities`. Its `executionIdFor` stays only for the editor's
   live node highlighting until that also reads the view.

A push dispatcher (root backlog 1058, proposed) subscribes to the same deltas. Each delta carries
its cursor token and typed `changes`. A device that opens a push requests `?at=<token>`, then
follows the stream.

## Scope

- In: the two routes and the cache; `migrate.py` keeps `seq`; tests through the real app; the
  benchmark script `scripts/bench_replay_view.py`; docs (`docs/api.md` "Replay view",
  `docs/architecture.md`); CHANGELOG; llms files.
- Out: the reducer and its fixtures (runtime 0853); each client's migration (each repo's own
  item, in the order above); a sessions index (0939); a cross-run query surface (root 1059).

## Validation

- `tests/test_gateway_replay_view.py`, run on both store backends where it matters:
  - the delegated child approval as the interaction;
  - ETag/304;
  - two clients converging on a fresh GET, with every record folded once for both;
  - a reconnect with `Last-Event-ID` (one delta), with the current token (`done` only), and with
    a foreign token (the whole view);
  - `?at=`;
  - a read that changes nothing broadcasts nothing (red when the suppression is removed).
- `tests/test_migrate_preserves_ledger_seq.py`: order, count and seq survive file-to-SQLite
  migration, and a held cursor stays valid.
- `tests/test_gateway_principal_isolation_matrix.py`: another principal's `/view` and
  `/view/stream` return 404.
- `scripts/bench_replay_view.py`: a 3,000-record run tree with 50 subscribers, before (raw
  `/ledger`, `/history_bundle` and `/ledger/stream`, with each client folding for itself) and
  after (`/view`, `/view/stream`). The numbers are recorded in the PR.

Completed 2026-10-28 (gateway part): PR #8 merged (`7d258efe`) with the validation above green in CI and the
benchmark numbers in the PR; released in 0.16.0. Each client's migration is its own repository's item, in
the order of the client migration plan.

## Dependencies

AbstractRuntime 0.14.0 (`abstractruntime.replay`, `seq` and `list_after` on every ledger store,
`SqliteLedgerStore.import_records`). The gateway floor is raised when the runtime releases.
