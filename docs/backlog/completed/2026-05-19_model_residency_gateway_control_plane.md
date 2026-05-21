# Proposed: Gateway Model Residency Control Plane

## Metadata
- Created: 2026-05-19
- Status: Completed
- Completed: 2026-05-21

## Context

AbstractCore now owns the canonical model residency API:

- `GET /acore/models/loaded`
- `POST /acore/models/load`
- `POST /acore/models/unload`

Runtime now exposes a public host facade for model-residency and prompt-cache control operations.
Runtime also exposes a public durable run facade for run-scoped image, TTS, and STT work.

Gateway already exposes thin-client contracts, capability discovery, generated media helpers, and
prompt-cache controls. It still needs to expose model residency to Flow and other clients without
creating another Core integration layer in Gateway itself.

## Problem

Thin clients need to see and control currently resident models without reaching around Gateway.
Workflows also need a stable path for prewarming and unloading models when it is explicitly useful.

The implementation must not create a second residency model in Gateway. Core is the source of truth.
Runtime integration should be the only component that translates Gateway calls into Core calls.

## Proposed Gateway endpoints

Add Gateway routes that delegate to Runtime's public host facade:

- `GET /api/gateway/models/loaded`
- `POST /api/gateway/models/load`
- `POST /api/gateway/models/unload`

Request/response shape should mirror Core's `/acore/models/*` surface as closely as possible, but
Gateway route code should not proxy Core directly. If a standalone Core server is configured, that
remote call should happen inside Runtime integration, not inside Gateway routes or proxy helpers.

## Capability contract

Advertise the surface in the versioned Gateway contract under `common.model_residency` using the
same descriptor style as other Gateway endpoints:

```json
{
  "route_available": true,
  "available": true,
  "source": "runtime_host_facade",
  "loaded": { "available": true, "endpoint": "/api/gateway/models/loaded" },
  "load": { "available": true, "endpoint": "/api/gateway/models/load" },
  "unload": { "available": true, "endpoint": "/api/gateway/models/unload" },
  "tasks": ["text_generation", "image_generation"],
  "supports": {
    "text_generation": false,
    "image_generation": true,
    "tts": false,
    "stt": false
  }
}
```

`available` should mean the Gateway can actually reach/use a meaningful Runtime-backed residency
path, not merely that the Gateway route exists. If no Core server base URL is configured, advertise
`route_available: true` and `available: false`, or omit the contract from client-required paths.

Do not advertise `text_generation: true` unless Gateway workflow execution is actually using the
same Core server or the same in-process runtime that was loaded.

Voice and STT can remain unsupported or informational for now. The expensive path this feature must
solve is local image model residency.

## Execution path requirement

For image residency to be real, model load and image generation must hit the same long-lived process.

Valid v0 path:

- Gateway configured with an AbstractCore server.
- Gateway calls Runtime's host facade.
- Runtime integration calls Core `/acore/models/load`.
- Generate Image execution uses the same Runtime/Core path and reaches the same long-lived process.

Invalid path:

- Preload through Core server, then generate through Runtime's one-shot local image subprocess.
- Report a model as warm when the next generation call cannot use that resident backend.

Gateway should prefer remote/hybrid Core execution for generated media when `ABSTRACTCORE_SERVER_BASE_URL`
is configured.

This requires execution-path wiring, not just routes:

- Bundle-hosted workflows must choose remote/hybrid Core runtime when Core server residency is the
  advertised warm path.
- Any retained Gateway image-generation surface must be Runtime-backed and use the same
  Runtime/Core path when Core server residency is the advertised warm path.
- Runtime remote URL joining must not duplicate `/v1`.

## Ledger constraints

Gateway must not make loaded models part of run truth.

Rules:

- Operator calls to `/api/gateway/models/*` are control-plane operations and should not append to a
  run ledger.
- Workflow-triggered residency should be represented by a Runtime `model_residency` effect, so the
  normal step start/success/failure records capture the request and result.
- Gateway routes may return current operational state, but history/replay must render the recorded
  ledger result, not the current loaded model list.
- Do not append ad hoc model-load records directly into a run ledger from Gateway routes unless the
  request is explicitly executing a Runtime effect.

## Expected Gateway files

- `abstractgateway/routes/gateway.py`
- `abstractgateway/hosts/bundle_host.py`
- `abstractruntime/integrations/abstractcore/host_facade.py`
- `abstractruntime/integrations/abstractcore/llm_client.py`
- `abstractgateway/tests/test_capabilities_endpoint_contract.py`
- Gateway route tests for `/api/gateway/models/*`

## Validation ideas

- Contract test advertises model residency endpoints only when the Gateway path is usable.
- Runtime-host-facade tests cover Core base URLs with and without `/v1`.
- Gateway tests prove route handlers delegate through Runtime's host facade instead of direct Core proxy helpers.
- Endpoint tests confirm list/load/unload request and response shapes match Core.
- Generated media test proves a warmed image model is used by the same Core server path.
- Bundle workflow test proves Generate Image under a configured Core server does not use the local
  image subprocess.
- Run-scoped media route tests prove Gateway uses Runtime's durable run facade instead of route-local
  image/TTS/STT execution.
- Ledger regression test confirms operator load/unload does not mutate any run ledger.

## Non-goals

- Do not duplicate Core's residency registry in Gateway.
- Do not add a separate warm-model API with different task names.
- Do not make model residency mandatory for Gateway startup.
- Do not hide generation failures behind fake warmup success.
- Do not add or preserve direct Gateway-to-Core proxy helpers for this surface once Runtime's host
  facade is available.

## Completion Report - 2026-05-21

Implemented in Gateway:

- `GET /api/gateway/models/loaded`
- `POST /api/gateway/models/load`
- `POST /api/gateway/models/unload`

The route layer now delegates through Runtime's public host-facade path rather
than owning a second residency integration in Gateway itself. Capability
discovery also advertises the residency contract as a Gateway surface with
truthful availability semantics.

Validation performed during the follow-through pass:

- `PYTHONPATH=src:../abstractruntime/src:../abstractcore pytest -q tests/test_gateway_model_residency_endpoints.py tests/test_capabilities_endpoint_contract.py`

Residual notes:

- The remaining Gateway/Core boundary work is now mostly outside residency and
  media execution. It is tracked separately in
  `../planned/0050_gateway_runtime_boundary_cleanup_for_workspace_comms_and_telegram.md`.
