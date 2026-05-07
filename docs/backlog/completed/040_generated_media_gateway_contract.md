# Planned: Generated-Media Gateway Contract

## Metadata

- Created: 2026-05-08
- Status: Completed
- Completed: 2026-05-08

## Context

Gateway 0.2.2 can directly synthesize voice and transcribe audio, and it can run
workflows that use Runtime/Core/AbstractVision for image generation. There is no
direct Gateway image-generation endpoint yet, and the generated-media event
contract is not fully declared for thin clients.

This item keeps image support from being buried inside generic "multimodal"
discovery. Advanced apps need to know whether images are workflow-backed,
direct-endpoint-backed, or unavailable.

## Current Code Reality

Inspected files and symbols:

- `src/abstractgateway/routes/gateway.py`
  - `voice_tts`
  - `audio_transcribe`
  - `discovery_capabilities`
  - artifact routes
  - ledger event emission for `abstract.voice.tts` and
    `abstract.audio.transcript`
- `pyproject.toml`
- `docs/api.md`
- `docs/configuration.md`

What exists now:

- Direct TTS endpoint stores audio artifacts and emits
  `abstract.voice.tts`.
- Direct STT endpoint reads audio artifacts, stores transcript artifacts, and
  emits `abstract.audio.transcript`.
- Image generation is available through Runtime/Core workflows when
  AbstractVision is installed and configured.
- Discovery reports AbstractVision/plugin/media presence, but not a stable
  generated-image contract.

What was missing or brittle before completion:

- There was no direct route like `/api/gateway/runs/{run_id}/images/generate`.
- No declared ledger event contract for generated images from direct routes or
  workflows.
- Discovery does not clearly distinguish image workflow support from a
  Gateway-native image endpoint.
- Thin clients need a consistent way to render generated artifacts regardless of
  whether they came from a workflow node or a direct gateway control.

## Problem

Apps can ask "can the gateway generate an image?", but today the honest answer
is nuanced: workflows can do it if the deployed Runtime/Core/Vision stack is
configured, while Gateway itself does not expose direct image generation. That
distinction must be visible and eventually backed by a direct endpoint if we want
image controls to feel first-class.

## What We Want To Do

Define a generated-media contract and, if the AbstractCore/AbstractVision
capability API supports it cleanly, add a direct image-generation endpoint.

Potential direct route:

```text
POST /api/gateway/runs/{run_id}/images/generate
```

Potential request:

```json
{
  "prompt": "diagram of a durable runtime gateway",
  "provider": "openai",
  "model": "gpt-image-1",
  "size": "1024x1024",
  "format": "png",
  "request_id": "client-idempotency-key"
}
```

Potential response:

```json
{
  "ok": true,
  "run_id": "...",
  "request_id": "...",
  "image_artifact": {
    "$artifact": "...",
    "content_type": "image/png",
    "filename": "generated.png",
    "sha256": "...",
    "size_bytes": 12345
  }
}
```

## Why

Voice and audio already have gateway-level abstractions. Image generation should
have equally clear discovery, artifact, and event semantics so AbstractFlow,
AbstractAssistant, and AbstractCode can render generated media consistently.

## Requirements

- First update discovery through task 010 to report:
  - `generated_image.workflow`
  - `generated_image.direct_endpoint`
  - install/config hints when unavailable
- Define a stable generated-image ledger event name and payload.
- If adding a direct endpoint, store image bytes as artifacts and emit the
  ledger event just like voice/STT do.
- Keep workflow-backed image generation valid and documented.
- Do not block this on a specific provider. Use AbstractCore/AbstractVision
  capability abstractions when available.
- Return structured unsupported responses when no usable image backend exists.
- Add size/content-type controls and avoid unbounded in-memory payloads where
  practical.

## Suggested Implementation

- Inspect AbstractVision and AbstractCore capability plugin APIs before coding
  the endpoint. Use an existing capability abstraction if it is stable.
