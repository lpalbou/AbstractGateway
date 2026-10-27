# 0945-gateway: [FIX] The test process kept every re-imported app alive; GC tuning leaked across tests

> Package: abstractgateway (tests/conftest.py, tests/app_reimport.py, apps_manager.py)
> Type: fix (test harness, one product default)
> Status: completed (2026-10-27, unreleased)
> Created: 2026-10-27
> Priority: P1
> Labels: tests, performance, memory, ci, gc
> Links: framework root recurrent 1060 (`docs/backlog/recurrent/1060_gateway_security_resilience_and_latency_standing_duty.md` in the AbstractFramework root; [relative link in a monorepo checkout](../../../../docs/backlog/recurrent/1060_gateway_security_resilience_and_latency_standing_duty.md)): latency budget, load drills

## Summary

The pytest process grew by ~13,500 objects per test (whole FastAPI route tables). CI logs showed
29-35 M objects alive late in the run (`[INFO] gc: froze 35404272 objects alive after boot`), the suite went
from 22 to 64 minutes on the 7 GB runner, and timing tests failed on multi-second GC pauses.

## Root cause

1. `tests/conftest.py` `_reset_gateway_service_between_tests` pops `abstractgateway.app` from `sys.modules`
   around every test, so each test that imports the app re-executes `app.py` and builds a fresh FastAPI app
   (~50,000 objects). FastAPI 0.142 memoises "is this endpoint a coroutine / generator" in three
   module-level `lru_cache(maxsize=4096)` tables keyed by the endpoint function
   (`fastapi/dependencies/models.py:137,167,197`, `_CallIdentity`). The five endpoints `app.py` defines
   (`/docs`, `/redoc`, `/`, `/console`, `/api/health`, `app.py:587-608`) are new functions on every import;
   a function holds its module's globals, which hold `app`. Every re-imported app stayed reachable until
   ~800 later imports pushed it out. The package attribute `abstractgateway.app` also kept the last one.
2. Tests that run `cli.main(["serve", ...])` record GC tuning process-wide (`service.set_gc_tuning`,
   `service.py:1245`); every later boot in the process then ran `gc.freeze()` (`service.py:1277`) over
   everything alive, the retained apps included. Frozen objects are never collected.

Production imports the app once per process (`cli.py` hands `abstractgateway.app:app` to uvicorn), so the
leak is the harness's.

## Fix

- `tests/app_reimport.py` `drop_gateway_app_module()`: pops the module, deletes the package attribute, and
  clears every `lru_cache` defined by a loaded `fastapi.*` module (found by kind, not private name).
- `tests/conftest.py`: the fixture uses it before and after every test; it resets `_gc_tuning_on` /
  `_gc_tuning_report` (monkeypatch), stops the runner, joins the `gateway-gc-tuning` thread, then
  `gc.unfreeze()` and restores the thresholds (in a `finally`).
- `tests/test_harness_reimported_app_is_freed.py`: four re-imports of a warmed, served app are all freed
  (weakrefs); a paired test proves the conftest frees the previous test's app; a paired test proves a test's
  GC tuning (choice, freeze, thresholds) does not reach the next test. Mutations: 6/6 red (no cache clear,
  no package attribute delete, both conftest drops reverted, no tuning reset, no unfreeze, no threshold
  restore).
- Time sinks (harness-side): `apps_manager._stop_pid` / `stop_leftover` read `STOP_TIMEOUT_S` at call time
  (a default bound at import made the leftovers test's own monkeypatch dead: 5 s instead of 0.3 s; pinned by a
  timing assertion, red without the change); the leftovers fixture uses a 1 s stop timeout; the two
  entity-replay stream tests wait for a 0.3 s heartbeat instead of 5 s; the two ledger-stream latency tests
  learn that the stream is open from the route's own run load (TestClient buffers a streamed body, so the
  event they set after `client.stream()` fired only after a 5 s timeout).

## Validation

- 300-test sample (every 14th test, same node ids, `gcw2` plugin counting after teardown):
  before 1.27 M objects at test 50, then everything frozen (1.62 M at 100, 2.29 M at 150, 3.00 M at 200,
  3.62 M at 250), peak RSS 1.36 GB; after 435 k, 445 k, 446 k, 452 k, 508 k (no freeze), peak RSS 0.55 GB.
