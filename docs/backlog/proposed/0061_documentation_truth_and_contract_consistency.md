# Proposed: Documentation truth and contract consistency

## Metadata
- Created: 2026-07-05
- Status: Proposed
- Completed: N/A
- Roadmap phase: 0 (truth and footguns)
- Effort: S

## ADR status
- Governing ADRs: None.

## Context

The product pitch is "stable contracts clients can trust." That pitch cannot
survive documentation that contradicts the code or itself.

## Current code reality

Verified mismatches:

- README (~line 45) advertises "OpenAPI docs (Swagger UI): `/docs`", but
  `src/abstractgateway/app.py:35` sets `docs_url=None` and `redoc_url=None`;
  Gateway serves a custom offline HTML viewer instead. Not Swagger UI.
- README (~lines 233-234) claims the catalog envelope
  `catalog.contract = gateway_catalog_v1` is stable, while `docs/faq.md`
  (~lines 292-311) still answers "Are catalog responses fully normalized by
  Gateway? Not yet" and lists the canonical envelope as not yet versioned. The
  two docs disagree about the flagship contract.
- `docs/configuration.md` presents itself as the env reference but documents
  roughly 42 of ~136 `ABSTRACTGATEWAY_*` variables found in source (see 0069).
- The `0050` completed ledger states the abstractcore boundary is clean; it has
  regressed (see 0059).

## Problem or opportunity

Doc rot directly undermines the differentiator and misleads client authors and
operators. These are the cheapest possible trust repairs.

## What we might want to do

1. Fix the Swagger claim (either re-enable a docs UI or describe the offline
   viewer accurately).
2. Reconcile README and FAQ on the catalog envelope: state one truth about what
   is versioned/stable vs still-normalized-by-clients.
3. Correct the `0050` ledger entry (cross-reference 0059).
4. Add a lightweight docs-vs-code check (see 0069 for the generated config
   reference) so these claims are checkable.

## Dependency boundary

Gateway-only (docs + a small check).

## Why

A careful evaluator finds these contradictions in minutes; they cost the
credibility the whole "trustworthy contracts" positioning depends on.

## Promotion criteria

Promote now.

## Validation to require on promotion

- Every changed claim verified against code; a CI check (even grep-based) guards
  the Swagger/docs-url claim and the catalog-envelope wording.
