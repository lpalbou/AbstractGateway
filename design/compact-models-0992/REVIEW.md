# Compact Models cards — 0992

Single-agent implementation and visual review. Live [dark](models-dark.png),
[light](models-light.png), themed [dark tooltip](tooltip-dark.png),
[light tooltip](tooltip-light.png) and [390px mobile](mobile-light.png).

A three-version card is 151px high (37px header, three 36px rows); three columns
fit the 1600px viewport. Header/body padding is 5px/8px and grid gaps are 10px.
Accessible capability glyphs sit beside the title. Publisher fits inline in each
version row. The contrasting expandable header retains hover/focus highlighting.
Repeated info buttons and their full-ID hover text are gone. Clicking the build
identity opens exact ID/copy, license and file-location evidence. No model-card
fetch/description stream was added; no description is fabricated.

Shared AbstractUIC tooltip surface/text/border use the actual active theme.
This is a kit correction, regenerated into the console rather than overridden
locally. Kit theme selection and reserved top-right widgets remain in place.

88 regression tests passed; one source-fingerprint failure was corrected by
regenerating console themes, then all 8 theme/island sync checks passed. Browser
checks: 138 catalog/wizard and 122 Resources checks, including default text route,
download/progress, dry-run deletion and cancel confirmations, three-version
expansion, keyboard/focus, loading/failure recovery, reader permissions, light/dark
and 320/390px layout. Kit: 42 tooltip checks, CSS balance, 21 theme-token blocks,
16 polish checks and strict contrast on all 20 named themes passed.

Gateway implementation `a352234b`, shared kit `fe951fd`. Redeployed via the
existing graceful restart after health/in-flight checks (zero model operations
and runner ticks). Live light/dark checks passed: three rows/columns, density,
capability placement, header activation, keyboard collapse, build details/copy,
actual tooltip colours, mobile fit and healthy gateway. No page errors or model
management writes occurred during live verification. [Machine receipt](validation.json).

No additional follow-up or ADR impact. Agora was unavailable (connection refused);
local backlog and scoped commits record delivery.
