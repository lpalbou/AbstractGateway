# Models library 0987 — delivery evidence

Implemented in `e0da0fdc`, locally deployed on 2026-10-30.

Execution mode: single-agent implementation and artifact review. No independent
review is claimed. The in-app browser connector was unavailable; Chromium was
driven through the local Playwright harness.

## Visible result

- [Live catalog](library-live.png): real host catalog, two-column grid, collapsed
  advanced filters, standard download/info icons, concise publisher/precision
  identity, five visible versions and access to every remaining version.
- [Downloaded library](downloaded-fixture.png): controlled fixture showing the
  current text-default star, default/delete/info controls and all precisions.
- [320px light theme](mobile-fixture.png): controlled fixture showing stacked
  identities and accessible actions without horizontal page overflow.

The screenshots were visually inspected. Search, library switching, expanded
filters, keyboard disclosure/details/copy, exact default-text route selection,
progress, retry, cancel/keep and delete dry-run/keep were exercised by browser
checks. Read-only users retain details but cannot mutate models. Shared themes
and reserved header widgets remain in place. Destructive confirmations retain
explicit text to make the decision clear.

## Validation and deployment

The targeted regression gate passed 81 tests. The final focused run passed 43
tests after the narrow-screen and scrolled-filter adjustments. Browser results:
100 catalog/wizard checks and 122 Resources checks, zero failures. See
[validation.json](validation.json) for machine-readable receipts.

The existing gateway was gracefully restarted with zero in-flight operations.
Live verification found healthy status, 83 matching catalog models, the 4-bit
default, collapsed filters, SVG-only action buttons, working build details,
Downloaded spanning all precisions, and 37 default-text actions. No browser
exceptions or management writes occurred during live verification. Management
writes were exercised only against fixtures; no live models were downloaded,
deleted or made default for testing.

Verdict: delivered. Full catalog fetch latency remains separately tracked in
[0986](../../docs/backlog/proposed/0986_catalog_fetch_latency.md). Other modality
routing contracts are unchanged; the star explicitly selects the text default.
