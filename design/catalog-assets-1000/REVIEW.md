# Cached assets in the catalogue — Core 0875 / Gateway 1000

Completed 2026-10-30; local deployment, unreleased.

The remaining Hugging Face cache inventory now appears as regular catalogue
cards. Core emits discovered entries with exact provider/reference identity,
local metadata provenance and explicit model/adapter/component/unknown kind.
Registry-backed curated models retain their existing identities; shared HF
repositories are deduplicated across engine names. Classification makes no
new network calls and does not fabricate registry capabilities.

Adapters require their base model; components require a pipeline. Unclassified
files stay explicitly unclassified. Cache bytes are actual stored bytes, not a
standalone fit estimate. Pending downloads, missing declared weight shards,
invalid shard indexes and metadata-only caches cannot be chat defaults.
Existing complete text checkpoints retain their default action. Incomplete and
supporting assets retain inspection and two-step deletion. Downloaded includes
cached files; the mixed summary counts assets without version counts. Shared
AbstractUIC structured tooltips, capability columns, chrome and themes remain.

## Evidence

- Core `17662521`, applied as `8f2fd00b` to the actual deployment worktree.
  Gateway `9021af01`; scoped commits preserve other agents’ work.
- 161 Core checks passed in source and in the actual deployment checkout.
  35 Gateway unit checks and 240 browser assertions passed. The browser gate
  exercises both themes, keyboard filtering, typed cache states, safe model
  controls, shared provider wizard and narrow viewports.
- [Local catalogue coverage](coverage.json): 162 inventory entries accounted
  for, zero unmatched. Base catalogue builds in 0.855 seconds: 224 rows,
  including 122 discovered assets (40 models, 44 adapters, 12 components,
  26 unclassified). Local completeness checks flag 26 incomplete caches.
- [Live validation](validation.json): 162 entries remain covered in the enriched
  catalogue (224 rows / 1825 builds at verification). Zero fallback sections,
  page errors or model-management writes. Desktop, light/dark, 390px, 320px
  and 320px touch pass. Supporting assets have no chat/default/download/Fits
  claims; structured adapter tooltips and partial cache controls pass.
- [Deployment](deployment.json): Gateway was healthy with zero model calls and
  runner ticks before its authorized graceful restart; healthy afterward.
- [Mock browser checks](browser-validation.json) also cover download/default
  writes and deletion dry-run confirmation without touching live models.

## Visual check

[Dark desktop](desktop-dark.png), [light desktop](desktop-light.png),
[adapter detail, dark](adapter-dark.png), [adapter detail, light](adapter-light.png),
[phone, light](phone-light.png), [touch phone, dark](phone-touch-dark.png).
The formerly uncatalogued assets use the same compact two-column cards and
fixed capability positions. Kind labels and explanatory details are theme-aware.

## Limits and follow-up triage

Local evidence cannot determine every asset’s role or runnable pipeline memory;
26 remain explicitly unclassified rather than being guessed from their names.
A cache passing the file/index probes is not a generation test. No weights were
loaded or downloaded for verification, no live model files/defaults were changed,
and no package publication or machine persistence was installed. The legacy
fallback remains for older or failed inventory APIs. No new ADR is required:
this extends the existing Core catalogue owner and Gateway rendering boundary.
The earlier 0873/0874 registry work is preserved; this closes its remaining cache
inventory gap. Agora was unavailable (connection refused), recorded locally.
