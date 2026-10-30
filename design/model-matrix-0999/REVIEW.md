# Models toolbar and capability matrix — 0999

Completed and locally deployed 2026-10-30. `cde6956e` / `4a69ff88`.

The live machine label (Apple M5 Max here) is inside the existing memory/GPU
widget. It comes from the snapshot's GPU names, with host-name/Host fallback;
no second resource store or poll was added. The separate Models host row is gone.
Refresh follows Filters in an inseparable action group, including phone layouts.
The quick filter is labelled Downloaded. Desktop search gets less flex space:
live measurement at 1600px is 495.58px versus 550.78px unrestricted (10.02% less).
On phones, the full machine name has its own line inside the same widget.
Shared About/docs/theme/account widgets remain intact.

Every card reserves the same nine capability columns: text, thinking, tools,
image input, audio input, embeddings, speech output, image output, video output.
Absent/unknown capabilities leave empty slots with no icon, tooltip or focus stop.
Reported badges retain their colour, useful shared explanation and keyboard/ARIA
behavior. A blank disclosure slot also reserves the chevron width when a card
has no hidden versions. This prevents the entire matrix shifting on those cards.
Narrow headers use smaller fixed slots and a second header line; names remain
readable and touch disclosure targets retain their size.

## Validation and visual review

70 primary catalog/filter/first-run/resource-widget checks passed; the focused
widget browser/unit gate passed 7; disclosure follow-up passed 35; final
responsive catalog/widget/filter gate passed 15. These gates overlap, so the
counts are not additive. Catalog browser receipt: 222 assertions, no failures.
The last desktop flex-weight calibration was verified directly in the live
browser after these gates. Legacy first-run Starter wording was updated to the
already-shipped Recommended label; the resource-widget browser fixture now uses
scoped bearer auth instead of the replaced legacy sign-in form. Renamed-switch
checks use its role rather than ambiguous Downloaded facet text.

Graceful restart used the existing host operation after model inflight and
runner ticks were zero. Live dark/light checks passed for machine placement,
refresh order and operation, short search, renamed toggle/keyboard behavior,
resource navigation, inert empty cells and matrix geometry. All 88 browse cards
and 29 downloaded cards align, including mixed disclosure states. 390px and
320px placement/overflow/full-machine-name/action-group checks passed, plus a
320px touch context. Health passed, with no page errors or management writes.

Single-agent visual inspection confirms the desktop matrix and readable phone
widget/action group. No independent reviewer claimed. In-app browser execution
was unavailable; local Playwright was used. Agora connection was refused; local
backlog and these receipts record completion. No new ADR: Gateway composes the
existing shared UI and owns its live resource widget; no kit fork, publication,
model operation, new service or machine persistence. No new residual work.

- [Dark desktop](desktop-dark.png)
- [Light desktop](desktop-light.png)
- [Downloaded matrix](downloaded-dark.png)
- [320px touch](phone-touch-dark.png)
- [Light phone](phone-light.png)
- [Validation](validation.json)
