# Planned: AbstractFlow Gateway-First Editor Contract

## Metadata

- Created: 2026-05-08
- Status: Completed
- Completed: 2026-05-08

## Context

AbstractFlow's browser editor now runs substantially through AbstractGateway:
it stores VisualFlow JSON through gateway routes, publishes draft flows as
WorkflowBundles, starts runs through the gateway, and renders execution through
ledger replay/SSE.

The existing API surface is broad enough to support this direction, but the
editor contract is implicit and not covered by one end-to-end Gateway test.

## Current Code Reality

Inspected files and symbols:

- `src/abstractgateway/routes/gateway.py`
  - `list_visualflows`, `create_visualflow`, `get_visualflow`,
    `update_visualflow`, `delete_visualflow`, `publish_visualflow`
  - `list_bundles`, `upload_bundle`, `reload_bundles`,
    `deprecate_bundle`, `undeprecate_bundle`, `get_bundle_flow`
  - `start_run`, `get_run`, `list_runs`, `get_run_input_data`,
    `get_run_history_bundle`, `list_run_artifacts`, `get_ledger`,
    `get_ledger_batch`, `stream_ledger`, `submit_command`
  - `discovery_providers`, `discovery_tools`, `get_semantics_registry`,
    `workspace_policy`, `kg_query`
- `../abstractflow/web/frontend/src/hooks/useWebSocket.ts`
- `../abstractflow/web/frontend/src/components/Toolbar.tsx`
- `../abstractflow/web/frontend/src/components/RunFlowModal.tsx`
- `../abstractflow/web/frontend/src/hooks/useProviders.ts`
- `../abstractflow/web/frontend/src/hooks/useTools.ts`
- `../abstractflow/web/frontend/src/hooks/useExecutionWorkspace.ts`

What exists now:

- Gateway VisualFlow CRUD and publish endpoints exist.
- The AbstractFlow frontend publishes a draft via
  `/api/gateway/visualflows/{flow_id}/publish`, then starts
  `/api/gateway/runs/start`.
- The frontend streams `/api/gateway/runs/{run_id}/ledger/stream`, fetches run
  summaries, input data, artifacts, history bundles, providers, models, tools,
  semantics, workspace policy, and KG query.
- `GET /api/gateway/runs/{run_id}/input_data` returns a filtered start-input
  view and workspace defaults.

What is missing or brittle:

- There is no explicit `flow_editor` contract in gateway discovery yet.
- There is no single contract test covering draft VisualFlow to publish to run
  to ledger/history/artifacts.
- Run input schema discovery is not first-class. The current `input_data`
  endpoint helps replay/follow-up, but it is not a schema contract for the Run
  Flow modal.
- AbstractFlow still has local connection/config backend endpoints; those can
  remain for dev, but Gateway should declare the editor/runtime surface clearly.

## Problem

AbstractFlow cannot be simplified into a true gateway-first editor while the
Gateway contract is implicit. Client code has to infer support from route
presence and fallback behavior, which makes future refactors risky.

## What We Want To Do

Define and validate a versioned AbstractFlow editor contract in Gateway.

The contract should include:

- VisualFlow CRUD and publish.
- Bundle discovery, upload, reload, deprecation, undeprecation, and flow
  inspection.
- Run lifecycle: start, schedule, commands, run summary/listing, input data,
  ledger replay, ledger batch, ledger stream, history bundle, artifacts.
- Discovery helpers: providers, models, model capabilities, tools, semantics,
  workspace policy, KG/embeddings when configured.
- A first-class run input schema descriptor for a bundle/flow.

## Why

AbstractFlow should be able to run its browser editor against only
AbstractGateway. That requires Gateway-owned tests and docs, not only frontend
assumptions.

## Requirements

- Add or extend `contracts.flow_editor` in discovery after task 010.
- Add a schema endpoint or extend an existing bundle/flow endpoint so the Run
  Flow modal can ask for input pins/defaults/types without parsing raw
  VisualFlow JSON too deeply.
- Preserve existing VisualFlow CRUD/publish behavior.
- Keep bundle mode as the recommended deployment path.
- Add contract tests for the complete editor path.
- Ensure auth still protects all `/api/gateway/*` editor endpoints.
- Document any endpoints that are intentionally best-effort.

## Suggested Implementation

- Start after task 010 defines the shared contract vocabulary.
- Add a route such as:

