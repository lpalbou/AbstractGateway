# Planned: Gateway Session Prompt-Cache Lifecycle

## Metadata

- Created: 2026-05-08
- Status: Completed
- Completed: 2026-05-08

## Context

AbstractGateway already exposes provider-level prompt-cache operator endpoints.
That is enough for expert debugging and some AbstractCode controls, but not yet
enough for thin apps that think in sessions, workflows, providers, models, and
stable agent templates.

This item promotes and narrows the incoming session-cache proposal. The gateway
should orchestrate a session lifecycle where the provider supports it, while
remaining honest when only keyed/server-managed cache hints are possible.

## Current Code Reality

Inspected files and symbols:

- `src/abstractgateway/routes/gateway.py`
  - `_gateway_runtime_llm_client`
  - `_gateway_prompt_cache_client_capabilities`
  - `_gateway_prompt_cache_client_call`
  - `/prompt_cache/stats`
  - `/prompt_cache/capabilities`
  - `/prompt_cache/set`
  - `/prompt_cache/update`
  - `/prompt_cache/fork`
  - `/prompt_cache/clear`
  - `/prompt_cache/prepare_modules`
  - `/prompt_cache/saved`
  - `/prompt_cache/save`
  - `/prompt_cache/load`
  - `start_run`
- `docs/api.md`
- `docs/configuration.md`
- `../abstractcode/web/src/lib/run_input.ts`
- `../abstractcode/web/src/ui/app.tsx`
- `../abstractcode/docs/api.md`
- `../abstractcode/docs/architecture.md`

What exists now:

- Provider-level prompt-cache control endpoints are implemented and documented.
- Local mode delegates to the in-process AbstractCore LLM client when available.
- Remote/hybrid mode follows whatever AbstractCore server prompt-cache surface is
  configured.
- `save` and `load` are explicitly local/provider-specific and currently aimed
  at MLX and HuggingFace GGUF/llama.cpp workers.
- AbstractCode derives a stable `prompt_cache_key` and passes
  `_runtime.prompt_cache` in run input. Runtime/Core inject provider-specific
  cache hints where supported.

What was missing or brittle before completion:

- There were no session-scoped Gateway endpoints such as
  `/sessions/{session_id}/prompt_cache/status`.
- Gateway did not yet derive namespaces from `session_id`, `bundle_id`,
  `flow_id`, `provider`, and `model`.
- Thin clients had to either call provider-level endpoints directly or derive keys
  locally.
- Discovery had to report `session_lifecycle=false` until this task landed.

## Problem

Apps need cache controls at the session level, not the provider internals level.
For example, an AbstractAssistant or AbstractFlow session should be able to ask:
"is cache supported for this provider/model/workflow?", "prepare this session",
"clear this session cache", and "what key/status should I show?".

Without that gateway layer, clients leak provider-specific naming and lifecycle
decisions.

## What We Want To Do

Add a Gateway-owned session prompt-cache lifecycle API that maps session/workflow
identity onto existing Runtime/Core provider controls.

Possible v1 routes:

```text
GET  /api/gateway/sessions/{session_id}/prompt_cache/status
POST /api/gateway/sessions/{session_id}/prompt_cache/prepare
POST /api/gateway/sessions/{session_id}/prompt_cache/rebuild
POST /api/gateway/sessions/{session_id}/prompt_cache/clear
```

Each route should accept provider/model/workflow identity:

```json
{
  "bundle_id": "basic-agent",
  "flow_id": "81795ea9",
  "provider": "mlx",
  "model": "mlx-community/Qwen3-4B-4bit",
  "template_id": "assistant.default"
}
```

## Why

This gives thin clients a clean abstraction while preserving provider truth. It
also makes MLX cache controls usable from gateway-first apps without each app
inventing its own key convention.

## Requirements

- Use a deterministic namespace derived from bounded, sanitized fields:
  `session_id`, `bundle_id`, `flow_id`, `provider`, `model`, and optional
  `template_id` or workflow version.
- Do not expose unsafe raw filesystem paths or unbounded provider metadata.
- Use existing provider-level methods where possible:
  `capabilities`, `set`, `prepare_modules`, `fork`, `update`, `clear`, `stats`.
- Support at least three honest modes:
  - `unsupported`: no provider prompt cache support.
  - `keyed`: stable `prompt_cache_key` can be supplied but module preparation is
    not available.
  - `local_control_plane`: module/update/fork controls are available.
- Keep provider-level `/prompt_cache/*` endpoints intact.
- Return `supported=false` with capabilities and hints instead of failing normal
  app workflows for unsupported providers.
- Update discovery to report `prompt_cache.session_lifecycle=true` only after
  these routes exist and have tests.
- Preserve the distinction between provider cache controls and a full
  Gateway-owned CachedSession HTTP API.

