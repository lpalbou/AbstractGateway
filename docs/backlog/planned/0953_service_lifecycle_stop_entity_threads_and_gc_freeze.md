# 0953-gateway: [FIX] Service lifecycle: a stopped service stops its entity threads; `gc.freeze()` no longer pins evicted services

> Package: abstractgateway (service.py, entity_chat.py, entity_visits.py, entity_repair.py)
> Type: fix (product)
> Status: planned (operator-approved for the 0.16.0 line in 0945; not started)
> Created: 2026-10-28
> Priority: P1
> Labels: memory, threads, gc, resilience, lifecycle
> Origin: follow-up of completed [0945](../completed/0945_test_suite_object_leak_and_time_sinks.md) ("Service lifecycle (product, operator-approved for 0.16.0, not started)"), promoted from a note to an item
> Root link: AbstractFramework recurrent 1060 ([relative link in a monorepo checkout](../../../../docs/backlog/recurrent/1060_gateway_security_resilience_and_latency_standing_duty.md))

## Summary

Two product leaks found while fixing the test suite (0945). (1) `EntityChatHost`, `EntityVisitHost` and
`start_repair_sweeper` start daemon threads that loop forever (`while True: time.sleep(interval)`) and are
never stopped by `_stop_gateway_service_instance`; each holds its host and, through it, the whole service.
A rebuilt or evicted service (`invalidate_gateway_service_for_runtime`) leaves three threads and its object
graph behind (4,053 such threads at the end of one full test run). (2) `serve` calls `gc.freeze()` after
boot; objects alive at the freeze that later die in a cycle (an evicted per-principal service, a registry
replaced on reload) are never collected because nothing calls `gc.unfreeze()`.

## Current code reality (origin/main 8369e5fb)

- `entity_chat.py:1223`, `entity_visits.py:530`, `entity_repair.py:447`: `while True:` reaper/sweeper loops.
- `service.py:1830` `_stop_gateway_service_instance`; `service.py:685` `invalidate_gateway_service_for_runtime`; `service.py:1257-1301` the GC tuning and `gc.freeze()`.

## Chosen route

1. One `threading.Event` per background worker: the loop is `while not stop.wait(interval)`; the service stop sets every event and joins with a bound; `unregister_worker` is identity-checked so a new service's worker is never stopped by the old one's stop.
2. After an eviction or rebuild: `gc.unfreeze(); gc.collect(); gc.freeze()` off the event loop, rate-limited (at most once per N seconds), logged with the freed count.
3. Measurement: live threads and RSS over 20 service rebuilds in one process, before and after (0945's method); a test that fails if threads grow with rebuilds.

## Open decision

- Rate limit for the re-freeze (proposed 60 s) vs only at idle.

## Acceptance criteria

- [ ] 20 rebuilds: thread count flat, RSS within a stated bound; the test is red with the Event removed.
- [ ] Evicted services are collected (weakref check) with the re-freeze in place.
