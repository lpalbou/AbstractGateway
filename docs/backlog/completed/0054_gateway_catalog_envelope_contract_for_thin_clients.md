# Completed: Gateway catalog envelope contract for thin clients

## Metadata
- Created: 2026-05-22
- Status: Completed
- Completed: 2026-05-22

## ADR status
- Governing ADRs: None
- ADR impact: None; this is an additive API contract owned by Gateway, not a new cross-package policy.

## Context

Gateway already exposed the right discovery route family for thin clients:

- `GET /api/gateway/discovery/providers`
- `GET /api/gateway/discovery/providers/{provider_name}/models`
- `GET /api/gateway/voice/voices`
- `GET /api/gateway/audio/speech/models`
- `GET /api/gateway/audio/transcriptions/models`
- `GET /api/gateway/audio/music/providers`
- `GET /api/gateway/audio/music/models`
- `GET /api/gateway/vision/provider_models`
- `GET /api/gateway/vision/models`

The higher-app problem was not missing routes. It was payload drift. Flow,
Assistant, and related clients still had to normalize several response shapes
such as `items|models|provider_models|models_by_provider|profiles`.

Backlog item `0053` mixed two scopes:

1. a Gateway-only catalog normalization contract
2. a richer deployment/readiness story that Gateway cannot state truthfully by
   itself

The first half was ready to ship here. The second half remains proposed.

## What changed

Implemented on 2026-05-22:

1. Added a Gateway-owned additive catalog contract, `gateway_catalog_v1`.
2. Added `common.discovery.catalog_contract` to the shared thin-client
   capability descriptor.
3. Updated the catalog routes above to preserve legacy fields while also
   returning:
   - `catalog`: versioned Gateway metadata
   - `items`: one canonical primary item array for the route
4. Preserved provenance by keeping the existing response `source` and recording
   lower-layer `upstream_source` when Runtime/Core discovery already reported
   one.

## Resulting contract behavior

- Thin clients can now read one stable field, `items`, instead of guessing
  which route-local list to parse first.
- The `catalog` object tells clients what the route is returning:
  - `contract`
  - `version`
  - `kind`
  - `scope`
  - `primary_items_field`
  - `route_source`
  - optional `upstream_source`
  - optional route filters such as `provider`, `task`, or `include_models`
- Existing clients continue to work because legacy fields like `models`,
  `providers`, `provider_models`, `profiles`, and `voices` are unchanged.

## Validation

- `python -m pytest -q tests/test_gateway_capability_catalog_proxy.py tests/test_gateway_discovery_endpoints.py tests/test_capabilities_endpoint_contract.py`

Result:
- focused verification passed: `25 passed`

## Completion report

Files/symbols touched:
- `src/abstractgateway/routes/gateway.py`
  - `_gateway_catalog_contract_descriptor()`
  - `_gateway_catalog_response()`
  - catalog item normalizers and discovery route wrappers
- `tests/test_gateway_capability_catalog_proxy.py`
- `tests/test_gateway_discovery_endpoints.py`
- `tests/test_capabilities_endpoint_contract.py`

Behavior changes:
- Gateway now owns a small stable catalog envelope for thin clients.
- Gateway still does not claim a new top-level readiness/deployment truth
  surface.

Residual follow-up:
- `0053` remains open for the cross-package readiness half. That work should
  wait for Runtime/Core truth rather than being improvised in Gateway.
