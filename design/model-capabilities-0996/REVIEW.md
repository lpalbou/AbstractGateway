# Model capability explanations — 0996

Implemented in `c332731e`, deployed locally 2026-10-30.

Every capability badge uses the existing shared themed title/description tooltip:
text, thinking, tools, image input, audio input, embeddings, speech, image and
video generation. Descriptions explain practical use in at most 30 words.
Thinking mentions response time; tool use explains app permissions and varies
for prompted tool support. Input capabilities and generated outputs are distinct.
No capability inference, backend contract or shared component fork was added.

Each badge is independently focusable, named and described. The group no longer
has a redundant tooltip listing labels. Card spacing, icons, colours, filtering
and reserved top-right widgets remain intact. Focus outline uses theme tokens.

34 focused tests passed, including the browser gate's 208 assertions in light
and dark themes. All nine meanings and native/prompted tools are checked. Live
hover/focus/ARIA/Escape, actual theme colours, card geometry and 320px placement
passed in both themes. No page errors or model-management writes. Restart used
the existing graceful host operation only after model requests and runner ticks
were idle; post-restart health is healthy.

Single-agent visual inspection of desktop and phone captures confirms compact,
readable explanations and bounded placement. A first live verifier attempt raced
with viewport scrolling; waiting for responsive layout and using keyboard focus
resolved that verifier timing issue without product changes. In-app browser
execution tool was unavailable; local Playwright was used. Agora also returned
connection refused; the backlog and these receipts provide the completion record.
Other agent's console-tui changes were excluded. No new ADR impact or residual
implementation work; no registry publication.

- [Dark desktop](thinking-dark.png)
- [Light desktop](thinking-light.png)
- [Light phone](thinking-mobile-light.png)
- [Validation receipts](validation.json)
