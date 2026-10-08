# 0846 — Publishing a workflow must not rebuild every service's runtime

**Status**: completed 2026-10-08 (round 16, R16.2; unreleased) · **Priority**: P1 · **Created**: 2026-09-18
**Package**: abstractgateway · **Related**: abstractcore 0847 (shared model pool),
abstractruntime 0845 (session cache contract), framework 0848 (supervisor wording)

## Why this is here

On 2026-09-17 the operator's gateway went "alive but not answering `/api/health`" four
times in 17 minutes. The supervisor blamed in-process inference; no run was executing in
any of the four windows. The audit log matched the probe log four for four — each window
was one `POST /visualflows/{id}/publish` (9–17 s) plus one
`POST /admin/workflow-catalog/promote` (27–53 s), sent by a desktop client reconciling its
managed workflow at launch. Measured again on 2026-09-18 after the fix below, with a
gateway that predates it: publish 22.2 s, promote 65.3 s, eight consecutive failed probes.

Every one of those requests calls `reload_bundles_from_disk()`, which rebuilds the whole
host — all bundles recompiled, the memory store reopened, a new `Runtime`, a new
`MultiLocalAbstractCoreLLMClient` and a new provider — and `reload_gateway_workflow_bundles()`
does it **once per instantiated service**. With an in-process MLX model that meant one
model load per service per publish, and a fresh (empty) prompt-cache store each time.

## What already landed (2026-09-17, uncommitted at time of writing)

- **The rebuild no longer runs on the event loop.** `_off_the_event_loop` (`routes/gateway.py`)
  hands it to a worker thread for `/visualflows/{id}/publish`,
  `/admin/workflow-catalog/promote`, `/bundles/reload`, `/bundles/upload`,
  `DELETE /bundles/{id}`, and the catalog resolution inside `/runs/start` and
  `/runs/schedule`. The host is still swapped under its own lock. Pinned by
  `tests/test_gateway_rebuild_does_not_block_event_loop.py`, which is red on the old routes
  ("/api/health was answered 1.21s after a 1.2s host rebuild began").
- **The weights are no longer duplicated per service** (abstractcore 0847): a second
  provider for the same model adopts the resident one instead of loading a second copy.

Health stays answerable and the memory blow-up is gone. The rebuild itself is unchanged.

## This package's slice

1. **Incremental reload.** When only bundle definitions changed, swap the compiled registry
   on the EXISTING runtime (`runtime.set_workflow_registry(...)`, already public and already
   used by `load_from_dir`) instead of constructing a new runtime + LLM client + memory
   store. Keep the current full rebuild as the fallback for changes that genuinely need it
   (provider/model change, capability defaults, effect handlers), and say which path ran.
2. **Reload only what changed.** `reload_gateway_workflow_bundles()` rebuilds every service
   in the process, including services whose data dir the published bundle does not belong
   to. Scope the reload to the affected service(s).
3. **Stop instantiating duplicate services.** `/api/health` on the operator's gateway lists
   four: `default/default` (inactive), `default/default` (active), `default/probe_start_local`,
   `default/user` — the global `_service` duplicating the admin principal's service over the
   same data dir. Each one is a full host. Establish whether the global default service is
   needed at all once multi-user is on, and drop the duplicate.
4. **Report the cost.** The publish/promote response should carry what the reload did
   (services rebuilt, bundles recompiled, whether a model was loaded, elapsed) so this class
   of stall is visible in the response instead of only in a supervisor log.

## Validation

- Publish + promote on a gateway with a resident 15 GB model completes without loading the
  model again and without dropping any session's prompt cache (check
  `metadata.prompt_cache.outcome` on the next turn of an existing session: `hit_restore`,
  not `cold`).
- `/api/health` answers within the supervisor's 3 s probe timeout throughout, and
  `af-stack.log` records no failed probe for the publish window.
- Publishing a bundle owned by one principal does not rebuild another principal's service.
- A change that DOES require a full rebuild (provider/model swap) still takes the full path,
  and the response says so.

## Evidence

- `runtime/audit_log.jsonl` 2026-09-17 21:49→22:06 and 2026-09-18 00:05→00:06 vs
  `runtime/logs/af-stack.log` probe failures.
- `hosts/bundle_host.py::reload_bundles_from_disk` → `load_from_dir` → `create_local_runtime`;
  `service.py::reload_gateway_workflow_bundles` (all services).
- Process footprint before the sharing fix: RSS 29.5 GB but physical footprint 113 GB
  (peak 129 GB on a 128 GB machine), ~85 GB compressed/swapped, system swap 18.3/19.5 GB.

## Completion Report - 2026-10-08

All four slices landed on branch `round16/2026-10-08-w3` (unreleased):

1. **Incremental reload.** `hosts/bundle_host.py` splits loading into
   `_compile_workflows` (bundles -> specs + a new `WorkflowRegistry`, no runtime
   touched, cached per file by path/mtime/size so a publish compiles one file) and
   `_build_runtime`. `reload_bundles_from_disk` swaps the new registry onto the
   running runtime (`registry_swap`). A service's runtime is rebuilt only when its
   workflows newly need a capability it was built without — LLM/model residency,
   tools, memory_kg (`service_reload`) — and on `POST /bundles/reload?full=true`
   (`full_rebuild`). A provider/model change never needed a rebuild: the default
   routes re-point the live client (`refresh_capability_defaults`).
2. **Reload only what changed.** A service whose files did not move does nothing
   (`unchanged_services`); a write to a user's own registry reloads only that
   user's service, the shared registry every running service.
3. **No duplicate service.** Under user auth a principal-less caller gets the
   admin's default-runtime service; `_service` is no longer built beside it.
4. **Report the cost.** Publish/upload/catalog upload/promote/`/bundles/reload`
   answer `reload {ok, kind, services, unchanged_services, duration_ms, sentence}`
   (`workflow_reload.py`) and the audit line records `reload {kind, duration_ms,
   services}`.

Also: runs in flight keep the spec they resolved (a version replaced in place
pins its RUNNING/WAITING runs; `_pin_in_flight_runs`), and the admin catalog
upload's reload moved off the event loop.

Validation:

- `tests/test_gateway_publish_without_rebuild.py` (fake engine: construction
  counted and GIL-holding, per-instance prompt-cache store): same runtime and
  client after a publish, spec identity of untouched bundles, unchanged = no swap,
  `service_reload`/`full_rebuild`, ledger `metadata.prompt_cache` of the next turn
  identical to the no-publish control (and a rebuild shown to miss), in-flight
  pinning for a new version and an overwritten one, `/api/health` p99 < 500 ms
  during 20 publishes, response + audit line, one service per data dir.
- Mutation: 12/12 mutants red (round16 scratch `w3/mutate.txt`), incl. teardown
  reintroduced (cache test red) and scope widened (health p99 1187 ms, red).
- Scratch live drive, fake engine with a 1 s GIL-holding load: before 1.0-1.3 s per
  publish, health p99 1048 ms, the next turn `miss_created`; after 3-6 ms per
  publish, health p99 2.1 ms, the next turn `hit` (222 cached tokens), one engine
  construction for 21 publishes.
- Full gateway suite: same failure set as origin/main (environment: browser tests
  without node on PATH; they pass with node except the pre-existing kit-islands
  sync check).

Residual notes:

- A sub-workflow that an in-flight run starts after its version was overwritten
  in place resolves the new spec (the runtime's sub-workflow lookup has no run
  context); new versions are unaffected (their ids differ).
- The supervisor wording (framework 0848) is the root package's; its banner can
  now point at the audit line's `reload` field.

