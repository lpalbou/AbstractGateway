# 0952-gateway: [TEST] Concurrency correctness suite and benchmark: N users x M sessions on a real server

> Package: abstractgateway (tests/concurrency, scripts/perf); abstractruntime (ledger `seq`, runner, session tool policy) under test
> Type: test + benchmark
> Status: planned (not implemented)
> Created: 2026-10-28
> Priority: P1 (high)
> Labels: concurrency, isolation, ledger, sse, tool-policy, performance, benchmark
> Root link: AbstractFramework backlog 1064 (`docs/backlog/planned/1064_concurrency_correctness_and_benchmark.md`; [relative link in a monorepo checkout](../../../../docs/backlog/planned/1064_concurrency_correctness_and_benchmark.md)); recurrent root 1060

## Summary

A real-server scenario suite where (a) one user and (b) several users each run several sessions at once,
asserting routing to the right session, no cross-talk, ledger integrity (`seq` gap-free, monotonic per
run), SSE delivery (ledger, view, `/me/events`; reconnect with `Last-Event-ID`) and session tool-policy
isolation; plus `scripts/perf/bench_concurrency.py` reporting p50/p95/p99 latency, throughput, CPU, RSS,
threads and store lock waits over a users x sessions matrix on JSONL and SQLite.

## Starting points (origin/main 8369e5fb)

- `tests/test_r13_gateway_concurrency.py`: real uvicorn + fake CPU-bound engines; health under generation + read-aloud; admission bound; TTFA. Reuse its server/client harness.
- `tests/test_resume_concurrent_same_wait.py`; isolation one request at a time: `test_gateway_principal_isolation_matrix.py`, `test_gateway_session_history_isolation.py`, `test_gateway_attachment_session_isolation.py`, `test_automations_plane_isolation.py`; streams: `test_gateway_ledger_stream_cursoring.py`, `test_gateway_live_delta_stream.py`, `test_events_stream_*`.
- `scripts/bench_replay_view.py` + `_bench_replay_server.py` (a gateway per backend, seeded run tree, psutil CPU, wire bytes); `scripts/perf/bench_gateway.sh` + `bench_driver.py` (`--fail-p99` gates, idle CPU/GC; `scripts/perf/ci.sh`).

## Chosen route, assertions, open decisions

As root 1064 (fake deterministic model echoing a per-session nonce; approval waits in some sessions, an
approve-all session policy in others; each assertion proven red by an injected fault; a CI smoke size and a
full size run before each release wave). Open: the CI budget and the gate numbers, set from the first baseline.

## Acceptance criteria

- [ ] Suite green on JSONL and SQLite; each assertion red under its injected fault.
- [ ] Baseline JSON + summary committed with commit ids; CI smoke gate wired in `scripts/perf/ci.sh`.
