# Balanced Models spacing — 0993

Operator feedback replaces the 0992 density target while preserving its interaction
work. Single-agent implementation/visual review: [dark](models-dark.png),
[light](models-light.png), [mobile](mobile-light.png).

Cards now measure 195px at 1600px viewport width: 45px header, three 44px rows,
12px body inset and 16px between cards. Two columns fit common desktop widths;
the grid adds columns on wider screens. The capability group sits after parameter
facts and before the chevron. Its right edge aligns across short/long titles,
parameter strings and different capability counts. Capabilities retain accessible
names and shared tooltips. No separate text line or info icon returns.

42 focused tests passed, then 7 browser/merged-model tests passed after adding
variable title and active-parameter fixtures. 140 catalog/wizard assertions pass,
covering spacing and alignment, light/dark, 320/390px, expansion, keyboard/focus,
loading/retry and management controls. Live dark/light verification passed with
zero page errors or management writes; one verifier selector initially searched
for a nested header rather than its direct capability child, and was corrected.
[Machine receipt](validation.json).

Implementation `af7a33a4` committed and deployed through the existing graceful
restart after healthy/zero-inflight checks. Live gateway healthy. No new API,
fetch behavior, shared chrome or ADR changes. Agora unavailable (connection refused).

Final header badge alignment: `fae77d55` places Starter/Hub badges before the right controls; fixture now includes a Starter badge. The 7 browser/merged checks and 140 browser assertions passed again, then healthy live verification passed after redeploy.
