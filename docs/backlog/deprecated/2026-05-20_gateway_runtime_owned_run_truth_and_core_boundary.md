# Gateway Runtime-Owned Run Truth And Core Boundary

## Metadata
- Created: 2026-05-20
- Status: Deprecated
- Completed: N/A
- Deprecated: 2026-05-21

## Problem

Gateway currently mixes three responsibilities in the same route layer:

1. durable run/control-plane transport
2. operator/helper routes
3. lower-level AbstractCore and capability integration logic

The most serious break is not simple duplication. It is that some run-scoped media routes execute work in Gateway route
handlers and then write synthetic run-history records as if Runtime had executed the step.

That violates the durable-ledger boundary.

## Consensus From Review

Gateway may still be the composition root that supplies Core URL/auth/config to Runtime.

But Gateway route code should not be the component that talks to Core directly.

The architecture therefore requires:

- durable run execution to stay `Gateway -> Runtime -> Runtime/Core integration`
- operator surfaces to remain explicit and separate
- Gateway route code not to reimplement Core behavior or write fake Runtime-authored run history
- Gateway route code not to proxy to Core directly when the same operation belongs in Runtime's public host facade

## Key Boundary Violations

### 1. Run-scoped media routes bypass Runtime

Routes under `/runs/{run_id}` currently perform media work in the route handler path and then append ledger records.

Even if the route family remains part of the Gateway product contract, it must not bypass Runtime and then fabricate
run truth afterward.

### 2. Gateway owns a second Core integration layer

Gateway currently uses a mix of:

- direct Core HTTP proxy helpers
- direct Core registry imports
- direct `CapabilityRegistry` calls
- private Runtime escape hatches
- provider-private prompt-cache reach-through

That creates drift between:

- what Runtime actually executes
- what Gateway advertises
- what Gateway operator routes do

### 3. Operator and durable semantics are blurred

A synchronous helper route may be legitimate.

But if it lives under the run namespace, emits run history, or is presented as replayable run execution, then it has to
obey Runtime semantics instead of route-local behavior.

## Goals

- Re-establish Runtime as the sole author of durable run history.
- Keep Gateway as the authenticated control plane and policy boundary.
- Preserve legitimate operator routes where they add value.
- Stop Gateway from owning AbstractCore integration details directly.

## Proposed Direction

### 1. Reclaim the `/runs/{run_id}` namespace

Every route under `/runs/{run_id}` should be one of two things:

1. a real Runtime-backed run command/effect
2. not a run route at all

There should be no third category where Gateway performs the work itself and then appends synthetic step records.

### 2. Move operator/discovery/media-control behavior onto a Runtime-owned host facade

Gateway should remain the HTTP/auth/policy layer, but discovery, residency, prompt-cache control, and helper/media
operations should be delegated through the public Runtime facade defined in `abstractruntime.integrations.abstractcore`.

Runtime phase 1 has already shipped the public host facade for:

- prompt-cache operations
- model-residency operations

Runtime phase 2 has already shipped the public durable run facade for:

- run-scoped image generation
- run-scoped TTS
- run-scoped STT/transcription

So Gateway should stop bypassing Runtime for those surfaces now.

The remaining discovery/catalog and provider-private admin routes still need one of two outcomes:

1. move onto an expanded Runtime-owned host facade
2. stop existing as Gateway-owned bypass routes

### 3. Remove private and provider-specific reach-through

Gateway should not rely on:

- `runtime._abstractcore_llm_client`
- direct provider instance mutation
- provider-private prompt-cache state
- direct Core server/router internals

If the needed operation is real, it belongs in the public host facade.

If it only works through provider-private implementation details, it is not part of the generic Gateway contract.

### 4. Keep the operator boundary explicit

If a standalone Core server is involved, that hop should be owned by Runtime integration or Runtime's public host
facade, not by Gateway route code itself.

Gateway may still:

- accept explicit Core config
- pass that config into Runtime or the Runtime-owned host facade
- expose authenticated operator routes to clients

But Gateway route code should not directly proxy to Core.

### 5. Improve contract truthfulness

Gateway should explicitly surface:

- whether an operator route exists
- whether a feature is available in this deployment
- whether it is supported for the requested task/backend
- whether the operation is durable Runtime execution or helper/operator only

## Acceptance Criteria

- No Gateway route appends durable run-step history for work executed outside Runtime.
- `/runs/{run_id}` media behavior is either Runtime-backed or moved/de-scoped.
- Gateway prompt-cache and model-residency routes depend on Runtime's public host facade rather than direct
  Core/provider/private internals.
- Gateway run-scoped media routes depend on Runtime's public durable run facade rather than route-local execution.
- Remaining discovery/catalog and provider-private admin bypasses are either removed or explicitly queued behind
  additional Runtime-facade work.
- Gateway route code no longer performs direct Core HTTP proxying for surfaces already covered by the Runtime facade.

## Initial Follow-On Work

1. Audit `/runs/{run_id}/voice/tts`, `/audio/transcribe`, and `/images/generate`.
2. Reclassify each route as either Runtime-backed run execution or operator/helper utility.
3. Replace Gateway-owned prompt-cache and model-residency integration code with Runtime host-facade calls.
4. Replace route-local `/runs/{run_id}/voice|audio|images` execution with Runtime durable run-facade calls.
5. Decide discovery/catalog and provider-private admin route fate: Runtime-facade expansion or route de-scope/removal.
6. Add tests that assert only Runtime-authored execution writes durable run truth.

## Deprecation Report

This broad item was useful while Gateway still mixed media/run-truth behavior,
direct Core proxies, and provider-private control-plane logic.

It no longer matches the remaining work:

- the run-scoped media path has already moved onto Runtime
- discovery, residency, and durable bloc prompt-cache surfaces already use
  Runtime-owned facades
- the remaining direct `abstractcore` imports are now concentrated in
  workspace/file helpers, email helper routes, Telegram bootstrap/bridge, and
  maintenance notifications

That residual work is narrower and is now tracked explicitly in:

- `../planned/0050_gateway_runtime_boundary_cleanup_for_workspace_comms_and_telegram.md`

Keep this deprecated item as the broader historical review note, but do not
re-promote it as the active execution plan.
