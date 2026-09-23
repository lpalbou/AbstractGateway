# AbstractGateway Backlog Overview

## Status

AbstractGateway is a durable HTTP/SSE host for AbstractRuntime runs. The current
main-branch surface provides run start/schedule/input/history, durable
commands, ledger replay/streaming, artifacts, VisualFlow CRUD/publish,
workspace policy, provider and tool discovery, Runtime-owned direct voice TTS,
audio transcription, direct generated and edited image child-run artifacts,
direct generated music, provider-level and session-level prompt-cache controls,
optional embeddings/KG helpers with configurable memory stores, Runtime-backed
voice/vision/music discovery routes, explicit install/config profiles, and a
versioned thin-client capability contract for Flow, Assistant, Observer, Code,
and shared Gateway features.

The next planning focus is to make the gateway a premium control plane for thin
AI apps such as AbstractFlow, AbstractAssistant, AbstractObserver, and
AbstractCode. The gateway must report what is actually usable, keep
provider/runtime concerns server-side, and expose stable contracts that clients
can trust without importing local gateway packages.

## Counts

- Planned: 2
- Proposed: 39
- Completed: 22
- Deprecated: 1
- Recurrent: 0

<!-- Counts recomputed from the directories on 2026-08-29 while filing
     0234. They had drifted (planned 0→1, proposed 30→38, completed
     20→22) across earlier passes that added items without updating this
     block; the skill treats count drift as a real backlog bug to fix in
     the same pass rather than carry. The Proposed Items table below
     still lists fewer rows than the directory holds — untabled items
     are a separate hygiene pass, flagged rather than silently
     backfilled. -->

## Priority Bands

- Completed: 010 capability discovery and thin-client feature gating.
- Completed: 020 AbstractFlow gateway-first editor contract and validation.
- Completed: 030 Gateway-owned session prompt-cache lifecycle.
- Completed: 040 Generated-media artifact and direct image contract.
- Completed: model residency truth, truthful media residency, durable bloc prompt-cache exposure, direct music contract truth, and Runtime boundary cleanup for workspace/comms/Telegram.
- Completed: install profiles and configuration entrypoint.
- Completed: memory store resolver and TripleStore abstraction.
- Completed: Core-backed Voice/Vision catalog proxy endpoints.
- Completed: provider-private prompt-cache save/load migration via Runtime.
- Completed: Gateway-owned thin-client catalog envelope normalization.
- Completed: Gateway-owned thin-client surface readiness summary.

## Next Recommended Work

A 2026-07-05 adversarial review (three code/security/product reviewers plus
dedicated design reviews of the ops suite and SSE streaming) produced a phased
roadmap now captured as proposed items `0059`-`0081`. The prior inward
contract/boundary work is largely complete; the review's finding is that effort
should shift to (a) restoring truth the code has drifted from, (b) making the
already-shipped hosted multi-user mode safe and durable, (c) unblocking velocity
and shrinking the trust surface, and (d) letting external clients cross the
contract boundary.

Recommended sequencing:

- Phase 0 (truth and footguns): `0059` import boundary + abstractcore regression,
  `0060` fail-closed auth/network defaults, `0061` documentation/contract truth.
- Phase 1 (safe + durable hosted multi-user): `0084` per-user RBAC + workspace
  grants, `0062` tenant execution isolation tiers (depends on `0084`), `0063`
  eager run rehydration + runner lock-retry, `0064` per-principal quotas +
  retention, `0065` auth DoS hardening, `0079` secret-at-rest + ledger-redaction,
  `0080` audit reads/integrity + session signature.
- Phase 2 (velocity + trust surface): `0066` decompose the router, `0067` ops
  suite boundary + durable execution (layer C with `0083`), `0068` externalize
  console, `0069` unified settings + generated config docs, `0070` authorization
  contract test + exception audit (COMPLETED 2026-07-21), `0081` failure-mode
  test suite, `0083` agentic orchestration parity to retire codex.
