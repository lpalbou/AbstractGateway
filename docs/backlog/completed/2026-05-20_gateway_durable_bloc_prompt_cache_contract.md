# Proposed: Gateway durable bloc prompt-cache contract via Runtime

## Metadata
- Created: 2026-05-20
- Status: Completed
- Completed: 2026-05-21

## Context

Gateway already has a good best-effort session prompt-cache lifecycle:

- deterministic session/workflow key derivation
- prepare/rebuild/status/clear routes
- honest capability reporting for keyed and unsupported providers

That solves volatile reuse while the Gateway/Runtime process stays alive.

It does not solve exact durable reuse across app restarts or runtime eviction.
AbstractCore 2.13.22 now provides the lower-level public bloc and binding
surface needed for that. The missing piece is a Gateway contract that exposes
those durable primitives through Runtime only.

## Current code reality

- `docs/backlog/completed/030_gateway_session_prompt_cache_lifecycle.md`
  established the current session prompt-cache contract.
- `src/abstractgateway/routes/gateway.py` exposes session prompt-cache routes
  keyed around `prompt_cache_key`, not durable bloc selectors or bindings.
- Gateway currently has no app-facing `blocs/*` routes backed by Runtime.
- Gateway still carries provider-private `/prompt_cache/save`,
  `/prompt_cache/load`, and `/prompt_cache/saved` behavior. That is not the
  right public app contract and should remain separate operator/admin work.
- Runtime already owns the broader boundary for discovery, prompt-cache
  control, residency, and run-scoped media, so Gateway should extend that same
  pattern here instead of adding a direct Core proxy layer.

## Problem or opportunity

Apps running on Gateway should be able to:

1. persist a reusable prompt or memory bloc durably;
2. quit and relaunch later;
3. load the durable bloc again through Gateway;
4. obtain a fresh exact `prompt_cache_binding`;
5. reuse that binding in later runs without knowing anything about
   provider-native cache files or provider-private APIs.

Gateway cannot currently offer that cleanly.

## Proposed direction

Once Runtime ships a durable bloc facade, Gateway should expose an authenticated
client contract that mirrors the AbstractCore bloc model through Runtime only.

### Route shape

Prefer explicit Gateway-owned routes such as:

- `POST /api/gateway/blocs/upsert_text`
- `GET /api/gateway/blocs/record`
- `POST /api/gateway/blocs/kv/ensure`
- `POST /api/gateway/blocs/kv/load`

The route family should stay close to Core naming instead of inventing a
Gateway-only save/load vocabulary. The durable handle for apps should be the
bloc selector (`bloc_id` or `sha256`) plus the returned binding payload, not a
provider-specific snapshot concept.

### Contract behavior

- Gateway authenticates and enforces policy.
- Gateway calls Runtime only.
- Runtime normalizes local/remote Core topology.
- Gateway returns the Runtime/Core binding payload without rewriting it into a
  new format.
- Gateway capability discovery should advertise this as a separate durable
  prompt-cache track from session prompt-cache lifecycle.

### Relationship to session prompt-cache lifecycle

Keep the existing session prompt-cache routes.

The intended split is:

- session prompt-cache lifecycle: best-effort volatile reuse while the live
  process state exists;
- bloc contract: exact durable reuse across app restarts and runtime eviction.

Gateway should document both and should not imply that provider-private
`/prompt_cache/save|load` is the normal app path.

## Why it might matter

This gives apps a clean durable prompt-cache story at the Gateway boundary:

- exact durable reuse is stable across relaunches;
- local provider implementations stay hidden;
- Gateway remains a gateway to Runtime rather than a second Core client;
- the same contract can support prompt prefixes, durable memory blocs, and
  other prompt-cache-backed reusable text artifacts.

## Promotion criteria

- Runtime lands `get_abstractcore_bloc_facade(runtime)` or equivalent.
- Runtime publishes binding-aware generation behavior so a loaded binding can be
  used in real runs.
- Gateway maintainers confirm the client route family and capability-contract
  shape.
- The current provider-private `/prompt_cache/save|load|saved` routes are
  explicitly positioned as separate admin-only backlog work.

## Validation ideas

- Gateway route tests proving all bloc routes delegate through Runtime only.
- Capability-contract tests proving clients can distinguish:
  - session prompt-cache lifecycle;
  - durable bloc prompt-cache support;
  - provider-private admin snapshot support, if any.
- End-to-end tests with a local provider fixture:
  1. create bloc,
  2. ensure/load KV,
  3. obtain binding,
  4. start a run using that binding,
  5. restart the Gateway/Runtime fixture,
  6. reload the binding and repeat.

## Non-goals

- Do not proxy Gateway routes directly to AbstractCore.
- Do not expose provider-native cache files or raw filesystem paths to apps.
- Do not collapse this route family into the existing `/prompt_cache/save|load`
  operator/admin proposal.
- Do not remove the existing session prompt-cache lifecycle.

## Guidance for future agents

Implement this only after the Runtime bloc facade is real. Keep the design
close to Core semantics and use Runtime as the only lower-level dependency.

If a convenience layer is later desired for client SDKs, add it on top of these
routes rather than hiding the bloc/binding model in the primary contract.

## Completion Report - 2026-05-21

Implemented in Gateway on top of `AbstractRuntime>=0.4.16`:

- `POST /api/gateway/blocs/upsert_text`
- `GET /api/gateway/blocs/record`
- `GET /api/gateway/blocs`
- `POST /api/gateway/blocs/delete`
- `GET /api/gateway/blocs/kv/manifest`
- `GET /api/gateway/blocs/kv/list`
- `POST /api/gateway/blocs/kv/ensure`
- `POST /api/gateway/blocs/kv/load`
- `POST /api/gateway/blocs/kv/delete`
- `POST /api/gateway/blocs/kv/prune`

Gateway now exposes the durable bloc/KV/binding contract through Runtime
instead of treating provider-private prompt-cache save/load as the public app
story. Capability discovery and docs were updated in the same pass.

Validation performed during the integration pass:

- `PYTHONPATH=src:../abstractruntime/src:../abstractcore pytest -q tests/test_gateway_durable_bloc_prompt_cache_endpoints.py tests/test_gateway_prompt_cache_endpoints.py tests/test_capabilities_endpoint_contract.py`

Residual notes:

- provider-private prompt-cache snapshot save/load remains separate operator/admin
  work and stays tracked in `../proposed/2026-05-20_gateway_prompt_cache_save_load_via_runtime.md`
- selector and response-shape tightening can be handled as future contract polish
  if real client usage shows ambiguity
