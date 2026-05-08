# Proposed: Dedicated Voice Profile Discovery Endpoint

## Metadata

- Created: 2026-05-08
- Status: Completed
- Completed: 2026-05-08

## Context

`GET /api/gateway/discovery/capabilities` now includes
`capabilities.contracts.assistant.voice.tts.voices` as a lightweight,
non-instantiating profile list. This is enough for static/built-in voice
profiles and env-configured voice ids, but remote AbstractVoice deployments may
load profiles dynamically from provider APIs.

Core Server is now the lower-level HTTP boundary for dynamic Voice and Vision capability catalogs.
AbstractCore `2.13.12` exposes:

- `GET /v1/audio/voices`
- `GET /v1/audio/speech/models`
- `GET /v1/vision/provider_models`

Gateway should prefer these Core catalog routes, or the equivalent in-process Core capability
facades, instead of importing AbstractVoice/AbstractVision directly or duplicating their
provider-specific discovery rules.

## Code-First Evidence

- `src/abstractgateway/routes/gateway.py` now has `_voice_profile_descriptors`,
  which deliberately avoids constructing `VoiceManager` or triggering remote
  calls/downloads during general discovery.
- Direct TTS exists at `/api/gateway/runs/{run_id}/voice/tts`.
- `abstractvoice` exposes profile APIs through `VoiceManager.get_profiles`, but
  calling them can be backend-specific and may be slower than the general
  capabilities endpoint should allow.
- AbstractCore `2.13.12` completed the lower-level catalog routes for dynamic TTS models, voice
  profiles, and Vision provider models.

## Why This Might Matter

Assistant and tray clients may eventually need a refreshable list of voice
profiles for remote OpenAI-compatible voice services, cloned voices, or
operator-managed profile catalogs.

## Promotion Criteria

- A client needs profile refresh independent of general capability discovery.
- Dynamic/remote profiles are common enough that static discovery is visibly
  insufficient.
- Gateway can bound latency and failure behavior cleanly, for example with a
  short timeout and explicit `stale`/`error` fields.
- Gateway implements this as a Core catalog proxy/in-process Core facade, not by importing
  AbstractVoice or AbstractVision internals directly.

## Possible API

```text
GET /api/gateway/voice/voices
GET /api/gateway/audio/speech/models
GET /api/gateway/vision/provider_models?task=text_to_image
```

The response should include `available`, `profiles`, `models`, `source`, `stale`, `refreshed_at`,
and explicit install/config errors.

Preferred implementation path:

- Gateway calls/proxies Core's voice catalog route when Core is configured as the capability server;
- Gateway calls/proxies Core's Vision provider-model route for dynamic image/video model catalogs;
- Gateway may use in-process Core capability APIs when it embeds Core;
- Gateway should not instantiate AbstractVoice or AbstractVision directly except as a documented
  temporary fallback.

This item can be renamed during promotion to "Gateway Core Capability Catalog Proxy" if Vision and
Voice are implemented together.

## Non-Goals

- Do not make general capabilities discovery instantiate heavy engines.
- Do not require this endpoint before direct TTS remains usable.
- Do not duplicate AbstractVoice's provider-specific profile/model parsing in Gateway.

## Validation Ideas

- Unit tests for static env/built-in profiles.
- Contract tests for unavailable AbstractVoice.
- Contract tests for unavailable AbstractVision.
- Proxy/facade tests for Core `2.13.11` catalog response shapes and auth/error propagation.

## Completion Report

Implemented as a Gateway Core capability catalog proxy.

- Added `GET /api/gateway/voice/voices`.
- Added `GET /api/gateway/audio/speech/models`.
- Added `GET /api/gateway/vision/provider_models`.
- When `ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_BASE_URL` or
  `ABSTRACTCORE_SERVER_BASE_URL` is configured, Gateway proxies the matching
  AbstractCore `/v1` catalog route with explicit Core auth settings.
- Without a configured Core server, Gateway returns bounded static/env-derived
  descriptors instead of instantiating AbstractVoice or AbstractVision engines.
- Capability discovery now advertises the catalog endpoints.
- Added tests for static voice profiles, Core proxy shape/header propagation,
  Core auth error propagation, and Vision task validation.
