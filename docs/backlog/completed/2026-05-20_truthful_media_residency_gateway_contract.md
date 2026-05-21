# Truthful Media Residency Gateway Contract

## Metadata
- Created: 2026-05-20
- Status: Completed
- Completed: 2026-05-21

## Problem

Gateway currently exposes model-residency routes, but media warmup is still easy to misread because:

- Gateway route exists
- request returns HTTP 200 with `ok:false`
- Flow workflow step can still be marked completed when `required=false`

This makes “route available” easy to misread as “media residency is actually usable here”.

Runtime now exposes a public host facade for model-residency control.
Runtime also exposes a public durable run facade for run-scoped image, TTS, and STT work.

That means Gateway no longer needs two route-level execution branches for this surface. Gateway should delegate to
Runtime's host facade and let Runtime decide whether the underlying mode is:

- remote Core-backed and meaningfully resident
- local and unsupported for media residency

## Goals

- Keep one endpoint family: `/api/gateway/models/loaded|load|unload`
- Keep one Gateway delegation path: Runtime host facade
- Make media support truthfully conditional on a long-lived Core server
- Normalize Runtime-facade responses enough for Flow/operator UI to reason correctly

## Proposed Direction

### 1. Keep the existing routes

Do not add a second warmup API.

Keep:

- `GET /api/gateway/models/loaded`
- `POST /api/gateway/models/load`
- `POST /api/gateway/models/unload`

### 2. Tighten the contract semantics

Gateway should call Runtime's public host facade for all three residency routes. Route code should not proxy
`/acore/models/*` directly.

When no Core server is configured:

- `supports.text_generation = true`
- `supports.image_generation = false`
- `supports.tts = false`
- `supports.stt = false`
- `available = false`
- `source = "runtime_client_when_available"`

and include an explicit config hint that media residency requires a long-lived AbstractCore server.

### 3. Normalize unsupported runtime-client responses

For the Runtime-facade path, normalize unsupported media replies so they always include:

- `ok`
- `supported`
- `operation`
- `code`
- `error`
- `source`

without inventing success.

### 4. Avoid route-level ambiguity

Operator routes should remain usable for capability discovery even when unsupported, but unsupported media requests must be
immediately recognizable as unsupported control-plane operations, not latent “warmup succeeded” responses.

## Acceptance Criteria

- Gateway contracts clearly advertise that media residency requires a configured Core server.
- Calling `/api/gateway/models/load` for `task=image_generation|tts|stt` without a Core server returns a stable explicit
  unsupported payload.
- Gateway residency route handlers do not directly proxy to Core; they delegate through Runtime's public host facade.
- Any related run-scoped media routes use Runtime's durable run facade rather than route-local execution, so warmup and
  execution truth stay aligned.
- Flow and other clients can decide UI state directly from the Gateway contract/response without guessing.

## Test Plan

- Contract tests for `supports.image_generation|tts|stt == false` without Core and `true` with Core.
- Endpoint tests proving the no-Core image load route returns `ok:false`, `supported:false`,
  `code=model_residency_unsupported`.
- Route tests prove Gateway uses Runtime's public host facade rather than direct Core proxy helpers.

## Completion Report - 2026-05-21

The residency contract was tightened so Gateway now reports route existence and
real availability separately for media warmup paths, and the residency routes
delegate through Runtime's public host facade rather than direct Core proxies.

The main user-visible outcome is clearer unsupported behavior: a client can now
distinguish "the route exists" from "this deployment can actually keep media
models warm in a long-lived Core process".

Validation performed during the follow-through pass:

- `PYTHONPATH=src:../abstractruntime/src:../abstractcore pytest -q tests/test_gateway_model_residency_endpoints.py tests/test_capabilities_endpoint_contract.py tests/test_gateway_capability_catalog_proxy.py`

Residual notes:

- this item is complete for the residency/media truthfulness surface
- remaining Gateway/Core cleanup is narrower and now tracked as
  `../planned/0050_gateway_runtime_boundary_cleanup_for_workspace_comms_and_telegram.md`