```text
GET /api/gateway/bundles/{bundle_id}/flows/{flow_id}/input_schema
```

or extend `GET /api/gateway/bundles/{bundle_id}/flows/{flow_id}` with a stable
schema block. Prefer an additive route if changing the existing payload would
make the contract muddy.

- Reuse existing VisualFlow pin extraction helpers where possible.
- Add a `tests/test_abstractflow_editor_gateway_contract.py` that:
  - creates a VisualFlow;
  - updates/fetches/lists it;
  - publishes it as a dev bundle;
  - starts it through `/runs/start`;
  - reads `/ledger`, `/history_bundle`, `/input_data`, `/artifacts`;
  - deletes the draft flow.
- Update `docs/api.md` with a short AbstractFlow editor contract section.

## Scope

- Gateway API contract, tests, and docs.
- Minimal additive input-schema API if needed.
- No AbstractFlow frontend redesign in this task.

## Non-Goals

- Do not remove AbstractFlow's local backend compatibility routes.
- Do not require a remote gateway for local development.
- Do not make the editor depend on unprotected local-only endpoints for runtime
  behavior.

## Dependencies And Related Tasks

- Depends on completed `docs/backlog/completed/010_versioned_client_capability_contract.md`.
- Related to `040_generated_media_gateway_contract.md` for generated artifact
  rendering.

## Expected Outcomes

- AbstractFlow can truthfully describe AbstractGateway as its primary runtime
  host.
- Gateway tests catch breaking changes to the editor path.
- The Run Flow modal can use a first-class schema contract rather than relying
  only on raw VisualFlow JSON conventions.

## Validation

- A: Unit tests for input schema extraction.
- B: TestClient contract test for VisualFlow CRUD/publish/start/observe.
- B: Auth checks for representative editor endpoints.
- C: Manual run of AbstractFlow frontend against a local gateway.
- Docs: Update API docs and any AbstractFlow-facing notes if the route names
  are finalized.

## Progress Checklist

- [x] Re-read current Gateway and AbstractFlow frontend route usage.
- [x] Add/verify `contracts.flow_editor`.
- [x] Design the run input schema endpoint/payload.
- [x] Add end-to-end editor contract tests.
- [x] Update docs.

## Guidance For The Implementing Agent

Keep this contract practical and route-based. AbstractFlow already works through
Gateway in many places; the job is to remove ambiguity, add validation, and avoid
creating a second editor-specific API layer unless it clearly reduces client
complexity.

## Completion Report

- Completed: 2026-05-08
- Summary: Added the Gateway-owned AbstractFlow editor contract in discovery and a first-class bundled flow input-schema endpoint.
- Files/symbols touched: `src/abstractgateway/routes/gateway.py` (`_entrypoint_input_schema_from_visualflow`, `_json_schema_for_visualflow_pin_type`, `_resolve_bundle_flow_json`, `get_bundle_flow_input_schema`, `_build_client_capability_contracts`), `tests/test_abstractflow_editor_gateway_contract.py`, `tests/test_capabilities_endpoint_contract.py`, `docs/api.md`.
- Behavior changes: `GET /api/gateway/bundles/{bundle_id}/flows/{flow_id}/input_schema` now returns `version=1`, entrypoint pins, defaults, and a compact JSON Schema object for Run Flow UI input collection.
- Tests: Added a realistic editor-path TestClient example that creates a draft VisualFlow, updates/fetches/lists it, publishes it as a bundle, verifies discovery/schema, starts a run, observes ledger/history/input/artifacts, and deletes the draft. Ran `PYTHONPATH=src pytest -q tests/test_capabilities_endpoint_contract.py tests/test_abstractflow_editor_gateway_contract.py tests/test_gateway_bundle_llm_tools_agents.py::test_gateway_bundle_metadata_endpoints_expose_entrypoint_inputs`; ran `PYTHONPATH=src pytest -q -m basic`.
- Docs: Added an AbstractFlow gateway-first editor contract section to `docs/api.md`.
- Residual risks: The input-schema route derives required/default status from current VisualFlow pin conventions; richer validation constraints would need upstream VisualFlow schema metadata if the editor wants more than pin ids/types/defaults.
- Follow-ups: No Runtime/Core changes were required. Future frontend work can consume the new discovery/schema fields, but that belongs in AbstractFlow rather than this Gateway task.
- Priority impact: 020 is complete; next planned Gateway work is 030 session prompt-cache lifecycle.
