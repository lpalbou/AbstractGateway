# 0987-gateway: [IMPROVEMENT] Redesign the Models page for fast visual browsing and management

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
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

- [ ] Search and library browsing are prominent; advanced filters are reachable without dominating the page.
- [ ] Model cards and versions are visually compact, with identifying publisher/precision information and full IDs/details reachable.
- [ ] Download/retry, default text and delete use standard named icon buttons, with actual busy/current/disabled state and kit help.
- [ ] Five-version disclosure, filtering, copied IDs, download/progress/cancel, confirmed deletion and exact default-route behavior remain correct.
- [ ] Desktop/mobile and light/dark browser checks cover keyboard, waiting/error, permission and management operations; shared widgets/themes remain intact.
- [ ] Scoped changes are committed and the live console is redeployed/verified.

## Receipts

- Intake: operator screenshot and source audit, 2026-10-30.