- Add Pydantic models for direct image request/response.
- Reuse artifact storage patterns from `voice_tts` and `audio_transcribe`.
- Emit an event such as `abstract.media.image.generated` with artifact metadata,
  prompt metadata, provider/model, and request_id.
- Add tests using a deterministic stub image backend.
- Update docs to state direct vs workflow-backed support precisely.

## Scope

- Discovery and docs for generated media.
- Direct image-generation endpoint if a clean existing backend abstraction is
  available.
- Stable artifact/event contract for generated images.
- Tests with stubs.

## Non-Goals

- Do not add local model downloads in the gateway route.
- Do not implement provider-specific image clients directly if an ecosystem
  abstraction already exists.
- Do not conflate image parsing/media ingestion with image generation.
- Do not remove workflow-backed image generation.

## Dependencies And Related Tasks

- Depends on completed `docs/backlog/completed/010_versioned_client_capability_contract.md`.
- Related to completed `docs/backlog/completed/020_abstractflow_gateway_first_editor_contract.md` for artifact
  rendering in the Flow editor.

## Expected Outcomes

- Thin clients can accurately display image generation availability.
- Generated image artifacts have a stable event/metadata shape.
- If direct endpoint support lands, apps can request images without creating a
  custom workflow solely for a one-shot generation.
- Workflow-backed image generation remains available and truthfully documented.

## Validation

- A: Unit tests for generated-media descriptor payload.
- A: Stub backend test for direct image endpoint if implemented.
- B: TestClient route test for artifact storage and ledger event emission.
- B: Contract test that discovery reports direct endpoint false until the route
  exists and true after it exists.
- C: Manual workflow-backed image generation with an AbstractVision-enabled
  gateway when credentials/models are available.

## Progress Checklist

- [x] Finalize generated-media discovery fields.
- [x] Define ledger event name and payload schema.
- [x] Inspect AbstractVision/AbstractCore generation APIs.
- [x] Decide whether direct endpoint is clean enough for this task.
- [x] Add route/tests if direct endpoint is viable.
- [x] Update docs.

## Guidance For The Implementing Agent

Keep the first version honest. A premium gateway is allowed to say "workflow
only" when that is the truth, but it should never leave clients guessing whether
an image button is supposed to work.

## Completion Report

- Completed: 2026-05-08
- Summary: Added a generated-media contract with a direct Gateway image-generation endpoint backed by Runtime/Core output selectors.
- Files/symbols touched: `src/abstractgateway/routes/gateway.py` (`ImageGenerateRequest`, `ImageGenerateResponse`, `_gateway_generated_media_llm_client`, `_gateway_generated_image_ref_from_result`, `image_generate`, `_generated_media_contract`), `tests/test_generated_media_gateway_contract.py`, `tests/test_capabilities_endpoint_contract.py`, `docs/api.md`, `docs/configuration.md`.
- Behavior changes: `POST /api/gateway/runs/{run_id}/images/generate` now generates images through the Runtime/Core `output.modality=image` / `task=image_generation` contract, stores the image as a run artifact, and emits `abstract.media.image.generated` with prompt metadata and `image_artifact`. Discovery now reports `generated_image.workflow` separately from `generated_image.direct_endpoint`, with the direct route path, event name, supported formats, and max image bytes.
- Tests: `PYTHONPATH=src python -m compileall -q src/abstractgateway/routes/gateway.py`; `PYTHONPATH=src pytest -q tests/test_capabilities_endpoint_contract.py tests/test_gateway_prompt_cache_endpoints.py tests/test_generated_media_gateway_contract.py`.
- Docs: Updated API and configuration docs to describe direct image generation, workflow-backed generation, artifact metadata, event name, and provider/model configuration.
- Residual risks: No live AbstractVision/OpenAI-compatible image backend was exercised here; validation used a deterministic stub image backend. Direct generation requires a configured Runtime/Core image backend or explicit provider/model on the request.
- Follow-ups: If clients need catalogs of image models/sizes/styles, add a separate discovery endpoint rather than bloating the v1 generated-media contract.
- Priority impact: 040 is complete; generated-media discovery now gives thin clients a clear direct-vs-workflow story.
