# mflux and MLX images & video are never offered off a Mac

**Status**: completed (2026-10-29, unreleased) · **Priority**: P1 · **Created**: 2026-10-29
**Source**: operator QA on the OVH release-test server (2026-10-28), batch item (d), gateway part. The Core part,
no MLX catalog rows off Apple silicon, is AbstractCore PR #12.

## Why
On the Linux server the capability editor's image and video provider list offered `mflux` and `mlx-gen` ("MLX
images & video"), engines that run only on Apple silicon. Read live on the server:
- `GET /api/gateway/vision/provider_models?task=text_to_image&providers_only=true` answered
  `providers: [openai, openai-compatible, huggingface, mlx-gen, mflux, sdcpp]` with upstream source
  `abstractruntime.discovery`.
- The full answer's `details` listed both as well.
- `_filter_vision_provider_models_response` pruned only when the source was `abstractvision.local`.

## What shipped
- `routes/gateway.py`, `_filter_vision_provider_models_response(..., remote=)`:
  - Every local answer is pruned off a Mac: `abstractvision.local`, `abstractvision_local_cache` and
    `abstractruntime.discovery` (the runtime's local catalog).
  - The pruned fields are `providers`, `available_providers`, `image_providers`, `models`, `models_by_provider` and
    `details`.
  - A request with a remote Core (`base_url`) keeps the answer as it is, since that host may be a Mac.
  - The set is `_APPLE_ONLY_VISION_PROVIDERS`.

## Evidence
- `tests/test_gateway_capability_catalog_proxy.py::test_vision_linux_hides_apple_engines_in_the_providers_only_answer`:
  - on Linux both local sources are pruned, `details` included;
  - `remote=True` keeps them;
  - on Darwin nothing is pruned.
  - It goes red with the old source rule (mutation-checked).
  - The existing `test_vision_linux_hides_apple_local_engines_only` still passes.
