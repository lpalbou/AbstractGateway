# AbstractGateway Backlog Overview

## Status

AbstractGateway is a durable HTTP/SSE host for AbstractRuntime runs. The current
0.2.6 surface already provides run start/schedule, durable commands, ledger
replay/streaming, artifacts, VisualFlow CRUD/publish, workspace policy, provider
and tool discovery, direct voice TTS, audio transcription, direct generated
image artifacts/events, provider-level and session-level prompt-cache controls,
optional embeddings/KG helpers with configurable memory stores, Core-backed
voice/vision catalog proxy endpoints, explicit install/config profiles, and a
versioned thin-client capability contract for Flow, Assistant, Code, and shared
Gateway features.

The next planning focus is to make the gateway a premium control plane for thin
AI apps such as AbstractFlow, AbstractAssistant, and AbstractCode. The gateway
must report what is actually usable, keep provider/runtime concerns server-side,
and expose stable contracts that clients can trust without importing local
gateway packages.

## Counts

- Planned: 0
- Proposed: 0
- Completed: 8
- Deprecated: 0
- Recurrent: 0

## Priority Bands

- Completed: 010 capability discovery and thin-client feature gating.
- Completed: 020 AbstractFlow gateway-first editor contract and validation.
- Completed: 030 Gateway-owned session prompt-cache lifecycle.
- Completed: 040 Generated-media artifact and direct image contract.
- Completed: install profiles and configuration entrypoint.
- Completed: memory store resolver and TripleStore abstraction.
- Completed: Core-backed Voice/Vision catalog proxy endpoints.

## Next Recommended Work

No planned or proposed items remain. Next, watch client adoption of the new
install/config, memory, and catalog contracts before expanding them.

## Planned Items

| Priority | Item | Why now |
| --- | --- | --- |
| - | None | All planned items are complete. |

## Proposed Items

| Item | Promotion criteria |
| --- | --- |
| - | None |

The three incoming proposals were merged into completed/planned work:

- `proposed/2026-05-07_assistant-thin-client-capability-contract.md`
- `proposed/2026-05-07_gateway-owned-assistant-session-cache.md`
- `proposed/2026-05-08_abstractflow-gateway-first-editor-contract.md`

## Completed Work Ledger