- Phase 3 (adoptability): `0071` client SDKs, `0072` conformance kit + frozen
  event vocabulary, `0073` webhooks/event egress, `0074` OpenAI-compatible
  facade, `0082` ledger replay integrity + durable cursor (prerequisite spike),
  then `0075` event-driven ledger streaming.
- Phase 4 (scale + hosted viability, behind explicit triggers): `0076` scale-out
  runner leasing + durable-log tail, `0077` usage metering/analytics, `0078`
  premium-control-plane SLOs + deprecation policy.

The pre-existing deeper provider/backend truth item (`0055`) and the three
older proposed items remain valid and are folded into the phases above where
relevant.

## Planned Items

| Item | Acceptance summary |
| --- | --- |
| [0846_publish_promote_must_not_rebuild_every_service.md](planned/0846_publish_promote_must_not_rebuild_every_service.md) | Publish/promote reloads only what changed, on the existing runtime, for the affected service only; no model reload, no prompt-cache loss, `/api/health` never misses a supervisor probe. |
| [0233_real_run_cancellation.md](planned/0233_real_run_cancellation.md) | Cancel aborts the in-flight generation (cross-package; see the framework-root master item). |

## Proposed Items

| Item | Promotion criteria |
| --- | --- |
| [2026-05-09_abstractflow_draft_spaces_and_ephemeral_runs.md](proposed/2026-05-09_abstractflow_draft_spaces_and_ephemeral_runs.md) | Remaining optional hardening: draft-bundle cleanup and default memory-scope isolation; run-tree purge is implemented. |
| [2026-05-13_shared_identity_context.md](proposed/2026-05-13_shared_identity_context.md) | Promote when shared identity/session context becomes an active Gateway contract decision instead of exploratory design work. Related: `0062`, `0064`. |
| [0055_gateway_provider_backend_readiness_truth_for_thin_clients.md](proposed/0055_gateway_provider_backend_readiness_truth_for_thin_clients.md) | Promote when Runtime/Core can supply selected backend/provider/model and stable degraded-state truth for Gateway to relay to thin clients. |
| [offline_first_gateway_connectivity.md](proposed/offline_first_gateway_connectivity.md) | Promote when offline-first connectivity guarantees become a near-term product commitment. |
| [0059_enforce_gateway_import_boundary_and_fix_abstractcore_regression.md](proposed/0059_enforce_gateway_import_boundary_and_fix_abstractcore_regression.md) | Phase 0. Promote now for the CI guard + ledger correction; facade migration when Runtime adds a config facade. |
| [0060_fail_closed_auth_and_network_defaults.md](proposed/0060_fail_closed_auth_and_network_defaults.md) | Phase 0. Promote now; catastrophic-downside, near-zero-cost footguns. |
| [0061_documentation_truth_and_contract_consistency.md](proposed/0061_documentation_truth_and_contract_consistency.md) | Phase 0. Promote now; cheapest trust repairs. |
| [0062_tenant_execution_isolation_tiers.md](proposed/0062_tenant_execution_isolation_tiers.md) | Phase 1. Promote Tier 1 with 0084 (safety floor for shipped multi-user); Tier 2/3 sandboxing with untrusted-tenant commitment. Admins run unsandboxed; regular users sandboxed. |
| [0084_user_rbac_and_workspace_grants.md](proposed/0084_user_rbac_and_workspace_grants.md) | Phase 1 foundation. Promote with 0062 Tier 1 — per-user rwx workspace grants are the policy ceiling execution isolation enforces. |
| [0063_eager_run_rehydration_and_runner_lease_retry.md](proposed/0063_eager_run_rehydration_and_runner_lease_retry.md) | Phase 1. Promote lock-retry now; eager rehydration with per-principal lifecycle work. |
| [0064_per_principal_quotas_and_nondraft_retention.md](proposed/0064_per_principal_quotas_and_nondraft_retention.md) | Phase 1. Promote with 0062 Tier 1. |
| [0065_auth_dos_hardening_token_index_and_rate_limits.md](proposed/0065_auth_dos_hardening_token_index_and_rate_limits.md) | Phase 1. Promote with hosted multi-user hardening. |
| [0066_decompose_gateway_router_god_module.md](proposed/0066_decompose_gateway_router_god_module.md) | Phase 2. Promote after Phase 0; unblocks most other work. |
| [0067_self_hosting_ops_suite_boundary.md](proposed/0067_self_hosting_ops_suite_boundary.md) | Phase 2. Promote layer A (trust-domain) now; B with 0066; C with 0083 when durable self-evolution is prioritized. |
| [0068_externalize_console_static_assets.md](proposed/0068_externalize_console_static_assets.md) | Phase 2. Promote with/after 0066. |
| [0069_unified_gateway_settings_and_generated_config_docs.md](proposed/0069_unified_gateway_settings_and_generated_config_docs.md) | Phase 2. Promote after 0066 or independently. |
| [0071_official_client_sdks_render_kit.md](proposed/0071_official_client_sdks_render_kit.md) | Phase 3. Promote after/with 0072. |
| [0072_client_conformance_kit_and_frozen_event_vocabulary.md](proposed/0072_client_conformance_kit_and_frozen_event_vocabulary.md) | Phase 3. Promote with or just before 0071. |
| [0073_run_lifecycle_webhooks_event_egress.md](proposed/0073_run_lifecycle_webhooks_event_egress.md) | Phase 3. Promote after 0075 (efficient triggering) or with a poll-based trigger. |
| [0074_openai_compatible_facade.md](proposed/0074_openai_compatible_facade.md) | Phase 3. Promote after 0071/0072 or as a demo spike. |
| [0075_event_driven_ledger_streaming.md](proposed/0075_event_driven_ledger_streaming.md) | Phase 3. Promote AFTER 0082; then single-node design; multi-worker bridge follows demand (0076 triggers). |
| [0082_ledger_replay_integrity_and_durable_cursor.md](proposed/0082_ledger_replay_integrity_and_durable_cursor.md) | Phase 3 prerequisite. Promote FIRST, before 0075/0076 — pins the durable cursor + replay-equivalence invariants so streaming cannot break replay. |
| [0083_agentic_orchestration_parity_to_retire_codex.md](proposed/0083_agentic_orchestration_parity_to_retire_codex.md) | Phase 2/3 enabler. Promote when durable self-evolution (0067-C) is prioritized; primarily AbstractRuntime/AbstractAgent work that lets the framework retire the codex subprocess. |
| [0076_scale_out_runner_leasing_and_durable_log_tail.md](proposed/0076_scale_out_runner_leasing_and_durable_log_tail.md) | Phase 4. Promote only when explicit scale triggers are measured. |
| [0077_usage_metering_quotas_analytics.md](proposed/0077_usage_metering_quotas_analytics.md) | Phase 4. Promote when hosted use goes beyond trusted teams. |
| [0078_premium_control_plane_slos_and_deprecation_policy.md](proposed/0078_premium_control_plane_slos_and_deprecation_policy.md) | Phase 4. Promote early once 0075 yields real single-node numbers. |
| [0079_secret_at_rest_encryption_and_ledger_redaction_audit.md](proposed/0079_secret_at_rest_encryption_and_ledger_redaction_audit.md) | Phase 1. Promote with hosted multi-user hardening. |
| [0080_audit_reads_integrity_and_session_signature.md](proposed/0080_audit_reads_integrity_and_session_signature.md) | Phase 1. Promote with hosted multi-user hardening. |
| [0081_failure_mode_test_suite.md](proposed/0081_failure_mode_test_suite.md) | Phase 2. Promote alongside 0063/0075 or incrementally now. |
| [0085_runner_lock_adversary_followups.md](proposed/0085_runner_lock_adversary_followups.md) | Hardening residue of the 2026-07-11 runner-lock handover (adversary F3-F13, non-blocking; P0/P1 already fixed + pinned). Runner items (F3-F8) promotable any time — each is small + testable; launcher items (F9-F13) need observer/flow coordination on their duplicated script copies. |
| [0234_deny_verb_for_run_tool_policy.md](proposed/0234_deny_verb_for_run_tool_policy.md) | Promote when a thin client commits to shipping the default-stance permission model — AbstractCode has asked (2026-08-28) and its client half is blocked on this. Verified gap: the run policy's two lists are both allow-shaped (`require_approval_tools` means ASK, not REFUSE), so "deny all", "deny + whitelist" and "approve + blacklist" are not expressible on the wire. |

