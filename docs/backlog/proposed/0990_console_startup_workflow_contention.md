# 0990-gateway: [IMPROVEMENT] Reduce console startup contention with workflow loading

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: proposed
> Priority: P2
> Labels: console, performance, seat-framework
> Work-item id: abstractgateway-0990
> Owner: framework
> Thread: unavailable (Agora connection refused)
> Hub refs: none

## Evidence and proposed work

Catalog-specific repeated work and blank-screen gating were fixed by 0986.
The final first browser after restart still measured 14.169s to usable cards
and 14.995s to the full catalog; a subsequent warm browser measured 4.279s /
4.484s and warm HTTP catalog 2.231s. Health reported runner.building=true during
startup. The live diagnostic profile also contained substantial bundle opening,
compilation and run-index scanning; its cross-thread timings are not a reliable
per-function SLA, so isolate contention before choosing an implementation.

Measure cold/warm request phases, bundle compilation duplication, GIL/CPU share,
and the console's initial unrelated discovery reads. Preserve Runtime-owned
compilation and truthful readiness. Consider reuse/scheduling only after data;
do not install workers/services or silently omit bundles. Validate first paint
under startup load, all models/versions, auth and operation freshness.
ADR impact: assess if changing execution ownership; none established here.
Related: completed 0986 and its retained validation artifact.