- Full suite, this Mac (M-series), `/usr/bin/time -l`: before (main 227c6890) 4102 passed, 8 failed,
  2 errors in 63.4 min (97 min wall), peak RSS 10.1 GB, 36-44 M objects frozen from test ~1000 on;
  after (leak fix) 4113 passed, 2 failed, 2 errors in 24.3 min (34 min wall), peak RSS 1.22 GB, objects
  715 k at test 500 and 1.26 M at test 4000. The six timing tests that failed before
  (`test_r13_gateway_concurrency` x3, `test_r13_chunked_stream_keeps_loop_free`,
  `test_perf_routes_off_loop`, `test_gateway_history_bundle_endpoint`) pass. The 2 failures and 2 errors
  left fail on main too (docs-site exclusion, `schedule_run` registry, network/`lms` guard on this machine).
- The time-sink fixes were measured per file (leftovers 50 tests 12.7 s; f4 10.5 s -> 1.7 s; backlog drain
  5.7 s -> 1.2 s; ledger-stream latency 5.7 s -> 1.8 s and 1.6 s), not in a further full run.

- CI on this branch (faster suite) exposed a pre-existing race in `tests/e2e_events` (seen before on
  `fix/webhook-test-vector-not-scannable`): a webhook call or feed poll made right after
  `POST /automations` found the controller not yet parked on its wake key (`woken` empty;
  `abstractruntime/triggers/connections.py:732-735`). `e2e_support.create_automation` now returns once the
  controller is armed. Reproduced locally by delaying the runner's first tick of each run by 1.5 s
  (both CI failures, red), green with the fix; 5 consecutive runs of `tests/e2e_events` +
  `test_automations_api.py` green.

## Follow-ups (not done here)

- **Entity background threads keep every service alive (product).** `EntityChatHost` (`entity_chat.py:460`),
  `EntityVisitHost` (`entity_visits.py:512`) and `start_repair_sweeper` (`entity_repair.py:454`) start daemon
  threads that loop forever (`while True: time.sleep(interval)`) and are never stopped by
  `_stop_gateway_service_instance` (`service.py:1806`). Each thread holds its host and, through it, the
  service. At the end of a full run 4,053 of them were alive (1,356 visit reapers, 1,349 chat reapers,
  1,348 self-repair sweepers). In production a stopped service (`invalidate_gateway_service_for_runtime`,
  a full rebuild) leaves its three threads and its whole object graph behind. Fix: an Event per thread
  (`wait(interval)` instead of `sleep`), set and joined by the service stop, with an identity-checked
  `unregister_worker`.
- **`gc.freeze()` in `serve` makes later cyclic garbage permanent (product).** Objects alive at the freeze
  (`service.py:1277`) that die afterwards in a cycle (an evicted per-principal service, a registry replaced on
  reload) are never collected; nothing calls `gc.unfreeze()`. Consider `unfreeze(); collect(); freeze()`
  off the loop after an eviction or rebuild, rate-limited.
- **Exit delay after the summary, not root-caused.** The first after-fix run spent 9.6 min between pytest's
  summary line and process exit (baseline: 34 min); a second run (no `-s`, no histogram plugin) exited in
  3 s. A run that reproduces it should dump the stacks of threads alive at `pytest_unconfigure`.
- Ten test modules import the app at module level and keep the one app built at collection (bounded).
- **Parallel CI (pytest-xdist) not adopted yet.** `-n auto --dist loadfile` ran the full suite in 1:50-2:22
  on an 18-core Mac (serial 24 min) with peak RSS 0.6 GB per worker, but 1 of 4 runs had 16 failures
  (`test_gateway_workflow_governance` x11, plane isolation, run-helper LLM resolution: "gateway boot
  failed" / "Service pre-warm failed"): shared state between worker processes still to find. Two
  parallel-only defects fixed on the way: `serve`'s non-daemon-straggler belt (`cli.py:60`, `os._exit(0)`)
  killed a worker (now named on stderr in tests, `conftest._serve_never_exits_the_test_process`), and
  `test_untrusted_input_run_is_never_offered_an_mcp_tool` read a parked root run as done before the
  agent's model call.
- **Service lifecycle (product, operator-approved for 0.16.0, not started):** the entity thread leak and the
  `gc.freeze` finding above, with a live-thread and RSS measurement over 20 service rebuilds.
