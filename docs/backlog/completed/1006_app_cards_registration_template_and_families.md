# 1006-abstractgateway: [FEATURE] App cards: one registration template, families, illustrations

> Package: abstractgateway
> Type: feature
> Created: 2026-11-01
> Status: completed
> Completed: 2026-11-01
> Release: unreleased (not committed, not deployed)
> Priority: P2
> Labels: console, apps, ux
> Work-item id: abstractgateway-1006
> Owner: gateway
> Thread: local

## Request

Operator, 2026-11-01: "please merge the 2 assistant in one card but with 2 buttons to launch them. same as
what we did here [Code: Open + Open in Terminal]. but for the regular 'open' (web), we should have an
icon/badge to let people know it's on the web or not. I would also redesign those cards (a) to have a small
description of each app and (b) accept an illustration. this must be a template for when we register new apps."

## Current code reality

- Three registries describe what the Apps tab shows: `apps_manager.APPS` (`AppSpec`, browser apps),
  `apps_desktop.DESKTOP_APPS` (`DesktopAppSpec`, the desktop Assistant) and `apps_manager.TUI_BY_APP`
  (terminal versions, reported as `interfaces[]` of kind `tui` on the browser row).
- `overview()` returns one row per browser app, then one row per desktop app; the console renders one
  card per row (`console_ui.appCardMarkup`). The Assistant therefore shows two cards: "Assistant (web)"
  and "Assistant".
- The card's two-letter mark and its one-line blurb are hardcoded in the console (`APP_COPY`), apart from
  the registry's `description`, which the card only uses as a fallback. The blurb is truncated to one line.
- "Open" on a browser app and "Open" on the desktop app look identical; only Code's terminal button carries
  a glyph (`>_`).
- The console has no static asset route: everything is inlined in the page.

## Scope

1. Registration template: `AppSpec` and `DesktopAppSpec` declare `mark`, `description` (one sentence, shown
   whole), `illustration` (a file under `src/abstractgateway/console_assets/apps/`, or `None`) and `family`
   (optional). `APP_FAMILIES` names each family's card. `APP_COPY` is folded into the registry and deleted.
2. `overview()` returns `cards[]`: one card per family (members in browser → desktop order; the browser
   member is the identity) or per app, each with `name`, `description`, `mark`, `illustration_url` (only
   when the registered file exists), `members` and `launchers` derived from member kinds
   (browser, desktop, terminal).
3. `GET /console/assets/apps/<file>`: read-only, allowlisted to the registered illustration files that
   exist, immutable cache headers, 404 otherwise; public like `/console` itself.
4. Card redesign: illustration slot (16:9, top), mark + title + the identity's status badge, the full
   description, ONE launcher row (globe badge on browser launchers, monitor on desktop launchers, `>_` on
   terminal ones), then body/notes/technical details unchanged in behaviour. A secondary member's status
   badge and notes (`launch_blocked`, `other_running`, `restart_note`, `version_reason`, install progress)
   sit beside its own launcher. 390 px: no horizontal scroll, 44 px targets, named breakpoints only.
5. Register the Assistant family: `assistant-web` + desktop `assistant` → one "Assistant" card.

Out of scope: the tray menu (its own table), the Rust console TUI (reads the same rows; `cards[]` is
additive), the illustrations themselves (a parallel worker drops `<app_id>.webp`, 768×432).

## Validation

- Offline console tests (node, `_slice_function` pattern): one card for the family with two launchers; the
  globe badge only on browser launchers; illustration URL only for registered files; removing `family`
  from the registry gives two cards again (red when the grouping is removed).
- Route tests: allowlisted file served with immutable headers; unknown, traversal and unregistered names 404.
- Existing `tests/test_gateway_console*.py` and `tests/test_gateway_apps*.py` stay green (pre-existing
  failures shown identical at HEAD).
- Disposable gateway (scratch HOME + data dir, own token): Chrome screenshots of the Apps tab at 1280×800
  and 390×844, with and without a placeholder illustration.
- Docs: `docs/apps.md` (the registration template), `docs/console.md`, changelog fragment.

- [x] Registry + overview `cards[]` + asset route
- [x] Console card redesign
- [x] Tests, browser check, docs

## Completion report