## Completed Work Ledger

| Item | Original path | Completed path | Outcome | Validation |
| --- | --- | --- | --- | --- |
| Route authorization contract test + exception-swallowing audit | `proposed/0070_route_authorization_contract_test_and_exception_audit.md` | [completed/0070_route_authorization_contract_test_and_exception_audit.md](completed/0070_route_authorization_contract_test_and_exception_audit.md) | Whole-app authorization invariant pinned in three layers (boundary, per-write decision, served-surface proof); 13 durability-relevant silent exception swallows now log with context and consequence. Accepted by laurent 2026-07-21 ("a route can never ship unprotected"). | `pytest -q tests/test_gateway_route_authorization_contract.py tests/test_gateway_runner_swallow_audit.py`; full suite 857 passed. |
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
| Model residency provider/task contract truth regression | `proposed/0051_model_residency_provider_task_contract_truth_regression.md` | [completed/0051_model_residency_provider_task_contract_truth_regression.md](completed/0051_model_residency_provider_task_contract_truth_regression.md) | Gateway now derives `common.model_residency` from Runtime's public `get_model_residency_capabilities(...)` surface instead of hard-coding media support flags, so higher apps see truthful task support. | `python -m pytest -q tests/test_gateway_model_residency_endpoints.py tests/test_capabilities_endpoint_contract.py`. |
| Gateway music generation contract for thin clients | `proposed/0052_gateway_music_generation_contract_for_thin_clients.md` | [completed/0052_gateway_music_generation_contract_for_thin_clients.md](completed/0052_gateway_music_generation_contract_for_thin_clients.md) | Gateway's direct music route and music catalogs were already present; the remaining workflow-availability truth gap is now closed and recorded as completed history. | `python -m pytest -q tests/test_capabilities_endpoint_contract.py tests/test_generated_media_gateway_contract.py tests/test_gateway_capability_catalog_proxy.py`. |
| Gateway thin-client catalog and readiness contract polish | `proposed/0053_gateway_thin_client_catalog_and_readiness_contract_polish.md` | [completed/0053_gateway_thin_client_catalog_and_readiness_contract_polish.md](completed/0053_gateway_thin_client_catalog_and_readiness_contract_polish.md) | Gateway now exposes a compact `common.readiness` surface summary derived from its own contract descriptors, without inventing deeper lower-layer backend truth. | `python -m pytest -q tests/test_capabilities_endpoint_contract.py tests/test_gateway_capability_catalog_proxy.py tests/test_gateway_discovery_endpoints.py`. |
| Gateway catalog envelope contract for thin clients | `proposed/0053_gateway_thin_client_catalog_and_readiness_contract_polish.md` (catalog half) | [completed/0054_gateway_catalog_envelope_contract_for_thin_clients.md](completed/0054_gateway_catalog_envelope_contract_for_thin_clients.md) | Gateway discovery routes now preserve legacy fields but also expose a versioned canonical `catalog` envelope plus `items` for thin clients. | `python -m pytest -q tests/test_gateway_capability_catalog_proxy.py tests/test_gateway_discovery_endpoints.py tests/test_capabilities_endpoint_contract.py`. |
| Gateway PDF Runtime floor and E2E contract | N/A | [completed/0056_gateway_pdf_runtime_floor_and_e2e_contract.md](completed/0056_gateway_pdf_runtime_floor_and_e2e_contract.md) | Gateway now depends on Runtime `0.4.28` for VisualFlow PDF nodes and proves write/read PDF bundle execution through the normal run path. | `PYTHONPATH=src:../abstractruntime/src:../abstractcore:../abstractagent/src:../abstractmemory/src:../abstractsemantics/src:../abstractvision/src:../abstractvoice/src:../abstractmusic/src:../abstractaudio/src:../abstractvideo/src:../abstractsound/src python -m pytest tests/test_gateway_visualflow_file_nodes.py tests/test_gateway_install_profiles.py tests/test_gateway_artifacts_endpoint.py::test_gateway_artifacts_api_list_metadata_and_download -q`. |
| Gateway Runtime Core Vision upscale surface | `planned/0057_gateway_runtime_core_vision_upscale_surface.md` | [completed/0057_gateway_runtime_core_vision_upscale_surface.md](completed/0057_gateway_runtime_core_vision_upscale_surface.md) | Gateway now exposes Runtime-backed image upscaling, advertises `upscaled_image` contracts/readiness, accepts `task=image_upscale` in Vision catalogs, and forwards richer media controls without calling Core directly. | `PYTHONPATH=src:../abstractruntime/src:../abstractcore python -m compileall -q src/abstractgateway/routes/gateway.py`; `PYTHONPATH=src:../abstractruntime/src:../abstractcore pytest -q tests/test_generated_media_gateway_contract.py::test_gateway_direct_image_upscale_uses_runtime_child_run_contract tests/test_gateway_capability_catalog_proxy.py::test_vision_provider_catalog_accepts_image_upscale_task tests/test_capabilities_endpoint_contract.py`. |
| Gateway vision adapter and batch surface | `proposed/0058_gateway_vision_adapter_and_batch_surface.md` | [completed/0058_gateway_vision_adapter_and_batch_surface.md](completed/0058_gateway_vision_adapter_and_batch_surface.md) | Gateway now exposes Runtime-backed vision adapter discovery, forwards batch/seeds/LoRA/flow-shift media fields, and returns plural artifact refs for batch image/video generation without changing lower-package ownership. | `PYTHONPATH=src:../abstractruntime/src:../abstractcore:../abstractvision/src pytest -q tests/test_generated_media_gateway_contract.py tests/test_gateway_capability_catalog_proxy.py tests/test_capabilities_endpoint_contract.py`. |

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
- Edited image support is now directly available through Gateway for run-scoped
  image-to-image and optional masked edits.
