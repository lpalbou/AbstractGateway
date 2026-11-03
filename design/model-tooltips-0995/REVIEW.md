# Models tooltip and downloaded filter — 0995 / UIC 0077

Completed 2026-10-30. Gateway `15a26edc`; AbstractUIC `3833355`.

## Result

Shared tooltips support a title, short explanation, aligned facts, labelled lists
and an optional note. Safe DOM construction uses textContent; no caller HTML.
String tooltips remain compatible, with paragraphs for multiline text. Shared
hover, keyboard/ARIA, Escape, theme, placement and internal scrolling apply.

Models fit tooltips use canonical fit fields, four useful facts at most and one
current action. Installed models omit obsolete download-disk warnings. Complete
estimator notes remain in expandable build details. Host and residency tooltips
use the same structure; action labels no longer repeat artifact names.

Downloaded only sits beside Fits this computer. One handler synchronizes it with
Browse/Downloaded tabs and the existing URL state. Downloaded shows all local
inventory; Browse restores prior filters. Keyboard focus survives the update.
On phones the shared filter bar wraps multiple switches without overflow.

## Verification and visual review

Single-agent implementation and visual review; no independent reviewer claimed.
55 shared tooltip, 41 filter-bar, 32 top-bar and 492 brand checks passed. Kit and
islands build, CSS balance, 21 theme-token blocks and strict contrast on 20 named
themes passed. Catalog browser receipt: 182 assertions; Resources regression
receipt: 122 assertions, both without failures. Focused pytest run: 53 passed,
one selective-export fixture failure; after fixing that fixture, all 34 checks
in the follow-up filter-bar/first-run/Resources gate passed. Earlier nested-list
test extraction and shortened-label expectations were updated with coverage.

Gracefully restarted the existing local Gateway only after model requests and
runner ticks were idle. Live dark/light and 320px checks passed for structured
content, installed warning relevance, keyboard descriptions, raw notes, quick
switch/tab synchronization, local inventory, reload persistence and restored
browse defaults. No page errors or model-management writes. Health passed.

The real installed 112 GB model from the reported case now shows a 320 × 166px
fit tooltip with four aligned facts and no stale disk warning. Visual inspection
of dark desktop and light mobile captures confirms readable theme contrast,
compact layout and bounded placement. Live available-memory values are snapshots.

- [Toolbar](toolbar-dark.png)
- [Fit, dark](fit-dark.png)
- [Fit, light](fit-light.png)
- [Phone, light](fit-mobile-light.png)
- [Machine-readable receipts](validation.json)

## Scope and limits

Shared source and Gateway vendored assets updated; no registry publication or
claim that other applications' already-built bundles were redeployed. No ADR
change: UIC owns the primitive, Gateway formats canonical host data. Unrelated
console-tui work was excluded. Agora was unavailable (connection refused), so
local backlog items and these receipts record completion. No new residual work.