## Suggested Implementation

- Add Pydantic request/response models near the prompt-cache route section.
- Implement a small session-cache key helper that hashes long identity fields and
  keeps labels readable but bounded.
- Implement `status` as the first endpoint. It should call provider/model
  capabilities and compute the active namespace/key without mutating state.
- Implement `prepare` using `prepare_modules` when available; otherwise return a
  stable key hint for runtime injection.
- Implement `clear` for the computed key. `rebuild` can initially be
  clear-plus-prepare if that is all provider capabilities allow.
- Consider adding a run-start convenience later, but keep v1 explicit so clients
  can decide when to prepare.

## Scope

- Gateway session prompt-cache endpoints.
- Discovery update.
- Tests for unsupported, keyed, and local-control-plane providers using stubs.
- Docs in API/configuration.

## Non-Goals

- Do not replace provider-level prompt-cache endpoints.
- Do not promise local KV persistence for remote providers.
- Do not make unsupported providers fail run start.
- Do not silently enable cache without a clear request or runtime input policy.

## Dependencies And Related Tasks

- Depends on completed `docs/backlog/completed/010_versioned_client_capability_contract.md`.
- Related to AbstractCore/Runtime prompt-cache capabilities.
- Related to AbstractCode's current `_runtime.prompt_cache` key injection.

## Expected Outcomes

- A gateway-first app can show session cache status and actions without owning
  provider internals.
- MLX local provider deployments can prepare/reuse/clear session cache keys
  through Gateway.
- Server-managed/keyed providers receive stable cache keys when applicable.
- Unsupported providers report structured unsupported responses.

## Validation

- A: Pure tests for namespace/key derivation and sanitization.
- A: Stub-provider tests for unsupported/keyed/local-control-plane responses.
- B: TestClient route tests for auth, status, prepare, clear, rebuild.
- B: Existing provider-level prompt-cache tests still pass.
- C: Manual MLX run when available: prepare cache, start repeated session turns,
  inspect stats, save/load if supported.

## Progress Checklist

- [x] Define session prompt-cache request/response models.
- [x] Add deterministic namespace/key helper.
- [x] Add `status`.
- [x] Add `prepare`.
- [x] Add `clear`.
- [x] Add `rebuild` or explicitly defer it with documented semantics.
- [x] Update discovery and docs.
- [x] Add A/B tests.

## Guidance For The Implementing Agent

Be conservative with claims. The gateway should make cache controls easier to
use, not hide provider limitations. Prefer explicit structured unsupported
responses over fallback behavior that makes clients think caching is active when
it is not.

## Completion Report

- Completed: 2026-05-08
- Summary: Added Gateway session prompt-cache lifecycle routes on top of the existing provider-level prompt-cache control plane.
- Files/symbols touched: `src/abstractgateway/routes/gateway.py` (`_GatewaySessionPromptCacheTarget`, `_GatewaySessionPromptCacheRequest`, `_session_prompt_cache_identity`, `_session_prompt_cache_mode`, `_session_prompt_cache_runtime_hint`, `_session_prompt_cache_modules_from_request`, `session_prompt_cache_status`, `session_prompt_cache_prepare`, `session_prompt_cache_clear`, `session_prompt_cache_rebuild`, `_build_client_capability_contracts`), `tests/test_gateway_prompt_cache_endpoints.py`, `tests/test_capabilities_endpoint_contract.py`, `docs/api.md`, `docs/configuration.md`.
- Behavior changes: Gateway now exposes `GET /sessions/{session_id}/prompt_cache/status`, `POST /prepare`, `POST /clear`, and `POST /rebuild`. The routes derive a stable bounded namespace/key from session, bundle, flow, provider, model, template, and version fields; return structured unsupported responses; provide runtime hints for keyed/server-managed providers; and use local provider operations when `prepare_modules`, `fork`, `set`, `clear`, and `stats` are available.
- Tests: `PYTHONPATH=src python -m compileall -q src/abstractgateway/routes/gateway.py`; `PYTHONPATH=src pytest -q tests/test_capabilities_endpoint_contract.py tests/test_gateway_prompt_cache_endpoints.py tests/test_generated_media_gateway_contract.py`.
- Docs: Updated API and configuration docs with session lifecycle endpoints, modes, and the continued distinction from provider-specific cache persistence.
- Residual risks: Manual MLX cache warm/reuse/save/load validation was not run in this environment. Keyed remote providers still depend on Runtime/Core honoring the returned `runtime_hint`.
- Follow-ups: Consider a run-start convenience hook only after clients prove they need automatic prepare/inject behavior; keep v1 explicit for now.
- Priority impact: 030 is complete; prompt-cache discovery can truthfully advertise `session_lifecycle=true`.
