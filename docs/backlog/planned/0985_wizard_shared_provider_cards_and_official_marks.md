# 0985-gateway: [IMPROVEMENT] Update wizard provider cards and use official company marks

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
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

- [ ] Wizard/Providers share official company artwork for native cloud providers and OVH.
- [ ] OVH preset and generic OpenAI-compatible endpoint forms are reachable in the deployed console.
- [ ] Shared kit, credential-flow and gateway sync checks pass; browser inspection confirms the cards.

## Receipts

- Intake: operator screenshots and live source audit, 2026-10-30.
