# AbstractGateway Backlog Overview

## Status

AbstractGateway is a durable HTTP/SSE host for AbstractRuntime runs. The current
0.2.15 surface already provides run start/schedule/input/history, durable
commands, ledger replay/streaming, artifacts, VisualFlow CRUD/publish,
workspace policy, provider and tool discovery, Runtime-owned direct voice TTS,
audio transcription, direct generated image child-run artifacts, provider-level
and session-level prompt-cache controls, optional embeddings/KG helpers with
configurable memory stores, Runtime-backed voice/vision discovery routes,
explicit install/config profiles, and a versioned thin-client capability
contract for Flow, Assistant, Code, and shared Gateway features.

The next planning focus is to make the gateway a premium control plane for thin
AI apps such as AbstractFlow, AbstractAssistant, and AbstractCode. The gateway
must report what is actually usable, keep provider/runtime concerns server-side,
and expose stable contracts that clients can trust without importing local
gateway packages.

## Counts

- Planned: 0
- Proposed: 3
- Completed: 13
- Deprecated: 1
- Recurrent: 0

## Priority Bands

- Completed: 010 capability discovery and thin-client feature gating.
- Completed: 020 AbstractFlow gateway-first editor contract and validation.
- Completed: 030 Gateway-owned session prompt-cache lifecycle.
- Completed: 040 Generated-media artifact and direct image contract.
- Completed: model residency, truthful media residency, durable bloc prompt-cache exposure, and Runtime boundary cleanup for workspace/comms/Telegram.
- Completed: install profiles and configuration entrypoint.
- Completed: memory store resolver and TripleStore abstraction.
- Completed: Core-backed Voice/Vision catalog proxy endpoints.
- Completed: provider-private prompt-cache save/load migration via Runtime.

## Next Recommended Work

The main Runtime-owned boundary work is now done for Gateway's prompt-cache,
durable bloc, residency, workspace/comms/Telegram, and operator snapshot
surfaces. The remaining proposed work is product-facing rather than boundary
cleanup.

## Planned Items

No planned items at the moment.

## Proposed Items

| Item | Promotion criteria |
| --- | --- |
| [2026-05-09_abstractflow_draft_spaces_and_ephemeral_runs.md](proposed/2026-05-09_abstractflow_draft_spaces_and_ephemeral_runs.md) | Promote when Gateway draft-space semantics and ephemeral-run lifecycle are validated against Flow UX and ledger expectations. |
| [2026-05-13_shared_identity_context.md](proposed/2026-05-13_shared_identity_context.md) | Promote when shared identity/session context becomes an active Gateway contract decision instead of exploratory design work. |
| [offline_first_gateway_connectivity.md](proposed/offline_first_gateway_connectivity.md) | Promote when offline-first connectivity guarantees become a near-term product commitment. |

## Completed Work Ledger

