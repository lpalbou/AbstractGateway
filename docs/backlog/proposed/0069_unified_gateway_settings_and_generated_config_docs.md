# Proposed: Unified Gateway settings object and generated config docs

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 2 (coherence)
- Effort: S-M

## ADR status
- Governing ADRs: None.

## Context

Configuration has accreted into a large, partly undocumented set of environment
variables read from many code paths.

## Current code reality (verified)

- ~136 unique `ABSTRACTGATEWAY_*` env vars appear in source; only ~77 appear
  anywhere in `docs/`, and `docs/configuration.md` (the designated reference)
  documents ~42. Add ~28 `ABSTRACT_TELEGRAM_*`, ~27 `ABSTRACT_EMAIL_*`, plus
  `ABSTRACT_BACKLOG_*` and legacy fallbacks (`ABSTRACTFLOW_*`,
  `ABSTRACTFRAMEWORK_*`).
- `_env` / `_as_bool` / `_as_int` helpers are re-implemented in ~13 modules
  (e.g. `config.py:18`, `users.py:26`, `security/gateway_security.py:53`,
  `integrations/telegram_bridge.py:46`, `integrations/email_bridge.py:25`,
  `hosts/bundle_host.py:434`).
- Policy is re-read from `os.getenv` at call sites (e.g.
  `security/gateway_security.py` ~112-160), so the same setting can behave
  differently by code path.

## Problem or opportunity

There is no single source of configuration truth; operators cannot discover the
knobs, and behavior can drift between code paths for the same variable.

## What we might want to do

1. Introduce one typed `GatewaySettings` loaded at the composition root; all env
   access goes through it. Legacy names map with a deprecation warning.
2. Delete the ~13 duplicate `_env/_as_bool/_as_int` helpers in favor of the
   settings object.
3. Generate `docs/configuration.md` (or an appendix) from the settings schema so
   documentation coverage is complete and cannot rot (ties to 0061).

## Dependency boundary

Gateway-only.

## Why

Gives operators a discoverable, consistent configuration surface and removes
per-code-path behavior drift.

## Promotion criteria

Promote after 0066 (easier once modules are smaller), or independently.

## Validation to require on promotion

- Every env var resolves through `GatewaySettings`; a test asserts the generated
  config reference lists every setting the settings object defines.
