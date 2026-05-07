# Proposed: Dedicated Voice Profile Discovery Endpoint

## Metadata

- Created: 2026-05-08
- Status: Proposed

## Context

`GET /api/gateway/discovery/capabilities` now includes
`capabilities.contracts.assistant.voice.tts.voices` as a lightweight,
non-instantiating profile list. This is enough for static/built-in voice
profiles and env-configured voice ids, but remote AbstractVoice deployments may
load profiles dynamically from provider APIs.

## Code-First Evidence

- `src/abstractgateway/routes/gateway.py` now has `_voice_profile_descriptors`,
  which deliberately avoids constructing `VoiceManager` or triggering remote
  calls/downloads during general discovery.
- Direct TTS exists at `/api/gateway/runs/{run_id}/voice/tts`.
- `abstractvoice` exposes profile APIs through `VoiceManager.get_profiles`, but
  calling them can be backend-specific and may be slower than the general
  capabilities endpoint should allow.

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

## Possible API

```text
GET /api/gateway/voice/voices
```

The response should include `available`, `profiles`, `source`, `stale`,
`refreshed_at`, and explicit install/config errors.

## Non-Goals

- Do not make general capabilities discovery instantiate heavy engines.
- Do not require this endpoint before direct TTS remains usable.

## Validation Ideas

- Unit tests for static env/built-in profiles.
- Contract tests for unavailable AbstractVoice.
- Optional integration test with a stub `VoiceManager.get_profiles`.