- Registry: `AppSpec` and `DesktopAppSpec` gained `mark`, `illustration`, `family` (the `description` field is the
  card's whole sentence); `APP_FAMILIES = {"assistant": "Assistant"}`; `assistant-web` and the desktop `assistant`
  are family `assistant` and share `assistant.webp`. `APP_COPY` is deleted: the console has no per-app copy.
- `apps_manager.app_cards()` builds `cards[]` (identity = browser member; launchers by kind: web → browser,
  desktop → desktop, `interfaces[]` tui → terminal); every row carries `mark`, `family`, `illustration_url`
  (version-tagged, only when the registered file exists); desktop rows carry `desktop.open_label`. A family with
  no `APP_FAMILIES` title raises (no silent fallback).
- `GET /console/assets/apps/{name}` (app.py): allowlist = registered file names that exist, WebP/PNG/JPEG/AVIF
  only, `Cache-Control: public, max-age=31536000, immutable`, `nosniff`, 404 otherwise; recorded in the route
  authorization contract as public. The seven illustrations (`<app_id>.webp`, `assistant.webp` for the family)
  ship in the wheel with the rest of `src/abstractgateway/` (checked with `uv build --wheel`; no pyproject change).
- Console (`console_ui.py`): `appMemberParts()` (one member's badge, body, launcher, update, terminal parts,
  gear, technical details) and `appCardMarkup(card, withArt)`; six-row subgrid (illustration row empty when no
  card has art; a card without art shows its mark when a neighbour has one); globe/monitor icons in `ICONS`;
  a secondary member's launcher, status badge and notes sit together (`data-app-member`); long gateway sentences
  wrap inside the card.
- Tests: new `tests/test_gateway_console_app_cards.py` (11 tests: registry declarations and packaged files,
  family card + launchers through the real `overview()`, two cards without `family`, missing family title raises,
  illustration URL only for registered existing files, asset route allowlist/headers/404s, node-rendered cards:
  one card with browser + desktop launchers, globe badge only on browser launchers, terminal `>_` without it,
  two cards without the family, art and mark placeholders). Six mutations (grouping removed, family removed,
  badge removed, member badge removed, URL for a missing file, route allowlist bypassed) each turn it red.
  Existing card tests and fixtures updated to the `cards[]` contract.
- Suite: `tests/test_gateway_console*.py tests/test_gateway_apps*.py` + badge/update/route-contract/tray-model
  tests: 627 passed, 20 failed, 17 skipped; the 20 failures fail identically at HEAD 310d972f (17 console tests on
  the venv's runtime 0.15.2, 3 route-contract tests).
- Browser check: a disposable gateway (scratch HOME and data dir, its own admin token, port 18877), Chrome
  headless at 1280×800 and 390×844 (touch), real overview and an in-page faked "installed" overview, with the real
  illustrations, a 768×432 placeholder, and no art: no page errors, no horizontal scroll, no card overflow, 44 px
  launchers at 390 px. Screenshots are kept outside the repository.
- Docs: docs/apps.md "Registering an app" (fields, example, cards, launchers, illustrations), docs/console.md
  Apps tab, llms.txt, llms-full.txt regenerated; changelog fragment `changelog.d/app-cards-template.md`.

Follow-ups (not done here): the tray's Apps submenu keeps its own app table (`tray/apps.py`) and still lists the
two Assistants separately; the console's "cannot manage apps" fallback (`FIRST_RUN_APPS` in console.py) keeps its
own short list; the Rust terminal console reads the same rows and ignores `cards[]`.

### Review fixes (2026-11-01)

Adversarial review approved with fixes, all applied: a family card's lead (badge, primary action) is the first
installed member, browser preferred (on a standard install the desktop Assistant, since the web package is not on
npm); Install on a family card names what it installs; the console's own app list (`FIRST_RUN_APPS`) is removed and
the error state shows the reason only; the illustration tag is a content hash computed once per file; dead
`is-oneline` CSS removed; each member's technical block is labelled (Browser app / Desktop app); a family card is
`data-app-card="family:<id>"`; the test parses WebP dimensions (768×432). 14 tests in the new file, each new rule
mutation-checked red; the requested test files: 214 passed, 3 failed (route-contract, identical at HEAD).
