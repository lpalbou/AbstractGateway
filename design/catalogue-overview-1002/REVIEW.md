# Catalogue overview and local browsing — Gateway 1002

Completed 2026-10-30, locally deployed; unreleased.

The catalogue previously requested local facts plus every upstream quantization,
then searched Hugging Face as a local query was typed. Loading more versions
appeared above usable cards. Meanwhile, three-build previews produced variable
card heights and large grid gaps; the fixed capability strip and recommendation
badge squeezed model names to ellipses. Operator feedback explicitly rejected
that interaction and layout.

## Result

The catalogue now makes one local catalogue read alongside optional local
inventory discovery. Local search, Downloaded, precision/asset filters and
expansion do not request the Hub. Refresh keeps existing results in place and
reports progress on its control. Cold loading still has a real accessible
spinner; failure stops it and supports retry. Explicit Hugging Face mode retains
online search, paging and every upstream quantization. Switching source preserves
the local query/filters, including an immediate switch before typing debounce.

Catalogue All now means all catalogue-known builds, not eager upstream discovery.
This is an intentional operator correction to the previous enrichment policy,
not a synthetic data cap. Every matching known build remains accessible.

Two-column overview cards show one primary build. A counted header chevron
expands every matching build without fetching more data. An active job, current
default or installed build takes precedence over proposing another download.
Order stays stable while the expanded card is being inspected. Names have their
own readable row, with fixed capability/parameter/disclosure positions below.
The Recommended badge remains clear; names are allowed to wrap. Phone layouts
keep 24px capability targets and 44px disclosure targets.

Download/default/delete operations, exact artifact identifiers, dry-run delete
confirmation and advanced adapter filters remain. A deletion that removes the
visible build/card leaves a success message. Shared theme/chrome are retained.

## Evidence

- Implementation: Gateway `5c766ce3`. Core and its deployment checkout unchanged.
- 51 focused tests passed: filtering/counts/links/disclosure, primary selection,
  expanded ordering, deletion/refusals, loaded badges, theme synchronization and
  catalogue-read concurrency/cache behaviour.
- [262 Chromium assertions](browser-validation.json) passed in both themes:
  delayed/failed loads and retry, local-only catalogue requests, explicit online
  search/all precisions, source restoration, full names, keyboard focus,
  confirmation and deliberate mock deletion, provider wizard and narrow layouts.
- [Live verification](validation.json): one initial catalogue request and one
  explicitly requested refresh; all query strings empty. Local search, empty
  recovery, Downloaded and disclosure add no requests. No Hub-enrichment request,
  page error or live model-management write.
- Latest live first-card times: 1.561s (dark), 2.040s (light), including console
  navigation/startup. These are local observations, not a latency guarantee.
- 88 default-filter cards and 71 downloaded cards; their first six collapsed
  heights are all 141px. Fixed capability columns and readable names pass.
- 390px/320px/light/dark, actual 320px touch, keyboard disclosure and advanced
  adapter access pass. [Restart receipt](deployment.json): healthy and idle before
  authorized graceful restart, healthy after verification.

## Captures

[Catalogue, dark](catalogue-dark.png), [catalogue, light](catalogue-light.png),
[Downloaded overview](downloaded-dark.png),
[full recommended model name](recommended-light.png),
[phone](phone-light.png), [touch](touch-dark.png), [empty/recovery](empty-dark.png),
[initial waiting](loading-dark.png), [explicit expansion](expanded-dark.png).

[UX review](UX_REVIEW.md) records the visible walkthrough and review limits.
Earlier [1001 screenshots](../model-types-1001/downloaded-models-dark.png) preserve
the prior variable-height layout rather than overwriting the history.

## Follow-up triage

No required implementation work remains. Online discovery remains network-bound
when explicitly requested. Expanded cards intentionally grow to show comparisons;
normal browsing remains the compact overview. No Core semantics, model files,
configured defaults, package releases or persistent services were changed.
No new ADR boundary; existing Core/Runtime ownership is retained. Agora was
unavailable (connection refused), so receipts are recorded locally.
