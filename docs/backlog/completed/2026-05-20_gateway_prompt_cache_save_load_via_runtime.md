# Completed: Gateway prompt-cache save/load via Runtime

## Metadata
- Created: 2026-05-20
- Status: Completed
- Completed: 2026-05-21

## Context

Gateway still exposes provider-local prompt-cache save/load behavior, but the
current implementation is not on an acceptable boundary:

- it reaches through `runtime._abstractcore_llm_client`
- it requires direct provider-instance access
- it inspects provider-private cache state

This is not the primary app-facing durable prompt-cache contract. The cleaner
normal app path is durable blocs plus `prompt_cache_binding` through Runtime.
This proposal is only for the separate operator/admin snapshot gap that may
remain afterward.

The feature is still useful, especially for local operator workflows, but the
implementation should not remain a Gateway-side hack.

## Problem

Gateway is currently acting like provider-specific orchestration code instead of
asking Runtime for a supported admin operation.

This is now a dedicated prompt-cache-specific boundary exception after the
Runtime facade migration for discovery, residency, prompt-cache control,
durable blocs, and run-scoped media. The other remaining non-Runtime Gateway
edges are tracked separately in the workspace/comms/Telegram cleanup item and
should not be conflated with this local-admin snapshot work.

## What we wanted to do

Once Runtime exposes a public local-admin prompt-cache save/load surface,
Gateway should adopt that surface and remove the private provider/state reach-through.

## Desired outcome

- Gateway calls Runtime only.
- No direct provider-private prompt-cache fields in Gateway route code.
- The API is explicit that save/load is local-admin behavior, not a generic
  remote/server prompt-cache contract.
- Flow does not surface provider-private cache save/load until Gateway can
  advertise a public Runtime-backed contract for it.

## Dependencies

- `../../../../abstractruntime/docs/backlog/proposed/2026-05-20_runtime_local_admin_prompt_cache_save_load.md`
- `../completed/2026-05-20_gateway_durable_bloc_prompt_cache_contract.md`
- `../planned/0050_gateway_runtime_boundary_cleanup_for_workspace_comms_and_telegram.md`
- `src/abstractgateway/routes/gateway.py`

## Non-goals

- Do not preserve the current private-provider hack as the long-term design.
- Do not promise remote/server save/load until Runtime and Core have a real
  public contract for it.

## Completion

Gateway now keeps the compatibility route paths:

- `GET /api/gateway/prompt_cache/saved`
- `POST /api/gateway/prompt_cache/save`
- `POST /api/gateway/prompt_cache/load`

But those routes are now thin adapters over Runtime's public host facade:

- `list_prompt_cache_exports(...)`
- `prompt_cache_export(...)`
- `prompt_cache_import(...)`

The Gateway-side private/provider-specific snapshot logic was removed:

- no `runtime._abstractcore_llm_client` reach-through
- no direct `get_provider_instance(...)`
- no provider-private `_prompt_cache_store` / `_gguf_prompt_cache_*` handling
- no Gateway-owned `prompt_cache/<provider>/<model>` snapshot tree

Gateway's local bundle runtime now passes `prompt_cache_export_root_dir` to
Runtime so host-local exports stay under the Gateway data dir at
`prompt_cache_exports/`.

## Validation

- `PYTHONPATH=src:../abstractruntime/src:../abstractcore python -m pytest -q tests/test_gateway_prompt_cache_endpoints.py tests/test_capabilities_endpoint_contract.py`

## Follow-ups

- Gateway now depends on Runtime's public export/import surface; release/publish
  should happen only once the corresponding Runtime version is actually
  installable from the target package index.
