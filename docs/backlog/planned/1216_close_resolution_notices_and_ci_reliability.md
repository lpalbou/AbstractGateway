# 1216: Carry close-time resolution notices and repair red CI

> Package: AbstractGateway, AbstractRuntime

## Metadata
- Created: 2026-11-14
- Status: Planned — in progress
- Completed: N/A
- Source: operator release recovery, root backlog 1209

## ADR status
- Governing ADRs: framework ADR 0050/0051 release gates; existing package contracts.
- ADR impact: implementation correction within existing contracts.

## Current code reality

close_notices only recognized memory_form elections; completed t-reflect diary metadata was omitted. Red main CI also exposed a concurrent sys.modules iteration error and summon cancellation race; an old archive fixture races boot indexing.

## Scope and expected outcome

Serve confirmed question_resolved notices from completed t-reflect metadata, in ledger order, deduplicated and private-title safe. Snapshot module dictionaries atomically in test harness; create archive fixtures before boot. Fix the cancellation publication race at its owning seam after deterministic diagnosis.

Deferred governance, spoken-language, Core reasoning/usage, composer redesign and breaking TUI migration remain outside this item.

## Validation

Two focused close-notice tests pass. Run full Gateway matrix and real scratch Gateway HTTP proof; no rerun-until-green substitute. Root recovery1209 tracks publication and main synchronization.

## Release acceptance

Code, tests and coredoc reach remote main; all exact-commit gates green; changed artifacts published in dependency order. Root/site publication remains held for operator GO.
