# Full models by default; adapter metadata for advanced users

Completed 2026-10-30, local deployment; unreleased.

The operator correction to 0875/1000 is that file inventory and normal model
browsing have different scopes. Core still accounts for every local asset.
Gateway defaults to full model entries in both browsing and Downloaded. Advanced
Filters → Asset type explicitly selects adapters, components, unclassified files
or all assets. Counts are scoped to that choice; supporting files cannot leak
back through legacy inventory fallback rows. Existing three-build previews,
capability positions, icon operations and shared chrome remain.

Switching to a supporting asset type clears irrelevant fit/precision/capability
constraints and records the choice in the URL. Switching back restores the model
filters. A reloaded adapter deep link retains the Downloaded scope and restores
all downloaded precisions on return to Models. Filter reset returns to full
models with the established 4-bit and Fits defaults.

## Adapter knowledge boundary

AbstractVision already owns
`src/abstractvision/assets/vision_adapter_capabilities.json` and
`adapter_capabilities.py`: its exact base/weight compatibility and inference
profiles remain there, unchanged. Core now exposes a separate `adapter` metadata
object from local PEFT config and model-card declarations, governed by
`abstractcore/assets/adapter_metadata.schema.json`. This records type, base
checkpoints, task, trigger words, target modules, rank and metadata provenance.
It does not claim engine compatibility or infer purpose from repository names.

Declared base and task appear in shared structured adapter tooltips; trigger
words appear when reported. Missing metadata stays unknown. A fine-tuned full
checkpoint does not become an adapter merely because its name includes LoRA or
its card names a base. Explicit adapter packages in Hub results are also marked
as adapters, including from previously cached Hub file lists.

## Validation and deployment

- Core `0062fa69`, selectively deployed as `449aefce`; Gateway `72079bc2` and
  `5abd3123`. Existing unrelated work and Vision ownership preserved.
- 168 Core checks passed in source and actual deployment checkout: local cache
  classification, schema, metadata provenance, real checkpoint probes, Hub
  adapters, identities, materializer/deletion and package assets.
- 38 Gateway unit checks passed; 248 Chromium assertions cover keyboard use,
  default/Downloaded scope, advanced types, mixed inventory, type-specific
  explanations, shared wizard/providers and theme/mobile layouts.
- [Live checks](validation.json): dark/light, default and Downloaded are full
  models only. All 44 adapters are explicitly reachable, with no false chat
  defaults or fit claims. 12 components and 26 unclassified entries are reachable
  through their facets. All-assets inspection retains current inventory of 162
  entries without fallback rows. URL reload, 390px/320px layout and actual 320px
  touch selection pass. No page errors or model-management writes.
- [Metadata coverage](metadata-coverage.json): 44 adapter descriptors, 41 with
  declared bases; local cards declare no trigger words, so none are invented.
  The 224-row base catalogue builds in 1.069 seconds, with 142 full-model rows.
- [Deployment receipt](deployment.json): healthy, zero model calls/runner ticks
  before the authorized graceful restarts; healthy after final verification.

## Visual evidence

[Models, dark](models-dark.png), [models, light](models-light.png),
[Downloaded models](downloaded-models-dark.png),
[advanced adapter metadata](adapter-details-light.png),
[phone models](models-phone-light.png), [touch adapters](adapters-touch-dark.png).

## Follow-up triage

No required work remains. Metadata declarations are not generation/compatibility
verification; no weights were loaded or downloaded, no live defaults/files were
changed, and no package was published. The existing Vision inference registry
was discovered and preserved rather than duplicated into Core. New ADR impact:
none, existing catalogue and plugin ownership boundaries remain. Agora was
unavailable (connection refused); local backlog and evidence carry the receipts.
