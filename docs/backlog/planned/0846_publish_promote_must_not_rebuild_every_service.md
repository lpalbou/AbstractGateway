# 0846 — Publishing a workflow must not rebuild every service's runtime

**Status**: planned · **Priority**: P1 · **Created**: 2026-09-18
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
