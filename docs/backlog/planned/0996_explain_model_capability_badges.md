# 0996-abstractgateway: [IMPROVEMENT] Explain model capability badges

> Package: abstractgateway
> Type: improvement
> Created: 2026-10-30
> Status: planned
> Priority: P1
> Labels: ux, accessibility, seat-framework
> Work-item id: abstractgateway-0996
> Owner: framework
> Thread: unavailable (Agora connection refused)
> Hub refs: none

## Current code reality and scope

`mcCapabilityMarkup` currently repeats each short capability name in a plain
hover, while only the whole icon group can receive keyboard focus. This gives
no explanation of what Thinking, Tools or the other badges let a user do.
Replace these hovers with the shared structured title/description layout for
all nine capabilities. Explain practical use and relevant cost or permission
implications in one or two short sentences, distinguish native/prompted tools,
and keep explicit image/audio input versus generated outputs accurate.
Make each badge focusable and described, without changing card geometry,
capability truth, filters, icons or reserved shared chrome. No new ADR impact:
shared tooltip and canonical model metadata ownership remain unchanged.

## Validation

Check all capability explanations and native/prompted distinctions; real-browser
hover/focus/ARIA/Escape, theme, card size and narrow placement checks. Scoped
commits and authorized graceful local redeploy, then verify actual badge hovers
and retain evidence. Other agent's console-tui work is excluded.

- [ ] Implement meaningful explanations for every advertised badge.
- [ ] Validate, commit, redeploy and record completion.
