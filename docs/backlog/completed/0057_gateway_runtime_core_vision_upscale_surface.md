# 0057 Gateway Runtime Core Vision Upscale Surface

## Metadata
- Created: 2026-06-07
- Completed: 2026-06-07
- Status: Completed
- Owner: AbstractGateway
- Origin: follow-up to AbstractCore/AbstractVision and Runtime support for richer model features, image upscaling, and media parameters.

## ADR Status
- Governing lower-layer ADRs:
  - Runtime `docs/adr/0004_runtime_owns_run_scoped_media_execution_truth.md`
  - Runtime `docs/adr/0005_runtime_owns_abstractcore_host_discovery_queries.md`
  - Runtime `docs/adr/0007_runtime_relays_core_owned_model_residency_truth.md`
- ADR impact: none. Gateway remains an HTTP projection layer over Runtime facades.

## Current Code Reality

- Gateway already exposes Runtime-backed direct routes for generated images, edited images, generated videos, image-to-video, TTS/STT, and music.
- Gateway Vision discovery proxies Runtime's `list_vision_provider_models(...)`, but only accepts four Vision tasks.
- Gateway's generated-media capability contract advertises generated/edited images and videos, but has no direct image upscale contract.
- Gateway request models forward common media parameters, but not newer Core/Vision fields such as `guidance_2` or upscaler controls.

## Architecture Review

Alternatives considered:

1. **Proxy Core's new upscaler route from Gateway.**
   - Rejected because run-scoped media must stay Runtime-authored and artifact-backed.
2. **Infer upscaler availability from package installation or provider names.**
   - Rejected because capability and residency truth belongs to Core/Runtime.
3. **Extend Gateway routes/contracts only when Runtime exposes a durable helper.**
   - Recommended. Gateway remains thin, truthful, and stable for Flow/Assistant clients.

## Goal

Expose the new Core/Vision surface through Gateway without bypassing Runtime:

- allow `task=image_upscale` on Vision provider-model discovery;
- add a direct `POST /api/gateway/runs/{run_id}/images/upscale` route backed by `AbstractCoreRunFacade.upscale_image(...)`;
- advertise the direct upscale route in thin-client capability contracts and readiness summaries;
- forward upscaler parameters and new guidance controls;
- keep generated artifacts projected into the parent run like existing generated/edited image routes.

## Acceptance Criteria

- Gateway Vision catalog accepts and returns `task=image_upscale`.
- Gateway direct image upscaling uses Runtime's run facade and never calls Core directly.
- Capability contracts expose an `upscaled_image` media entry with provider-model task `image_upscale`.
- Request payload tests prove `scale`, `resolution`, `softness`, `vae_tiling`, `quantize`, `guidance_2`, provider/model, and source artifact refs reach Runtime.
- Generated image, edit, video, image-to-video, music, and capability-contract tests remain green.

## Review Checklist

- No new direct source import of Core execution/discovery APIs in Gateway routes.
- Gateway does not invent provider/model availability.
- Existing direct media response shape remains backward compatible.
- Capability contracts remain additive and versioned.

## Implementation Summary

- Added `POST /api/gateway/runs/{run_id}/images/upscale`, backed by `AbstractCoreRunFacade.upscale_image(...)`.
- Added the `upscaled_image` media contract and readiness surface for Flow/Assistant clients, including provider-model task `image_upscale`.
- Extended Vision provider-model discovery to accept `task=image_upscale`.
- Forwarded `guidance_2` on image/video generation/edit requests and upscaler controls on image-upscale requests.
- Kept generated image/upscaled image outputs projected into the parent run with the existing artifact-backed response shape.
- Updated Gateway API/configuration/FAQ/README docs for the additive route.

## Validation

- `PYTHONPATH=src:../abstractruntime/src:../abstractcore python -m compileall -q src/abstractgateway/routes/gateway.py`
- `PYTHONPATH=src:../abstractruntime/src:../abstractcore pytest -q tests/test_generated_media_gateway_contract.py::test_gateway_direct_image_upscale_uses_runtime_child_run_contract tests/test_gateway_capability_catalog_proxy.py::test_vision_provider_catalog_accepts_image_upscale_task tests/test_capabilities_endpoint_contract.py`
  - Result: `9 passed in 1.08s`
