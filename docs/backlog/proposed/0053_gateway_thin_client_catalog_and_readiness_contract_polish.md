# Proposed: Gateway thin-client catalog and readiness contract polish

## Metadata
- Created: 2026-05-22
- Status: Proposed
- Completed: N/A

## ADR status
- Governing ADRs: None
- ADR impact: None for now; promote only if this becomes a durable product contract rather than API cleanup.

## Context

Gateway 0.2.x now exposes Runtime-backed direct image generation, image edit,
voice TTS, STT, music generation, prompt-cache controls, durable blocs, and
model residency through one shared thin-client capability contract.

The remaining higher-app friction is no longer missing routes. It is contract
polish:

- AbstractFlow still normalizes several possible provider/model catalog shapes
  in the UI.
- AbstractObserver wants a more explicit deployment/readiness block from
  Gateway, not only package/plugin discovery.
- Gateway contracts now expose voice/STT/listen more clearly, but catalog and
  deployment/readiness semantics remain partly implicit.

## Scope split

This proposal currently spans two different scopes:

1. Gateway-only catalog normalization:
   - feasible entirely in `abstractgateway`
   - would normalize the existing provider/model catalog route bodies into one
     stable Gateway envelope
   - would not require Runtime/Core changes if the work is limited to response
     shaping and explicit route-level metadata
2. Truthful deployment/readiness reporting:
   - not cleanly Gateway-only if we want it to be useful
   - would likely require richer Runtime truth, and possibly Core truth behind
     Runtime, for selected backend/provider/model readiness, degraded reasons,
     and per-capability configured-vs-installed state

As written, this backlog item should stay proposed because the second scope is
cross-package. Implementing only the first half under this item would blur the
boundary and make the readiness part look more ready than it is.

## Current code reality

- `src/abstractgateway/routes/gateway.py` already advertises the right route
  family under `common.discovery`.
- Catalog routes are still intentionally best-effort and preserve lower-layer
  payload variation instead of one stricter canonical Gateway response shape.
- `discovery/capabilities` reports package/plugin availability and shared
  thin-client contracts, but there is no dedicated `common.deployment` or
  similar operator-readiness block.
- AbstractFlow currently carries fallback parsing for several catalog shapes:
  - `web/frontend/src/components/RunFlowModal.tsx`
  - `web/frontend/src/components/PropertiesPanel.tsx`
  - `web/frontend/src/components/nodes/BaseNode.tsx`
- AbstractFlow also normalizes model-residency route bodies defensively in
  `web/frontend/src/hooks/useModelResidency.ts`.
- AbstractAssistant currently consumes the shared contract conservatively in
  `abstractassistant/gateway/capabilities.py`, but richer media/provider
  readiness would let it drop environment and UI fallback logic.
- AbstractObserver is the natural consumer for a deployment/operator readiness
  block, but its current `GatewayClient` only has raw route calls and no typed
  deployment model yet (`src/lib/gateway_client.ts`).

## Problem or opportunity

Higher apps can already function, but they still need extra local normalization
logic or host-specific assumptions that Gateway could own more cleanly.

## What we might want to do

### A. Gateway-only catalog envelopes

Define canonical Gateway response shapes for provider/model catalogs used by
Flow/Assistant/Observer, without changing the underlying route family.

Illustrative provider catalog shape:

```json
{
  "ok": true,
  "scope": "music",
  "task": "text_to_music",
  "items": [
    {
      "id": "stability-ai",
      "label": "Stability AI",
      "provider": "stability-ai",
      "tasks": ["text_to_music"],
      "available": true
    }
  ],
  "source": "gateway_catalog_v1"
}
```

Illustrative model catalog shape:

```json
{
  "ok": true,
  "scope": "vision",
  "task": "image_to_image",
  "provider": "openai",
  "items": [
    {
      "id": "gpt-image-1",
      "label": "gpt-image-1",
      "provider": "openai",
      "tasks": ["text_to_image", "image_to_image"],
      "formats": ["png", "jpeg"],
      "parameters": {
        "size": ["1024x1024", "1536x1024"]
      }
    }
  ],
  "source": "gateway_catalog_v1"
}
```

The benefit to higher apps is direct:

- AbstractFlow can stop parsing `models|items|data|provider_models|music_models`
  variants in multiple components.
- AbstractAssistant can populate provider/model pickers from one stable shape
  instead of route-specific heuristics.
- AbstractObserver can render catalog inspection/debug views without route-local
  adapters.

### B. Deployment/readiness contract block

Add a small deployment/readiness contract block for operator dashboards and
higher-app gating, but only when the underlying truth is available.

Illustrative shape:

```json
{
  "mode": "gateway_runtime",
  "capabilities": {
    "text": { "route_available": true, "configured": true },
    "image_generation": { "route_available": true, "configured": true },
    "image_edit": { "route_available": true, "configured": true },
    "stt": { "route_available": true, "configured": true },
    "tts": { "route_available": true, "configured": false, "reason": "provider_unconfigured" },
    "music": { "route_available": true, "configured": true },
    "prompt_cache_exports": { "local_only": true },
    "model_residency": { "supported_tasks": ["text_generation"] }
  }
}
```

This is only valuable if the fields are truthful. Route existence alone is not
enough. A useful readiness block needs answers to questions like:

- which backend/provider/model is actually selected right now?
- is the capability merely installed, or also configured?
- is it local-only, remote-only, or hybrid?
- is the failure reason missing credentials, missing local engine, unsupported
  task, or remote server unreachability?

Gateway cannot answer all of that reliably by itself today without either
guessing from env/config or reverse-engineering lower-layer payloads.

## Dependency boundary

### Can be done in Gateway only

- canonical catalog envelopes
- versioned catalog route metadata
- clearer contract docs for route families and payload guarantees
- optional response headers or `source` fields showing whether a catalog was
  local, proxied, or fallback

### Likely needs Runtime and possibly Core

- a truthful `common.deployment` or `common.readiness` block
- selected backend/provider/model truth across local, remote, and hybrid modes
- stable degraded-reason enums for media/provider/backend readiness
- explicit configured-vs-installed capability truth
- richer prompt-cache/export/import residency/readiness summaries beyond the
  current per-route surfaces

If Runtime later exposes a stable host-facade readiness surface, Gateway can
adopt it cleanly. If not, Gateway should avoid inventing its own pseudo-health
model.

## Why

This would reduce duplicated normalization in higher apps and make Gateway a
cleaner control plane for both end-user clients and operator dashboards.

## Promotion criteria

Promote when one of these becomes active engineering work:

- AbstractFlow removes its catalog-shape fallback logic in favor of a stricter
  Gateway contract.
- AbstractObserver starts using Gateway as a first-class deployment/operator
  readiness surface and Runtime/Core can supply the required truth.
- Gateway needs to version catalog payloads separately from the current shared
  media contract.
- Runtime grows a host-facade readiness surface that Gateway can adopt instead
  of inferring deployment truth itself.

## Validation to require on promotion

- Gateway contract tests for canonical catalog response shapes.
- Flow/Assistant/Observer adoption checks against the stricter catalog
  envelope.
- If the readiness half is promoted, Runtime-backed integration tests must
  prove that configured/unsupported/degraded states are not guessed locally by
  Gateway.
- Docs updates in `README.md`, `docs/api.md`, `docs/configuration.md`, and
  `llms.txt`.
