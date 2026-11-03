# Primary model filters — 1004

Gateway `2dd60d51` / `a6529057`, locally redeployed 2026-10-30. Two primary toolbar
rows: source/search/Fits/Downloaded above, Provider and Capability selectors
below with the model count, Filters and refresh. Both selectors use the existing
themed native form controls. Advanced Filters contains only asset type and
quantization; the duplicate Status facet and its version counts are removed.
Legacy linked status values remain readable. Downloaded stays the sole quick
library control and restores previous provider/capability selections on exit.

51 focused tests and 286 Chromium assertions pass. Live dark/light checks verify
two aligned desktop rows at 1600px and 1024px, local provider/capability filtering,
URL reload and Downloaded restoration. Responsive 390px/320px and actual touch
checks pass without page overflow. One-line model headers, capability matrix,
themes and local-only catalogue reads are preserved. No page errors, model writes
or Hub requests occurred. Restart waited for an idle, healthy Gateway; the served
console and subsequent health check confirm the new version. Core unchanged.

[Dark](desktop-dark.png), [light](desktop-light.png),
[advanced](advanced-dark.png), [phone](phone-light.png), [touch](touch-dark.png),
[live checks](validation.json), [browser checks](browser-validation.json),
[restart](deployment.json).

No new ADR boundary, publication, persistent service or required follow-up.
