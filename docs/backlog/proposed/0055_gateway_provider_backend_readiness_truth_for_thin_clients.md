# Proposed: Gateway provider/backend readiness truth for thin clients

## Metadata
- Created: 2026-05-22
- Status: Proposed
- Completed: N/A

## ADR status
- Governing ADRs: None
- ADR impact: None for now; promote only if Runtime/Core expose a stable lower-layer truth surface that Gateway can adopt.

## Context

Gateway now exposes:

- `gateway_catalog_v1` for canonical provider/model/voice catalog parsing
- `gateway_surface_readiness_v1` for compact Gateway-owned surface readiness

That closes the Gateway-only contract work.

The remaining gap is deeper truth that Gateway should not invent locally:

- selected backend/provider/model
- verified configured-vs-usable state
- stable degraded reason codes
- local vs remote vs hybrid execution truth by capability
- prompt-cache/provider/backend compatibility truth beyond route wiring

## Current code reality

- `common.readiness` is intentionally derived from Gateway descriptors and
  existing Runtime-exported task support.
- `common.model_residency` is already Runtime-backed and truthful at task
  scope.
- direct media descriptors still mix real Runtime/plugin truth with Gateway
  route/config/env-level hints.
- no Runtime host-facade method currently exposes one unified per-capability
  readiness payload with selected backend/provider/model and bounded degraded
  reasons for Gateway to relay directly.

## Problem or opportunity

AbstractObserver and other operator-facing clients can now tell whether a
Gateway surface exists and is advertised as usable, but they still cannot ask
Gateway one precise question like:

"What backend/provider/model is actually selected for image generation on this
deployment, and why is it unavailable right now?"

That is useful product truth, but it belongs below Gateway.

## What we might want to do

Promote only when Runtime/Core can expose a stable readiness surface that
Gateway can adopt without guessing.

Illustrative target shape:

```json
{
  "capability": "image_generation",
  "mode": "local_runtime | remote_core_server | hybrid",
  "selected_backend": "abstractvision:openai",
  "selected_provider": "openai",
  "selected_model": "gpt-image-1",
  "configured": true,
  "ready": false,
  "reason_code": "missing_api_key",
  "truth_source": "abstractruntime.host_facade"
}
```

## Dependency boundary

This is not cleanly Gateway-only.

Likely lower-layer requirements:

- Runtime host-facade readiness methods or one consolidated readiness payload
- stable per-capability reason enums
- selected backend/provider/model truth for local, remote, and hybrid modes
- explicit prompt-cache/provider/backend compatibility truth where relevant

## Why

This would let Observer and similar higher apps show truthful deployment state
instead of generic "available/configured" summaries, while keeping the
Gateway -> Runtime boundary clean.

## Promotion criteria

Promote when one of these becomes active work:

- Runtime grows a stable host-facade readiness surface for media/tool/cache
  capabilities.
- Observer starts depending on Gateway for deployment diagnostics instead of
  only route discovery and lightweight readiness.

## Validation to require on promotion

- Runtime/Core-backed integration tests proving Gateway does not infer selected
  backend/provider/model from env guesses.
- Observer/Assistant adoption checks against the new truth surface.
- Docs updates in `README.md`, `docs/api.md`, `docs/configuration.md`, and
  `llms.txt`.
