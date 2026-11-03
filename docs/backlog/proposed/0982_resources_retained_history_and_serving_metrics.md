# 0982-gateway: [IMPROVEMENT] Retain resource history and correlate serving bottlenecks

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: proposed
> Priority: P3
> Labels: resources, observability, history, seat-framework
> Work-item id: abstractgateway-0982
> Owner: framework
> Thread: unavailable (Agora connection refused; local operator request)
> Hub refs: none

## Summary

Consider shared retained resource samples, authoritative model-operation events,
queue depth, time to first token, generation tokens/s, p50/p95 latency, loop lag,
errors and platform-supported pressure/thermal telemetry after 0980/0981.

## Current code reality

The original `design/resources` prototype simulates history. Current host APIs
return snapshots; model and run truth comes through Runtime. The implementation
track starts with explicitly browser-local bounded history and available probes.
Do not mistake that for monitoring while the browser is closed.

## Proposed direction and promotion criteria

Promote once an operator needs retained/offline history or serving bottleneck
diagnosis, and sample ownership, retention, multi-user access, overhead budgets
and Runtime inference metric sources have been agreed. Prefer shared bounded
sampling, real timestamps and gaps; correlate events without asserting causation.
Verify per-provider/model denominators before aggregating latency or throughput.

## Non-goals and ADR state

No authorization here for permanent collectors, machine services, privileged
tools, automatic eviction or a new metrics API. Reassess ADR 0001/0002 boundaries
and record any durable collection policy in an ADR before implementation.

## Validation ideas

Profile simultaneous tray/console sampling; test restart gaps, independent probe
failure, retention bounds, authorization, model/provider attribution and no
inference slowdown. Use real-model validation only when specifically authorized.
