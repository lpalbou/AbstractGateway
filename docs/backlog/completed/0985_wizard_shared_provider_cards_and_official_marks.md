# 0985-gateway: [IMPROVEMENT] Update wizard provider cards and use official company marks

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: completed
> Completed: 2026-10-30
> Priority: P1
> Labels: console, ux, seat-framework
> Work-item id: abstractgateway-0985
> Owner: framework
> Thread: unavailable (Agora connection refused; direct operator request)
> Hub refs: none

## Summary and code audit

Current code reality: the committed AbstractUIC kit 0.9.10 already includes OVH
and a generic OpenAI-compatible card, plus the official OpenAI mark. The console
vendors 0.9.9. Anthropic, Portkey and OVH use deliberate text fallbacks in the
kit. The operator explicitly requests official company logos on these cards.

Scope: source the actual company artwork into shared `brand_icons.tsx`, preserve
provenance and original geometry/colours, update mark checks/docs and regenerate
the gateway islands from the committed kit. Reuse existing OVH/generic endpoint
flows and check-before-save behavior. No provider credentials written during QA;
no new provider runtime or API adapter. ADR impact: none; existing shared kit and
endpoint-profile contracts are reused. Kit changes are referenced here rather
than duplicating the committed AbstractUIC cloud-card work (PR #46).

## Acceptance criteria

- [x] Wizard/Providers share official company artwork for native cloud providers and OVH.
- [x] OVH preset and generic OpenAI-compatible endpoint forms are reachable in the deployed console.
- [x] Shared kit, credential-flow and gateway sync checks pass; browser inspection confirms the cards.

## Receipts

- Intake: operator screenshots and live source audit, 2026-10-30.


## Completion report

Completed 2026-10-30, unreleased; locally deployed. Gateway implementation
`334a2e6a`; intake `ae884b88`. Shared kit changes: `3b99aa4`, `8a25c33`.

Validation across the changed surfaces: 71 console/catalog/first-run/brand/sync
checks passed in the project environment; 15 credential-route, Resources logic
and Resources browser tests passed using the deployment environment (Runtime
0.15.3). The first credential-route attempt in the project environment failed
at the pre-existing Runtime 0.15.2 floor; it passed with the deployment Runtime.
No dependency environment was modified. Total: 86 passing targeted tests across
the two lanes. Browser scenarios contain 52 catalog/wizard checks and 122
Resources checks, all passing. Kit: TypeScript build and strict unused checks,
CSS balance, 21 theme-token invariants, 289 mark checks and 64 cloud-flow checks.
Reviewed desktop dark/light, mobile and delayed-loading screenshots. The small
first-run VM now binds real kit markup helpers; its synthetic DOM omits the
unrelated Resources initializer, covered by the real browser suites instead.

Deployment receipt: existing authenticated graceful restart returned
`{ok:true,restart:true}` after runner/model-host inflight were zero. Health
returned healthy. Live browser inspection exercised All (96 models, 1,798
matching versions), expansion/collapse, the five-row presentation, and the
wizard's six shared cards. The Providers tab was separately opened through its
navigation button: six cards, no page errors. No model operation or provider
credential write was performed during live QA. Source/artwork provenance is in
AbstractUIC `docs/icons.md`. Fixture screenshots and the validation receipt are
in `design/console-0984-0985/`.

Agora receipts/mirror were unavailable (connection refused); this local record
preserves the directly authorized delivery. ADR impact: none; existing shared
kit, measurement and endpoint-profile ownership remain in place. No package was
published and no service/persistence was installed.

Shared brand artwork replaces Anthropic/Portkey/OVH text badges. The OpenAI
mark's empty viewport margin is trimmed for a readable tile; its paths are
unchanged. Original sources, colours, geometry and trademark attribution remain
recorded. Portkey's official website PNG is embedded byte-for-byte in an SVG
viewport on a dark tile, without a network dependency. Only the non-brand
OpenAI-compatible protocol retains an API text badge.

The gateway now vendors kit 0.9.10 plus these local source changes, integrating
the already-committed OVH and generic compatible endpoint forms (UIC PR #46).
The wizard's text describes the new Connect action. The resync also supplied the
kit filter bar, so the interim CSS/markup copies were removed and replaced with
thin calls to the shared helpers. Source/validation: kit `brand_icons.tsx`,
`theme.css`, `check_brand_icons.mjs`, `docs/icons.md`, `CHANGELOG.md`; gateway
`console.py`, generated `console_islands.py` and sync/brand/flow/browser tests.

Follow-up triage: generic endpoint supports the user's additional compatible
services without duplicating native adapters or guessing service-specific
URLs. No new provider account was connected, and real remote inference was not
attempted. API-key checks/saving remain covered by the existing contract tests.
No new architecture or policy is established by this presentation correction.

Original path: `planned/0985_wizard_shared_provider_cards_and_official_marks.md`.
Final path: `completed/0985_wizard_shared_provider_cards_and_official_marks.md`.
Counts, ledger and links updated; legacy filename hygiene remains outside this change.

Final fresh-page live receipt: 4-bit selected by default, 83 matching model cards,
349 version rows, every card at or below five versions, zero page errors. This
load completed when allowed a longer wait; the latency observation remains valid.
