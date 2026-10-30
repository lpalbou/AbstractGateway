# 1000-abstractgateway: [IMPROVEMENT] Render discovered catalogue assets as cards

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Release: unreleased (local deployment verified)
> Priority: P1
> Labels: catalog, ux, seat-framework
> Work-item id: abstractgateway-1000
> Owner: framework
> Thread: unavailable (Agora connection refused)
> Hub refs: none

## Current code reality and scope

Consume Core 0875 discovered rows as regular searchable cards, with clear adapter/component/unclassified labels, evidence-based capability badges and honest cache status. Downloaded includes actual local files while default text selection requires a verified standalone text checkpoint. Keep delete confirmation, exact artifact IDs and other safety controls. The old Not in the catalog fallback remains only for genuinely older/broken servers; do not hide unknown inventory. Verify no current inventory is left in that section.

## Validation

Offline real-cache fixtures for full/partial models, LoRA, ControlNet, encoders,
unknown files and duplicate aliases; actual local inventory coverage and timing;
light/dark/mobile browser checks for typed cards, filtering and safe operations.
Scoped commits, apply only task patches to the real Core deployment checkout,
authorized graceful restart, live verification and durable evidence. No new
package publication or service persistence. Existing Core/UI ownership applies.

- [x] Implement and verify the catalogue correction.
- [x] Commit, deploy locally and record completion.

## Completion report

Gateway `9021af01` renders Core 0875 discovered assets as native searchable cards
with typed badges and shared structured explanations. Actual cached bytes and
partial states replace misleading fit/default controls. Downloaded includes
local files; standalone complete text checkpoints retain default selection.
Adapters, components, unclassified and partial caches cannot be chat defaults.
Shared chrome, theme widgets and capability positions are preserved.

35 Gateway unit checks and 240 browser assertions passed; 161 Core checks passed
in source and the actual deployment checkout. Core `17662521` deployed as
`8f2fd00b`; healthy authorized graceful Gateway restart verified. All 162 actual
inventory entries are covered by the catalogue, including 122 discovered rows.
No live uncatalogued fallback section remains. Live dark/light/390px/320px/touch
checks pass without page errors, overflow or model-management writes.
[Review and captures](../../../design/catalog-assets-1000/REVIEW.md),
[validation](../../../design/catalog-assets-1000/validation.json).

26 cached assets remain explicitly unclassified from local evidence; no inferred
capabilities or fake memory fits. Older or broken inventory APIs retain the
legacy fallback and explicit errors. No new ADR impact, publication, persistence
or residual required work; unrelated agent changes preserved. Agora unavailable;
local backlog receipts carry the evidence.
