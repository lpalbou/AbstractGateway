# Proposed: Gateway prompt-cache save/load via Runtime

## Metadata
- Created: 2026-05-20
- Status: Proposed
- Completed: N/A

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

## What we want to do

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

## Why it is proposed, not planned

- Gateway should not implement the new surface before Runtime defines the public contract.
- The durable app-facing bloc contract should be handled first.
- The adoption work is straightforward once the Runtime-side API shape is chosen.
