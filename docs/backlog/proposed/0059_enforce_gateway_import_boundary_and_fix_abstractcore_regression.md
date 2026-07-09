# Proposed: Enforce Gateway import boundary and fix the abstractcore regression

## Metadata
- Created: 2026-07-05
- Status: Proposed (partially implemented 2026-07-05 — see Implementation status)
- Completed: N/A
- Roadmap phase: 0 (truth and footguns)
- Effort: S (CI guard) + S-M (facade fix)

## Implementation status (2026-07-05)

Done and tested:
- Added `AbstractCoreConfigFacade` as `abstractruntime/.../integrations/abstractcore/config_facade.py`
  (get/set/clear capability defaults, config-file resolution, capability specs,
  provider-config API-key read), exported from the integration package.
- Routed `capability_defaults.py` and `provider_connections.py` through the
  facade; removed all 10 direct `abstractcore.config` imports.
- Added `tests/test_gateway_import_boundary.py` (AST guard: fails on any
  `abstractcore` import in Gateway source). Verified with LMStudio: a
  gateway-written scoped capability default drives a live generation end to end,
  and set/list/clear roundtrips. Affected suites pass.

Remaining (still open under this item):
- Extend the CI guard to also forbid private Runtime attribute access
  (`runner.py` `runtime._execute_effect_with_retry`; `bundle_host.py`
  `runtime._handlers` monkey-patch) and add the corresponding public Runtime
  surfaces to replace those reach-ins.
- Correct the `0050` completed-ledger entry to reference this follow-up.

## ADR status
- Governing ADRs: None yet. Promote alongside an ADR that states the layering
  rule ("Gateway imports AbstractRuntime only; never AbstractCore directly") so
  the contract is durable policy, not prose.

## Context

The package's differentiator is clean layering: Gateway is an HTTP/SSE control
plane over AbstractRuntime, and AbstractRuntime owns the AbstractCore
integration. Completed backlog `0050` (2026-05-21) declared "Gateway source no
longer imports `abstractcore` directly."

## Current code reality

The claim has regressed and nothing enforces it:

- `src/abstractgateway/capability_defaults.py` has 8 direct imports:
  `from abstractcore.config.manager import ConfigurationManager` at lines 95,
  135, 145, 161, 222, 239, 258, and
  `from abstractcore.config.capability_defaults import iter_capability_default_specs`
  at line 279.
- `src/abstractgateway/provider_connections.py` has 2 more at lines 263 and 276.
- Both files were introduced/expanded in releases 0.2.26 (2026-06-03) and 0.2.27
  (2026-06-14), i.e. *after* `0050` was completed. The completed ledger was
  never corrected.
- All 10 imports are AbstractCore *config management* (read/write capability
  defaults and provider connections), not LLM execution.
- AbstractRuntime's AbstractCore integration facades
  (`abstractruntime/.../integrations/abstractcore/host_facade.py`,
  `discovery_facade.py`, `run_facade.py`, `factory.py`) expose prompt-cache,
  residency, discovery, comms, and run surfaces, but no config/capability-default
  management surface. That absence is why the author reached back into Core.
- There is no CI check (no import-linter contract, no AST test) asserting the
  boundary, so regressions are invisible.

## Problem or opportunity

A Runtime-internal refactor of AbstractCore config handling would break Gateway
in production, and the "clean layering" story is currently false. The regression
happened precisely because a new need (capability-default persistence) had no
Runtime facade to satisfy it.

## What we might want to do

1. Add an `AbstractCoreConfigFacade` to AbstractRuntime's abstractcore
   integration (mirroring `host_facade.py`) exposing get/set/clear capability
   defaults and provider-connection persistence against the Core config schema.
2. Route `capability_defaults.py` and `provider_connections.py` through the
   Runtime facade; delete the direct `abstractcore.config` imports. Keep
   Gateway-owned config-file layout logic (scoped `abstractcore.json` paths)
   local, but the "translate to Core schema" logic moves behind the facade.
3. Add a CI-enforced import contract (import-linter or an AST-walking pytest):
   Gateway may import `abstractruntime` (+ sanctioned `abstractagent`/
   `abstractmemory`); `abstractcore` is forbidden; private `_`-prefixed Runtime
   attributes are forbidden (see also `runner.py:963,1085` reaching
   `runtime._execute_effect_with_retry`, and `bundle_host.py:356` monkey-patching
   `runtime._handlers`).
4. Correct the `0050` completed ledger entry to note the follow-up.

## Dependency boundary

Not Gateway-only: step 1 requires a new AbstractRuntime facade. Steps 3-4 are
Gateway-local and can ship immediately as a failing-then-passing guard.

## Why

Restores the load-bearing architectural claim and makes it self-defending, so it
cannot silently rot again.

## Promotion criteria

Promote now for steps 3-4 (cheap, Gateway-local). Promote step 1 when a Runtime
release adds the config facade.

## Validation to require on promotion

- CI contract test fails on any `abstractcore` import and any private Runtime
  attribute access, and passes after the facade migration.
- `PYTHONPATH=src pytest -q tests/test_capabilities_endpoint_contract.py` and the
  capability-defaults tests still pass through the facade path.
