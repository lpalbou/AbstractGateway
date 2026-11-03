# Model count and inline fit status — 0997

Implemented `55763bab`, deployed locally 2026-10-30.

The toolbar reports filtered/total models only, retaining provisional loading
truth. Version totals are removed from this summary. Every version remains
available through the card header disclosure.

The fixed 42px status column caused downloaded folders and fit labels to wrap.
The grid now sizes that column to its content; its flex group does not wrap and
icons do not shrink. This also accommodates resident-state icons and longer fit
labels. Existing narrow-card layout and card spacing are preserved.

35 tests and 212 browser assertions passed in the focused catalog, browser,
merged-inventory and residency gates. Checks cover model-only text, unchanged
filter/inventory truth and version disclosure, plus actual icon/text alignment
on desktop and at 320px in dark/light. The test harness still checks underlying
artifact totals separately; its Hugging Face snapshot was corrected to use the
current view's source rather than the curated source.

Graceful restart used the existing host operation after model requests and
runner ticks were idle. Live checks in both themes passed: 82 of 96 models in
Browse, 19 of 96 models in Downloaded, with the latter matching the 19 catalog
model cards. The separate non-catalog inventory section is not a model card and
was excluded from the corrected live verifier selector. All 23 visible local
catalog build status groups keep their icons and Fits/Tight text on one line at
both widths. No horizontal overflow, page errors or management writes. Health
passed after restart.

Single-agent visual inspection confirms readable desktop and phone layout.
No shared component fork, ADR change, registry publication or residual work.
Agora was unavailable (connection refused); local backlog and these receipts
record completion. Unrelated changes were excluded from scoped commits.

- [Dark downloaded models](downloaded-dark.png)
- [Light downloaded models](downloaded-light.png)
- [320px phone](downloaded-mobile-light.png)
- [Validation](validation.json)