| Item | Original planned path | Completed path | Outcome | Validation |
| --- | --- | --- | --- | --- |
| Swagger UI bearer auth docs | N/A | [completed/001_openapi_swagger_auth.md](completed/001_openapi_swagger_auth.md) | OpenAPI advertises bearer auth for `/api/gateway/*`. | `PYTHONPATH=src pytest` passed at completion time. |
| Versioned Gateway client capability contract | `planned/010_versioned_client_capability_contract.md` | [completed/010_versioned_client_capability_contract.md](completed/010_versioned_client_capability_contract.md) | Discovery now exposes `capabilities.contracts.version=1` with common, Flow editor, Assistant, and AbstractCode feature gates. | `PYTHONPATH=src pytest -q tests/test_capabilities_endpoint_contract.py tests/test_abstractflow_editor_gateway_contract.py tests/test_gateway_bundle_llm_tools_agents.py::test_gateway_bundle_metadata_endpoints_expose_entrypoint_inputs`; `PYTHONPATH=src pytest -q -m basic`. |
| AbstractFlow gateway-first editor contract | `planned/020_abstractflow_gateway_first_editor_contract.md` | [completed/020_abstractflow_gateway_first_editor_contract.md](completed/020_abstractflow_gateway_first_editor_contract.md) | Gateway now documents and tests the draft VisualFlow -> publish -> start -> observe editor path and exposes a first-class bundle flow input-schema route. | `PYTHONPATH=src pytest -q tests/test_capabilities_endpoint_contract.py tests/test_abstractflow_editor_gateway_contract.py tests/test_gateway_bundle_llm_tools_agents.py::test_gateway_bundle_metadata_endpoints_expose_entrypoint_inputs`; `PYTHONPATH=src pytest -q -m basic`. |
| Gateway session prompt-cache lifecycle | `planned/030_gateway_session_prompt_cache_lifecycle.md` | [completed/030_gateway_session_prompt_cache_lifecycle.md](completed/030_gateway_session_prompt_cache_lifecycle.md) | Gateway now exposes session-scoped prompt-cache status, prepare, rebuild, and clear routes with deterministic bounded keys and honest unsupported/keyed/local-control-plane modes. | `PYTHONPATH=src python -m compileall -q src/abstractgateway/routes/gateway.py`; `PYTHONPATH=src pytest -q tests/test_capabilities_endpoint_contract.py tests/test_gateway_prompt_cache_endpoints.py tests/test_generated_media_gateway_contract.py`. |
| Generated-media gateway contract | `planned/040_generated_media_gateway_contract.md` | [completed/040_generated_media_gateway_contract.md](completed/040_generated_media_gateway_contract.md) | Gateway now declares generated-image workflow/direct support and exposes `POST /runs/{run_id}/images/generate` with artifact storage and `abstract.media.image.generated` events. | `PYTHONPATH=src python -m compileall -q src/abstractgateway/routes/gateway.py`; `PYTHONPATH=src pytest -q tests/test_capabilities_endpoint_contract.py tests/test_gateway_prompt_cache_endpoints.py tests/test_generated_media_gateway_contract.py`. |
| Gateway install profiles and configuration entrypoint | `proposed/2026-05-08_gateway_install_profiles_and_config_entrypoint.md` | [completed/2026-05-08_gateway_install_profiles_and_config_entrypoint.md](completed/2026-05-08_gateway_install_profiles_and_config_entrypoint.md) | Base install is minimal, dependency cascades live in explicit extras, `abstractgateway-config` was added, and Gateway-owned env is translated into Runtime state. | `PYTHONPATH=src pytest -q tests/test_gateway_install_profiles.py tests/test_gateway_config_cli.py tests/test_gateway_runtime_handoff.py`. |
| Gateway memory store resolver and TripleStore abstraction | `proposed/2026-05-08_gateway_memory_store_resolver_and_triplestore_abstraction.md` | [completed/2026-05-08_gateway_memory_store_resolver_and_triplestore_abstraction.md](completed/2026-05-08_gateway_memory_store_resolver_and_triplestore_abstraction.md) | Bundle memory effects and `/kg/query` now use a shared resolver for LanceDB, in-memory, and SQLite-capable AbstractMemory builds with capability metadata. | `PYTHONPATH=src pytest -q tests/test_gateway_memory_store_resolver.py`. |
| Core-backed Voice/Vision catalog proxy endpoints | `proposed/2026-05-08_voice_profile_discovery_endpoint.md` | [completed/2026-05-08_voice_profile_discovery_endpoint.md](completed/2026-05-08_voice_profile_discovery_endpoint.md) | Gateway now exposes voice, speech-model, and vision provider-model catalog endpoints that proxy AbstractCore catalog routes when configured and use static bounded fallback otherwise. | `PYTHONPATH=src pytest -q tests/test_gateway_capability_catalog_proxy.py`. |

## Planning Notes

- Backlog is not authority over code. Each task has a Current code reality
  section based on inspection of `src/abstractgateway/routes/gateway.py`,
  `docs/api.md`, sibling app clients, and existing tests.
- Capability discovery must be truthful. Installed packages and plugin entry
  points are not enough to claim a feature is ready.
- Prompt cache remains provider/model capability first. The Gateway session
  lifecycle orchestrates names, key hints, and thin-client controls without
  pretending unsupported providers have local KV state.
- Generated image support is now both workflow-backed and directly available
  through Gateway when a Runtime/Core image backend is configured.

## Completion Process

When a planned item is completed:

1. Finish code, tests, and docs.
2. Add a Completion report to the backlog item.
3. Update metadata with `Status: Completed` and a completion date.
4. Move the file to `docs/backlog/completed/`.
5. Update this overview counts, tables, and completed ledger.
6. Review follow-up signals and create proposed/planned work only when justified
   by code evidence.
