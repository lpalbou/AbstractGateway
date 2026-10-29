# 0943-gateway: Incident ledger: every unavailability, overload and restart is recorded and reportable

> Package: abstractgateway
> Type: reliability
> Status: proposed
> Created: 2026-10-27
> Priority: P1
> 2026-10-29: first writer beyond the watchdog: the model host supervisor (ADR 0002, 0967) writes `incidents/model-host-*.json`, schema `abstractgateway.incident.v1` (kinds `exit`, `memory_ceiling`, `unresponsive`, `gave_up`).
> Labels: resilience, incidents, watchdog, observability, performance

## Summary

The operator has seen the gateway unavailable, overloaded or restarting several times without a record that
explains it. Today only the event-loop watchdog writes an incident file
(`loop_watchdog.py`, `<data dir>/incidents/watchdog-<stamp>.json`, schema
`abstractgateway.watchdog_incident.v1`) and the console shows the newest one. Overload (slow responses,
refused connections, queue depth), failed health probes, supervisor restarts, split-mode runner loss, out
of memory and clean restarts leave nothing behind.

Make incidents a first-class ledger: one append-only store (`<data dir>/incidents/`, JSONL + the existing
files) with a typed record per incident (kind, start, end, cause when known, load sample at the time:
open connections, active runs, SSE subscribers, TTS streams, event-loop lag, memory; stack when captured;
the process that failed), written by the watchdog, the supervisor, the health probe, the overload guard and
the lifespan hooks; `GET /api/gateway/admin/incidents` and `abstractgateway status --incidents`; a console
panel; a weekly summary in the operator's notifications (0992).

## Why

Root 1060 (standing duty): "any such incidents should be logged, so we can understand/benchmark/report the
problems and see how to fix it."

## Approach

1. Record schema `abstractgateway.incident.v1` with the fields above; the watchdog record becomes one kind.
2. Writers: watchdog (exists), `tray_supervisor.py` restarts, health probe failures, an event-loop lag
   sampler (the runtime reliability track's 0052 tripwire and 0054 counters feed it), uvicorn lifespan
   start/stop with the reason (signal, exception, watchdog exit code), split-mode runner loss.
3. Readers: admin route, CLI, console; retention by count and age.
4. An overload guard with explicit 503 + Retry-After when the loop lag or queue depth passes a limit,
   recorded as an incident, instead of silent slowness.

## Validation

- Tests: each writer produces a record (delete the writer, the test goes red); the route is admin-only in
  the authorization contract test; a load test (0944's drill) produces overload records with numbers.
- Evidence in the release report: the incident table of the release week.

## Links

- Root 1060, 1054; gateway `loop_watchdog.py`, `tray_supervisor.py`, `service.py`; runtime reliability
  track 0045/0052/0054; 0944.
