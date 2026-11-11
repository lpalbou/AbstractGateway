# 1168 — The Entity monitor's calibration reads should not be ledgered as the operator reading the book

> Package: AbstractGateway (diary read route: the ledger event), AbstractEntity (the monitor's automatic calibration read at page load)
> Type: enhancement
> Status: proposed (likely moot: Entity 0.12.0's F4 fix, 2026-11-11, makes the monitor's automatic read stop using the diary door; verify after the tag, then deprecate)
> Created: 2026-11-11
> Priority: P3
> Labels: entities, ledger, honesty

## Summary

Found 2026-11-11 by the Entity session: the Entity app's cognitive monitor reads the diary automatically at page load for
its calibration, and each read lands in the entity's ledger as "The book was read ×N · operator review (cognition
calibration)", once per browser. The ledger should record the person's deliberate reads, not a page's automatic ones.

## Scope

A request marker (header or query, declared in the API docs) for a calibration read that the gateway serves without
the ledger event; the monitor sends it; a deliberate read in the Book stays ledgered.

## Validation

A page load records nothing; opening an entry in the Book records one read.
