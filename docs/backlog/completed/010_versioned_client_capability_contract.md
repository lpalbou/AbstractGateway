# Planned: Versioned Gateway Client Capability Contract

## Metadata

- Created: 2026-05-08
- Status: Completed
- Completed: 2026-05-08

## Context

AbstractGateway is now the control plane for thin clients such as AbstractFlow,
AbstractAssistant, and AbstractCode. These apps need to enable voice, audio,
generated-media, workflow, provider, workspace, and prompt-cache controls from
gateway discovery instead of probing local packages or waiting for endpoint
failures.

This item merges the discovery portions of the assistant and AbstractFlow
proposals into one shared, versioned capability contract.

## Current Code Reality

Inspected files and symbols:

- `src/abstractgateway/routes/gateway.py`
  - `discovery_capabilities`
  - `discovery_tools`
  - `discovery_providers`
  - `voice_tts`
  - `audio_transcribe`
  - `/visualflows`, `/bundles`, `/runs`, `/workspace/policy`, `/prompt_cache/*`
- `docs/api.md`
- `tests/test_capabilities_endpoint_contract.py`
- `tests/test_voice_audio_api_contract.py`
- `../abstractflow/web/frontend/src/hooks/useWebSocket.ts`
- `../abstractflow/web/frontend/src/hooks/useProviders.ts`
- `../abstractflow/web/frontend/src/hooks/useTools.ts`
- `../abstractassistant/abstractassistant/gateway/client.py`
- `../abstractcode/web/src/lib/gateway_client.ts`

What exists now:

- `GET /api/gateway/discovery/capabilities` is authenticated and tested.
- The payload reports package/plugin presence for AbstractRuntime, AbstractCore,
  AbstractVoice, AbstractVision, tools, VisualFlow, media, and capability
  plugins.
- Direct TTS exists at `POST /api/gateway/runs/{run_id}/voice/tts`.
- Direct STT exists at `POST /api/gateway/runs/{run_id}/audio/transcribe`.
- Prompt-cache operator endpoints exist under `/api/gateway/prompt_cache/*`.
- AbstractFlow already uses gateway routes for VisualFlow CRUD, publish,
  start, stream, run summaries, input data, artifacts, history, providers,
  tools, semantics, KG, and workspace policy.

What is missing or brittle:

- Discovery mostly says packages are installed, not whether a feature is usable
  right now.
- Voice discovery does not expose endpoint URLs, formats, configured voice
  profiles, STT content types, size limits, or install/config hints.
- Generated images are only workflow-backed today, but discovery does not state
  that clearly as a contract.
- Prompt-cache discovery is split across operator endpoints and does not tell
  apps whether cache is provider-level only or session-managed.
- AbstractFlow and assistant-facing capabilities are not versioned, so client
  code has to rely on informal route knowledge.

## Problem

Thin clients can accidentally over-enable controls because `installed=true` is
not the same as `ready=true`. This creates poor UX, brittle client-side probes,
and inconsistent behavior across local, remote, Docker, and split API/runner
deployments.

## What We Want To Do

Extend `GET /api/gateway/discovery/capabilities` with a stable `contracts`
block for thin clients. Keep existing keys backward compatible.

Suggested high-level shape:

```json
{
  "capabilities": {
    "...": "existing fields",
    "contracts": {
      "version": 1,
      "common": {
        "runs": {"start": true, "schedule": true, "commands": true},
        "ledger": {"replay": true, "batch": true, "stream": true},
        "artifacts": {"list": true, "metadata": true, "content": true},
        "workspace": {"policy_endpoint": "/api/gateway/workspace/policy"}
      },
      "flow_editor": {
        "available": true,
        "version": 1,
        "visualflows": {"crud": true, "publish": true},
        "input_schema": {"available": false}
      },
      "assistant": {
        "available": true,
        "version": 1,
        "voice": {
          "tts": {"available": true, "endpoint": "/api/gateway/runs/{run_id}/voice/tts"},
          "stt": {"available": true, "endpoint": "/api/gateway/runs/{run_id}/audio/transcribe"}
        },
        "media": {
          "generated_image": {"workflow": true, "direct_endpoint": false}
        },
        "prompt_cache": {
          "provider_controls": true,
          "session_lifecycle": false
        }
      }
    }
  }
}
```

The exact field names may change during implementation, but the contract must
use explicit booleans and JSON-safe lists/objects that clients can branch on.

## Why

This is the foundation for clean thin clients. It makes the gateway the
deployment authority and avoids leaking provider/package assumptions into
AbstractFlow, AbstractAssistant, and AbstractCode.

## Requirements

- Preserve all existing discovery response fields.
- Add `contracts.version = 1`.
- Report direct endpoint paths for stable Gateway APIs.
- Distinguish `installed`, `available`, `configured`, and `unsupported` states
  where the code can know the difference.
