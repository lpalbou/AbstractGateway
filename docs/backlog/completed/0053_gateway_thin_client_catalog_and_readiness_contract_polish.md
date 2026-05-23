# Completed: Gateway thin-client catalog and readiness contract polish

## Metadata
- Created: 2026-05-22
- Status: Completed
- Completed: 2026-05-22

## ADR status
- Governing ADRs: None
- ADR impact: None; this is additive Gateway contract work, not a new cross-package policy.

## Context

Gateway already exposed the route family higher apps needed, and `0054` closed
the catalog-shape half by adding `gateway_catalog_v1`.

The remaining question was whether Gateway could also expose a useful readiness
summary without lying about lower-layer state.

The answer was yes, but only at a narrower scope:

- Gateway can summarize surface readiness from its own versioned endpoint
  descriptors and capability contracts.
- Gateway still cannot truthfully invent selected backend/provider/model or
  stable degraded reasons when Runtime/Core do not expose them directly.

That meant `0053` could be completed as a Gateway-owned surface-readiness
contract, while the deeper lower-layer truth remained a separate follow-up.

## What changed

Implemented on 2026-05-22:

1. Added `common.readiness` to the versioned thin-client contract.
2. Scoped it explicitly as a Gateway surface summary:
   - `contract = gateway_surface_readiness_v1`
   - `version = 1`
   - `truth_scope = gateway_contract_surface`
3. Populated it only from already-advertised contract truth:
   - run, ledger, artifact, attachment, workspace, discovery, and VisualFlow
     route wiring
   - prompt-cache surface availability
   - memory store route/backend/query capability state
   - model-residency task support exported by Runtime
   - direct media and host-capture route/configuration booleans already present
     in the shared contract
4. Recorded the remaining deeper provider/backend truth gap as a new proposed
   follow-up (`0055`).

## Resulting contract behavior

- Higher apps and operator UIs can read one compact readiness summary without
  re-deriving it from many separate descriptors.
- The summary is honest about its limits. It does not claim:
  - selected backend/provider/model truth
  - verified degraded reasons beyond existing per-surface hints
  - deeper Runtime/Core health that Gateway cannot prove itself

## Validation

- `python -m pytest -q tests/test_capabilities_endpoint_contract.py tests/test_gateway_capability_catalog_proxy.py tests/test_gateway_discovery_endpoints.py`

Result:
- focused verification passed: `25 passed`

## Completion report

Files/symbols touched:
- `src/abstractgateway/routes/gateway.py`
  - `_gateway_readiness_descriptor()`
  - `_build_client_capability_contracts()`
  - `_gateway_catalog_contract_descriptor()` alignment with Flow's
    `primary_items_field`
- `tests/test_capabilities_endpoint_contract.py`
- `README.md`
- `docs/api.md`
- `docs/architecture.md`
- `llms.txt`

Behavior changes:
- Gateway now exposes a compact, explicit surface-readiness summary for thin
  clients.
- The catalog contract now advertises `primary_items_field = items`, matching
  Flow's client-side contract typing.

Residual follow-up:
- Deep provider/backend/model readiness truth is still a lower-layer contract
  problem and remains open in proposed item `0055`.