- Image upscaling is now directly available through Gateway for run-scoped
  artifact-backed source images, using Runtime's durable child-run facade.
- Gateway now exposes run-scoped TTS, STT, and music generation contracts for
  higher apps, plus a host-capture `voice.listen` contract for clients that
  record locally and submit events or uploaded audio.
- Gateway discovery routes now expose a versioned `gateway_catalog_v1`
  envelope plus canonical `items`, so higher apps no longer need route-local
  parsing for common provider/model/voice pickers.
- Gateway now also exposes a narrow `gateway_surface_readiness_v1` summary in
  `common.readiness`, but deeper provider/backend/model truth still belongs in
  Runtime/Core rather than Gateway-side inference.
- Gateway source now imports Runtime rather than Core directly for the main
  execution path and the remaining comms/Telegram helper paths. Deeper Runtime
  host-helper polish can continue without reopening Gateway's source boundary.
- Gateway prompt-cache snapshot aliases now use Runtime's public host facade;
  the old Gateway-local provider-private snapshot code and dead Core catalog
  proxy module were removed.
- Gateway now owns AbstractFlow draft-test run purge through
  `/api/gateway/runs/purge_drafts`, using Runtime optional deletion protocols
  while keeping draft/published taxonomy out of Runtime.

## Completion Process

When a planned item is completed:

1. Finish code, tests, and docs.
2. Add a Completion report to the backlog item.
3. Update metadata with `Status: Completed` and a completion date.
4. Move the file to `docs/backlog/completed/`.
5. Update this overview counts, tables, and completed ledger.
6. Review follow-up signals and create proposed/planned work only when justified
   by code evidence.
