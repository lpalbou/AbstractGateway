# 1156-gateway: [INVESTIGATION] The run listing stays fast under load; the long-lived process does not slow down

> Package: abstractgateway (run listing route, run store reads, process memory)
> Type: investigation + fix
> Status: proposed
> Created: 2026-11-08
> Priority: P1 (every client's conversation list depends on it)
> Labels: performance, runs, observability

## What was seen (2026-11-08, live :8080, before and after a restart)

- 23:10–01:15: `GET /api/gateway/runs?status=running&limit=20` took 3–16 s (Board review), then did not answer within
  60 s at 01:10 while `/api/health` answered in 0.09 s; Code showed "Loading conversations…" for 12–43 s (audio review)
  and once 90 s. The process was at 121–136 % CPU and 2.1 GB RSS after ~3 h of life; the machine was swapping (15 of
  16 GB) under other sessions' Playwright and test runs, load average 24.
- 01:20, fresh process on the same code (main 6476d4e0): the same listing answered in 0.15 s; `/network` in 0.05 s;
  the cognition-profile route 8 s on its first cold call.

So the listing is fast when the process is fresh and slow after hours under load: either a lock held by a slow path
(SSE streams, a run-store scan), a per-request scan that grows with the store (~2,400 old `basic-agent` runs, 374 done
in 48 h), or memory growth that turns into swapping. The gate's own perf job passes, so this is a soak problem, not a
unit one.

## Measured 2026-11-08 04:00 (worker on a hermetic gateway with a copy of the live store, 14,411 runs)

- The cost is the run store's `--run-index verify` mode (the `serve` default, `cli.py:1101`, `stores.py:64`): every
  `list_run_index` call globs the whole run folder (`json_files.py:2310`) and stats every file (41,836 stats per
  request, `json_files.py:2316` + `_scan_fields` :631), whatever the `limit`; the route's widening loop made two such
  passes for root-only and the Observer query. Under load each stat waits on the GIL: 0.5 s becomes 30 s.
- Fix landed (gateway main `dca062f4`): the listing remembers the depth a given filter shape needed and reads once when
  warm (root-only warm 0.31–0.64 s → 0.15–0.29 s; Observer 0.31–0.64 → 0.21–0.44 s; rows identical; test
  `tests/test_gateway_runs_listing_one_pass.py`, red without the memory). The cold request still reads twice.
- `--run-index trusted` answers from memory: 38–176 ms for every query. Live health: `mode: verify`, 743 shadow
  comparisons, 0 mismatches, 165 reconciles, 0 repairs — the evidence backlog 1054 asked for before flipping.
- AbstractRuntime: `JsonlLedgerStore.count` (`json_files.py:3276`) reads whole ledger files (50–70 MB per page of 200
  rows) and ignores its `.idx`; cheap while the file cache holds, slow under swap. Runtime-side fix.

## Scope

1. Measure on the live store: time the listing route with a 3-hour-old process vs a fresh one, with `py-spy dump` (or
   the gateway's own profiler if any) on the slow process to see where the request waits (lock, I/O, GC).
2. Make the listing read an index, never scan run files per request; cap the work per request by `limit`.
3. Watch RSS and open streams over 6 h on the operator's gateway; a leak (listeners, SSE semaphores, per-run caches)
   is a defect to fix at the source.
4. Expose `GET /api/health` detail: in-flight requests, open streams, run-store size, RSS — so the next slow spell is
   diagnosable without a restart.
5. Tests: a listing over a 10,000-run store answers under 200 ms; a 1-hour soak with 6 open SSE streams keeps RSS flat.

## Related

Observer Board (reads this listing every few seconds for 5 lanes), Code conversation list, root 1140 (shared browser
connection), the 2026-11-07 connection-starvation investigation (`untracked/release-2026-11-07/connections/`).
