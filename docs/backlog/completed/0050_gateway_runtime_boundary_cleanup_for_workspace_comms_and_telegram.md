# Completed: Gateway Runtime boundary cleanup for workspace, comms, and Telegram

## Metadata
- Created: 2026-05-21
- Status: Completed
- Completed: 2026-05-21

## ADR status
- Governing ADRs: None
- ADR impact: None; execution follow-through on the existing Gateway -> Runtime -> Core boundary direction.

## Context

Gateway had already moved its main LLM/media/control-plane path behind
`AbstractRuntime>=0.4.16`, but a small set of operator/tooling paths still
imported `abstractcore` directly:

- workspace/file helpers in `routes/gateway.py`
- email helper routes in `routes/gateway.py`
- Telegram bootstrap in `cli.py`
- TDLib lifecycle access in `integrations/telegram_bridge.py`
- maintenance notifications in `maintenance/notifier.py`

That left the package boundary inconsistent: the main execution path was
Runtime-owned, while some host-local helpers still reached into Core directly.

## What changed

Implemented on 2026-05-21:

1. Gateway now owns workspace/file helpers locally in
   `src/abstractgateway/workspace_tools.py`.
   - `.abstractignore` loading and matching
   - bounded `read_file(...)`
   - bounded `skim_files(...)`
2. `src/abstractgateway/routes/gateway.py` now uses:
   - Gateway-local workspace helpers for `/files/read`, `/files/skim`,
     attachment-ingest ignore checks, and search indexing
   - Runtime's comms facade for `/api/gateway/email/*`
3. `src/abstractgateway/cli.py` now uses Runtime's
   `bootstrap_telegram_auth_from_env(...)` for `abstractgateway telegram-auth`.
4. `src/abstractgateway/integrations/telegram_bridge.py` now uses Runtime's
   public Telegram wrappers for TDLib lifecycle access.
5. `src/abstractgateway/maintenance/notifier.py` now:
   - uses Runtime's Telegram send helper
   - uses Runtime's comms facade for email sends
   - aligns email notification sending with account-based email config

## Resulting boundary

- Gateway owns workspace/file policy and host HTTP behavior locally.
- Runtime owns the public import boundary for email and Telegram helpers.
- Core remains an implementation backend behind Runtime.

Gateway source no longer imports `abstractcore` directly.

## Residual follow-up

Runtime needed one small addition to close the boundary cleanly: a module-level
email/comms facade parallel to `send_telegram_message(...)`. Gateway now uses
that public Runtime surface for email routes and notifications, while the
Runtime host facade still remains available for prompt-cache/model-residency
controls and host-bound email convenience methods.

## Validation

- `rg -n "from abstractcore|import abstractcore" src/abstractgateway`
- `PYTHONPATH=src:../abstractruntime/src:../abstractcore pytest -q tests/test_gateway_email_inbox_endpoints.py tests/test_gateway_cli_split_runner.py tests/test_gateway_telegram_bridge_unit.py tests/test_gateway_discovery_endpoints.py tests/test_gateway_files_skim_endpoint.py tests/test_gateway_attachments_ingest.py tests/test_gateway_workspace_policy_enforcement.py tests/test_maintenance_notifier_unit.py`

Result:
- import grep returned no matches under `src/abstractgateway`
- focused verification passed: `38 passed`

## Completion report

This cleanup finished the narrow boundary work without widening into a larger
tool-type migration.

Notable design decisions:

- kept workspace/file helpers Gateway-owned instead of trying to route them
  through Runtime
- added and used a Runtime comms facade for nondurable email routes
- used Runtime Telegram wrappers for CLI bootstrap, bridge lifecycle, and
  notifier sends
- did not use the Runtime run facade for these host-local paths because that
  would create child-run history for operator actions
