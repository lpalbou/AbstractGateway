# 0986-gateway: [IMPROVEMENT] Bound model catalog fetch latency while keeping all versions reachable

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Priority: P1
> Labels: catalog, performance, seat-framework
> Work-item id: abstractgateway-0986
> Owner: framework
> Thread: unavailable (Agora connection refused)
> Hub refs: none

## Summary and evidence

0984 improves initial DOM work (4-bit and five versions per card), but the
client still requests the complete `hub=true&extended=true` catalog. The first
post-restart live request completed with 96 models/1,798 versions; subsequent
fresh browser reads waited beyond 30s and an authenticated HTTP probe timed out
at 12s, while health stayed healthy and the new spinner remained visible.
Do not mistake the frontend precision filter for server-side quant projection.

## Current code reality

`console_catalog.py:MC_CATALOG_PATH` always asks the extended catalog;
`mcLoad` also waits for the parallel installed-model inventory before painting.
`routes/gateway.py:models_catalog_get` delegates to `core_model_catalog` through
`_core_host_call`; AbstractCore `config/model_catalog.py:catalog` owns catalog
assembly, Hub caching, fit and presence semantics. The exact latency cause has
not been isolated; this is a proposal, not permission to bypass these owners.

## Possible next work

Measure catalog vs installed-inventory latency and in-flight request behavior.
Evaluate rendering the first truthful inventory as soon as it arrives, safe
cache/coalescing improvements, or a documented progressive/server-filtered
contract. Keep every precision and matching version accessible through All and
expand; preserve accurate incomplete/loading labels, provider truth and errors.
Do not introduce silent caps, fabricated sizes or background polling services.

## Validation expectations

A delayed/failing provider fixture must not hold useful catalog data behind an
unrelated inventory. Measure cold/warm p50/p95; repeated refreshes must not create
unbounded duplicate work. Preserve full reachability, filters, downloads and
explicit failure/retry behavior. Architecture changes require owner review/ADR
assessment before implementation. Related: completed 0984/0985.

## Authorized implementation — 2026-10-30

The operator reports a stuck loading screen and requests investigation/fix.
Measured live catalog >20s and installed discovery 10.818s. Repeated runtime
stack samples locate catalog CPU time in `llamacpp_knows_architecture`: each
GGUF artifact rescans the entire installed library for sentinels and names.
The library bytes are cached but membership results are not. Fix this in
AbstractCore, preserving library-change invalidation and unknown semantics;
apply the isolated commit to the clean deployment checkout. Decouple catalog
rendering from installed discovery in the console and coalesce repeated loads.
Prove delayed inventory cannot hide catalog data, preserve errors/retry and
all versions, measure live latency before/after, and gracefully redeploy.
ADR impact: none; fixes within existing owners/contracts. Agora unavailable.

## Completion report

Completed 2026-10-30. Promoted from proposed by the direct operator request;
intake checkpoint `1c779c52`. Original path: `proposed/0986_catalog_fetch_latency.md`,
then `planned/0986_catalog_fetch_latency.md`; final path:
`completed/0986_catalog_fetch_latency.md`. Historical audit above is retained.

Core source commits `57d28e86`, `ad8d1707`, `522fe653` cache architecture membership
per library bytes (path/mtime/size invalidation remains), resolve HF cache roots
once per existing presence sweep, and share compatibility/provider facts within
that sweep. The isolated commits were cherry-picked to the clean existing local
deployment worktree as `3ca06942`, `0d47b689`, `83709403`. Core owns all presence,
fit, compatibility and catalog assembly semantics; no Gateway duplication.

Gateway `ce0c2e12`, `054f45d1`, `703af90b`, `5bcd4d8a`: render independently of optional
installed discovery; share concurrent browser and identical authorized HTTP
reads without retaining completed answers; shield other waiters from disconnects;
show an initial curated catalog through the existing API while always fetching
the full extended result. Preview counts explicitly say “so far”; loading more
versions is visible; empty filters during enrichment do not claim a final empty
catalog. Full results replace previews; late previews cannot overwrite them.
Visible data survives refresh/enrichment failures with explicit errors and retry.

Validation: 106 Core tests, 30 Gateway concurrency/endpoint tests, 81 console
regression tests passed. Browser checks: 112 catalog/wizard and 122 Resources,
zero failures. Tests cover snapshot freshness/invalidation, canceled waiters,
failed-read retry, pending enrichment and usable preview, delayed/failed installed
inventory, complete version reachability, exact fixture operations, permissions,
mobile, light/dark and the shared widgets. [Evidence and review](../../../design/catalog-loading-0986-0988/REVIEW.md).

Measured original catalog 26.983s and installed inventory 10.818s. Intermediate
live first paint 54.872s. Final warm live browser: cards 4.279s, full data 4.484s;
warm HTTP full catalog 2.231s. First final browser after restart: 14.169s / 14.995s
under workflow-startup contention. Samples are not a p95 SLA. Residual startup
contention is proposed as [0990](../proposed/0990_console_startup_workflow_contention.md).
A native crash coinciding with signal-based diagnostics was recovered by the
existing supervisor and retained as [0989](../proposed/0989_native_stack_capture_crash.md).
No further signal diagnostics were used. Temporary profiling was removed before
final redeploy. These follow-ups are proposals, not hidden implementation scope.

Live console healthy; full initial 4-bit view has 83 model matches, working
management/details/disclosure and shared themes/chrome; no page errors or live
management writes in verification. Existing graceful restart used zero in-flight
runner work. No package publication, new service or persistence. No other agent's
changes staged. ADR impact: none; existing owners/contracts and request snapshots.
No backlog/code drift or recurrent tasks found. Agora connection refused;
local evidence retained without claiming hub receipt/mirror synchronization.
