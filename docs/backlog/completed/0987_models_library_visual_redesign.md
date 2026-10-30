# 0987-gateway: [IMPROVEMENT] Redesign the Models page for fast visual browsing and management

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Priority: P1
> Labels: console, models, ux, seat-framework
> Work-item id: abstractgateway-0987
> Owner: framework
> Thread: unavailable (Agora connection refused; direct operator request)
> Hub refs: none

## Summary and current code reality

The operator's screenshot shows a large four-row filter panel and wide model
cards repeating full artifact ids, download status, fit status and text action
buttons for every version. `console_catalog.py` owns this UI. Existing code
already supports five-version disclosure, exact artifact downloads, downloads
progress/cancellation, confirmed deletion and default-text route saves. Reuse
these operations and the shared kit rather than introducing new backend APIs.
The prior delivery is committed (`334a2e6a`, `83a38b40`); both working trees were
clean at intake.

## Scope

Compact responsive model grid, concise build identity and status, standard
icon actions with accessible names/kit tooltips, on-demand build details,
collapsible advanced filters and quick browsing/downloaded library views.
Default-text selection must stay explicit about its scope. Preserve the full
catalog, 4-bit initial filter, five-version disclosure, exact download/default
payloads, permissions, destructive confirmation, progress and shared chrome.
Use existing theme tokens and icon primitives. Browser review and authorized
local graceful redeploy are part of delivery. Do not change another agent's
work, catalog discovery APIs, capability ownership or services. Fetch latency
remains the separate proposed item 0986. ADR impact: none; presentation within
existing contracts (ADR-0026 reachability and shared kit conventions).

## Acceptance criteria

- [x] Search and library browsing are prominent; advanced filters are reachable without dominating the page.
- [x] Model cards and versions are visually compact, with identifying publisher/precision information and full IDs/details reachable.
- [x] Download/retry, default text and delete use standard named icon buttons, with actual busy/current/disabled state and kit help.
- [x] Five-version disclosure, filtering, copied IDs, download/progress/cancel, confirmed deletion and exact default-route behavior remain correct.
- [x] Desktop/mobile and light/dark browser checks cover keyboard, waiting/error, permission and management operations; shared widgets/themes remain intact.
- [x] Scoped changes are committed and the live console is redeployed/verified.

## Receipts

- Intake: operator screenshot and source audit, 2026-10-30.

## Completion report

Completed 2026-10-30. Intake checkpoint `cc1d345b`; implementation `e0da0fdc`.
Original path: `planned/0987_models_library_visual_redesign.md`.
Final path: `completed/0987_models_library_visual_redesign.md`.

Replaced the full-width model table with a responsive library grid. Search and
Browse/Downloaded views are prominent; advanced shared-kit filters open on
demand and scroll into view when used from the sticky bar. Downloaded spans all
precisions, including owned models outside the catalog. Cards show concise
provider, precision, publisher, size and fit; full IDs, copy, license and file
information are available through the info control. Five-version disclosure
retains the complete catalog.

Standard SVG download/retry, default-text star, delete, cancel and info actions
have accessible names and shared-kit help. Busy, permission and current-default
states are explicit. Exact route payloads, download/progress/cancel semantics,
and delete dry-run/confirmation are preserved. Keyboard focus survives updates,
including default selection. Shared themes and reserved header widgets are
unchanged. Source: `console_catalog.py`, additive icons in `console.py`; seven
catalog/browser/first-run regression files updated.

Validation: 81 targeted regression tests passed; final focused gate 43 passed
after the last responsive/filter reveal adjustments. Catalog/wizard browser
checks 100/100; Resources checks 122/122. Desktop and narrow mobile, light/dark,
waiting, disclosure, details/copy, permissions and fixture management operations
were covered. [Screenshots and review](../../../design/models-library-0987/REVIEW.md)
and [machine-readable evidence](../../../design/models-library-0987/validation.json)
are retained.

The existing local gateway was gracefully restarted with zero in-flight work.
Live health is healthy; 83 models match the initial 4-bit filter; collapsed
filters, icon actions, details, downloaded all-precision view, 37 default-text
actions and shared widgets were verified. No page errors or live management
writes occurred. No package release, new service or machine persistence was
introduced. Other agents' work was not staged.

ADR impact: none; presentation within existing contracts. No code/backlog drift
or new follow-up was found. The existing proposed 0986 retains full-fetch latency
work; this change makes no backend latency claim. Default selection remains
explicitly scoped to text. No recurrent tasks exist in this backlog. Agora
remained connection-refused; local evidence substitutes for unavailable hub
receipts/mirror updates, without claiming hub synchronization.
