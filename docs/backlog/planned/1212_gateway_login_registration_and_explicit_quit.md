# 1212: Reliable Gateway login registration and explicit Quit

> Package: AbstractGateway, AbstractFramework

## Metadata
- Created: 2026-11-14
- Status: Planned — in progress
- Completed: N/A
- Source: operator release recovery, root backlog 1209

## ADR status
- Governing ADRs: framework ADR 0050/0051 release gates; existing package contracts.
- ADR impact: implementation correction within existing contracts.

## Current code reality

The Windows HKCU Run command includes repeated long interpreter/data/log paths and can exceed its 260 UTF-16-unit limit while status says on. The root local supervisor restarts a critical Gateway even after its clean tray Quit.

## Scope and expected outcome

Use an instance-specific compact Windows bootstrap and JSON arguments, validate command length and readback shape/instance, return actionable enable errors. Root Gateway selects on-failure; clean Quit stays stopped and startup Quit is a clean cancellation. Preserve crash/watchdog recovery and next-login preference.

Deferred governance, spoken-language, Core reasoning/usage, composer redesign and breaking TUI migration remain outside this item.

## Validation

72 isolated startup tests, real-child supervisor exit tests, independent startup review. Native Windows login and actual macOS tray execution remain unverified; no machine persistence installed.

## Release acceptance

Code, tests and coredoc reach remote main; all exact-commit gates green; changed artifacts published in dependency order. Root/site publication remains held for operator GO.