| Item | Original path | Completed path | Outcome | Validation |
| --- | --- | --- | --- | --- |
| Swagger UI bearer auth docs | N/A | [completed/001_openapi_swagger_auth.md](completed/001_openapi_swagger_auth.md) | OpenAPI advertises bearer auth for `/api/gateway/*`. | `PYTHONPATH=src pytest` passed at completion time. |
| Versioned Gateway client capability contract | `planned/010_versioned_client_capability_contract.md` | [completed/010_versioned_client_capability_contract.md](completed/010_versioned_client_capability_contract.md) | Discovery now exposes `capabilities.contracts.version=1` with common, Flow editor, Assistant, and AbstractCode feature gates. | `PYTHONPATH=src pytest -q tests/test_capabilities_endpoint_contract.py tests/test_abstractflow_editor_gateway_contract.py tests/test_gateway_bundle_llm_tools_agents.py::test_gateway_bundle_metadata_endpoints_expose_entrypoint_inputs`; `PYTHONPATH=src pytest -q -m basic`. |
| AbstractFlow gateway-first editor contract | `planned/020_abstractflow_gateway_first_editor_contract.md` | [completed/020_abstractflow_gateway_first_editor_contract.md](completed/020_abstractflow_gateway_first_editor_contract.md) | Gateway now documents and tests the draft VisualFlow -> publish -> start -> observe editor path and exposes a first-class bundle flow input-schema route. | `PYTHONPATH=src pytest -q tests/test_capabilities_endpoint_contract.py tests/test_abstractflow_editor_gateway_contract.py tests/test_gateway_bundle_llm_tools_agents.py::test_gateway_bundle_metadata_endpoints_expose_entrypoint_inputs`; `PYTHONPATH=src pytest -q -m basic`. |
| Gateway session prompt-cache lifecycle | `planned/030_gateway_session_prompt_cache_lifecycle.md` | [completed/030_gateway_session_prompt_cache_lifecycle.md](completed/030_gateway_session_prompt_cache_lifecycle.md) | Gateway now exposes session-scoped prompt-cache status, prepare, rebuild, and clear routes with deterministic bounded keys and honest unsupported/keyed/local-control-plane modes. | `PYTHONPATH=src python -m compileall -q src/abstractgateway/routes/gateway.py`; `PYTHONPATH=src pytest -q tests/test_capabilities_endpoint_contract.py tests/test_gateway_prompt_cache_endpoints.py tests/test_generated_media_gateway_contract.py`. |
| Generated-media gateway contract | `planned/040_generated_media_gateway_contract.md` | [completed/040_generated_media_gateway_contract.md](completed/040_generated_media_gateway_contract.md) | Gateway now declares generated-image workflow/direct support and exposes `POST /runs/{run_id}/images/generate` with artifact storage and `abstract.media.image.generated` events. | `PYTHONPATH=src python -m compileall -q src/abstractgateway/routes/gateway.py`; `PYTHONPATH=src pytest -q tests/test_capabilities_endpoint_contract.py tests/test_gateway_prompt_cache_endpoints.py tests/test_generated_media_gateway_contract.py`. |
| Gateway install profiles and configuration entrypoint | `proposed/2026-05-08_gateway_install_profiles_and_config_entrypoint.md` | [completed/2026-05-08_gateway_install_profiles_and_config_entrypoint.md](completed/2026-05-08_gateway_install_profiles_and_config_entrypoint.md) | Base install is minimal, dependency cascades live in explicit extras, `abstractgateway-config` was added, and Gateway-owned env is translated into Runtime state. | `PYTHONPATH=src pytest -q tests/test_gateway_install_profiles.py tests/test_gateway_config_cli.py tests/test_gateway_runtime_handoff.py`. |
| Gateway memory store resolver and TripleStore abstraction | `proposed/2026-05-08_gateway_memory_store_resolver_and_triplestore_abstraction.md` | [completed/2026-05-08_gateway_memory_store_resolver_and_triplestore_abstraction.md](completed/2026-05-08_gateway_memory_store_resolver_and_triplestore_abstraction.md) | Bundle memory effects and `/kg/query` now use a shared resolver for LanceDB, in-memory, and SQLite-capable AbstractMemory builds with capability metadata. | `PYTHONPATH=src pytest -q tests/test_gateway_memory_store_resolver.py`. |
| Core-backed Voice/Vision catalog proxy endpoints | `proposed/2026-05-08_voice_profile_discovery_endpoint.md` | [completed/2026-05-08_voice_profile_discovery_endpoint.md](completed/2026-05-08_voice_profile_discovery_endpoint.md) | Gateway now exposes voice, speech-model, and vision provider-model catalog endpoints that proxy AbstractCore catalog routes when configured and use static bounded fallback otherwise. | `PYTHONPATH=src pytest -q tests/test_gateway_capability_catalog_proxy.py`. |
| Gateway model residency control plane | `proposed/2026-05-19_model_residency_gateway_control_plane.md` | [completed/2026-05-19_model_residency_gateway_control_plane.md](completed/2026-05-19_model_residency_gateway_control_plane.md) | Gateway now exposes `/api/gateway/models/loaded|load|unload` through Runtime's public host facade instead of direct Core route logic. | `PYTHONPATH=src:../abstractruntime/src:../abstractcore pytest -q tests/test_gateway_model_residency_endpoints.py tests/test_capabilities_endpoint_contract.py`. |
| Gateway durable bloc prompt-cache contract via Runtime | `proposed/2026-05-20_gateway_durable_bloc_prompt_cache_contract.md` | [completed/2026-05-20_gateway_durable_bloc_prompt_cache_contract.md](completed/2026-05-20_gateway_durable_bloc_prompt_cache_contract.md) | Gateway now exposes durable bloc/KV/binding routes through Runtime and distinguishes that app-facing contract from provider-private snapshot save/load. | `PYTHONPATH=src:../abstractruntime/src:../abstractcore pytest -q tests/test_gateway_durable_bloc_prompt_cache_endpoints.py tests/test_gateway_prompt_cache_endpoints.py tests/test_capabilities_endpoint_contract.py`. |
| Gateway prompt-cache save/load via Runtime | `proposed/2026-05-20_gateway_prompt_cache_save_load_via_runtime.md` | [completed/2026-05-20_gateway_prompt_cache_save_load_via_runtime.md](completed/2026-05-20_gateway_prompt_cache_save_load_via_runtime.md) | Gateway now routes the legacy `saved/save/load` prompt-cache aliases through Runtime's public host facade and no longer reaches into provider-private prompt-cache state. | `PYTHONPATH=src:../abstractruntime/src:../abstractcore python -m pytest -q tests/test_gateway_prompt_cache_endpoints.py tests/test_capabilities_endpoint_contract.py`. |
| Truthful media residency gateway contract | `proposed/2026-05-20_truthful_media_residency_gateway_contract.md` | [completed/2026-05-20_truthful_media_residency_gateway_contract.md](completed/2026-05-20_truthful_media_residency_gateway_contract.md) | Gateway now advertises media residency truthfully and delegates the residency routes through Runtime's public host facade. | `PYTHONPATH=src:../abstractruntime/src:../abstractcore pytest -q tests/test_gateway_model_residency_endpoints.py tests/test_capabilities_endpoint_contract.py tests/test_gateway_capability_catalog_proxy.py`. |
| Gateway Runtime boundary cleanup for workspace, comms, and Telegram | `planned/0050_gateway_runtime_boundary_cleanup_for_workspace_comms_and_telegram.md` | [completed/0050_gateway_runtime_boundary_cleanup_for_workspace_comms_and_telegram.md](completed/0050_gateway_runtime_boundary_cleanup_for_workspace_comms_and_telegram.md) | Gateway now owns workspace/file helpers locally, uses Runtime comms/Telegram helper surfaces for operator paths, and no longer imports `abstractcore` directly in source. | `PYTHONPATH=src:../abstractruntime/src:../abstractcore pytest -q tests/test_gateway_email_inbox_endpoints.py tests/test_gateway_cli_split_runner.py tests/test_gateway_telegram_bridge_unit.py tests/test_gateway_discovery_endpoints.py tests/test_gateway_files_skim_endpoint.py tests/test_gateway_attachments_ingest.py tests/test_gateway_workspace_policy_enforcement.py tests/test_maintenance_notifier_unit.py`. |

## Deprecated Work

| Item | Reason |
| --- | --- |
| [deprecated/2026-05-20_gateway_runtime_owned_run_truth_and_core_boundary.md](deprecated/2026-05-20_gateway_runtime_owned_run_truth_and_core_boundary.md) | Superseded by completed Runtime-owned media/discovery/control-plane work plus the narrower planned cleanup for workspace, comms, and Telegram. |

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
- Gateway source now imports Runtime rather than Core directly for the main
  execution path and the remaining comms/Telegram helper paths. Deeper Runtime
  host-helper polish can continue without reopening Gateway's source boundary.
- Gateway prompt-cache snapshot aliases now use Runtime's public host facade;
  the old Gateway-local provider-private snapshot code and dead Core catalog
  proxy module were removed.

## Completion Process

When a planned item is completed:

1. Finish code, tests, and docs.
2. Add a Completion report to the backlog item.
3. Update metadata with `Status: Completed` and a completion date.
4. Move the file to `docs/backlog/completed/`.
5. Update this overview counts, tables, and completed ledger.
6. Review follow-up signals and create proposed/planned work only when justified
   by code evidence.
