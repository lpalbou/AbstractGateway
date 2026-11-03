# 0976 — The console About adopts the kit About dialog

**Status**: planned · **Priority**: P2 · **Created**: 2026-10-29
**Cross-package**: AbstractUIC PR #37 (islands host a page-owned About extra; not released as of 2026-10-29).
> Root link: operator ruling 2026-10-29 — the top-bar set (docs drawer button, Appearance, About, one account widget)
> is the SAME kit components in every app and in the gateway console.

## Why
After the account-widget PR (feat/console-account-widget: ui-kit 0.9.6 vendored, `identity: "menu"` +
`connection.me`), three of the four top-bar controls in the console are kit islands: the Docs drawer button
(`mountDocsAssistant`), Appearance (`mountAppearance`) and the account widget (`mountTopBar`). About is not: it is
the console's own dialog (`#gateway-about-backdrop` in `console.py`), opened by an `info` extra
(`gateway-about-open` in `console_ui.py` `topBarIslandProps()`), with `about: null` passed to the island. It stays
local because it carries the host's update controls (Check for updates / Update / Restart gateway), and the released
islands (ui-kit 0.9.6) have no way to put page-owned content inside the kit's `AfAboutDialog`.

AbstractUIC PR #37 adds `about.extra` (an `HTMLElement`) to the islands' top bar, which lets the kit About host the
update controls.

## Scope
- After AbstractUIC releases PR #37: resync the vendored islands to that kit release
  (`(cd abstractuic/ui-kit && node scripts/build_islands.mjs)`, then `python -m abstractgateway.console_islands_sync`
  and `python -m abstractgateway.console_theme_sync`, both from the released tag).
- `topBarIslandProps()` passes `about: { ...consoleAboutProps(...), extra: <the update-controls element> }` and drops
  the `gateway-about-open` extra; the local `#gateway-about-backdrop` dialog and its open/close code are removed. The
  update controls (`gateway-about-check`, `-install`, `-restart`, badge, status, hint) move into the element passed as
  `about.extra` and keep their ids and behaviour.
- Tests: the console passes `about` with `extra`, has no `gateway-about-open` extra and no `gateway-about-backdrop`; the
  update-control tests still pass against the new host element. Prove each pin red once.

## Done when
On the operator's instance (8443 or 8080, at a stated sha) the console's About is the kit `AfAboutDialog` (same look as
the other apps' About), and Check for updates / Update / Restart gateway work from inside it.