- Include install/config hints for unavailable optional features.
- Report generated images as workflow-backed and not direct-endpoint-backed
  until a direct endpoint exists.
- Report prompt cache as provider-level controls, not Gateway session lifecycle,
  until task 030 is implemented.
- Avoid heavy probes in discovery. Do not trigger model downloads or long
  provider calls.
- Keep response JSON-safe and bounded.

## Suggested Implementation

- Add small helpers near `discovery_capabilities` for endpoint and readiness
  descriptors.
- Add a voice/audio descriptor that checks package/plugin status and configured
  gateway env without forcing downloads.
- Add a media descriptor that reports workflow-backed generation separately from
  direct endpoint support.
- Add a prompt-cache descriptor based on the presence of `/prompt_cache/*`
  routes and `_gateway_runtime_llm_client` availability, while leaving detailed
  provider/model capabilities to `/prompt_cache/capabilities`.
- Add contract tests for available, unavailable, and partial states.
- Update `docs/api.md` and `docs/configuration.md`.

## Scope

- Discovery payload only.
- Contract tests and docs.
- No new direct image generation endpoint.
- No session prompt-cache lifecycle endpoints.
- No AbstractFlow frontend changes unless needed to validate the contract.

## Non-Goals

- Do not remove existing discovery keys.
- Do not instantiate expensive local model backends in discovery.
- Do not make package presence imply runtime readiness.
- Do not make the gateway claim ownership of prompt-cache sessions before task
  030 lands.

## Dependencies And Related Tasks

- Related: completed `docs/backlog/completed/020_abstractflow_gateway_first_editor_contract.md`
- Related: `030_gateway_session_prompt_cache_lifecycle.md`
- Related: `040_generated_media_gateway_contract.md`
- Related memory/decision: apps should query capability discovery before
  exposing audio, image, or cache controls.

## Expected Outcomes

- Thin clients have one stable discovery call for feature gating.
- AbstractAssistant can enable or disable direct TTS/STT controls from gateway
  discovery.
- AbstractFlow can see whether the gateway exposes the editor/runtime contract.
- AbstractCode can distinguish provider prompt-cache controls from future
  Gateway session cache controls.

## Validation

- A: Unit/contract tests for discovery payload shape.
- A: Tests for missing optional packages/plugins where practical with monkeypatch.
- B: TestClient check that `/api/gateway/discovery/capabilities` remains auth
  protected and backward compatible.
- B: Docs check for `docs/api.md` and `docs/configuration.md`.
- C: Manual call against a server install and a minimal install to compare
  `available` vs `installed` states.

## Progress Checklist

- [x] Draft the `contracts.version=1` schema in code comments or docs.
- [x] Add common run/ledger/artifact/workspace descriptors.
- [x] Add assistant voice/audio/media/prompt-cache descriptors.
- [x] Add flow editor descriptor.
- [x] Add tests for the new payload shape.
- [x] Update docs.

## Guidance For The Implementing Agent

Re-check current routes before coding. Favor truthful, conservative capability
claims over optimistic feature flags. Keep the contract small enough that clients
can use it without needing provider-specific knowledge.

## Completion Report

- Completed: 2026-05-08
- Summary: Added `capabilities.contracts.version=1` to `GET /api/gateway/discovery/capabilities` while preserving existing discovery keys.
- Files/symbols touched: `src/abstractgateway/routes/gateway.py` (`_build_client_capability_contracts`, `_voice_contract_descriptor`, `_generated_media_contract`, `discovery_capabilities`), `tests/test_capabilities_endpoint_contract.py`, `docs/api.md`, `docs/configuration.md`.
- Behavior changes: Thin clients now receive explicit common, Flow editor, assistant, and AbstractCode contract blocks with stable endpoint paths, direct TTS/STT descriptors, workflow-backed image generation status, and provider-level-not-session prompt-cache status.
- Tests: `PYTHONPATH=src pytest -q tests/test_capabilities_endpoint_contract.py tests/test_abstractflow_editor_gateway_contract.py tests/test_gateway_bundle_llm_tools_agents.py::test_gateway_bundle_metadata_endpoints_expose_entrypoint_inputs`; `PYTHONPATH=src pytest -q -m basic`.
- Docs: Updated API and configuration docs with the versioned contract vocabulary.
- Residual risks: Voice profile discovery remains lightweight and avoids instantiating engines; deployments with remote/dynamic voice catalogs may still need a future dedicated voice-profile endpoint.
- Follow-ups: Session prompt-cache lifecycle remains correctly deferred to planned item 030; direct generated image endpoint/event work remains deferred to planned item 040.
- Priority impact: 010 is complete; 020 can rely on the shared contract vocabulary.
