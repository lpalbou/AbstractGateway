# 0058 Gateway vision adapter and batch surface

**Status**: Completed  
**Completed**: 2026-06-13

## Problem

Gateway already proxied task-specific vision provider/model catalogs, but it did
not yet surface the newer Runtime/Core media request contract cleanly enough
for thin clients:

- installed compatible adapter discovery
- ordered `lora_adapters`
- batch `count` / `n` and `seeds`
- route-specific video control such as `flow_shift`

The missing surface had to stay thin and reuse Runtime/Core truth instead of
rebuilding capability logic in Gateway.

## Outcome

- Added `GET /api/gateway/vision/adapters`, backed by Runtime's public
  discovery facade.
- Extended direct image/video request models to accept the latest Runtime/Core
  fields for `text_to_image`, `image_to_image`, `image_upscale`,
  `text_to_video`, and `image_to_video`.
- Batch direct media responses now return plural artifact refs
  (`image_artifacts`, `video_artifacts`) while preserving the first-item
  compatibility fields for existing callers.
- Gateway capability discovery and readiness descriptors now advertise the new
  adapter catalog route and batch/LoRA/flow-shift support explicitly.

## Validation

1. `PYTHONPATH=src:../abstractruntime/src:../abstractcore:../abstractvision/src pytest -q tests/test_generated_media_gateway_contract.py tests/test_gateway_capability_catalog_proxy.py tests/test_capabilities_endpoint_contract.py`
2. Result at completion time: `38 passed`

## Follow-up notes

- Gateway still does not own provider/model/adapter compatibility truth. Any
  future compatibility expansion should land in Runtime/Core/AbstractVision
  first, then be relayed here.
- The control-plane console already supports task-specific default configuration;
  adapter-specific picker UX can be layered later without changing the Gateway
  route contract.
