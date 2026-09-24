"""Console UI layer (mission L, 2026-09-24): the premium layout pass.

The operator's verdict on a fresh install (Safari, 1920 px): "there are
layout issues everywhere", "we are far from premium" -- a 760 px modal
wizard in a 1920 px window, engine tables that overflow and wrap
``http://localhost:11434`` one character per line, a raw ``uv`` log in a
nested scrolling card, downloads shown as a bare "downloading" pill, and
terminal commands on the Apps step.

This module owns the console's layout layer, spliced into the page by
``console.py`` (pure ``str.replace`` placeholders, like every other splice):

- ``CONSOLE_UI_CSS``: the full-page first-run flow (left step rail, wide
  content, sticky footer), card grids (engines, apps, starter models),
  progress bars, responsive tables (a row becomes a card when the table no
  longer fits -- never a horizontal scrollbar), ellipsis-with-hover for
  long values (ADR-0026: never character-wrap a URL or a path), and the
  "technical details" (Advanced) switch that hides CLI lines by default.
- ``CONSOLE_UI_JS``: the matching behavior. It runs INSIDE the console's
  main script (same scope: ``state``, ``api``, ``$``, ``esc``...), so its
  functions are ordinary console functions.

Colours, radii, type sizes and pill tones come from the abstractuic kit's
tokens (``theme.css``: ``--bg-*``, ``--text-*``, ``--*-subtle``,
``--*-border``, ``--ui-*``, ``--radius-*``, ``--font-size-*``), which the
page carries verbatim (``console_themes.py`` + ``console_islands.py``).
"""

from __future__ import annotations

CONSOLE_UI_CSS = r"""
    /* ================= Console UI layer (mission L) ================= */
    /* ---- Technical details switch: CLI lines, route ids, commands ---- */
    .ui-advanced { display: none !important; }
    body.show-advanced .ui-advanced { display: block !important; }
    body.show-advanced span.ui-advanced, body.show-advanced code.ui-advanced { display: inline !important; }
    .acc-root .acc-cli-line, .acc-root .acc-hint { display: none; }
    body.show-advanced .acc-root .acc-cli-line, body.show-advanced .acc-root .acc-hint { display: block; }
    .acc-root .acc-job .acc-sub:has(> code) { display: none; }
    body.show-advanced .acc-root .acc-job .acc-sub:has(> code) { display: block; }
    .ui-switch { display: inline-flex; align-items: center; gap: 8px; margin: 0; cursor: pointer; text-transform: none; letter-spacing: 0; font-size: var(--font-size-sm); font-weight: 500; color: var(--text-secondary); }
    .ui-switch input { appearance: none; -webkit-appearance: none; width: 30px; height: 18px; min-height: 0; padding: 0; margin: 0; border-radius: 999px; border: 1px solid var(--ui-border-2); background: var(--ui-surface-3); position: relative; cursor: pointer; flex: 0 0 auto; transition: background-color 120ms ease; }
    .ui-switch input::after { content: ""; position: absolute; top: 2px; left: 2px; width: 12px; height: 12px; border-radius: 999px; background: var(--text-secondary); transition: transform 120ms ease; }
    .ui-switch input:checked { background: var(--accent); border-color: var(--accent); }
    .ui-switch input:checked::after { transform: translateX(12px); background: #fff; }
    .ui-switch input:focus-visible { outline: 2px solid var(--info); outline-offset: 2px; box-shadow: none; }
    .shell_sidebar_foot { padding: 10px 14px 14px; border-top: 1px solid var(--ui-border-1); }
    @media (max-width: 900px) { .shell_sidebar_foot .ui-switch span { display: none; } .shell_sidebar_foot { padding: 10px 8px; display: flex; justify-content: center; } }

    /* ---- Top bar island (kit AfTopBarActions) ---- */
    .af-topbar-island { display: flex; align-items: center; min-width: 0; }
    .af-topbar-island .af-topbar { gap: 8px; }
    .af-topbar-island button { min-height: 0; font-weight: 600; gap: 7px; filter: none; }
    .af-topbar-island .af-topbar__btn { padding: 0; }
    /* Kit dialogs rendered as islands keep the kit's own look: neutralise the
       console's global form rules inside them. */
    .af-appearance label, .af-appearance__label { display: block; margin: 0; text-transform: none; letter-spacing: 0; }
    .af-appearance button, .af-select button { min-height: 0; }
    .af-appearance .af-select-trigger { width: 100%; background: var(--ui-surface-3); color: var(--text-primary); font-weight: 500; justify-content: space-between; text-align: left; }
    .af-select-trigger { text-align: left; }
    td .actions { gap: 6px; flex-wrap: wrap; }
    .af-select-search-input { min-height: 0; }
    .af-appearance-overlay { z-index: 1200; }
    .af-select-popover { z-index: 1300; }

    /* ---- Pills (kit tone tokens) ---- */
    .ui-pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px; border: 1px solid var(--muted-border, var(--ui-border-2)); background: var(--ui-pill-bg, var(--ui-surface-1)); color: var(--text-secondary); font-size: var(--font-size-xs); font-weight: 600; white-space: nowrap; line-height: 1.4; }
    .ui-pill::before { content: ""; width: 6px; height: 6px; border-radius: 999px; background: currentColor; opacity: .9; }
    .ui-pill.is-plain::before { display: none; }
    .ui-pill.tone-ok { color: var(--success); border-color: var(--success-border, var(--ui-border-2)); background: var(--success-subtle, transparent); }
    .ui-pill.tone-warn { color: var(--warning); border-color: var(--warning-border, var(--ui-border-2)); background: var(--warning-subtle, transparent); }
    .ui-pill.tone-err { color: var(--error); border-color: var(--error-border, var(--ui-border-2)); background: var(--error-subtle, transparent); }
    .ui-pill.tone-info { color: var(--info); border-color: var(--info-border, var(--ui-border-2)); background: var(--info-subtle, transparent); }
    .ui-pill.tone-busy::before { animation: ui-pulse 1.2s ease-in-out infinite; }
    @keyframes ui-pulse { 0%, 100% { opacity: .35; } 50% { opacity: 1; } }

    /* ---- Buttons used by the layer ---- */
    .ui-btn { min-height: 34px; padding: 7px 14px; border-radius: var(--radius-md); font-size: var(--font-size-md); font-weight: 600; }
    .ui-btn.is-primary { background: var(--accent); color: #fff; }
    .ui-btn.is-ghost { background: transparent; color: var(--text-primary); box-shadow: inset 0 0 0 1px var(--ui-border-2); }
    .ui-btn.is-ghost:hover:not(:disabled) { background: var(--ui-surface-2); }
    .ui-btn.is-quiet { background: transparent; color: var(--info); padding-inline: 6px; min-height: 30px; }
    .ui-btn:disabled { opacity: .5; }
    a.ui-btn { display: inline-flex; align-items: center; text-decoration: none; }
    a.ui-link { color: var(--info); font-size: var(--font-size-sm); text-decoration: none; }
    a.ui-link:hover { text-decoration: underline; }

    /* ---- Cards ---- */
    .ui-card-grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fill, minmax(min(100%, 330px), 1fr)); align-items: stretch; }
    .ui-card-grid.is-wide { grid-template-columns: repeat(auto-fill, minmax(min(100%, 400px), 1fr)); }
    .ui-card-grid.is-fit { grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr)); }
    .ui-card { display: flex; flex-direction: column; gap: 12px; min-width: 0; padding: 18px 18px 16px; border: 1px solid var(--ui-border-1); border-radius: var(--radius-lg); background: var(--bg-card, var(--bg-secondary)); box-shadow: 0 1px 2px rgba(0, 0, 0, .08); }
    .ui-card.is-attention { border-color: var(--error-border, var(--ui-border-2)); }
    .ui-card.is-busy { border-color: var(--info-border, var(--ui-border-2)); }
    .ui-card__head { display: flex; align-items: flex-start; gap: 12px; min-width: 0; }
    .ui-mark { flex: 0 0 40px; width: 40px; height: 40px; border-radius: var(--radius-lg); display: grid; place-items: center; font-weight: 750; font-size: 13px; letter-spacing: -.02em; color: var(--accent); background: var(--accent-subtle); border: 1px solid var(--accent-border, var(--ui-border-2)); }
    .ui-card__titles { min-width: 0; flex: 1 1 auto; display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; min-height: 40px; }
    .ui-card__title { font-size: var(--font-size-lg); font-weight: 650; line-height: 1.25; color: var(--text-primary); min-width: 0; overflow-wrap: normal; }
    .ui-card__blurb { color: var(--text-secondary); font-size: var(--font-size-md); line-height: 1.45; }
    .ui-card__status { flex: 0 0 auto; margin-left: auto; }
    .ui-facts { display: flex; flex-wrap: wrap; gap: 6px 16px; margin: 0; padding: 0; list-style: none; color: var(--text-secondary); font-size: var(--font-size-sm); }
    .ui-facts li { display: inline-flex; gap: 5px; min-width: 0; max-width: 100%; }
    .ui-facts b { color: var(--text-primary); font-weight: 600; }
    .ui-card__note { color: var(--text-secondary); font-size: var(--font-size-sm); line-height: 1.45; }
    .ui-card__actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: auto; padding-top: 2px; }
    .ui-card__actions .ui-spacer { flex: 1 1 auto; }
    .ui-confirm { display: grid; gap: 10px; padding: 12px 14px; border-radius: var(--radius-md); background: var(--ui-surface-1); border: 1px solid var(--ui-border-2); }
    .ui-confirm p { color: var(--text-primary); font-size: var(--font-size-md); }
    .ui-alert { display: grid; gap: 6px; padding: 10px 12px; border-radius: var(--radius-md); border: 1px solid var(--ui-border-2); background: var(--ui-surface-1); font-size: var(--font-size-md); line-height: 1.45; }
    .ui-alert strong { font-weight: 650; }
    .ui-alert.tone-err { border-color: var(--error-border, var(--ui-border-2)); background: var(--error-subtle, var(--ui-surface-1)); }
    .ui-alert.tone-warn { border-color: var(--warning-border, var(--ui-border-2)); background: var(--warning-subtle, var(--ui-surface-1)); }
    .ui-alert.tone-info { border-color: var(--info-border, var(--ui-border-2)); background: var(--info-subtle, var(--ui-surface-1)); }
    .ui-alert.tone-ok { border-color: var(--success-border, var(--ui-border-2)); background: var(--success-subtle, var(--ui-surface-1)); }
    details.ui-details > summary { cursor: pointer; color: var(--text-secondary); font-size: var(--font-size-sm); list-style: none; display: inline-flex; align-items: center; gap: 6px; user-select: none; }
    details.ui-details > summary::-webkit-details-marker { display: none; }
    details.ui-details > summary::before { content: "\25B8"; font-size: 10px; transition: transform 120ms ease; }
    details.ui-details[open] > summary::before { transform: rotate(90deg); }
    .ui-log { margin: 8px 0 0; max-height: 240px; overflow-y: auto; overflow-x: hidden; white-space: pre-wrap; overflow-wrap: anywhere; font: var(--font-size-xs)/1.5 var(--font-mono); color: var(--text-secondary); background: var(--ui-surface-3); border: 1px solid var(--ui-border-1); border-radius: var(--radius-md); padding: 10px 12px; }
    .ui-cmd { display: flex; align-items: center; gap: 8px; min-width: 0; }
    .ui-cmd code { min-width: 0; }
    /* ---- App cards (mission GG): icon + name + pill / one line / ONE action
       row at the same level across the grid. `.is-aligned` makes every card
       five subgrid rows (head, blurb, body, actions, tech) so each row's
       tracks are shared: the action rows line up even when one card has a
       progress bar or a Technical-details line and its neighbour has none.
       A command is ONE mono line with an ellipsis + Copy, never wrapped. ---- */
    .ui-card__blurb.is-oneline { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
    .ui-card__body, .ui-card__tech { display: grid; gap: 10px; min-width: 0; align-content: start; }
    .ui-card__body:empty, .ui-card__tech:empty { display: none; }
    .ui-card__tech { gap: 8px; padding-top: 10px; border-top: 1px solid var(--ui-border-1); }
    .ui-card__techline { display: flex; flex-wrap: wrap; align-items: center; gap: 2px 4px; color: var(--text-secondary); font-size: var(--font-size-sm); min-width: 0; }
    .ui-card__sep { color: var(--text-muted, var(--text-secondary)); padding-inline: 2px; }
    .ui-card__techrow { display: flex; align-items: center; gap: 8px; min-width: 0; color: var(--text-secondary); font-size: var(--font-size-xs); }
    .ui-card__techrow > code.ui-ellip, .ui-card__techrow > .ui-cmdline { flex: 1 1 auto; min-width: 0; }
    .ui-card__techrow > code.ui-ellip { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .ui-card__techrow.is-stacked { display: grid; gap: 4px; }
    .ui-card__techrow.is-stacked > .ui-card__techlabel { max-width: 100%; }
    .ui-card__techlabel { flex: 0 0 auto; min-width: 0; max-width: 45%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-weight: 600; }
    .ui-btn.is-text { background: transparent; color: var(--info); min-height: 26px; padding: 2px 4px; font-size: var(--font-size-sm); font-weight: 600; box-shadow: none; }
    .ui-btn.is-text:hover:not(:disabled) { text-decoration: underline; }
    .ui-btn__glyph { display: inline-block; margin-right: 7px; font: 650 11px/1 var(--font-mono); letter-spacing: -.02em; color: var(--text-secondary); }
    .ui-card__actions > .ui-btn { white-space: nowrap; }
    .ui-card-grid.is-aligned > .ui-card > .ui-card__actions:not(:empty) { min-height: 48px; }
    @supports (grid-template-rows: subgrid) {
      .ui-card-grid.is-aligned { row-gap: 0; }
      .ui-card-grid.is-aligned > .ui-card { display: grid; grid-row: span 5; grid-template-rows: subgrid; row-gap: 0; margin-bottom: 16px; }
      .ui-card-grid.is-aligned > .ui-card > .ui-card__blurb { padding-top: 10px; }
      .ui-card-grid.is-aligned > .ui-card > .ui-card__body:not(:empty) { padding-top: 12px; }
      .ui-card-grid.is-aligned > .ui-card > .ui-card__body:empty, .ui-card-grid.is-aligned > .ui-card > .ui-card__tech:empty { display: block; }
      .ui-card-grid.is-aligned > .ui-card > .ui-card__actions { margin-top: 0; padding-top: 14px; align-self: start; }
      .ui-card-grid.is-aligned > .ui-card > .ui-card__tech:not(:empty) { margin-top: 12px; }
      .ui-card-grid.is-aligned > .ui-card > .ui-card__tech:empty { border-top: 0; padding: 0; }
    }
    .ui-cmdline { display: flex; align-items: center; gap: 4px; min-width: 0; padding: 3px 3px 3px 10px; border-radius: var(--radius-md); background: var(--ui-surface-3); border: 1px solid var(--ui-border-1); }
    .ui-cmdline code, .ui-cmdline code.ui-ellip { flex: 1 1 auto; min-width: 0; max-width: 100%; display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-size: var(--font-size-xs); color: var(--text-primary); background: transparent; border: 0; padding: 0; box-shadow: none; }
    .ui-cmdline .ui-btn { flex: 0 0 auto; min-height: 28px; padding: 4px 8px; font-size: var(--font-size-sm); }
    .ui-console-tui { display: grid; gap: 8px; min-width: 0; margin: 0; max-width: 100%; }
    .ui-console-tui__line { margin: 0; color: var(--text-secondary); font-size: var(--font-size-sm); line-height: 1.45; }
    .ui-console-tui .ui-cmdline { max-width: 72ch; }
    .ui-section-title { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
    .ui-section-title h3 { font-size: var(--font-size-lg); font-weight: 650; }
    .ui-section-title .ui-sub { color: var(--text-secondary); font-size: var(--font-size-sm); }
    .ui-toolbar { display: flex; align-items: center; gap: 10px 14px; flex-wrap: wrap; color: var(--text-secondary); font-size: var(--font-size-sm); }
    .ui-empty { padding: 28px; text-align: center; color: var(--text-secondary); border: 1px dashed var(--ui-border-2); border-radius: var(--radius-lg); }

    /* ---- Progress (downloads and installs) ---- */
    .ui-progress { display: grid; gap: 6px; min-width: 0; }
    .ui-progress__head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; font-size: var(--font-size-sm); }
    .ui-progress__label { color: var(--text-primary); font-weight: 600; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ui-progress__pct { font-variant-numeric: tabular-nums; color: var(--text-primary); font-weight: 650; }
    .ui-progress__bar { position: relative; height: 8px; border-radius: 999px; overflow: hidden; background: var(--ui-surface-3); box-shadow: inset 0 0 0 1px var(--ui-border-1); }
    .ui-progress__bar > span { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 999px; background: linear-gradient(90deg, color-mix(in srgb, var(--info) 75%, var(--accent)), var(--info)); transition: width .45s ease; }
    .ui-progress__bar.is-indeterminate > span { width: 32% !important; animation: ui-indeterminate 1.4s ease-in-out infinite; }
    @keyframes ui-indeterminate { 0% { left: -32%; } 100% { left: 100%; } }
    .ui-progress[data-state="stalled"] .ui-progress__bar > span { background: var(--warning); }
    .ui-progress[data-state="failed"] .ui-progress__bar > span { background: var(--error); }
    .ui-progress[data-state="done"] .ui-progress__bar > span { background: var(--success); }
    .ui-progress__meta { display: flex; flex-wrap: wrap; gap: 4px 14px; color: var(--text-secondary); font-size: var(--font-size-xs); font-variant-numeric: tabular-nums; }
    .ui-progress__msg { color: var(--text-secondary); font-size: var(--font-size-sm); line-height: 1.4; }
    .ui-files { list-style: none; margin: 8px 0 0; padding: 0; display: grid; gap: 6px; }
    .ui-files li { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 10px; font-size: var(--font-size-xs); color: var(--text-secondary); }
    .ui-files .ui-progress__bar { grid-column: 1 / -1; height: 4px; }
    @media (prefers-reduced-motion: reduce) { .ui-progress__bar > span, .ui-pill.tone-busy::before { transition: none; animation: none; } }

    /* ---- Long values: ellipsis + full value on hover, click to copy ---- */
    .ui-ellip, td code, .acc-root td code {
      display: inline-block; max-width: min(100%, 38ch); overflow: hidden; text-overflow: ellipsis;
      white-space: nowrap; overflow-wrap: normal; word-break: normal; vertical-align: bottom;
    }
    /* In a table cell a percentage would let the cell shrink the value: cap
       in characters, and let a stacked (card) row use the full width. */
    td code, td .ui-ellip, .acc-root td code { max-width: 38ch; }
    code { font-family: var(--font-mono); }
    .ui-ellip.is-block { display: block; max-width: 100%; }
    .ui-facts .ui-ellip { max-width: min(100%, 44ch); }
    .is-truncated { cursor: copy; }
    #ui-toast { position: fixed; left: 50%; bottom: 28px; transform: translateX(-50%) translateY(12px); z-index: 1400; padding: 8px 14px; border-radius: 999px; background: var(--text-primary); color: var(--bg-primary); font-size: var(--font-size-sm); font-weight: 600; opacity: 0; pointer-events: none; transition: opacity 160ms ease, transform 160ms ease; }
    #ui-toast.is-on { opacity: 1; transform: translateX(-50%) translateY(0); }

    /* ---- Responsive tables: a row becomes a card when the table no longer
       fits its box (measured by the layer, per table) ---- */
    .table-scroll, .acc-root .acc-table-scroll { overflow-x: hidden; }
    /* Grid/flex items default to min-width:auto and GROW to a wide table's
       min-content -- the table then "fits" a box that itself overflows the
       page. Every layout box shrinks to its track instead, so a table that
       does not fit is measured as such (and stacks). */
    .workspace-shell > *, .tab-panel, .tab-panel > *, .tab-grid > *, .tab-stack > *, .providers-workspace > *, .shell_content section, .core-console-root, .acc-root, .acc-section { min-width: 0; }
    /* :not(#ui-none) lifts these rules above any class-based table styling
       (the console's and AbstractCore's) without !important. */
    table.ui-stacked:not(#ui-none), table.ui-stacked:not(#ui-none) > tbody { display: block; width: 100%; }
    table.ui-stacked:not(#ui-none) > thead { display: none; }
    table.ui-stacked:not(#ui-none) > tbody > tr { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 190px), 1fr)); gap: 10px 18px; padding: 14px 14px; border-bottom: 1px solid var(--ui-border-1); }
    table.ui-stacked:not(#ui-none) > tbody > tr > td { display: block; min-width: 0; width: auto !important; padding: 0; border: 0; white-space: normal; text-align: left; background: transparent; }
    table.ui-stacked:not(#ui-none) > tbody > tr > td::before { content: attr(data-label); display: block; margin-bottom: 3px; font-size: 10.5px; font-weight: 650; letter-spacing: .05em; text-transform: uppercase; color: var(--text-muted); }
    table.ui-stacked:not(#ui-none) > tbody > tr > td:not([data-label])::before, table.ui-stacked:not(#ui-none) > tbody > tr > td[data-label=""]::before { display: none; }
    table.ui-stacked:not(#ui-none) > tbody > tr > td:first-child, table.ui-stacked:not(#ui-none) > tbody > tr > td[colspan], table.ui-stacked:not(#ui-none) > tbody > tr > td[data-ui-role="actions"] { grid-column: 1 / -1; }
    table.ui-stacked:not(#ui-none) > tbody > tr > td:first-child::before { display: none; }
    table.ui-stacked:not(#ui-none) > tbody > tr > td:empty { display: none; }
    table.ui-stacked:not(#ui-none) > tbody > tr > td[data-ui-role="actions"] .actions, table.ui-stacked:not(#ui-none) > tbody > tr > td[data-ui-role="actions"] .acc-actions { flex-wrap: wrap; }
    table.ui-stacked:not(#ui-none) td code, table.ui-stacked:not(#ui-none) .ui-ellip { max-width: 100%; }
    .acc-root table.ui-stacked:not(#ui-none) > tbody > tr.acc-group-start { border-top: 1px solid var(--ui-border-2); }

    /* Resources: the meters and the memory breakdown use the card's width
       (they were capped at 640 px inside 1500 px cards). */
    .meter-stack, .mem-breakdown { max-width: min(100%, 1080px); }

    /* ---- AbstractCore's embedded Models screen: host-level polish ---- */
    .acc-root label { display: inline-flex; align-items: center; gap: 6px; margin: 0; text-transform: none; letter-spacing: 0; font-size: var(--font-size-sm); font-weight: 500; color: var(--text-secondary); }
    .acc-root .acc-toolbar { gap: 10px 16px; }
    .acc-root .acc-toolbar select { width: auto; min-width: 140px; }
    .acc-root .acc-toolbar input[type=search] { flex: 1 1 280px; min-width: min(280px, 100%); width: auto; }
    .acc-root .acc-btn { min-height: 32px; }
    .acc-root .acc-job pre { white-space: pre-wrap; overflow-wrap: anywhere; overflow-x: hidden; }
    .acc-root .acc-job-title { overflow-wrap: normal; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
    .acc-root .acc-jobs { grid-template-columns: repeat(auto-fill, minmax(min(100%, 340px), 1fr)); }
    .acc-root .acc-host-line { font-size: var(--font-size-md); }
    .acc-root td > span.acc-sub:only-child { display: inline-block; max-width: 44ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; overflow-wrap: normal; }
    table.ui-stacked td > span.acc-sub:only-child { max-width: 100%; }
    .acc-root [data-acc="catalog-table"] td:nth-child(4), .acc-root td.acc-num { white-space: nowrap; }

    /* ---- Mission L2: downloads group, stalls, locations, logs, runtime, network ---- */
    .ui-stall { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; padding: 6px 10px; border-radius: var(--radius-md); border: 1px solid var(--warning-border, var(--ui-border-2)); background: var(--warning-subtle, var(--ui-surface-1)); color: var(--warning); font-size: var(--font-size-sm); }
    .ui-stall strong { font-weight: 700; }
    .ui-stall span::before { content: "\00B7"; margin-right: 8px; opacity: .7; }
    .ui-stall-inline { color: var(--warning); }
    .ui-dl-group .ui-mark { font-size: 18px; }
    .ui-dl-count { color: var(--text-secondary); font-size: var(--font-size-sm); }
    .ui-dl-children { list-style: none; margin: 0; padding: 0; display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(min(100%, 260px), 1fr)); }
    .ui-dl-child { display: grid; gap: 6px; min-width: 0; padding: 10px 12px; border-radius: var(--radius-md); border: 1px solid var(--ui-border-1); background: var(--ui-surface-1); }
    .ui-dl-child[data-state="stalled"] { border-color: var(--warning-border, var(--ui-border-2)); }
    .ui-dl-child[data-state="failed"] { border-color: var(--error-border, var(--ui-border-2)); }
    .ui-dl-child[data-state="stalled"] .ui-progress__bar > span { background: var(--warning); }
    .ui-dl-child[data-state="failed"] .ui-progress__bar > span { background: var(--error); }
    .ui-dl-child[data-state="done"] .ui-progress__bar > span { background: var(--success); }
    .ui-dl-child__head { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-width: 0; }
    .ui-dl-child__name { font-size: var(--font-size-sm); font-weight: 600; color: var(--text-primary); max-width: 100%; min-width: 0; }
    .ui-dl-child__meta { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; color: var(--text-secondary); font-size: var(--font-size-xs); font-variant-numeric: tabular-nums; min-height: 30px; }
    .ui-dl-child__meta .ui-btn { margin-left: auto; }
    .ui-choice-list { margin: 0; padding: 0; list-style: none; display: grid; gap: 8px; color: var(--text-secondary); font-size: var(--font-size-sm); line-height: 1.45; }
    .ui-choice-list li { display: grid; gap: 4px; padding: 8px 10px; border-radius: var(--radius-md); background: var(--bg-card, var(--bg-secondary)); border: 1px solid var(--ui-border-1); }
    .ui-choice-list b { color: var(--text-primary); }
    .ui-working::before { content: ""; display: inline-block; width: 8px; height: 8px; margin-right: 8px; border-radius: 999px; background: var(--info); animation: ui-pulse 1.2s ease-in-out infinite; vertical-align: middle; }
    .ui-btn[aria-busy="true"] { cursor: progress; opacity: .85; }
    .ui-logpanel { display: grid; gap: 8px; min-width: 0; padding: 12px; border-radius: var(--radius-md); border: 1px solid var(--ui-border-1); background: var(--ui-surface-1); }
    .ui-logpanel__head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; font-size: var(--font-size-sm); }
    .ui-logpanel__head .ui-spacer { flex: 1 1 auto; }
    .ui-logpanel__foot { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; color: var(--text-secondary); font-size: var(--font-size-xs); min-width: 0; }
    .ui-logpanel__foot code { max-width: 100%; }
    .ui-log.ui-log--tall { margin: 0; max-height: 360px; }
    /* An open log needs the row's full width: its card spans the grid. */
    .ui-card-grid > .ui-card:has(.ui-logpanel) { grid-column: 1 / -1; }
    .ui-runtime-row { gap: 10px; padding-block: 14px; }
    .ui-runtime-row__main { display: flex; flex-wrap: wrap; align-items: center; gap: 12px 16px; min-width: 0; }
    .ui-runtime-row__text { flex: 1 1 320px; min-width: 0; display: grid; gap: 4px; }
    .ui-runtime-row__text .ui-card__titles { min-height: 0; }
    .ui-runtime-row__actions { display: flex; flex-wrap: wrap; gap: 8px; margin-left: auto; }
    #network-root, .ui-net-root { display: grid; gap: 18px; min-width: 0; }
    .first-run-network { display: grid; gap: 18px; min-width: 0; padding-top: 6px; border-top: 1px solid var(--ui-border-1); }
    .ui-seg { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 190px), 1fr)); gap: 4px; padding: 4px; border: 1px solid var(--ui-border-2); border-radius: var(--radius-lg); background: var(--ui-surface-1); }
    .ui-seg__opt { display: grid; align-content: start; gap: 6px; min-width: 0; min-height: 0; padding: 12px 14px; border: 0; border-radius: var(--radius-md); background: transparent; color: var(--text-secondary); text-align: left; font-weight: 500; white-space: normal; box-shadow: none; cursor: pointer; }
    .ui-seg__opt:hover:not(:disabled) { background: var(--ui-surface-2); filter: none; }
    .ui-seg__opt.is-on { background: var(--accent-subtle); box-shadow: inset 0 0 0 2px var(--accent); color: var(--text-primary); }
    .ui-seg__opt:disabled { cursor: default; opacity: 1; }
    .ui-seg__opt:disabled:not(.is-on) { opacity: .7; }
    .ui-seg__title { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; font-size: var(--font-size-md); font-weight: 650; color: var(--text-primary); }
    .ui-seg__title::before { content: ""; width: 14px; height: 14px; flex: 0 0 14px; border-radius: 999px; border: 2px solid var(--ui-border-2); background: var(--bg-primary); }
    .ui-seg__opt.is-on .ui-seg__title::before { border-color: var(--accent); background: radial-gradient(circle, var(--accent) 0 3px, var(--bg-primary) 4px); }
    .ui-seg__lock { font-size: var(--font-size-xs); font-weight: 600; color: var(--warning); padding: 1px 8px; border-radius: 999px; border: 1px solid var(--warning-border, var(--ui-border-2)); background: var(--warning-subtle, transparent); }
    .ui-seg__text { font-size: var(--font-size-sm); line-height: 1.45; color: var(--text-secondary); }
    .ui-addr-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }
    .ui-addr { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; min-width: 0; padding: 10px 14px; border-radius: var(--radius-md); border: 1px solid var(--ui-border-1); background: var(--bg-card, var(--bg-secondary)); }
    .ui-addr.is-primary { border-color: var(--accent-border, var(--ui-border-2)); box-shadow: inset 3px 0 0 var(--accent); }
    .ui-addr__text { flex: 1 1 280px; min-width: 0; display: grid; gap: 3px; }
    .ui-addr__label { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; font-size: var(--font-size-xs); font-weight: 650; letter-spacing: .04em; text-transform: uppercase; color: var(--text-muted); }
    .ui-addr__text code { font-size: var(--font-size-md); color: var(--text-primary); background: transparent; border: 0; padding: 0; box-shadow: none; }
    .ui-addr__note { font-size: var(--font-size-xs); color: var(--text-secondary); line-height: 1.4; }
    .ui-addr__side { display: flex; align-items: center; gap: 10px; margin-left: auto; }
    .ui-addr__copy { min-width: 72px; }
    .ui-warn-list { margin: 8px 0 0; padding-left: 20px; display: grid; gap: 6px; color: var(--text-secondary); font-size: var(--font-size-sm); line-height: 1.5; }
    .ui-net-confirm { border-color: var(--warning-border, var(--ui-border-2)); background: var(--warning-subtle, var(--ui-surface-1)); }
    /* ---- Network: Advanced reverse proxy (mission Z) ---- */
    details.ui-net-proxy { border: 1px solid var(--ui-border-1); border-radius: var(--radius-lg); background: var(--bg-card, var(--bg-secondary)); padding: 0; min-width: 0; }
    details.ui-net-proxy > summary { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; width: 100%; padding: 14px 16px; font-size: var(--font-size-md); color: var(--text-primary); }
    details.ui-net-proxy > summary .ui-net-proxy__title { font-weight: 650; }
    details.ui-net-proxy > summary .ui-net-proxy__sum { color: var(--text-secondary); font-size: var(--font-size-sm); font-weight: 500; }
    details.ui-net-proxy > summary .ui-pill { margin-left: auto; }
    .ui-net-proxy__body { display: grid; gap: 14px; padding: 4px 16px 16px; border-top: 1px solid var(--ui-border-1); min-width: 0; }
    .ui-net-proxy__grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr)); gap: 16px; min-width: 0; }
    .ui-net-proxy__field { display: grid; align-content: start; gap: 10px; min-width: 0; padding-top: 14px; }
    .ui-net-proxy__head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; }
    .ui-net-proxy__head h4 { margin: 0; font-size: var(--font-size-md); font-weight: 650; color: var(--text-primary); }
    .ui-net-proxy__text { margin: 0; color: var(--text-secondary); font-size: var(--font-size-sm); line-height: 1.5; }
    .ui-net-proxy__danger { margin: 0; display: flex; gap: 8px; align-items: baseline; color: var(--warning); font-size: var(--font-size-sm); line-height: 1.45; }
    .ui-net-proxy__danger::before { content: "!"; flex: 0 0 16px; height: 16px; display: inline-grid; place-items: center; border-radius: 999px; border: 1px solid var(--warning-border, var(--ui-border-2)); font-size: 11px; font-weight: 800; }
    .ui-chips { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 8px; min-width: 0; }
    .ui-chip { display: inline-flex; align-items: center; gap: 4px; max-width: 100%; min-width: 0; padding: 3px 4px 3px 12px; border-radius: 999px; border: 1px solid var(--ui-border-2); background: var(--ui-surface-1); color: var(--text-primary); font-family: var(--font-mono); font-size: var(--font-size-sm); }
    .ui-chip.is-warn { border-color: var(--warning-border, var(--ui-border-2)); background: var(--warning-subtle, var(--ui-surface-1)); }
    .ui-chip.is-muted { color: var(--text-secondary); padding-right: 12px; border-style: dashed; }
    .ui-chip__text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
    .ui-chip__x { display: inline-grid; place-items: center; width: 22px; height: 22px; min-height: 0; padding: 0; border: 0; border-radius: 999px; background: transparent; color: var(--text-secondary); font-size: 15px; line-height: 1; box-shadow: none; cursor: pointer; }
    .ui-chip__x:hover:not(:disabled) { background: var(--ui-surface-3); color: var(--text-primary); filter: none; }
    .ui-chip__x:focus-visible { outline: 2px solid var(--info); outline-offset: 1px; box-shadow: none; }
    .ui-net-proxy__add { display: flex; flex-wrap: wrap; gap: 8px; min-width: 0; }
    .ui-net-proxy__add input { flex: 1 1 240px; min-width: 0; margin: 0; font-family: var(--font-mono); }
    .ui-net-proxy__add input[aria-invalid="true"] { border-color: var(--error); box-shadow: 0 0 0 1px var(--error); }
    .ui-field-msg { margin: 0; font-size: var(--font-size-sm); line-height: 1.45; }
    .ui-field-msg.tone-err { color: var(--error); }
    .ui-field-msg.tone-warn { color: var(--warning); }
    .ui-net-proxy__switch { font-size: var(--font-size-md); color: var(--text-primary); font-weight: 600; }
    .ui-net-proxy__switch input { width: 38px; height: 22px; }
    .ui-net-proxy__switch input::after { width: 16px; height: 16px; }
    .ui-net-proxy__switch input:checked::after { transform: translateX(16px); }
    .ui-net-proxy__saved { margin: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; padding-top: 12px; border-top: 1px solid var(--ui-border-1); color: var(--text-secondary); font-size: var(--font-size-sm); }
    .ui-net-proxy__saved b { color: var(--text-primary); }
    .ui-net-proxy__saved.tone-ok b { color: var(--success); }
    .ui-net-proxy__saved.tone-warn b { color: var(--warning); }
    .ui-net-proxy__saved.tone-err b { color: var(--error); }
    .ui-net-proxy .ui-alert { font-size: var(--font-size-sm); }
    /* ---- Apps settings (apps.* runtime-config keys, mission Z) ---- */
    .ui-apps-settings__rows { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 340px), 1fr)); gap: 14px 18px; min-width: 0; }
    .ui-apps-setting { display: grid; align-content: start; gap: 6px; min-width: 0; }
    .ui-apps-setting__head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; }
    .ui-apps-setting__head label { margin: 0; font-size: var(--font-size-md); font-weight: 650; color: var(--text-primary); text-transform: none; letter-spacing: 0; }
    .ui-apps-setting input { width: 100%; min-width: 0; margin: 0; font-family: var(--font-mono); }
    #island-address { font-family: var(--font-mono); font-size: var(--font-size-sm); max-width: 26ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    @media (max-width: 1100px) { #island-address { display: none; } }
    /* Focus: every control the layer draws shows where the keyboard is. */
    .ui-btn:focus-visible, .ui-seg__opt:focus-visible, .first-run-step:focus-visible, details.ui-details > summary:focus-visible, .ui-log:focus-visible, a.ui-link:focus-visible { outline: 2px solid var(--info); outline-offset: 2px; box-shadow: none; }
    .ui-seg__opt:focus-visible { outline-offset: 1px; }
    #first-run-step-title:focus { outline: none; }

    /* ---- First run: a full-page guided flow ---- */
    .first-run-page { position: fixed; inset: 0; z-index: 800; display: flex; background: var(--bg-primary); color: var(--text-primary); }
    .first-run-shell { display: grid; grid-template-columns: clamp(248px, 19vw, 320px) minmax(0, 1fr); width: 100%; height: 100%; min-height: 0; }
    .first-run-rail { display: flex; flex-direction: column; gap: 22px; min-height: 0; overflow-y: auto; padding: 26px 18px 18px; background: var(--bg-secondary); border-right: 1px solid var(--ui-border-1); }
    .first-run-brand { display: flex; align-items: center; gap: 10px; padding: 0 8px; font-weight: 700; font-size: var(--font-size-md); letter-spacing: .01em; }
    .first-run-brand .shell_brand_mark { font-size: 20px; }
    .first-run-intro { display: grid; gap: 6px; padding: 0 8px; }
    .first-run-intro h2 { font-size: calc(19px * var(--font-scale)); font-weight: 700; letter-spacing: -.01em; }
    .first-run-intro p { color: var(--text-secondary); font-size: var(--font-size-sm); line-height: 1.5; }
    .first-run-steps { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
    .first-run-step { display: grid; grid-template-columns: 30px minmax(0, 1fr); column-gap: 12px; align-items: center; width: 100%; min-height: 0; padding: 10px 10px; border-radius: var(--radius-md); background: transparent; color: var(--text-secondary); text-align: left; font-weight: 500; white-space: normal; box-shadow: none; }
    .first-run-step:hover:not(:disabled) { background: var(--ui-surface-2); filter: none; }
    .first-run-step__num { grid-row: span 2; width: 30px; height: 30px; border-radius: 999px; display: grid; place-items: center; font-size: 12px; font-weight: 700; border: 1px solid var(--ui-border-2); color: var(--text-secondary); background: var(--ui-surface-1); }
    .first-run-step__title { color: var(--text-primary); font-weight: 600; font-size: var(--font-size-md); }
    .first-run-step__hint { font-size: var(--font-size-xs); color: var(--text-muted); line-height: 1.35; }
    .first-run-step.is-active { background: var(--accent-subtle); }
    .first-run-step.is-active .first-run-step__num { background: var(--accent); border-color: var(--accent); color: #fff; }
    .first-run-step.is-done .first-run-step__num { color: var(--success); border-color: var(--success-border, var(--ui-border-2)); background: var(--success-subtle, transparent); }
    .first-run-rail-foot { margin-top: auto; display: grid; gap: 10px; padding: 12px 8px 0; border-top: 1px solid var(--ui-border-1); }
    .first-run-rail-foot .subtle { font-size: var(--font-size-xs); color: var(--text-muted); line-height: 1.45; }
    .first-run-main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
    .first-run-scroll { flex: 1 1 auto; min-height: 0; overflow-y: auto; overflow-x: hidden; }
    .first-run-content { width: 100%; max-width: 1560px; margin: 0 auto; padding: clamp(24px, 4vh, 44px) clamp(20px, 3.4vw, 60px) 36px; display: grid; gap: 24px; }
    .first-run-head { display: grid; gap: 8px; max-width: 920px; }
    .first-run-kicker { color: var(--accent); font-size: var(--font-size-xs); font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
    .first-run-head h3 { font-size: calc(26px * var(--font-scale)); font-weight: 700; letter-spacing: -.02em; line-height: 1.2; }
    .first-run-head p { color: var(--text-secondary); font-size: calc(15px * var(--font-scale)); line-height: 1.55; }
    .first-run-panel { display: grid; gap: 22px; min-width: 0; border: 0; background: transparent; padding: 0; box-shadow: none; border-radius: var(--radius-sm); }
    .first-run-panel.hidden { display: none !important; }
    .first-run-footer { flex: 0 0 auto; display: flex; align-items: center; gap: 10px; padding: 14px clamp(20px, 3.4vw, 60px); border-top: 1px solid var(--ui-border-1); background: var(--bg-secondary); }
    .first-run-footer .message { flex: 1 1 auto; margin: 0; min-width: 0; font-size: var(--font-size-md); }
    .first-run-footer button { min-width: 96px; }
    .first-run-footer #first-run-skip { background: transparent; color: var(--text-secondary); box-shadow: none; min-width: 0; }
    .first-run-footer #first-run-skip:hover:not(:disabled) { color: var(--text-primary); background: var(--ui-surface-2); }
    .first-run-footer #first-run-next, .first-run-footer #first-run-finish { background: var(--accent); color: #fff; }
    #first-run-host-summary, #first-run-engines-body, #first-run-apps-body, #first-run-done-body, #first-run-model-recommended, #engines-core-root, #apps-root { display: grid; gap: 18px; min-width: 0; }
    .first-run-tiles { display: grid; gap: 14px; margin: 0; grid-template-columns: repeat(auto-fill, minmax(max(240px, calc((100% - 28px) / 3)), 1fr)); }
    .first-run-tile code { font-weight: 500; font-size: var(--font-size-sm); }
    .first-run-tile { display: grid; gap: 6px; align-content: start; padding: 16px 18px; min-width: 0; border-radius: var(--radius-lg); border: 1px solid var(--ui-border-1); background: var(--bg-card, var(--bg-secondary)); }
    .first-run-tile dt { color: var(--text-muted); font-size: var(--font-size-xs); font-weight: 650; letter-spacing: .06em; text-transform: uppercase; }
    .first-run-tile dd { margin: 0; font-size: calc(15px * var(--font-scale)); font-weight: 600; color: var(--text-primary); min-width: 0; }
    .first-run-tile .ui-sub { color: var(--text-secondary); font-size: var(--font-size-sm); font-weight: 400; }
    .first-run-note { margin: 0; color: var(--text-secondary); line-height: 1.5; }
    .first-run-default-bar { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; padding: 12px 16px; border-radius: var(--radius-lg); border: 1px dashed var(--ui-border-2); color: var(--text-secondary); }
    .first-run-default-bar code { max-width: min(100%, 48ch); }
    #first-run-model-catalog .acc-root { display: grid; gap: 4px; }
    .first-run-table { width: 100%; border-collapse: collapse; }
    @media (max-width: 1100px) {
      .first-run-shell { grid-template-columns: 216px minmax(0, 1fr); }
      .first-run-step__hint { display: none; }
      .first-run-intro p { display: none; }
    }
    @media (max-width: 860px) {
      .first-run-shell { grid-template-columns: 1fr; grid-template-rows: auto minmax(0, 1fr); }
      .first-run-rail { flex-direction: row; align-items: center; gap: 12px; padding: 10px 14px; overflow-x: auto; overflow-y: hidden; border-right: 0; border-bottom: 1px solid var(--ui-border-1); }
      .first-run-intro, .first-run-rail-foot { display: none; }
      .first-run-steps { grid-auto-flow: column; }
      .first-run-step { grid-template-columns: 26px auto; padding: 6px 8px; }
      .first-run-step__num { width: 26px; height: 26px; grid-row: auto; }
    }
"""

CONSOLE_UI_JS = r"""
    // ================= Console UI layer (mission L) =================
    // Layout behavior for console_ui.CONSOLE_UI_CSS: the Advanced switch,
    // responsive tables, ellipsis+copy, progress markup, the engines cards,
    // the kit islands (top bar + appearance). Same scope as the console.
    const UI_ADVANCED_KEY = "abstractgateway_show_advanced_v1";
    function uiShowAdvanced() {
      try { return String(document.body.className || "").split(/\s+/).includes("show-advanced"); } catch { return false; }
    }
    function uiSetAdvanced(on) {
      const next = !!on;
      try { document.body.classList.toggle("show-advanced", next); } catch { /* no body (tests) */ }
      writeStringSetting(UI_ADVANCED_KEY, next ? "1" : "0");
      for (const id of ["first-run-advanced", "sidebar-advanced"]) {
        const box = $(id);
        if (box) box.checked = next;
      }
      // The Apps cards and the Done step's terminal note RENDER their
      // technical parts only when the switch is on (mission GG): re-render.
      try { appRender(); consoleTuiRender(); } catch { /* not initialised yet */ }
    }
    function uiInitAdvanced() {
      uiSetAdvanced(readStringSetting(UI_ADVANCED_KEY, "0") === "1");
      for (const id of ["first-run-advanced", "sidebar-advanced"]) {
        const box = $(id);
        if (box) box.onchange = () => uiSetAdvanced(!!box.checked);
      }
    }
    function uiNum(n) { return typeof n === "number" && isFinite(n); }
    // Progress sizes in DECIMAL units (MB, GB): the gateway's job messages
    // ("Downloading Ollama (50 of 120 MB)") use them, so the bar's numbers
    // and the sentence under it agree.
    function uiBytes(n) {
      if (!uiNum(n)) return "";
      const units = ["B", "kB", "MB", "GB", "TB"];
      let v = n; let i = 0;
      while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
      return i === 0 ? `${Math.round(v)} B` : `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
    }
    function uiDuration(s) {
      if (!uiNum(s) || s < 0) return "";
      if (s < 60) return `${Math.max(1, Math.round(s))} s`;
      if (s < 3600) return `${Math.round(s / 60)} min`;
      const h = Math.floor(s / 3600);
      const m = Math.round((s - h * 3600) / 60);
      return m ? `${h} h ${m} min` : `${h} h`;
    }
    function uiPill(label, tone, title) {
      return `<span class="ui-pill tone-${esc(tone || "muted")}"${title ? ` title="${esc(title)}"` : ""}>${esc(label)}</span>`;
    }
    // ---- Progress: one renderer for every download/install job ----
    // Reads the host_job_v1 progress contract (state, bytes_done/bytes_total,
    // bytes_per_second, eta_s, files, current_file, size_unknown, size_note,
    // message) and the older gateway job fields (downloaded_bytes,
    // total_bytes, percent, message) -- whichever the job carries.
    function uiJobBytes(job) {
      const j = job || {};
      const done = uiNum(j.bytes_done) ? j.bytes_done : (uiNum(j.downloaded_bytes) ? j.downloaded_bytes : null);
      const total = uiNum(j.bytes_total) && j.bytes_total > 0 ? j.bytes_total : (uiNum(j.total_bytes) && j.total_bytes > 0 ? j.total_bytes : null);
      return { done, total };
    }
    function uiJobPercent(job) {
      const j = job || {};
      if (uiNum(j.percent)) return Math.max(0, Math.min(100, j.percent));
      const { done, total } = uiJobBytes(j);
      if (uiNum(done) && uiNum(total) && total > 0) return Math.max(0, Math.min(100, (done / total) * 100));
      return null;
    }
    function uiJobPhase(job) {
      const j = job || {};
      const st = String(j.state || "").toLowerCase();
      if (st) return st === "done" ? "done" : st;
      const s = String(j.status || "").toLowerCase();
      if (s === "completed") return "done";
      if (s === "failed" || s === "cancelled" || s === "queued") return s;
      return "running";
    }
    const UI_PHASE_LABELS = {
      queued: "Waiting to start", resolving: "Preparing", downloading: "Downloading", verifying: "Checking files",
      installing: "Installing", running: "Working", stalled: "Stalled", done: "Done", failed: "Failed", cancelled: "Cancelled",
    };
    function uiProgressMarkup(job, label) {
      const j = job || {};
      const phase = uiJobPhase(j);
      const pct = uiJobPercent(j);
      const active = !["done", "failed", "cancelled"].includes(phase);
      const indeterminate = pct === null && active;
      const { done, total } = uiJobBytes(j);
      const meta = [];
      if (uiNum(done) && uiNum(total)) meta.push(`${uiBytes(done)} of ${uiBytes(total)}`);
      else if (uiNum(done) && done > 0) meta.push(`${uiBytes(done)}${j.size_unknown ? " (total size unknown)" : ""}`);
      if (active && uiNum(j.bytes_per_second) && j.bytes_per_second > 0) meta.push(`${uiBytes(j.bytes_per_second)}/s`);
      if (active && uiNum(j.eta_s) && j.eta_s > 0) meta.push(`about ${uiDuration(j.eta_s)} left`);
      // A stall is its own line, in the warning tone, above the bar's numbers:
      // the download is alive and retrying, not failed (N: `stalled` turns
      // back to `downloading` by itself when data flows again).
      const stall = phase === "stalled"
        ? `<div class="ui-stall" role="status"><strong>Stalled</strong><span>${esc(uiNum(j.stalled_for_s) ? `no data for ${uiDuration(j.stalled_for_s)}` : "no data right now")}</span><span>still trying</span></div>`
        : "";
      const files = Array.isArray(j.files) ? j.files : [];
      const fileRows = files.map((f) => {
        const fd = uiNum(f && f.bytes_done) ? f.bytes_done : null;
        const ft = uiNum(f && f.bytes_total) && f.bytes_total > 0 ? f.bytes_total : null;
        const fp = uiNum(fd) && uiNum(ft) ? Math.max(0, Math.min(100, (fd / ft) * 100)) : (String((f && f.state) || "") === "done" ? 100 : 0);
        const size = uiNum(fd) && uiNum(ft) ? `${uiBytes(fd)} / ${uiBytes(ft)}` : (uiNum(ft) ? uiBytes(ft) : String((f && f.state) || ""));
        return `<li><span class="ui-ellip" title="${esc((f && f.name) || "")}">${esc((f && f.name) || "file")}</span><span>${esc(size)}</span>`
          + `<div class="ui-progress__bar"><span style="width:${fp.toFixed(1)}%"></span></div></li>`;
      }).join("");
      const doneFiles = files.filter((f) => String((f && f.state) || "") === "done").length;
      const message = String(j.message || "").trim();
      return `<div class="ui-progress" data-state="${esc(phase)}" role="group" aria-label="${esc(label || "Progress")}">`
        + `<div class="ui-progress__head"><span class="ui-progress__label">${esc(label || UI_PHASE_LABELS[phase] || "Working")}</span>`
        + `<span class="ui-progress__pct">${pct === null ? esc(UI_PHASE_LABELS[phase] || "") : `${pct.toFixed(pct < 10 ? 1 : 0)}%`}</span></div>`
        + `<div class="ui-progress__bar${indeterminate ? " is-indeterminate" : ""}" role="progressbar" aria-label="${esc(label || UI_PHASE_LABELS[phase] || "Progress")}" aria-valuemin="0" aria-valuemax="100"${pct === null ? "" : ` aria-valuenow="${pct.toFixed(0)}"`}><span style="width:${pct === null ? 0 : pct.toFixed(1)}%"></span></div>`
        + stall
        + (meta.length ? `<div class="ui-progress__meta">${meta.map((m) => `<span>${esc(m)}</span>`).join("")}</div>` : "")
        + (message ? `<div class="ui-progress__msg">${esc(message)}</div>` : "")
        + (j.size_note ? `<div class="ui-progress__msg">${esc(j.size_note)}</div>` : "")
        + (files.length ? `<details class="ui-details"><summary>Files (${doneFiles} of ${files.length} done)</summary><ul class="ui-files">${fileRows}</ul></details>` : "")
        + `</div>`;
    }
    // A reload must not lose a running download's bar: re-attach to every
    // running job this gateway knows (GET /models/downloads, newest first),
    // the "Download all" parent (`kind: download_group`) included.
    async function uiRestoreDownloads() {
      let res = null;
      try { res = await api("/api/gateway/models/downloads"); } catch { return; }
      for (const job of (res && Array.isArray(res.jobs) ? res.jobs : [])) {
        if (!job || !dlJobId(job) || !dlActive(job)) continue;
        const known = dlFind(dlJobId(job));
        if (known && dlActive(known)) continue;
        trackDownloadJob(job);
      }
    }

    // ---- Downloads: one live feed (mission L2) ----
    // N's contract (docs/model-downloads.md): GET /models/downloads/stream is
    // SSE (`event: downloads`, data {jobs:[...]}, on every change, <= 0.5 s),
    // the same dicts as the polling routes; parents (`kind:
    // download_group`, `grp_...`) are listed with their `children`, and each
    // child names its `parent_job`. The stream is the primary feed; polling
    // (GET /models/download/{id}, 1.5 s) runs whenever the stream is not
    // OPEN (not yet connected, reconnecting, refused, no EventSource), so a
    // dropped stream never strands a bar mid-way.
    const dlFeed = { es: null, errors: 0, disabled: false, ids: new Set(), group: null, finished: new Set(), cancelling: new Set(), polling: new Set(), lastEventAt: 0, streamError: "" };
    function dlJobId(job) { return String((job && (job.job || job.job_id)) || ""); }
    function dlActive(job) { return !!job && (job.status === "running" || job.status === "queued"); }
    function dlStreamOpen() { return !!(dlFeed.es && dlFeed.es.readyState === 1); }
    function dlFind(id) {
      if (!id) return null;
      if (dlFeed.group && dlJobId(dlFeed.group) === id) return dlFeed.group;
      for (const j of state.downloadJobs.values()) if (dlJobId(j) === id) return j;
      return null;
    }
    function dlAnyActive() {
      if (dlActive(dlFeed.group)) return true;
      for (const j of state.downloadJobs.values()) if (dlActive(j)) return true;
      return false;
    }
    function dlRender() {
      if (Array.isArray(state.defaults) && state.defaults.length) renderDefaultRows(state.defaults);
      renderFirstRunModel();
      mcOnDownloads();  // the catalog cards' rows (console_catalog.py)
    }
    // Store one job (a group stores its children too). `ended` collects the
    // jobs that went from active to finished with this update.
    function dlApply(job, ended) {
      if (!job || typeof job !== "object") return;
      const id = dlJobId(job);
      if (!id) return;
      const prev = dlFind(id);
      if (job.kind === "download_group") {
        dlFeed.group = job;
        for (const child of (Array.isArray(job.children) ? job.children : [])) {
          if (child && typeof child === "object") dlApply(Object.assign({}, child, { parent_job: id }), ended);
        }
      } else {
        const key = downloadJobKey(job.provider, job.artifact);
        const known = state.downloadJobs.get(key);
        // An older finished job for the same model never hides the live one.
        if (known && dlJobId(known) !== id && dlActive(known) && !dlActive(job)) return;
        state.downloadJobs.set(key, job);
      }
      dlFeed.ids.add(id);
      if (prev && dlActive(prev) && !dlActive(job) && ended) ended.push(job);
    }
    function dlFinished(job) {
      const id = dlJobId(job);
      if (!id || dlFeed.finished.has(id)) return;
      dlFeed.finished.add(id);
      dlFeed.cancelling.delete(id);
      if (job.kind !== "download_group" && job.status === "failed") {
        const result = job.result || {};
        const msg = $("defaults-message");
        if (msg) {
          msg.textContent = `${job.provider} ${job.artifact}: ${job.message || "download failed"}${result.instruction ? " — " + result.instruction : ""}`;
          msg.className = "message error";
        }
      }
      clearTimeout(dlFinished.timer);
      dlFinished.timer = setTimeout(() => { refreshAvailability(); }, 250);
    }
    function dlIngest(jobs) {
      // Oldest first, so the newest job per model wins.
      const list = (Array.isArray(jobs) ? jobs : []).slice().reverse();
      const ended = [];
      for (const job of list) {
        const id = dlJobId(job);
        if (!id) continue;
        if (!dlFeed.ids.has(id) && !dlActive(job)) continue;  // history this page never showed
        dlApply(job, ended);
      }
      ended.forEach(dlFinished);
    }
    function dlCloseFeed() {
      if (dlFeed.es) { try { dlFeed.es.close(); } catch { /* closed */ } }
      dlFeed.es = null;
    }
    function dlEnsureFeed() {
      if (dlFeed.es || dlFeed.disabled) return;
      if (typeof EventSource !== "function") { dlFeed.disabled = true; dlFeed.streamError = "this browser has no EventSource"; return; }
      let es = null;
      try { es = new EventSource("/api/gateway/models/downloads/stream", { withCredentials: true }); }
      catch (err) { dlFeed.disabled = true; dlFeed.streamError = String((err && err.message) || err); return; }
      dlFeed.es = es;
      es.addEventListener("downloads", (ev) => {
        dlFeed.errors = 0;
        dlFeed.lastEventAt = Date.now();
        let data = null;
        try { data = JSON.parse(ev.data); } catch { return; }
        dlIngest(data && data.jobs);
        dlRender();
        if (!dlAnyActive()) dlCloseFeed();
      });
      es.addEventListener("error", (ev) => {
        // `event: error` with data = the gateway said what broke (the stream
        // then ends); a bare error = the connection dropped (the browser
        // reconnects by itself; polling covers the gap). Three drops in a
        // row without one update: polling alone, and the reason is kept.
        let said = "";
        try { said = ev && ev.data ? String((JSON.parse(ev.data) || {}).message || "") : ""; } catch { said = String(ev.data || ""); }
        dlFeed.errors += 1;
        if (said) dlFeed.streamError = said;
        if (said || dlFeed.errors >= 3) {
          if (!said) dlFeed.streamError = "the live download stream kept dropping";
          dlFeed.disabled = true;
          dlCloseFeed();
          console.warn(`AbstractGateway console: download stream off (${dlFeed.streamError}); polling instead.`);
        }
      });
    }
    function dlTrack(job) {
      const id = dlJobId(job);
      if (!id) return;
      const ended = [];
      dlApply(job, ended);
      ended.forEach(dlFinished);
      dlRender();
      if (dlActive(job)) {
        dlEnsureFeed();
        dlPoll(id);
      } else {
        dlFinished(job);
      }
    }
    async function dlPoll(jobId) {
      if (!jobId || dlFeed.polling.has(jobId)) return;
      dlFeed.polling.add(jobId);
      try {
        for (;;) {
          await new Promise((resolve) => setTimeout(resolve, 1500));
          let job = null;
          if (dlStreamOpen()) {
            job = dlFind(jobId);        // the stream keeps it current
            if (!job) return;
          } else {
            let res = null;
            try {
              res = await api(`/api/gateway/models/download/${encodeURIComponent(jobId)}`);
            } catch (err) {
              // 404 after a Gateway restart: jobs are in-process. The weights
              // may well have landed, so re-probe rather than report a failure
              // we cannot substantiate.
              await refreshAvailability();
              return;
            }
            job = (res && res.job) || null;
            if (!job) return;
            const ended = [];
            dlApply(job, ended);
            ended.forEach(dlFinished);
            dlRender();
          }
          if (!dlActive(job)) { dlFinished(job); return; }
        }
      } finally {
        dlFeed.polling.delete(jobId);
      }
    }
    async function dlCancel(jobId, button) {
      if (!jobId) return;
      dlFeed.cancelling.add(jobId);
      if (button) { button.disabled = true; button.textContent = "Cancelling..."; }
      try {
        // slow: the cancel goes through the core host, one call per running
        // member of a `grp_…` group, on a host that is busy downloading; the
        // 60 s default would report a failure the gateway did not have.
        const res = await api(`/api/gateway/models/download/${encodeURIComponent(jobId)}/cancel`, { slow: true, method: "POST", body: JSON.stringify({}) });
        const ended = [];
        dlApply(res && res.job, ended);
        ended.forEach(dlFinished);
      } catch (err) {
        dlFeed.cancelling.delete(jobId);
        const msg = $("first-run-message");
        if (msg) { msg.textContent = `Could not cancel: ${String((err && err.message) || err)}`; msg.className = "message error"; }
      }
      dlRender();
    }
    const DL_STATE_PILLS = {
      queued: ["Waiting", "info tone-busy"], resolving: ["Preparing", "info tone-busy"], downloading: ["Downloading", "info tone-busy"],
      verifying: ["Checking files", "info tone-busy"], installing: ["Installing", "info tone-busy"], stalled: ["Stalled", "warn"],
      done: ["Ready", "ok"], failed: ["Failed", "err"], cancelled: ["Cancelled", "muted"], pending: ["Waiting", "muted"],
    };
    function dlStatePill(job) {
      const phase = uiJobPhase(job);
      if (dlFeed.cancelling.has(dlJobId(job)) && dlActive(job)) return uiPill("Cancelling", "warn tone-busy");
      const [label, tone] = DL_STATE_PILLS[phase] || [UI_PHASE_LABELS[phase] || phase || "Working", "info"];
      return uiPill(label, tone);
    }
    // The "Download all" parent: ONE card with the overall bar (bytes, speed,
    // ETA, N's own sentence), then one row per model with its own bar and
    // Cancel, and "Cancel all" for the parent (POST .../grp_.../cancel
    // cancels every running child server-side).
    function dlGroupMarkup(group) {
      const g = group || {};
      const id = dlJobId(g);
      const children = Array.isArray(g.children) ? g.children.filter((c) => c && typeof c === "object") : [];
      const active = dlActive(g);
      const cancelling = dlFeed.cancelling.has(id) && active;
      const rows = children.map((c) => {
        const cid = dlJobId(c);
        const live = dlFind(cid) || c;
        const provider = state.providerLabels.get(live.provider) || live.provider || "";
        const name = `${provider} ${live.artifact || ""}`.trim();
        const pct = uiJobPercent(live);
        const { done, total } = uiJobBytes(live);
        const size = uiNum(done) && uiNum(total) ? `${uiBytes(done)} of ${uiBytes(total)}` : (uiNum(done) && done > 0 ? uiBytes(done) : "");
        const phase = uiJobPhase(live);
        const stall = phase === "stalled" ? `<span class="ui-stall-inline">no data for ${esc(uiDuration(live.stalled_for_s) || "a while")}</span>` : "";
        const canCancel = dlActive(live) && !dlFeed.cancelling.has(cid);
        return `<li class="ui-dl-child" data-dl-child="${esc(cid)}" data-state="${esc(phase)}">`
          + `<div class="ui-dl-child__head"><span class="ui-ellip ui-dl-child__name" title="${esc(name)}">${esc(name)}</span>${dlStatePill(live)}</div>`
          + `<div class="ui-progress__bar${pct === null && dlActive(live) ? " is-indeterminate" : ""}" role="progressbar" aria-label="${esc(name)}" aria-valuemin="0" aria-valuemax="100"${pct === null ? "" : ` aria-valuenow="${pct.toFixed(0)}"`}><span style="width:${pct === null ? 0 : pct.toFixed(1)}%"></span></div>`
          + `<div class="ui-dl-child__meta"><span>${esc(size)}</span>${stall}${pct === null ? "" : `<span>${esc(pct.toFixed(pct < 10 ? 1 : 0))}%</span>`}`
          + (canCancel ? `<button type="button" class="ui-btn is-quiet ui-dl-cancel" data-dl-cancel="${esc(cid)}">Cancel</button>` : "")
          + `</div>`
          + (live.message && !dlActive(live) && phase !== "done" ? `<div class="ui-progress__msg">${esc(live.message)}</div>` : "")
          + `</li>`;
      }).join("");
      const doneCount = children.filter((c) => uiJobPhase(dlFind(dlJobId(c)) || c) === "done").length;
      const head = uiProgressMarkup(Object.assign({}, g, { files: [] }), "All models");
      const phase = uiJobPhase(g);
      const title = { done: "The recommended models are ready", failed: "Some recommended models did not download", cancelled: "Download all was cancelled" }[phase] || "Downloading the recommended models";
      const errorBox = g.error && !active ? `<details class="ui-details"><summary>Show details</summary><pre class="ui-log">${esc(g.error)}</pre></details>` : "";
      return `<article class="ui-card ui-dl-group${active ? " is-busy" : (g.state === "failed" ? " is-attention" : "")}" data-dl-group="${esc(id)}">`
        + `<div class="ui-card__head"><span class="ui-mark" aria-hidden="true">&#8595;</span><div class="ui-card__titles"><div class="ui-card__title">${esc(title)}</div>`
        + `<span class="ui-card__status">${cancelling ? uiPill("Cancelling", "warn tone-busy") : dlStatePill(g)}</span></div></div>`
        + head
        + (/\bready\b/.test(String(g.message || "")) ? "" : `<div class="ui-sub ui-dl-count">${esc(`${doneCount} of ${children.length} ready`)}</div>`)
        + (rows ? `<ul class="ui-dl-children">${rows}</ul>` : "")
        + errorBox
        + (active ? `<div class="ui-card__actions"><button type="button" class="ui-btn is-ghost ui-dl-cancel" data-dl-cancel="${esc(id)}"${cancelling ? " disabled" : ""}>${cancelling ? "Cancelling..." : "Cancel all"}</button></div>` : "")
        + `</article>`;
    }
    // ---- Toast + ellipsis copy ----
    function uiToast(text) {
      let el = $("ui-toast");
      if (!el && typeof document.createElement === "function" && document.body && typeof document.body.append === "function") {
        el = document.createElement("div");
        el.id = "ui-toast";
        el.setAttribute("role", "status");
        document.body.append(el);
      }
      if (!el) return;
      el.textContent = text;
      el.classList.add("is-on");
      clearTimeout(uiToast.timer);
      uiToast.timer = setTimeout(() => el.classList.remove("is-on"), 1600);
    }
    function uiCopy(text) {
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(() => uiToast("Copied"), () => uiToast("Select the text to copy it"));
          return;
        }
      } catch { /* fall through */ }
      uiToast("Select the text to copy it");
    }
    const UI_ELLIP_SELECTOR = "td code, .ui-ellip, .acc-root td code, .acc-root td > span.acc-sub:only-child";
    const UI_NO_STACK = ".entity-matrix, .af-matrix, [data-ui-no-stack]";
    function uiMarkTruncation(root) {
      if (!root || typeof root.querySelectorAll !== "function") return;
      root.querySelectorAll(UI_ELLIP_SELECTOR).forEach((el) => {
        const full = String(el.textContent || "").trim();
        if (!full) return;
        const cut = el.scrollWidth > el.clientWidth + 1;
        el.classList.toggle("is-truncated", cut);
        if (cut) { if (!el.title) { el.title = full; el.dataset.uiTitle = "1"; } }
        else if (el.dataset.uiTitle === "1") { el.removeAttribute("title"); delete el.dataset.uiTitle; }
      });
    }
    // A single long token in a cell (an id, a model name, a path, a URL) is
    // ellipsized with its full value on hover -- never broken at hyphens or
    // characters (ADR-0026). Prose (anything with spaces) wraps normally.
    const UI_TOKEN_SKIP = /(^|\s)(ui-ellip|badge|state-pill|pill|acc-badge|acc-chip|ui-pill|chip)(\s|$)/;
    function uiEllipTokens(root) {
      const cells = root.tagName === "TD" ? [root] : Array.from(root.querySelectorAll("td"));
      cells.forEach((td) => {
        if (td.closest(UI_NO_STACK)) return;
        const targets = td.childElementCount === 0 ? [td] : Array.from(td.children).filter((c) => c.childElementCount === 0 && (c.tagName === "DIV" || c.tagName === "SPAN"));
        for (const el of targets) {
          if (UI_TOKEN_SKIP.test(String(el.className || ""))) continue;
          const text = String(el.textContent || "").trim();
          if (text.length < 8 || /\s/.test(text) || !/[\/\-_.:]/.test(text)) continue;
          if (el === td) td.innerHTML = `<span class="ui-ellip">${esc(text)}</span>`;
          else el.classList.add("ui-ellip");
        }
      });
    }
    // ---- Responsive tables ----
    function uiLabelTable(table) {
      const head = table.tHead && table.tHead.rows && table.tHead.rows[0];
      if (!head) return;
      const labels = [];
      for (const th of Array.from(head.cells)) {
        const text = String(th.textContent || "").trim();
        const span = Math.max(1, Number(th.colSpan) || 1);
        for (let i = 0; i < span; i++) labels.push(text);
      }
      for (const body of Array.from(table.tBodies || [])) {
        for (const tr of Array.from(body.rows)) {
          let col = 0;
          for (const td of Array.from(tr.cells)) {
            const label = labels[col] || "";
            if (td.dataset.label !== label) td.dataset.label = label;
            const role = /^(actions?)?$/i.test(label) && col === labels.length - 1 ? "actions" : "";
            if (role) td.dataset.uiRole = role; else if (td.dataset.uiRole) delete td.dataset.uiRole;
            col += Math.max(1, Number(td.colSpan) || 1);
          }
        }
      }
    }
    function uiFitTable(table) {
      const box = table.parentElement;
      if (!box || !box.clientWidth) return;  // hidden tab: measured when shown
      const avail = box.clientWidth;
      if (table.classList.contains("ui-stacked")) {
        const natural = Number(table.dataset.uiNatural || 0);
        if (natural && natural <= avail) {
          table.classList.remove("ui-stacked");
          if (table.scrollWidth > box.clientWidth + 1) {
            table.dataset.uiNatural = String(table.scrollWidth);
            table.classList.add("ui-stacked");
          }
        }
      } else if (table.scrollWidth > avail + 1) {
        table.dataset.uiNatural = String(table.scrollWidth);
        table.classList.add("ui-stacked");
      }
    }
    const uiTables = { resize: null, seen: new WeakSet(), scheduled: false };
    function uiEnhance() {
      uiTables.scheduled = false;
      if (typeof document.querySelectorAll !== "function") return;
      uiEllipTokens(document);
      document.querySelectorAll("table").forEach((table) => {
        if (table.closest && table.closest(UI_NO_STACK)) return;
        uiLabelTable(table);
        if (!uiTables.seen.has(table)) {
          uiTables.seen.add(table);
          if (uiTables.resize && table.parentElement) uiTables.resize.observe(table.parentElement);
        }
        uiFitTable(table);
      });
      uiMarkTruncation(document);
    }
    function uiScheduleEnhance() {
      if (uiTables.scheduled) return;
      uiTables.scheduled = true;
      (typeof requestAnimationFrame === "function" ? requestAnimationFrame : (fn) => setTimeout(fn, 16))(uiEnhance);
    }
    function uiInitLayout() {
      if (typeof MutationObserver !== "function" || typeof document.querySelectorAll !== "function") return;
      if (typeof ResizeObserver === "function") uiTables.resize = new ResizeObserver(() => uiScheduleEnhance());
      // Token cells are ellipsized in the observer itself (a microtask, before
      // the next paint), so a re-rendered row never shows one hyphen-wrapped
      // frame; the measuring pass (stacking) follows on the next frame.
      new MutationObserver((records) => {
        const seen = new Set();
        for (const r of records) {
          const t = r.target && r.target.nodeType === 1 ? r.target : (r.target && r.target.parentElement);
          const host = t && (t.tagName === "TD" ? t : (t.closest ? t.closest("td") : null)) || t;
          if (host && !seen.has(host) && typeof host.querySelectorAll === "function") { seen.add(host); uiEllipTokens(host); }
        }
        uiScheduleEnhance();
      }).observe(document.body, { childList: true, subtree: true, characterData: true });
      if (typeof window !== "undefined" && window.addEventListener) window.addEventListener("resize", uiScheduleEnhance);
      document.addEventListener("click", (event) => {
        const el = event.target && event.target.closest ? event.target.closest(".is-truncated") : null;
        if (!el) return;
        if (event.target.closest("button, a, input, select, textarea, [data-acc-action]")) return;
        uiCopy(String(el.getAttribute("title") || el.textContent || "").trim());
      });
      uiScheduleEnhance();
    }

    // ---- Local engines as cards (wizard step + Engines tab) ----
    // Contract gateway_engines_v2 (routes/engines.py, docs/engines.md):
    // GET /engines?probe=1 -> {schema, engines:[row], install_allowed, ...};
    // row.actions [{id: install|open_page|recheck|start|stop|docs, label,
    // enabled, reason, method, path, url}], row.active_job; jobs
    // (engine_install_job_v1) on /engines/jobs/{id}, with
    // /continue {approve_admin|install_tools|recheck} and /cancel. The
    // job's `message` is already one plain sentence; `details` is the log.
    const ENGINE_MARKS = { ollama: "Ol", lmstudio: "LM", mlx: "MLX", llamacpp: "cpp", vllm: "vL", huggingface: "HF" };
    const ENGINE_DOWNLOAD_LINKS = [
      { id: "ollama", name: "Ollama", url: "https://ollama.com/download" },
      { id: "lmstudio", name: "LM Studio", url: "https://lmstudio.ai/download" },
    ];
    const ENGINE_WAITING = ["needs_admin", "needs_tools"];
    const engineStore = { data: null, error: "", loading: false, jobs: new Map(), confirm: "", views: new Map(), polling: false, message: "", busy: new Set(), pending: new Map(), notices: new Map(), plans: new Map() };
    // Where an app engine (Ollama, LM Studio on macOS) goes: the row is
    // planned with `location: auto`, so the console asks the gateway for the
    // two real plans (POST /engines/{id}/install {dry_run: true, location:
    // user|system}; a dry run runs nothing and needs no install permission)
    // and offers them truthfully: "Install" = just for you (~/Applications,
    // never a password), "Install for all users" = /Applications, marked
    // "(administrator)" only when that plan says `needs_admin`.
    async function engineLoadPlans(id) {
      const cur = engineStore.plans.get(id);
      if (cur && (cur.loading || cur.user)) return;
      engineStore.plans.set(id, { loading: true });
      engineRender();
      const one = (location) => api(`/api/gateway/engines/${encodeURIComponent(id)}/install`, { slow: true, method: "POST", body: JSON.stringify({ dry_run: true, location }) });
      try {
        const [user, system] = await Promise.all([one("user"), one("system")]);
        if (!user || !user.plan || !system || !system.plan) throw new Error("the dry run returned no `plan`");
        engineStore.plans.set(id, { user: user.plan, system: system.plan });
      } catch (err) {
        engineStore.plans.set(id, { error: String((err && err.message) || err), data: err && err.data });
      }
      engineRender();
    }
    function engineLocationChoice(e) {
      const p = engineStore.plans.get(e.id);
      if (!p || p.loading) return `<p class="ui-card__note ui-working">Checking where ${esc(e.name || e.id)} can go on this computer...</p>`;
      if (p.error) return `<div class="ui-alert tone-warn"><strong>Could not check the install locations.</strong><span>${esc(p.error)}</span>`
        + `<span>Install still works: the gateway picks /Applications when your account can write it, else your own Applications folder.</span></div>`;
      const sys = p.system || {};
      const user = p.user || {};
      return `<ul class="ui-choice-list">`
        + `<li><b>Install</b> puts it in your own Applications folder: only your account sees it, no password needed.<code class="ui-advanced ui-ellip is-block" title="${esc(user.target || "")}">${esc(user.target || "")}</code></li>`
        + `<li><b>${esc(sys.needs_admin ? "Install for all users (administrator)" : "Install for all users")}</b> puts it in /Applications for every account on this computer.`
        + (sys.needs_admin ? ` ${esc(sys.admin_reason || "Your account cannot write there, so an administrator password is asked first.")}` : "")
        + `<code class="ui-advanced ui-ellip is-block" title="${esc(sys.target || "")}">${esc(sys.target || "")}</code></li></ul>`;
    }
    function engineLocationButtons(e) {
      const p = engineStore.plans.get(e.id);
      if (!p || p.loading) return engineBtn("install-go", e, "Install", true, ' data-location="user" disabled');
      if (p.error) return engineBtn("install-go", e, "Install", true, ' data-location="auto"');
      const sys = p.system || {};
      return engineBtn("install-go", e, "Install", true, ' data-location="user"')
        + engineBtn("install-go", e, sys.needs_admin ? "Install for all users (administrator)" : "Install for all users", false, ' data-location="system"');
    }
    function engineNoticeMarkup(e) {
      const n = engineStore.notices.get(e.id);
      if (!n) return "";
      return `<div class="ui-alert tone-${esc(n.tone || "info")}"${n.tone === "err" ? ' role="alert"' : ' role="status"'}><strong>${esc(n.text)}</strong>${n.hint ? `<span>${esc(n.hint)}</span>` : ""}</div>`
        + (n.details ? `<details class="ui-details"><summary>Show details</summary><pre class="ui-log">${esc(n.details)}</pre></details>` : "");
    }
    // The full reason behind a failed call: the gateway's own words, then
    // the whole response body (never a tail) for "Show details".
    function uiErrorDetails(err) {
      const data = err && err.data;
      if (!data || typeof data !== "object") return "";
      const d = data.detail && typeof data.detail === "object" ? data.detail : data;
      const parts = [];
      for (const k of ["details", "hint", "fix", "log", "log_tail"]) {
        const v = d[k];
        if (typeof v === "string" && v.trim()) parts.push(v.trim());
        else if (Array.isArray(v) && v.length) parts.push(v.join("\n"));
      }
      parts.push(JSON.stringify(data, null, 2));
      return parts.join("\n\n");
    }
    function engineById(id) { return ((engineStore.data && engineStore.data.engines) || []).find((e) => e && e.id === id) || null; }
    function engineJobActive(job) { return !!job && ["queued", "downloading", "installing", "needs_admin", "needs_tools"].includes(String(job.state || "")); }
    function engineJobOf(e) {
      const tracked = engineStore.jobs.get(e.id);
      return tracked || e.active_job || null;
    }
    function engineAction_(e, id) { return (Array.isArray(e.actions) ? e.actions : []).find((a) => a && a.id === id) || null; }
    function engineState(e, job) {
      if (job && job.state === "needs_admin") return { key: "waiting", pill: uiPill("Needs your approval", "warn") };
      if (job && job.state === "needs_tools") return { key: "waiting", pill: uiPill("Needs Apple tools", "warn") };
      if (engineJobActive(job)) return { key: "installing", pill: uiPill("Installing", "info tone-busy") };
      if (e.supported === false) return { key: "unsupported", pill: uiPill("Not for this computer", "muted") };
      if (e.installed !== true) return { key: "absent", pill: uiPill("Not installed", "muted") };
      if (e.running === null || e.running === undefined) return { key: "ready", pill: uiPill("Ready", "ok") };
      if (e.running === true && e.reachable !== false) return { key: "running", pill: uiPill("Running", "ok") };
      if (e.running === true) return { key: "starting", pill: uiPill("Not answering", "warn") };
      return { key: "stopped", pill: uiPill("Installed, stopped", "warn") };
    }
    function engineBtn(action, e, text, primary, extra) {
      const busy = engineStore.busy.has(e.id);
      // The clicked button names what is happening ("Starting...") until the
      // gateway answers; the others wait disabled.
      const p = busy ? engineStore.pending.get(e.id) : null;
      const mine = p && p.action === action && (!p.location || String(extra || "").includes(`data-location="${p.location}"`));
      // Callers append a bare " disabled" last (never inside a quoted value).
      const off = / disabled$/.test(String(extra || ""));
      const attrs = String(extra || "").replace(/ disabled$/, "");
      return `<button type="button" class="ui-btn ${primary ? "is-primary" : "is-ghost"}" data-engine-action="${esc(action)}" data-engine="${esc(e.id)}"${attrs}${busy || off ? " disabled" : ""}${mine ? ' aria-busy="true"' : ""}>${esc(mine ? p.label : text)}</button>`;
    }
    function engineCardMarkup(e) {
      const job = engineJobOf(e);
      const view = engineState(e, job);
      const install = (e.install && typeof e.install === "object") ? e.install : {};
      const admin = !!(state.principal && state.principal.admin);
      const act = (id) => engineAction_(e, id);
      const facts = [];
      if (e.version) facts.push(`<li>Version <b>${esc(e.version)}</b></li>`);
      if (uiNum(e.models_count)) facts.push(`<li><b>${esc(e.models_count)}</b> model${e.models_count === 1 ? "" : "s"}</li>`);
      if (e.base_url) facts.push(`<li class="ui-advanced">Address <code class="ui-ellip" title="${esc(e.base_url)}">${esc(e.base_url)}</code></li>`);
      if (e.install_location) facts.push(`<li class="ui-advanced">Location <code class="ui-ellip" title="${esc(e.install_location)}">${esc(e.install_location)}</code></li>`);
      let body = "";
      let actions = "";
      const failed = job && ["failed", "cancelled"].includes(String(job.state || "")) && e.installed !== true;
      if (job && job.state === "needs_admin") {
        const p = job.admin_prompt || {};
        body += `<div class="ui-alert tone-warn" role="alert"><strong>${esc(job.message || p.reason || "This step needs an administrator.")}</strong>`
          + (p.command ? `<span>It will run, as administrator:</span><code class="ui-ellip is-block" title="${esc(p.command)}">${esc(p.command)}</code>` : "")
          + (p.where ? `<span>${esc(p.where)}</span>` : "") + `</div>`;
        if (admin && (job.continue_actions || []).includes("approve_admin")) actions += engineBtn("continue:approve_admin", e, p.button || "Continue with administrator password", true);
        if (admin && (job.continue_actions || []).includes("recheck")) actions += engineBtn("continue:recheck", e, "Re-check", false);
      } else if (job && job.state === "needs_tools") {
        const p = job.tools_prompt || {};
        const a = p.action || {};
        body += `<div class="ui-alert tone-warn" role="alert"><strong>${esc(job.message || p.reason || "This install needs extra tools.")}</strong>`
          + (p.started ? `<span>Apple's installer is open on this computer's screen; this continues by itself when it finishes.</span>` : "") + `</div>`;
        if (admin && (job.continue_actions || []).includes("install_tools") && a.available !== false) actions += engineBtn("continue:install_tools", e, a.button || "Install tools", true);
        if (admin && (job.continue_actions || []).includes("recheck")) actions += engineBtn("continue:recheck", e, "Re-check", false);
      } else if (view.key === "installing") {
        body += uiProgressMarkup(job, `Installing ${e.name || e.id}`);
      } else if (view.key === "unsupported") {
        body += `<p class="ui-card__note">${esc(e.support_reason || "This engine does not run on this computer.")}</p>`;
      } else if (engineStore.confirm === e.id) {
        const steps = Array.isArray(install.steps) ? install.steps : [];
        body += `<div class="ui-confirm"><p>Install ${esc(e.name || e.id)} on <strong>${esc(CORE_CONSOLE.hostName || "this computer")}</strong>?</p>`
          + (install.notes && install.method !== "app" ? `<p class="ui-card__note">${esc(install.notes)}</p>` : "")
          + (steps.length && install.method !== "app" ? `<p class="ui-card__note">${steps.map((x) => esc(x)).join(" · ")}</p>` : "")
          + (install.needs_admin && install.method !== "app" ? `<p class="ui-card__note">${esc(install.admin_reason || "One step needs an administrator password; you will be asked first.")}</p>` : "")
          + (install.method === "app" ? engineLocationChoice(e) : "")
          + (Array.isArray(install.command_preview) && install.command_preview.length && install.method !== "app" ? `<div class="ui-advanced"><pre class="ui-log">${esc(install.command_preview.join("\n"))}</pre></div>` : "")
          + `<div class="ui-card__actions">${install.method === "app" ? engineLocationButtons(e) : engineBtn("install-go", e, install.needs_admin ? "Install (administrator)" : "Install now", true, ' data-location="auto"')}${engineBtn("install-cancel", e, "Not now", false)}</div></div>`;
      } else {
        if (failed) {
          const msg = (job.error && job.error.message) || job.message || "The install did not finish.";
          body += `<div class="ui-alert tone-err" role="alert"><strong>${esc(msg)}</strong></div>`;
        }
        const inst = act("install");
        if (inst) {
          const title = !admin ? "Only an admin can install engines" : (inst.enabled ? `Install ${e.name || e.id} on this computer` : (inst.reason || "Installing is not available right now"));
          actions += engineBtn("install", e, failed ? "Try again" : (install.needs_admin ? "Install (administrator)" : "Install"), true, ` title="${esc(title)}"${admin && inst.enabled ? "" : " disabled"}`);
          if (!inst.enabled && inst.reason) body += `<p class="ui-card__note">${esc(inst.reason)}</p>`;
        }
        const start = act("start");
        if (start && view.key !== "running") actions += engineBtn("start", e, start.label || "Start", !inst, ` title="${esc(start.reason || `Start ${e.name || e.id}`)}"${admin && start.enabled ? "" : " disabled"}`);
        if ((view.key === "running" || view.key === "ready" || view.key === "starting") && e.installed) actions += engineBtn("models", Object.assign({}, e, { id: e.provider || e.id }), "Browse models", false);
        const stop = act("stop");
        if (stop) actions += engineBtn("stop", e, stop.label || "Stop", false, ` title="${esc(stop.reason || `Stop ${e.name || e.id}`)}"${admin && stop.enabled ? "" : " disabled"}`);
        const page = act("open_page");
        if (page && page.url && (!inst || !inst.enabled)) actions += `<a class="ui-btn is-ghost" href="${esc(page.url)}" target="_blank" rel="noopener noreferrer">Download page</a>`;
        if (page && act("recheck") && (!inst || !inst.enabled)) actions += engineBtn("refresh", e, "I installed it, check again", false);
        if (view.key === "stopped" && !start) body += `<p class="ui-card__note">Installed but not running. Start it from its app.</p>`;
        if (view.key === "ready") body += `<p class="ui-card__note">Built in: models run inside the gateway when a workflow needs them.</p>`;
      }
      if (job && job.can_cancel && engineJobActive(job) && admin) actions += engineBtn("cancel", e, "Cancel", false);
      const log = job ? String(job.details || (Array.isArray(job.log_tail) ? job.log_tail.join("\n") : "") || "") : "";
      const details = log ? `<details class="ui-details"><summary>Show details</summary><pre class="ui-log">${esc(log)}</pre></details>` : "";
      const docs = act("docs");
      const learn = docs && docs.url ? `<span class="ui-spacer"></span><a class="ui-link" href="${esc(docs.url)}" target="_blank" rel="noopener noreferrer">Learn more</a>` : "";
      const tone = view.key === "installing" ? " is-busy" : (failed || view.key === "waiting" ? " is-attention" : "");
      return `<article class="ui-card engine-card${tone}" data-engine-card="${esc(e.id)}">`
        + `<div class="ui-card__head"><span class="ui-mark" aria-hidden="true">${esc(ENGINE_MARKS[e.id] || String(e.name || e.id || "?").slice(0, 2))}</span>`
        + `<div class="ui-card__titles"><div class="ui-card__title">${esc(e.name || e.id)}</div>`
        + `<span class="ui-card__status">${view.pill}</span></div></div>`
        + `<div class="ui-card__blurb">${esc(e.description || "")}</div>`
        // Mission GG: the same five rows as the app cards (head, blurb, body,
        // action row, technical) so `.is-aligned` puts every action row of a
        // grid row at the same level, even a "Learn more"-only one.
        + `<div class="ui-card__body">${facts.length ? `<ul class="ui-facts">${facts.join("")}</ul>` : ""}${engineNoticeMarkup(e)}${body}${details}</div>`
        + `<div class="ui-card__actions">${actions}${learn}</div>`
        + `<div class="ui-card__tech"></div>`
        + `</article>`;
    }
    function engineViewMarkup() {
      if (engineStore.error && !engineStore.data) {
        // The gateway could not report its engines: say so plainly, and still
        // offer the two desktop engines' own downloads (never a dead end).
        const upgrade = CORE_CONSOLE.available ? "" : ` ${coreConsoleUnavailableText()}`;
        const links = ENGINE_DOWNLOAD_LINKS.map((d) => `<article class="ui-card"><div class="ui-card__head"><span class="ui-mark" aria-hidden="true">${esc(ENGINE_MARKS[d.id] || "")}</span>`
          + `<div class="ui-card__titles"><div class="ui-card__title">${esc(d.name)}</div></div></div>`
          + `<div class="ui-card__actions"><a class="ui-btn is-ghost" href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">Download ${esc(d.name)}</a></div></article>`).join("");
        return `<div class="ui-alert tone-warn" role="alert"><strong>This gateway cannot list its engines right now.</strong><span>${esc(engineStore.error)}${esc(upgrade)}</span></div>`
          + `<div class="ui-card-grid">${links}</div>`;
      }
      if (!engineStore.data) return `<div class="ui-empty">Looking at this computer's engines...</div>`;
      const engines = (engineStore.data.engines || []).filter(Boolean);
      const rank = (e) => (e.supported === false ? 3 : e.installed === true ? 0 : 1);
      const ordered = engines.slice().sort((a, b) => rank(a) - rank(b));
      const installed = engines.filter((e) => e.installed === true).length;
      const running = engines.filter((e) => e.running === true).length;
      const when = engineStore.data.generated_at ? new Date(engineStore.data.generated_at).toLocaleTimeString() : "";
      const policy = engineStore.data.install_allowed === false
        ? `<div class="ui-alert tone-info"><strong>Installing engines is turned off on this gateway.</strong><span>An admin can allow it in the gateway settings (allow engine install). You can still install an engine yourself and check again.</span></div>` : "";
      const summary = `<div class="ui-toolbar"><span>${installed} of ${engines.length} installed${running ? `, ${running} running` : ""}${when ? ` · checked ${esc(when)}` : ""}</span>`
        + `<button type="button" class="ui-btn is-quiet" data-engine-action="refresh">${engineStore.loading ? "Checking..." : "Check again"}</button>`
        + `<span class="ui-advanced ui-sub">CLI: <code>abstractgateway engines status --probe</code></span></div>`;
      const msg = engineStore.message ? `<div class="ui-alert tone-err" role="alert">${esc(engineStore.message)}</div>` : "";
      return summary + policy + msg + (ordered.length ? `<div class="ui-card-grid is-aligned">${ordered.map(engineCardMarkup).join("")}</div>` : `<div class="ui-empty">No engines reported by this gateway.</div>`);
    }
    function engineRender() {
      for (const el of engineStore.views.values()) {
        if (el) el.innerHTML = engineViewMarkup();
      }
    }
    async function engineRefresh() {
      engineStore.loading = true;
      engineRender();
      try {
        const data = await api("/api/gateway/engines?probe=1", { slow: true });
        if (!data || data.schema !== "gateway_engines_v2") throw new Error(`unexpected engines payload (schema ${String((data && data.schema) || "none")}, expected gateway_engines_v2)`);
        engineStore.data = data;
        engineStore.error = "";
        for (const e of data.engines || []) if (e && e.active_job && !engineStore.jobs.has(e.id)) engineStore.jobs.set(e.id, e.active_job);
      } catch (err) {
        engineStore.error = String((err && err.message) || err);
      }
      engineStore.loading = false;
      engineRender();
      enginePoll();
    }
    async function engineRestoreJobs() {
      // The newest job per engine (a failure stays visible after a reload
      // until the engine is installed or retried).
      let res = null;
      try { res = await api("/api/gateway/engines/jobs"); } catch { return; }
      for (const job of (res && Array.isArray(res.jobs) ? res.jobs : [])) {
        if (!job || !job.engine || engineStore.jobs.has(job.engine)) continue;
        engineStore.jobs.set(job.engine, job);
      }
      engineRender();
      enginePoll();
    }
    async function enginePoll() {
      if (engineStore.polling) return;
      engineStore.polling = true;
      try {
        while (engineStore.views.size && Array.from(engineStore.jobs.values()).some(engineJobActive)) {
          const waiting = Array.from(engineStore.jobs.values()).every((j) => !engineJobActive(j) || ENGINE_WAITING.includes(j.state));
          await new Promise((resolve) => setTimeout(resolve, waiting ? 3000 : 1000));
          let finished = false;
          for (const [engine, job] of Array.from(engineStore.jobs.entries())) {
            if (!engineJobActive(job)) continue;
            try {
              const next = await api(`/api/gateway/engines/jobs/${encodeURIComponent(job.job_id)}`);
              const got = next && next.job_id ? next : ((next && next.job) || job);
              engineStore.jobs.set(engine, got);
              if (!engineJobActive(got)) {
                finished = true;
                const nm = (engineById(engine) || {}).name || engine;
                if (got.state === "done") engineStore.notices.set(engine, { tone: "ok", text: got.message || `${nm} is installed.` });
                if (got.state === "cancelled") engineStore.notices.set(engine, { tone: "info", text: got.message || `The ${nm} install was cancelled.` });
              }
            } catch (err) {
              if (err && err.status === 404) { engineStore.jobs.delete(engine); finished = true; }
            }
          }
          engineRender();
          if (finished) await engineRefresh();
        }
      } finally {
        engineStore.polling = false;
      }
    }
    // Every engine action shows its progress on the clicked button
    // (`pending`) and its result on the card (`notices`): a success sentence,
    // or the gateway's reason with the full response behind "Show details".
    async function enginePost(id, path, body, what, pending) {
      engineStore.busy.add(id);
      if (pending) engineStore.pending.set(id, pending);
      engineStore.notices.delete(id);
      engineStore.message = "";
      engineRender();
      let out = null;
      try {
        out = await api(path, { slow: true, method: "POST", body: JSON.stringify(body || {}) });
      } catch (err) {
        engineStore.notices.set(id, { tone: "err", text: `${what}: ${String((err && err.message) || err)}`, details: uiErrorDetails(err) });
      }
      engineStore.busy.delete(id);
      engineStore.pending.delete(id);
      return out;
    }
    const ENGINE_PENDING = {
      "install-go": "Starting the install...", start: "Starting...", stop: "Stopping...", cancel: "Cancelling...",
      "continue:approve_admin": "Waiting for the password...", "continue:install_tools": "Opening Apple's installer...", "continue:recheck": "Checking...",
    };
    async function engineAction(action, id, button) {
      if (action === "refresh") { engineStore.message = ""; engineStore.notices.clear(); await engineRefresh(); return; }
      // "Browse models": the catalog cards, filtered to this engine's builds.
      if (action === "models") { closeFirstRunWizard(); openCatalogTab(MC_ENGINE_PROVIDER[id] ? { provider: MC_ENGINE_PROVIDER[id] } : {}); return; }
      const e = engineById(id) || { id, name: id };
      if (action === "install") {
        engineStore.confirm = id;
        engineStore.message = "";
        engineStore.notices.delete(id);
        engineRender();
        if (e.install && e.install.method === "app") engineLoadPlans(id);
        return;
      }
      if (action === "install-cancel") { engineStore.confirm = ""; engineRender(); return; }
      const name = e.name || id;
      if (action === "install-go") {
        const location = (button && button.dataset && button.dataset.location) || "auto";
        const job = await enginePost(id, `/api/gateway/engines/${encodeURIComponent(id)}/install`, { dry_run: false, location }, `Could not start installing ${name}`, { action, location, label: ENGINE_PENDING[action] });
        engineStore.confirm = "";
        const tracked = job && job.job_id ? job : ((job && job.job) || null);
        if (tracked) engineStore.jobs.set(id, tracked);
        engineRender();
        enginePoll();
        return;
      }
      if (action === "start" || action === "stop") {
        const res = await enginePost(id, `/api/gateway/engines/${encodeURIComponent(id)}/${action}`, {}, `Could not ${action} ${name}`, { action, label: ENGINE_PENDING[action] });
        if (res) {
          const ok = action === "start" ? res.running !== false : res.running !== true;
          const text = action === "start"
            ? (ok ? `${name} is running.` : `${name} was started but is not answering yet.`)
            : (ok ? `${name} is stopped.` : `${name} is still running.`);
          engineStore.notices.set(id, { tone: ok ? "ok" : "warn", text, hint: res.message || "" });
        }
        await engineRefresh();
        return;
      }
      const job = engineStore.jobs.get(id);
      if (!job) return;
      if (action === "cancel") {
        const next = await enginePost(id, `/api/gateway/engines/jobs/${encodeURIComponent(job.job_id)}/cancel`, {}, "Could not cancel", { action, label: ENGINE_PENDING[action] });
        if (next && next.job_id) engineStore.jobs.set(id, next);
        engineRender();
        enginePoll();
        return;
      }
      if (action.startsWith("continue:")) {
        const next = await enginePost(id, `/api/gateway/engines/jobs/${encodeURIComponent(job.job_id)}/continue`, { action: action.slice("continue:".length) }, "Could not continue", { action, label: ENGINE_PENDING[action] || "Working..." });
        if (next && next.job_id) engineStore.jobs.set(id, next);
        engineRender();
        enginePoll();
      }
    }
    function mountEngineCards(key, el) {
      if (!el) return;
      const fresh = !engineStore.views.has(key);
      engineStore.views.set(key, el);
      el.onclick = (event) => {
        const b = event && event.target && event.target.closest ? event.target.closest("[data-engine-action]") : null;
        if (!b || b.disabled) return;
        engineAction(b.dataset.engineAction, b.dataset.engine || "", b);
      };
      engineRender();
      engineRefresh();
      if (fresh) engineRestoreJobs();
    }
    function unmountEngineCards(key) { engineStore.views.delete(key); }

    // ---- Browser apps as cards (wizard step + Apps tab) ----
    // Contract: GET /api/gateway/apps (apps_manager.overview), POST
    // /apps/{id}/install {launch}, /launch, /stop, /update, /open (one-time
    // signed-in link), jobs on /apps/jobs/{id} (state queued|running|
    // succeeded|failed|cancelled, percent, indeterminate, bytes_done/_total,
    // message, steps, error{reason,message,hint}, details, log_tail).
    const APP_COPY = {
      // One line on the card (ellipsis + the whole sentence on hover).
      flow: { mark: "Fl", blurb: "Design workflows visually and run them here." },
      code: { mark: "Co", blurb: "A coding assistant whose sessions survive restarts." },
      observer: { mark: "Ob", blurb: "Watch runs live, replay them, steer running work." },
      continuum: { mark: "Cn", blurb: "Your backlog, inbox and long-running processes." },
      entity: { mark: "En", blurb: "Create entities and talk with them as they learn." },
    };
    // An app that holds nothing yet on this gateway opens where its first
    // thing is made (mission JJ). Keyed by app id; `when` reads the row's
    // content_summary (apps_manager.content_summary): a count of exactly 0,
    // never an unknown (null) one. `path` rides POST /open {path} and the
    // handover lands the browser there, signed in.
    const APP_FIRST_RUN = {
      entity: { when: (s) => !!s && s.entities_count === 0, label: "Create your first entity", path: "/#new", title: (name) => `Open ${name} on the form that creates your first entity, signed in` },
    };
    function appFirstRun(app) {
      const f = APP_FIRST_RUN[app && app.id];
      return f && f.when(app.content_summary || null) ? f : null;
    }
    const APP_STATUS = {
      not_installed: ["Not installed", "muted"], stopped: ["Installed", "muted"], starting: ["Starting", "info tone-busy"],
      running: ["Running", "ok"], stopping: ["Stopping", "info tone-busy"], crashed: ["Stopped unexpectedly", "err"], crash_loop: ["Keeps crashing", "err"],
    };
    const appStore = { data: null, error: "", loading: false, views: new Map(), jobs: new Map(), polling: false, message: "", busy: new Set(), pending: new Map(), notices: new Map(), logs: new Map() };
    const APP_PENDING = { install: "Starting the install...", launch: "Starting...", stop: "Stopping...", update: "Starting the update...", cancel: "Cancelling...", open: "Opening...", "runtime-install": "Starting...", "tui-open": "Opening the terminal...", "tui-install": "Starting the install...", "tui-cancel": "Cancelling..." };
    const APP_LOG_TAIL = 200;
    const APP_LOG_MAX = 5000;  // the route's own ceiling (routes/apps.py), said out loud in the panel
    function appNoticeMarkup(key) {
      const n = appStore.notices.get(key);
      if (!n) return "";
      return `<div class="ui-alert tone-${esc(n.tone || "info")}"${n.tone === "err" ? ' role="alert"' : ' role="status"'}><strong>${esc(n.text)}</strong>${n.hint ? `<span>${esc(n.hint)}</span>` : ""}${n.cmd ? appCmdLine(n.cmd) : ""}</div>`
        + (n.details ? `<details class="ui-details"><summary>Show details</summary><pre class="ui-log">${esc(n.details)}</pre></details>` : "");
    }
    // ---- Terminal versions (mission Y): interfaces[] on each app row ----
    // {kind: "web"|"tui", installed, version, install_available,
    // install_method: npm|release_binary|cargo, install_blocked_reason,
    // launch_available, launch_blocked_reason, launch_mode: terminal|copy,
    // command, install_command, signin_command, active_job}. Nothing here is
    // shown for an app whose row has no "tui" entry (browser-only apps).
    function appTuiOf(app) {
      return (Array.isArray(app && app.interfaces) ? app.interfaces : []).find((i) => i && i.kind === "tui") || null;
    }
    // One mono line, ellipsis + full value on hover, and Copy: a command is
    // never wrapped (ADR-0026), and never typed by hand.
    function appCmdLine(cmd, label) {
      return `<div class="ui-cmdline"><code class="ui-ellip" title="${esc(cmd)}">${esc(cmd)}</code>`
        + `<button type="button" class="ui-btn is-quiet" data-app-action="tui-copy" data-cmd="${esc(cmd)}" aria-label="${esc(label || `Copy: ${cmd}`)}">Copy</button></div>`;
    }
    // Mission GG (operator, 2026-09-24: "it's so complicated"): a card is
    // icon + name + pill / one line / ONE action row, pinned at the same
    // level across the grid (`.ui-card-grid.is-aligned`: five subgrid rows).
    // The action row holds the primary action for the state and, right next
    // to it, the terminal action. Stop, Show log, Update, versions, commands,
    // the address and the log panel exist only with "Technical details" ON
    // (not rendered at all otherwise; uiSetAdvanced re-renders the cards).
    // Result boxes are transient (APP_NOTICE_MS) unless they report an error.
    const APP_NOTICE_MS = 6000;
    function appNotify(key, notice) {
      const n = Object.assign({}, notice, { at: Date.now() + Math.random() });
      appStore.notices.set(key, n);
      if (n.tone !== "err" && typeof setTimeout === "function") {
        setTimeout(() => {
          if (appStore.notices.get(key) !== n) return;
          appStore.notices.delete(key);
          appRender();
        }, APP_NOTICE_MS);
      }
    }
    function appTechSep(items) {
      return items.filter(Boolean).join(`<span class="ui-card__sep" aria-hidden="true">·</span>`);
    }
    // The terminal version in three parts: the action-row button, what the
    // card's body says (install progress, a failure), and the technical line.
    function appTuiParts(app, techOn) {
      const out = { button: "", body: "", tech: [], lines: "" };
      const t = appTuiOf(app);
      if (!t) return out;
      const admin = !!(state.principal && state.principal.admin);
      const key = `tui:${app.id}`;
      const job = appStore.jobs.get(key) || t.active_job || null;
      const busy = appStore.busy.has(key);
      const pend = busy ? appStore.pending.get(key) : null;
      const name = app.name || app.id;
      const glyph = `<span class="ui-btn__glyph" aria-hidden="true">&gt;_</span>`;
      const btn = (action, text, cls, title, withGlyph) => {
        const mine = pend && pend.action === action;
        return `<button type="button" class="ui-btn ${cls}" data-app-action="${esc(action)}" data-app="${esc(app.id)}" data-app-tui="${esc(app.id)}"${title ? ` title="${esc(title)}"` : ""}${busy ? " disabled" : ""}${mine ? ' aria-busy="true"' : ""}>${withGlyph ? glyph : ""}${esc(mine ? pend.label : text)}</button>`;
      };
      if (appJobActive(job)) {
        out.body += uiProgressMarkup(appJobAsProgress(job), job.title || `Installing ${name} for the terminal`);
        if (admin) out.button = btn("tui-cancel", "Cancel terminal install", "is-quiet", `Stop installing ${name}'s terminal app`);
      } else if (t.installed && t.launch_available) {
        out.button = btn("tui-open", "Open in Terminal", "is-ghost", `The same ${name}, in a terminal window on this computer, signed in`, true);
      } else if (!t.installed && t.install_available) {
        out.button = admin ? btn("tui-install", "Install for Terminal", "is-quiet", `The same ${name}, in a terminal window: a ready-made download from ${name}'s release, checked against its published checksums`)
          : `<button type="button" class="ui-btn is-quiet" disabled title="Only an admin can install apps">Install for Terminal</button>`;
      }
      if (job && job.state === "failed" && !appJobActive(job)) {
        const err = (job.error && typeof job.error === "object") ? job.error : { message: String(job.error || job.message || "") };
        const tail = job.details || (Array.isArray(job.log_tail) ? job.log_tail.join("\n") : "");
        out.body += `<div class="ui-alert tone-err" role="alert"><strong>${esc(err.message || `${name}'s terminal app did not install.`)}</strong>${err.hint ? `<span>${esc(err.hint)}</span>` : ""}</div>`
          + (tail ? `<details class="ui-details"><summary>Show details</summary><pre class="ui-log">${esc(tail)}</pre></details>` : "");
      }
      out.body += appNoticeMarkup(key);
      if (!techOn) return out;
      // Technical details: the terminal version's facts, its update, and the
      // exact commands (one line each, Copy) for every case the plain view
      // leaves out (the Rust toolchain, a browser on another computer).
      if (t.installed && t.version) out.tech.push(`<span>Terminal ${esc(t.version)}</span>`);
      if (t.installed && t.update_available && t.install_available && admin && !appJobActive(job)) out.tech.push(btn("tui-install", t.latest_version ? `Update terminal app to ${t.latest_version}` : "Update terminal app", "is-text", `Install the newest ${name} terminal app`));
      let lines = "";
      if (!t.installed && !t.install_available && t.install_method === "cargo") {
        lines += `<div class="ui-card__techrow is-stacked" data-app-tui-tech="toolchain"><span class="ui-card__techlabel" title="${esc(t.install_blocked_reason || "")}">Terminal version: needs the Rust toolchain</span>${appCmdLine(t.install_command, `Copy the install command for ${name}'s terminal app`)}</div>`;
      } else if (!t.installed && !t.install_available) {
        lines += `<div class="ui-card__techrow"><span class="ui-card__techlabel">Terminal version: ${esc(t.install_blocked_reason || "installing is not available right now")}</span></div>`;
      } else if (t.installed && !t.launch_available) {
        lines += `<div class="ui-card__techrow is-stacked" data-app-tui-tech="remote"><span class="ui-card__techlabel" title="${esc(t.launch_blocked_reason || "")}">Terminal version, on the other computer</span>${appCmdLine(t.command, `Copy the command that opens ${name} in a terminal`)}</div>`;
        if (t.signin_command) lines += `<div class="ui-card__techrow is-stacked"><span class="ui-card__techlabel">First time there, sign in once</span>${appCmdLine(t.signin_command, "Copy the sign-in command")}</div>`;
      } else if (t.installed && t.command) {
        lines += `<div class="ui-card__techrow is-stacked"><span class="ui-card__techlabel">Terminal</span>${appCmdLine(t.command)}</div>`;
      }
      out.lines = lines;
      return out;
    }
    // The console's own terminal app (crates.io only: never installable from
    // here), said once on the guide's Done step: one quiet line; the install
    // and open commands only with "Technical details" ON.
    let consoleTuiState = { el: null, ct: null };
    function consoleTuiMarkup(ct) {
      if (!ct || !ct.command) return "";
      const techOn = uiShowAdvanced();
      const state_ = ct.installed ? (ct.version ? `installed, ${ct.version}` : "installed") : "needs the Rust toolchain";
      const line = `<p class="ui-console-tui__line">This console also exists as a terminal app${techOn ? ` <span class="ui-sub">(${esc(state_)}; it asks for your admin token on its Connection screen)</span>` : ""}.</p>`;
      const cmds = techOn
        ? (ct.installed ? "" : appCmdLine(ct.install_command, "Copy the install command")) + appCmdLine(ct.command, "Copy the command that opens the terminal console")
        : "";
      return `<div class="ui-console-tui" role="group" data-console-tui aria-label="Terminal console">${line}${cmds}</div>`;
    }
    function consoleTuiRender() {
      const { el, ct } = consoleTuiState;
      if (el) el.innerHTML = consoleTuiMarkup(ct);
    }
    async function mountConsoleTuiNote(el) {
      if (!el) return;
      el.onclick = (event) => {
        const b = event && event.target && event.target.closest ? event.target.closest("[data-app-action='tui-copy']") : null;
        if (b) uiCopy(b.dataset.cmd || "");
      };
      consoleTuiState = { el, ct: (appStore.data && appStore.data.console_tui) || null };
      consoleTuiRender();
      if (consoleTuiState.ct) return;
      try {
        const d = await api("/api/gateway/apps?latest=false");
        consoleTuiState = { el, ct: (d && d.console_tui) || null };
        consoleTuiRender();
      } catch { el.innerHTML = ""; }
    }
    // "Show log": GET /apps/{id}/logs?tail=N -> {path, lines}. The panel says
    // how much it shows ("last 200 lines" / "the whole log, 37 lines") and
    // "Show more" asks for 4x as many, up to the route's 5000-line ceiling,
    // where it names the log file for the rest (ADR-0026: never a silent cut).
    function appLogMarkup(app) {
      const L = appStore.logs.get(app.id);
      if (!L || !L.open) return "";
      const lines = Array.isArray(L.lines) ? L.lines : [];
      let head;
      if (L.loading && !lines.length) head = "Reading the log...";
      else if (L.error) head = "Could not read the log";
      else if (!lines.length) head = "The log is empty";
      else if (lines.length < L.tail) head = `The whole log · ${lines.length} line${lines.length === 1 ? "" : "s"}`;
      else head = `The last ${lines.length} lines`;
      const more = !L.error && lines.length >= L.tail && L.tail < APP_LOG_MAX;
      const capped = !L.error && lines.length >= APP_LOG_MAX;
      return `<section class="ui-logpanel" data-app-log="${esc(app.id)}" aria-label="${esc(app.name || app.id)} log">`
        + `<div class="ui-logpanel__head"><strong>${esc(head)}</strong><span class="ui-spacer"></span>`
        + `<button type="button" class="ui-btn is-quiet" data-app-action="log-refresh" data-app="${esc(app.id)}"${L.loading ? " disabled" : ""}>${L.loading ? "Reading..." : "Refresh"}</button>`
        + (more ? `<button type="button" class="ui-btn is-quiet" data-app-action="log-more" data-app="${esc(app.id)}"${L.loading ? " disabled" : ""}>Show more</button>` : "")
        + (lines.length ? `<button type="button" class="ui-btn is-quiet" data-app-action="log-copy" data-app="${esc(app.id)}">Copy</button>` : "")
        + `</div>`
        + (L.error ? `<div class="ui-alert tone-err" role="alert"><strong>${esc(L.error)}</strong></div>` : "")
        + (lines.length ? `<pre class="ui-log ui-log--tall" tabindex="0">${esc(lines.join("\n"))}</pre>` : "")
        + (capped || L.path ? `<div class="ui-logpanel__foot">${capped ? `<span>Older lines are in the log file.</span>` : ""}${L.path ? `<span>Log file</span><code class="ui-ellip" title="${esc(L.path)}">${esc(L.path)}</code>` : ""}</div>` : "")
        + `</section>`;
    }
    async function appLoadLog(id, tail) {
      const L = appStore.logs.get(id) || { open: true, tail: APP_LOG_TAIL, lines: [] };
      L.open = true;
      L.loading = true;
      L.tail = Math.min(APP_LOG_MAX, tail || L.tail || APP_LOG_TAIL);
      appStore.logs.set(id, L);
      appRender();
      try {
        const res = await api(`/api/gateway/apps/${encodeURIComponent(id)}/logs?tail=${L.tail}`);
        L.lines = Array.isArray(res && res.lines) ? res.lines : [];
        L.path = (res && res.path) || "";
        L.error = "";
      } catch (err) {
        L.error = String((err && err.message) || err);
      }
      L.loading = false;
      appRender();
      if (typeof document.querySelector === "function") {
        const pre = document.querySelector(`[data-app-log="${id}"] pre`);
        if (pre) pre.scrollTop = pre.scrollHeight;  // newest lines in view
      }
    }
    // Node.js: its own row with its own Install (POST /apps/runtime/install),
    // the same job bar as the apps, and the source it was found in.
    function appRuntimeMarkup(d) {
      const node = ((d && d.runtime) || {}).node || {};
      const admin = !!(state.principal && state.principal.admin);
      const job = appStore.jobs.get("__node__") || node.active_job || null;
      const busy = appStore.busy.has("__node__");
      const SOURCES = { system: "found on this computer", managed: "installed by the gateway", none: "" };
      let pill;
      let body = "";
      let buttons = "";
      if (appJobActive(job)) {
        pill = uiPill("Installing", "info tone-busy");
        body = uiProgressMarkup(appJobAsProgress(job), "Installing Node.js");
        if (admin) buttons += `<button type="button" class="ui-btn is-ghost" data-app-action="cancel" data-app="__node__"${busy ? " disabled" : ""}>${busy && appStore.pending.get("__node__") ? esc(appStore.pending.get("__node__")) : "Cancel"}</button>`;
      } else if (node.available) {
        pill = uiPill("Ready", "ok");
      } else {
        pill = uiPill("Not installed", "muted");
        body = `<p class="ui-card__note">Node.js will be installed for you: the gateway puts it in its own folder (about 56 MB, no password, no terminal) the first time you install an app, or now.</p>`
          + (node.message ? `<p class="ui-card__note ui-advanced">${esc(node.message)}</p>` : "");
        if (node.install_available !== false) {
          buttons += admin
            ? `<button type="button" class="ui-btn is-primary" data-app-action="runtime-install" data-app="__node__"${busy ? ' disabled aria-busy="true"' : ""}>${esc(busy ? APP_PENDING["runtime-install"] : "Install Node.js")}</button>`
            : `<button type="button" class="ui-btn is-primary" disabled title="Only an admin can install Node.js">Install Node.js</button>`;
        }
      }
      const facts = [];
      if (node.version) facts.push(`<li>Version <b>${esc(node.version)}</b></li>`);
      if (SOURCES[node.source]) facts.push(`<li>${esc(SOURCES[node.source])}</li>`);
      if (node.path) facts.push(`<li class="ui-advanced">Path <code class="ui-ellip" title="${esc(node.path)}">${esc(node.path)}</code></li>`);
      return `<article class="ui-card ui-runtime-row${appJobActive(job) ? " is-busy" : ""}" data-app-runtime="node">`
        + `<div class="ui-runtime-row__main"><span class="ui-mark" aria-hidden="true">JS</span>`
        + `<div class="ui-runtime-row__text"><div class="ui-card__titles"><div class="ui-card__title">Node.js</div><span class="ui-card__status">${pill}</span></div>`
        + `<div class="ui-card__blurb">The engine the browser apps run on.</div>${facts.length ? `<ul class="ui-facts">${facts.join("")}</ul>` : ""}</div>`
        + (buttons ? `<div class="ui-runtime-row__actions">${buttons}</div>` : "")
        + `</div>`
        + appNoticeMarkup("__node__")
        + body
        + `</article>`;
    }
    function appJobActive(job) { return !!job && ["queued", "running"].includes(String(job.state || "")); }
    function appJobAsProgress(job) {
      const j = job || {};
      const state = j.state === "succeeded" ? "done" : (j.state === "running" ? "installing" : j.state);
      return Object.assign({}, j, { state, percent: j.indeterminate ? null : j.percent, bytes_total: j.bytes_total || null, bytes_done: j.bytes_total ? j.bytes_done : null });
    }
    function appRowById(id) { return ((appStore.data && appStore.data.apps) || []).find((a) => a && a.id === id) || null; }
    function appCardMarkup(app) {
      const copy = APP_COPY[app.id] || { mark: String(app.name || app.id || "?").slice(0, 2), blurb: "" };
      const admin = !!(state.principal && state.principal.admin);
      const techOn = uiShowAdvanced();
      const job = appStore.jobs.get(app.id) || app.active_job || null;
      const actions = Array.isArray(app.actions) ? app.actions : [];
      const busy = appStore.busy.has(app.id);
      const name = app.name || app.id;
      const [label, tone] = appJobActive(job) ? ["Installing", "info tone-busy"] : (APP_STATUS[app.status] || [String(app.status || "Unknown"), "muted"]);
      const crashed = (app.status === "crashed" || app.status === "crash_loop");
      // The body: only what the user must see now (progress, a failure, a
      // result they just caused). Never a box that restates the pill.
      let body = appNoticeMarkup(app.id);
      if (appJobActive(job)) {
        body += uiProgressMarkup(appJobAsProgress(job), job.title || `Installing ${name}`);
      } else if (job && job.state === "failed") {
        const err = (job.error && typeof job.error === "object") ? job.error : { message: String(job.error || job.message || "") };
        const tail = job.details || (Array.isArray(job.log_tail) ? job.log_tail.join("\n") : "");
        body += `<div class="ui-alert tone-err" role="alert"><strong>${esc(err.message || "The install did not finish.")}</strong>${err.hint ? `<span>${esc(err.hint)}</span>` : ""}</div>`
          + (tail ? `<details class="ui-details"><summary>Show details</summary><pre class="ui-log">${esc(tail)}</pre></details>` : "");
      } else if (crashed) {
        body += `<div class="ui-alert tone-err" role="alert"><strong>${esc(name)} stopped unexpectedly.</strong><span>Open starts it again.</span></div>`
          + (app.last_error ? `<details class="ui-details"><summary>Show details</summary><pre class="ui-log">${esc(app.last_error)}</pre></details>` : "");
      }
      if (!app.installed && !appJobActive(job) && app.install_blocked_reason) body += `<p class="ui-card__note">${esc(app.install_blocked_reason)}</p>`;
      const tui = appTuiParts(app, techOn);
      body += tui.body;
      const pend = busy ? appStore.pending.get(app.id) : null;
      const b = (action, text, cls, title, appPath) => {
        const mine = pend && pend.action === action;
        return `<button type="button" class="ui-btn ${cls}" data-app-action="${esc(action)}" data-app="${esc(app.id)}"${appPath ? ` data-app-path="${esc(appPath)}"` : ""}${title ? ` title="${esc(title)}"` : ""}${busy ? " disabled" : ""}${mine ? ' aria-busy="true"' : ""}>${esc(mine ? pend.label : text)}</button>`;
      };
      const first = appFirstRun(app);
      const off = (text, title) => `<button type="button" class="ui-btn is-primary" disabled title="${esc(title)}">${esc(text)}</button>`;
      // The action row: ONE primary action for the state + the terminal one.
      let primary = "";
      if (appJobActive(job)) {
        primary = admin ? b("cancel", "Cancel", "is-ghost", `Stop installing ${name}`) : "";
      } else if (!app.installed) {
        primary = actions.includes("install") && admin
          ? b("install", "Install and open", "is-primary", `Install ${name} and start it${app.needs_node_install ? " (Node.js is installed for you first: about 56 MB, no password needed)" : ""}`)
          : off("Install", app.install_blocked_reason || (admin ? "Installing is not available right now" : "Only an admin can install apps"));
      } else if (app.running) {
        primary = first ? b("open", first.label, "is-primary", first.title(name), first.path)
          : b("open", "Open", "is-primary", `Open ${name} in a new tab, signed in`);
      } else {
        // Installed but stopped (or crashed): Open starts it, then opens it.
        primary = actions.includes("launch") && admin
          ? (first ? b("open", first.label, "is-primary", `Start ${name}, then ${first.title(name).charAt(0).toLowerCase()}${first.title(name).slice(1)}`, first.path)
            : b("open", "Open", "is-primary", `Start ${name} and open it in a new tab, signed in`))
          : off("Open", admin ? "Starting is not available right now" : "Only an admin can start apps");
      }
      const row = primary + tui.button;
      // Technical details ON: the secondary line (text buttons + facts), the
      // terminal commands, the address, the npx line, the logs.
      let tech = "";
      if (techOn) {
        const items = [];
        // Started outside the gateway (the dev stack, npx, a service): the
        // row offers Open only; this line says why there is no Stop.
        const ext = app.source === "external" && app.external ? app.external : null;
        if (ext) items.push(`<span data-app-external="${esc(app.id)}">Started outside the gateway on port ${esc(ext.port)}</span>`);
        if (app.running && actions.includes("stop") && admin) items.push(b("stop", "Stop", "is-text", `Stop ${name}`));
        if (app.installed && !app.running && actions.includes("launch") && admin && !appJobActive(job)) items.push(b("launch", "Start", "is-text", `Start ${name} without opening it`));
        const logOpen = !!(appStore.logs.get(app.id) || {}).open;
        if (actions.includes("logs") && admin) items.push(`<button type="button" class="ui-btn is-text" data-app-action="logs" data-app="${esc(app.id)}" aria-expanded="${logOpen ? "true" : "false"}">${logOpen ? "Hide log" : "Show log"}</button>`);
        if (app.update_available && actions.includes("update") && admin && !appJobActive(job)) items.push(b("update", app.latest_version ? `Update to ${app.latest_version}` : "Update", "is-text", `Install the newest ${name} (${app.latest_version || "latest"}); a running app restarts on it`));
        if (app.version) items.push(`<span>Version ${esc(app.version)}</span>`);
        else if (app.update_available && app.latest_version) items.push(`<span>Latest ${esc(app.latest_version)}</span>`);
        items.push(...tui.tech);
        const doneLog = job && job.state === "succeeded" && (job.details || (Array.isArray(job.log_tail) && job.log_tail.length)) ? (job.details || job.log_tail.join("\n")) : "";
        tech = (items.length ? `<div class="ui-card__techline" data-app-tech="${esc(app.id)}">${appTechSep(items)}</div>` : "")
          + tui.lines
          + (app.url ? `<div class="ui-card__techrow"><span class="ui-card__techlabel">Address</span><code class="ui-ellip" title="${esc(app.url)}">${esc(app.url)}</code></div>` : "")
          + `<div class="ui-card__techrow"><span class="ui-card__techlabel">npm</span><code class="ui-ellip" title="npx ${esc(app.package || "")}">npx ${esc(app.package || "")}</code></div>`
          + (doneLog ? `<details class="ui-details"><summary>Install log</summary><pre class="ui-log">${esc(doneLog)}</pre></details>` : "")
          + appLogMarkup(app);
      }
      const blurb = copy.blurb || app.description || "";
      const tint = appJobActive(job) ? " is-busy" : ((job && job.state === "failed") || app.status === "crash_loop" ? " is-attention" : "");
      return `<article class="ui-card ui-app-card${tint}" data-app-card="${esc(app.id)}"><div class="ui-card__head"><span class="ui-mark" aria-hidden="true">${esc(copy.mark)}</span>`
        + `<div class="ui-card__titles"><div class="ui-card__title">${esc(name)}</div><span class="ui-card__status">${uiPill(label, tone)}</span></div></div>`
        + `<div class="ui-card__blurb is-oneline" title="${esc(blurb)}">${esc(blurb)}</div>`
        + `<div class="ui-card__body">${body}</div>`
        + `<div class="ui-card__actions">${row}</div>`
        + `<div class="ui-card__tech">${tech}</div>`
        + `</article>`;
    }
    function appViewMarkup() {
      const url = gatewayBaseUrl();
      if (appStore.error && !appStore.data) {
        const rows = FIRST_RUN_APPS.map((a) => `<article class="ui-card"><div class="ui-card__head"><span class="ui-mark" aria-hidden="true">${esc(a.mark)}</span>`
          + `<div class="ui-card__titles"><div class="ui-card__title">${esc(a.name)}</div></div></div><div class="ui-card__blurb">${esc(a.what)}</div>`
          + `<div class="ui-advanced"><div class="ui-cmd"><code class="ui-ellip">npx ${esc(a.pkg)}</code></div></div></article>`).join("");
        return `<div class="ui-alert tone-warn" role="alert"><strong>This gateway cannot manage apps right now.</strong><span>${esc(appStore.error)}</span></div><div class="ui-card-grid">${rows}</div>`;
      }
      if (!appStore.data) return `<div class="ui-empty">Looking for the apps...</div>`;
      const d = appStore.data;
      const node = ((d.runtime || {}).node) || {};
      const apps = Array.isArray(d.apps) ? d.apps : [];
      let intro = `<div class="ui-toolbar"><span>Apps open in your browser, already signed in, and talk to this gateway at <code class="ui-ellip">${esc(d.gateway_url || url)}</code>.</span>`
        + `<button type="button" class="ui-btn is-quiet" data-app-action="refresh">${appStore.loading ? "Checking..." : "Check again"}</button></div>`;
      intro += appRuntimeMarkup(d);
      if (d.registry && d.registry.reachable === false) intro += `<div class="ui-alert tone-warn" role="alert"><strong>The app store (npm) is not reachable.</strong><span>Installed apps keep working; installing needs the internet.</span></div>`;
      const msg = appStore.message ? `<div class="ui-alert tone-err" role="alert">${esc(appStore.message)}</div>` : "";
      return intro + msg + `<div class="ui-card-grid is-aligned">${apps.map(appCardMarkup).join("")}</div>`;
    }
    function appRender() { for (const el of appStore.views.values()) if (el) el.innerHTML = appViewMarkup(); }
    async function appRefresh() {
      appStore.loading = true;
      appRender();
      try {
        appStore.data = await api("/api/gateway/apps?latest=true", { slow: true });
        appStore.error = "";
        for (const app of (appStore.data.apps || [])) {
          if (app && app.active_job) appStore.jobs.set(app.id, app.active_job);
          const t = appTuiOf(app);
          if (t && t.active_job) appStore.jobs.set(`tui:${app.id}`, t.active_job);
        }
        const nodeJob = ((appStore.data.runtime || {}).node || {}).active_job;
        if (nodeJob && !appStore.jobs.has("__node__")) appStore.jobs.set("__node__", nodeJob);
      } catch (err) {
        appStore.error = String((err && err.message) || err);
      }
      appStore.loading = false;
      appRender();
      appPoll();
    }
    async function appPoll() {
      if (appStore.polling) return;
      appStore.polling = true;
      try {
        while (appStore.views.size && Array.from(appStore.jobs.values()).some(appJobActive)) {
          await new Promise((resolve) => setTimeout(resolve, 1000));
          let finished = false;
          for (const [id, job] of Array.from(appStore.jobs.entries())) {
            if (!appJobActive(job)) continue;
            try {
              const res = await api(`/api/gateway/apps/jobs/${encodeURIComponent(job.id)}`);
              const next = (res && res.job) || job;
              appStore.jobs.set(id, next);
              if (!appJobActive(next)) { finished = true; appJobResult(id, next); }
            } catch (err) {
              if (err && err.status === 404) { appStore.jobs.delete(id); finished = true; }
            }
          }
          appRender();
          if (finished) await appRefresh();
        }
      } finally {
        appStore.polling = false;
      }
    }
    function appName(id) {
      if (id === "__node__") return "Node.js";
      if (String(id).startsWith("tui:")) return `${appName(String(id).slice(4))} (terminal)`;
      const app = ((appStore.data && appStore.data.apps) || []).find((a) => a && a.id === id);
      return (app && app.name) || id;
    }
    // A finished job leaves its result on the card: what is installed/running
    // now (with Open for a launched app: a click, so popup-safe), or the
    // reason + hint + the whole log behind "Show details".
    function appJobResult(id, job) {
      const name = appName(id);
      const r = (job && job.result) || {};
      if (job.state === "succeeded" && String(id).startsWith("tui:")) {
        const base = appName(String(id).slice(4));
        appNotify(id, { tone: "ok", text: `${base}'s terminal app ${r.version || ""} is installed.`.replace("  ", " "), hint: "" });
      } else if (job.state === "succeeded") {
        const verb = job.kind === "update" ? "updated" : "installed";
        const text = id === "__node__"
          ? `Node.js ${r.version || ""} is installed.`.replace("  ", " ")
          : (r.url ? `${name} ${r.version || ""} is ${verb} and running.` : `${name} ${r.version || ""} is ${verb}.`).replace("  ", " ");
        appNotify(id, { tone: "ok", text });
      } else if (job.state === "failed") {
        const err = (job.error && typeof job.error === "object") ? job.error : { message: String(job.error || job.message || "") };
        appStore.notices.delete(id);  // the card's own failure box shows it (message, hint, Show details)
        if (id === "__node__") appNotify(id, { tone: "err", text: err.message || "Node.js did not install.", hint: err.hint || "", details: job.details || (Array.isArray(job.log_tail) ? job.log_tail.join("\n") : "") });
      } else if (job.state === "cancelled") {
        appNotify(id, { tone: "info", text: `${name}: cancelled. Nothing was changed.` });
      }
    }
    async function appAction(action, id, button) {
      if (action === "refresh") { appStore.message = ""; appStore.notices.clear(); await appRefresh(); return; }
      if (action === "logs") {
        const L = appStore.logs.get(id);
        if (L && L.open) { L.open = false; appRender(); return; }
        await appLoadLog(id, (L && L.tail) || APP_LOG_TAIL);
        return;
      }
      if (action === "log-refresh") { await appLoadLog(id); return; }
      if (action === "log-more") { const L = appStore.logs.get(id) || {}; await appLoadLog(id, Math.min(APP_LOG_MAX, (L.tail || APP_LOG_TAIL) * 4)); return; }
      if (action === "log-copy") { const L = appStore.logs.get(id) || {}; uiCopy((L.lines || []).join("\n")); return; }
      if (action === "tui-copy") { uiCopy((button && button.dataset && button.dataset.cmd) || ""); return; }
      if (action === "tui-open" || action === "tui-install" || action === "tui-cancel") { await appTuiAction(action, id); return; }
      if (action === "cancel") {
        const job = appStore.jobs.get(id);
        appStore.busy.add(id);
        appStore.pending.set(id, { action, label: APP_PENDING.cancel });
        appRender();
        if (job) {
          try {
            const res = await api(`/api/gateway/apps/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST", body: JSON.stringify({}) });
            if (res && res.job) appStore.jobs.set(id, res.job);
          } catch (err) {
            appNotify(id, { tone: "err", text: `Could not cancel: ${String((err && err.message) || err)}`, details: uiErrorDetails(err) });
          }
        }
        appStore.busy.delete(id);
        appStore.pending.delete(id);
        appRender();
        appPoll();
        return;
      }
      if (action === "runtime-install") {
        appStore.busy.add("__node__");
        appStore.notices.delete("__node__");
        appRender();
        try {
          const res = await api("/api/gateway/apps/runtime/install", { slow: true, method: "POST", body: JSON.stringify({}) });
          if (res && res.job) appStore.jobs.set("__node__", res.job);
          else if (res && res.message) appNotify("__node__", { tone: "ok", text: res.message });
        } catch (err) {
          appNotify("__node__", { tone: "err", text: `Could not install Node.js: ${String((err && err.message) || err)}`, details: uiErrorDetails(err) });
        }
        appStore.busy.delete("__node__");
        await appRefresh();
        return;
      }
      if (action === "open") {
        // The tab opens synchronously on the click (popup blockers), then
        // goes to the one-time signed-in link once the gateway minted it. A
        // stopped (or crashed) app is started first: Open is the one action
        // a plain user needs (POST /launch, then POST /open).
        const row = appRowById(id);
        const start = !!(row && row.installed && !row.running);
        const tab = typeof window !== "undefined" && window.open ? window.open("about:blank", "_blank") : null;
        if (start) {
          try { if (tab && tab.document) { tab.document.title = `Starting ${appName(id)}`; tab.document.body.textContent = `Starting ${appName(id)}...`; } } catch { /* cross-origin or closed */ }
          appStore.busy.add(id);
          appStore.pending.set(id, { action, label: APP_PENDING.launch });
          appStore.notices.delete(id);
          appRender();
        }
        try {
          if (start) {
            const st = await api(`/api/gateway/apps/${encodeURIComponent(id)}/launch`, { slow: true, method: "POST", body: JSON.stringify({}) });
            if (st && st.app && st.app.running === false) {
              const e = new Error(`${appName(id)} did not start (${APP_STATUS[st.app.status] ? APP_STATUS[st.app.status][0].toLowerCase() : String(st.app.status || "unknown")}).`);
              e.data = { hint: st.app.last_error || "" };
              throw e;
            }
          }
          // A first-run button lands inside the app (data-app-path, e.g.
          // Entity's "/#new"); the gateway validates it as a same-app path.
          const appPath = (button && button.dataset && button.dataset.appPath) || "";
          const res = await api(`/api/gateway/apps/${encodeURIComponent(id)}/open`, { method: "POST", body: JSON.stringify(appPath ? { path: appPath } : {}) });
          if (tab) tab.location = res.open_url; else if (typeof location !== "undefined") location.assign(res.open_url);
          appNotify(id, { tone: "ok", text: tab ? `${appName(id)} opened in a new tab.` : `Opening ${appName(id)}...` });
        } catch (err) {
          if (tab) tab.close();
          appNotify(id, { tone: "err", text: `Could not open ${appName(id)}: ${String((err && err.message) || err)}`, hint: (err && err.data && err.data.hint) || "", details: uiErrorDetails(err) });
        }
        if (start) {
          appStore.busy.delete(id);
          appStore.pending.delete(id);
          await appRefresh();
        } else {
          appRender();
        }
        return;
      }
      const path = { install: "install", launch: "launch", stop: "stop", update: "update" }[action];
      if (!path) return;
      const name = appName(id);
      appStore.busy.add(id);
      appStore.pending.set(id, { action, label: APP_PENDING[action] });
      appStore.notices.delete(id);
      appStore.message = "";
      appRender();
      try {
        const res = await api(`/api/gateway/apps/${encodeURIComponent(id)}/${path}`, { slow: true, method: "POST", body: JSON.stringify(action === "install" ? { launch: true } : {}) });
        if (res && res.job) appStore.jobs.set(id, res.job);
        if (action === "launch" && res && res.app) appNotify(id, { tone: "ok", text: `${name} is ${res.app.running ? "running" : String(res.app.status || "starting")}.` });
        if (action === "stop" && res && res.app) appNotify(id, { tone: "ok", text: `${name} is stopped.` });
        if ((action === "update" || action === "install") && res && res.job && res.created === false) appNotify(id, { tone: "info", text: `${name}: already in progress; showing that job.` });
      } catch (err) {
        const verb = { install: "install", launch: "start", stop: "stop", update: "update" }[action];
        appNotify(id, { tone: "err", text: `Could not ${verb} ${name}: ${String((err && err.message) || err)}`, hint: (err && err.data && err.data.hint) || "", details: uiErrorDetails(err) });
      }
      appStore.busy.delete(id);
      appStore.pending.delete(id);
      await appRefresh();
    }
    // "Open in Terminal" (POST /launch-tui: a new terminal window on the
    // gateway machine, signed in through a one-time code), "Install for
    // Terminal" (POST /install-tui: a job) and its Cancel. A refusal keeps
    // the gateway's words and, when it sent one, the command to copy.
    async function appTuiAction(action, id) {
      const key = `tui:${id}`;
      const name = appName(id);
      appStore.busy.add(key);
      appStore.pending.set(key, { action, label: APP_PENDING[action] });
      appStore.notices.delete(key);
      appRender();
      try {
        if (action === "tui-open") {
          const res = await api(`/api/gateway/apps/${encodeURIComponent(id)}/launch-tui`, { method: "POST", body: JSON.stringify({}) });
          appNotify(key, { tone: "ok", text: (res && res.message) || `${name} is opening in a terminal window on this computer's screen.` });
        } else if (action === "tui-install") {
          const res = await api(`/api/gateway/apps/${encodeURIComponent(id)}/install-tui`, { slow: true, method: "POST", body: JSON.stringify({}) });
          if (res && res.job) appStore.jobs.set(key, res.job);
        } else {
          const job = appStore.jobs.get(key);
          if (job) {
            const res = await api(`/api/gateway/apps/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST", body: JSON.stringify({}) });
            if (res && res.job) appStore.jobs.set(key, res.job);
          }
        }
      } catch (err) {
        const data = (err && err.data) || {};
        const verb = { "tui-open": "open", "tui-install": "install", "tui-cancel": "cancel" }[action];
        appNotify(key, { tone: "err", text: `Could not ${verb} ${name} for the terminal: ${String((err && err.message) || err)}`, hint: data.hint || "", cmd: data.command || "", details: uiErrorDetails(err) });
      }
      appStore.busy.delete(key);
      appStore.pending.delete(key);
      appRender();
      appPoll();
    }
    function mountAppCards(key, el) {
      if (!el) return;
      appStore.views.set(key, el);
      el.onclick = (event) => {
        const b = event && event.target && event.target.closest ? event.target.closest("[data-app-action]") : null;
        if (!b || b.disabled) return;
        appAction(b.dataset.appAction, b.dataset.app || "", b);
      };
      appRender();
      appRefresh();
    }
    function unmountAppCards(key) { appStore.views.delete(key); }

    // ---- Network: who can reach this gateway (mission L2) ----
    // Contract gateway_network_v1 (network_exposure.py, routes/network.py):
    // GET /network -> {schema, writable, configured{mode,label,port,...},
    // effective{mode,label,bind_host,port,overridden_by_cli,...},
    // restart_required, restart{available,applies,reason,how,...},
    // auth{ok_for_mode,will_enable_user_auth,reason,fix,...},
    // modes[{id,label,selected,allowed,reason,fix}], addresses[{kind:
    // loopback|lan|hostname|public, url, host, port, reachable, note,
    // interface_label}], copy_hint, warnings[]}; POST /network {mode, port?,
    // acknowledge_internet?} -> 200 | 409 {reason_code, refused_reason, fix,
    // warnings}; POST /network/restart -> 200 {next} | 409 {refused_reason}.
    // Every address comes from the gateway (discovered there); the console
    // never guesses an IP.
    const NET_MODE_TEXT = {
      localhost: "Only this computer can connect: apps and browsers running here. Nothing on your network sees the gateway.",
      lan: "Phones, tablets and other computers on the same Wi-Fi or office network can connect. Everyone signs in with their own account.",
      internet: "Reachable from anywhere, through a secure proxy or tunnel that you set up. The gateway itself speaks plain HTTP and never touches your router.",
    };
    const NET_KIND_LABEL = { loopback: "This computer", lan: "Local network", hostname: "Network name", public: "Public address" };
    const netStore = { data: null, error: "", loading: false, views: new Map(), busy: "", confirm: null, refused: null, notice: null, restarting: false, publicLoading: false,
      // Advanced: reverse proxy (mission Z): the origin being typed, its
      // validation message, which field is saving, the last save's result
      // line, and the disclosure's open state once the user toggled it.
      proxy: { draft: "", error: "", saving: "", saved: null, open: null } };
    function netPrimaryUrl() { return (netStore.data && netStore.data.copy_hint) || ""; }
    function netAddressRow(a, primary) {
      const url = a.url || "";
      const label = a.kind === "lan" && a.interface_label ? `${NET_KIND_LABEL.lan} · ${a.interface_label}` : (NET_KIND_LABEL[a.kind] || a.kind || "Address");
      let pill;
      if (a.reachable === true) pill = uiPill("Works now", "ok");
      else if (a.reachable === false) pill = uiPill("Not in this mode", "muted");
      else pill = uiPill(a.kind === "public" ? "Through your proxy only" : "Unknown", "muted");
      return `<li class="ui-addr${url && url === primary ? " is-primary" : ""}" data-net-address="${esc(a.kind || "")}">`
        + `<div class="ui-addr__text"><span class="ui-addr__label">${esc(label)}${url && url === primary ? ` ${uiPill("Primary", "info")}` : ""}</span>`
        + (url ? `<code class="ui-ellip is-block" title="${esc(url)}">${esc(url)}</code>` : `<span class="ui-card__note">${esc(a.note || "No address")}</span>`)
        + (a.note && url ? `<span class="ui-addr__note">${esc(a.note)}</span>` : "")
        + `</div><div class="ui-addr__side">${pill}`
        // Copy on every address a client can use (not on the ones this mode
        // does not serve: `reachable: false`).
        + (url && a.reachable !== false ? `<button type="button" class="ui-btn ${url === primary ? "is-primary" : "is-ghost"} ui-addr__copy" data-net-copy="${esc(url)}" aria-label="Copy ${esc(url)}">Copy</button>` : "")
        + `</div></li>`;
    }
    function netViewMarkup() {
      if (netStore.error && !netStore.data) {
        return `<div class="ui-alert tone-err" role="alert"><strong>Could not read this gateway's network settings.</strong><span>${esc(netStore.error)}</span></div>`
          + `<div class="ui-toolbar"><button type="button" class="ui-btn is-ghost" data-net-action="refresh">${netStore.loading ? "Checking..." : "Try again"}</button></div>`;
      }
      if (!netStore.data) return `<div class="ui-empty">Looking at this computer's network...</div>`;
      const d = netStore.data;
      const conf = d.configured || {};
      const eff = d.effective || {};
      const admin = !!d.writable;
      const primary = d.copy_hint || "";
      const modes = Array.isArray(d.modes) ? d.modes : [];
      const opts = modes.map((m) => {
        const on = m.id === conf.mode;
        const busy = netStore.busy === m.id;
        const locked = m.allowed === false;
        return `<button type="button" role="radio" class="ui-seg__opt${on ? " is-on" : ""}${locked ? " is-locked" : ""}" data-net-mode="${esc(m.id)}" aria-checked="${on ? "true" : "false"}" tabindex="${on ? "0" : "-1"}"${admin && !netStore.busy && !netStore.restarting ? "" : " disabled"}${busy ? ' aria-busy="true"' : ""}>`
          + `<span class="ui-seg__title">${esc(busy ? "Saving..." : (m.label || m.id))}${locked ? `<span class="ui-seg__lock">Needs accounts</span>` : ""}</span>`
          + `<span class="ui-seg__text">${esc(NET_MODE_TEXT[m.id] || "")}</span></button>`;
      }).join("");
      let out = `<div class="ui-section-title"><h3>Who can reach this gateway</h3><span class="ui-sub">Running now: <b>${esc(eff.label || "unknown")}</b>${eff.port ? ` · port ${esc(eff.port)}` : ""}</span></div>`
        + `<div class="ui-seg" role="radiogroup" aria-label="Who can reach this gateway">${opts}</div>`;
      if (!admin) out += `<p class="ui-card__note">Only an admin can change who can reach this gateway.</p>`;
      if (netStore.refused) {
        const r = netStore.refused;
        out += `<div class="ui-alert tone-warn" role="alert"><strong>${esc(r.reason || "This mode is not available right now.")}</strong>`
          + (r.fix ? `<span><b>How to fix it:</b> ${esc(r.fix)}</span>` : "") + `</div>`
          + (r.details ? `<details class="ui-details"><summary>Show details</summary><pre class="ui-log">${esc(r.details)}</pre></details>` : "");
      }
      if (netStore.confirm) {
        const c = netStore.confirm;
        out += `<div class="ui-confirm ui-net-confirm" role="alertdialog" aria-label="Confirm Internet mode"><p><strong>Before you open the gateway to the internet</strong></p>`
          + (c.reason ? `<p class="ui-card__note">${esc(c.reason)}</p>` : "")
          + `<ul class="ui-warn-list">${(c.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("")}</ul>`
          + `<div class="ui-card__actions"><button type="button" class="ui-btn is-primary" data-net-action="ack-internet"${netStore.busy ? " disabled" : ""}>${netStore.busy ? "Saving..." : "I understand, use Internet mode"}</button>`
          + `<button type="button" class="ui-btn is-ghost" data-net-action="ack-cancel">Keep ${esc(conf.label || "the current mode")}</button></div></div>`;
      }
      if (netStore.notice) {
        const n = netStore.notice;
        out += `<div class="ui-alert tone-${esc(n.tone || "info")}"${n.tone === "err" ? ' role="alert"' : ' role="status"'}><strong>${esc(n.text)}</strong>${n.hint ? `<span>${esc(n.hint)}</span>` : ""}</div>`
          + (n.details ? `<details class="ui-details"><summary>Show details</summary><pre class="ui-log">${esc(n.details)}</pre></details>` : "");
      }
      if (d.restart_required) {
        const rs = d.restart || {};
        const can = rs.available && rs.applies;
        // R: "Restart to apply" only when a restart CAN apply it (restart_required
        // && restart.applies && restart.available); otherwise say why not.
        out += `<div class="ui-alert tone-${can ? "info" : "warn"} ui-net-restart" role="status"><strong>${can
            ? `Restart to apply: ${esc(conf.label || conf.mode)} on port ${esc(conf.port)}.`
            : `Saved: ${esc(conf.label || conf.mode)} on port ${esc(conf.port)}, not applied yet.`}</strong>`
          + `<span>The gateway keeps running as ${esc(eff.label || "before")}${eff.bind_host ? ` (${esc(eff.bind_host)}:${esc(eff.port)})` : ""} until it restarts.</span>`
          + (!can && (rs.reason || rs.unavailable_reason || rs.how) ? `<span>${esc(rs.reason || rs.unavailable_reason || "")}${(rs.reason || rs.unavailable_reason) && rs.how ? " " : ""}${esc(rs.how || "")}</span>` : "")
          + (can && admin ? `<div class="ui-card__actions"><button type="button" class="ui-btn is-primary" data-net-action="restart"${netStore.restarting ? ' disabled aria-busy="true"' : ""}>${netStore.restarting ? "Restarting..." : "Restart now"}</button></div>` : "")
          + `</div>`;
      }
      if (d.auth && d.auth.ok_for_mode === false && !netStore.refused) {
        out += `<div class="ui-alert tone-warn" role="alert"><strong>${esc(d.auth.reason || "This mode's sign-in requirement is not met.")}</strong>${d.auth.fix ? `<span><b>How to fix it:</b> ${esc(d.auth.fix)}</span>` : ""}</div>`;
      } else if (d.auth && d.auth.will_enable_user_auth && conf.mode !== "localhost") {
        out += `<p class="ui-card__note">Sign-in with accounts stays on for this mode: every person uses their own account (Users tab).</p>`;
      }
      const addrs = Array.isArray(d.addresses) ? d.addresses : [];
      out += `<div class="ui-section-title"><h3>Addresses</h3><span class="ui-sub">Other devices use one of these to connect.</span></div>`
        + (addrs.length ? `<ul class="ui-addr-list">${addrs.map((a) => netAddressRow(a, primary)).join("")}</ul>` : `<div class="ui-empty">The gateway found no address to show.</div>`);
      const tools = [];
      if (admin && (conf.mode === "internet" || eff.mode === "internet") && !addrs.some((a) => a.kind === "public")) {
        tools.push(`<button type="button" class="ui-btn is-ghost" data-net-action="lookup-public"${netStore.publicLoading ? ' disabled aria-busy="true"' : ""}>${netStore.publicLoading ? "Looking up..." : "Look up my public address"}</button>`);
      }
      tools.push(`<button type="button" class="ui-btn is-quiet" data-net-action="refresh">${netStore.loading ? "Checking..." : "Check again"}</button>`);
      if (d.checked_at) tools.push(`<span>checked ${esc(new Date(d.checked_at).toLocaleTimeString())}</span>`);
      tools.push(`<span class="ui-advanced ui-sub">CLI: <code>abstractgateway network status</code></span>`);
      out += `<div class="ui-toolbar">${tools.join("")}</div>`;
      const warnings = Array.isArray(d.warnings) ? d.warnings : [];
      if (warnings.length) {
        out += `<details class="ui-details ui-net-warnings"${d.restart_required || conf.mode === "internet" ? " open" : ""}><summary>What to know about ${esc(conf.label || "this mode")} (${warnings.length})</summary>`
          + `<ul class="ui-warn-list">${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></details>`;
      }
      out += netProxyMarkup(d);
      return out;
    }
    // ---- Advanced: reverse proxy (mission Z) ----
    // gateway_network_v1 `reverse_proxy`: {allowed_origins: {value[],
    // source: setting|env|default, overridden_by_env, env_name?, env_value?,
    // effective[], builtin[], self_origins[], applies: "live", warnings[],
    // note?}, trust_proxy: {value, source, overridden_by_env, effective,
    // applies, note?, warning?}}. Saved through the same door as the mode:
    // POST /network {allowed_origins} | {trust_proxy} -> 200 {changed{field:
    // {applies: live|restart|overridden_by_env}}} | 400 {errors[{value, error}]}.
    // Validation lives in ONE place, the gateway (network_exposure.
    // normalize_origin): the console, the TUI and the CLI show its words
    // verbatim (operator: identical semantics and messages on every door).
    function netProxySourceText(f) {
      if (!f) return "";
      if (f.overridden_by_env) return uiPill("Set by the environment", "warn", `This gateway was started with ${f.env_name || "an environment value"}: it decides until the gateway starts without it.`);
      if (f.source === "setting") return uiPill("Saved setting", "info");
      return uiPill("Default", "muted");
    }
    function netProxyMarkup(d) {
      const rp = d && d.reverse_proxy;
      if (!rp || !rp.allowed_origins || !rp.trust_proxy) return "";
      const o = rp.allowed_origins, t = rp.trust_proxy;
      const admin = !!d.writable;
      const conf = d.configured || {};
      const p = netStore.proxy;
      const value = Array.isArray(o.value) ? o.value : [];
      const auto = conf.mode === "internet" || value.length > 0 || !!t.value || !!o.overridden_by_env || !!t.overridden_by_env;
      const open = p.open === null ? auto : p.open;
      const busy = !!p.saving || !admin;
      const sum = [`${value.length ? `${value.length} origin${value.length === 1 ? "" : "s"}` : "no extra origin"}`, `trust proxy ${t.effective ? "on" : "off"}`];
      const chipWarn = (x) => x === "*" || x.includes("://*.") || /:\*$/.test(x) || (/^http:\/\//.test(x) && !/^http:\/\/(localhost|127\.|\[::1\])/.test(x));
      let out = `<details class="ui-details ui-net-proxy" data-net-proxy${open ? " open" : ""}>`
        + `<summary><span class="ui-net-proxy__title">Advanced: reverse proxy</span><span class="ui-net-proxy__sum">${esc(sum.join(" · "))}</span>`
        + (o.overridden_by_env || t.overridden_by_env ? uiPill("Environment override", "warn") : "") + `</summary>`
        + `<div class="ui-net-proxy__body"><div class="ui-net-proxy__grid">`;
      // Browser origins.
      out += `<section class="ui-net-proxy__field" aria-labelledby="net-origins-h"><div class="ui-net-proxy__head"><h4 id="net-origins-h">Browser origins</h4>${netProxySourceText(o)}</div>`
        + `<p class="ui-net-proxy__text">Web addresses whose pages may use this gateway from a browser, besides this computer's own. Add the https address your proxy or tunnel serves, for example <code>https://gateway.example.com</code>.</p>`;
      if (o.overridden_by_env) {
        out += `<div class="ui-alert tone-warn" role="status" data-net-proxy-env="origins"><strong>This gateway was started with ${esc(o.env_name || "an origins list")} in its environment, so that list decides:</strong>`
          + `<span>${esc((o.env_value || []).join(", ") || "none")}. The origins below are saved and apply once the gateway is started without it.</span></div>`;
      }
      out += `<ul class="ui-chips" aria-label="Allowed browser origins">`
        + (value.length ? value.map((x) => `<li class="ui-chip${chipWarn(x) ? " is-warn" : ""}" data-net-origin="${esc(x)}"><span class="ui-chip__text" title="${esc(x)}">${esc(x)}</span>`
          + `<button type="button" class="ui-chip__x" data-net-origin-remove="${esc(x)}" aria-label="Remove ${esc(x)}"${busy ? " disabled" : ""}>&times;</button></li>`).join("")
          : `<li class="ui-chip is-muted">${o.overridden_by_env ? "None saved" : "None yet: only this computer's own pages"}</li>`)
        + `</ul>`;
      if (admin) {
        out += `<div class="ui-net-proxy__add"><input type="text" id="net-origin-input" data-net-origin-input inputmode="url" autocomplete="off" spellcheck="false" placeholder="https://gateway.example.com" value="${esc(p.draft)}" aria-label="Origin to allow" aria-describedby="net-origin-msg"${p.error ? ' aria-invalid="true"' : ""}${busy ? " disabled" : ""}>`
          + `<button type="button" class="ui-btn is-ghost" data-net-action="origin-add"${busy ? " disabled" : ""}${p.saving === "allowed_origins" ? ' aria-busy="true"' : ""}>${p.saving === "allowed_origins" ? "Saving..." : "Add origin"}</button></div>`;
      }
      out += `<p class="ui-field-msg${p.error ? " tone-err" : ""}" id="net-origin-msg" role="${p.error ? "alert" : "status"}">${esc(p.error || "")}</p>`;
      for (const w of (o.warnings || [])) out += `<p class="ui-field-msg tone-warn">${esc(w)}</p>`;
      const always = [...(o.builtin || []), ...(o.self_origins || [])];
      if (always.length && !o.overridden_by_env) out += `<p class="ui-net-proxy__text">Always allowed: ${always.map((x) => `<code>${esc(x)}</code>`).join(" ")}</p>`;
      out += `</section>`;
      // Trust proxy.
      out += `<section class="ui-net-proxy__field" aria-labelledby="net-trust-h"><div class="ui-net-proxy__head"><h4 id="net-trust-h">Client address</h4>${netProxySourceText(t)}</div>`
        + `<label class="ui-switch ui-net-proxy__switch"><input type="checkbox" role="switch" data-net-trust${t.value ? " checked" : ""}${busy ? " disabled" : ""} aria-describedby="net-trust-text net-trust-danger"><span>${p.saving === "trust_proxy" ? "Saving..." : "Trust the proxy's client address"}</span></label>`
        + `<p class="ui-net-proxy__text" id="net-trust-text">Behind a reverse proxy every request seems to come from the proxy itself. On: the gateway reads the real client address from the X-Forwarded-For header the proxy adds, for sign-in lockouts and the audit log.</p>`
        + `<p class="ui-net-proxy__danger" id="net-trust-danger">Only when your own proxy sits in front of every request: otherwise anyone can choose the address the gateway sees.</p>`;
      if (t.overridden_by_env) {
        out += `<div class="ui-alert tone-warn" role="status" data-net-proxy-env="trust"><strong>This gateway was started with ${esc(t.env_name || "a proxy setting")} in its environment: trust is ${t.effective ? "on" : "off"}.</strong>`
          + `<span>The switch is saved and applies once the gateway is started without it.</span></div>`;
      }
      out += `</section></div>`;
      if (p.saved) out += `<p class="ui-net-proxy__saved tone-${esc(p.saved.tone)}" role="status" data-net-proxy-saved><b>${esc(p.saved.head)}</b><span>${esc(p.saved.text)}</span></p>`;
      else if (!admin) out += `<p class="ui-net-proxy__saved" role="status"><span>Only an admin can change these.</span></p>`;
      else out += `<p class="ui-net-proxy__saved" role="status"><span>Changes apply to the next request: no restart.</span><span class="ui-advanced ui-sub">CLI: <code>abstractgateway network set --allowed-origins … --trust-proxy on|off</code></span></p>`;
      out += `</div></details>`;
      return out;
    }
    async function netProxySave(field, body) {
      const p = netStore.proxy;
      p.saving = field;
      p.saved = null;
      netRender();
      try {
        const res = await api("/api/gateway/network", { method: "POST", body: JSON.stringify(body) });
        const ch = ((res && res.changed) || {})[field];
        const label = field === "trust_proxy" ? "Trust proxy" : "Origins";
        if (!ch) p.saved = { tone: "ok", head: "Saved", text: `${label}: no change.` };
        else if (ch.applies === "overridden_by_env") p.saved = { tone: "warn", head: "Saved · not in effect", text: "The environment this gateway was started with decides until it starts without it." };
        else if (ch.applies === "restart") p.saved = { tone: "warn", head: "Saved · restart required", text: `${label} applies when the gateway restarts.` };
        else p.saved = { tone: "ok", head: "Saved · applies now", text: `${label} applies to the next request.` };
        if (field === "allowed_origins") { p.draft = ""; p.error = ""; }
      } catch (err) {
        const data = (err && err.data) || {};
        if (err && err.status === 400 && field === "allowed_origins" && data.refused_reason) {
          // The gateway's sentence verbatim: the TUI and the CLI print the same one.
          p.error = String(data.refused_reason);
        } else {
          p.saved = { tone: "err", head: "Not saved", text: String((data && data.refused_reason) || (err && err.message) || err) };
        }
      }
      p.saving = "";
      await netRefresh({ quiet: true });
      if (field === "allowed_origins" && p.error) netFocusOriginInput();
    }
    function netFocusOriginInput() {
      for (const el of netStore.views.values()) {
        const i = el && el.querySelector ? el.querySelector("[data-net-origin-input]") : null;
        if (i && i.offsetParent !== null) { i.focus(); try { i.setSelectionRange(i.value.length, i.value.length); } catch { /* not a text input */ } return; }
      }
    }
    async function netOriginAdd() {
      const p = netStore.proxy;
      const d = netStore.data;
      if (!d || !d.reverse_proxy || p.saving) return;
      const typed = String(p.draft || "").trim();
      if (!typed) { p.error = "Type an origin, for example https://gateway.example.com."; netRender(); netFocusOriginInput(); return; }
      const cur = (d.reverse_proxy.allowed_origins.value || []).slice();
      if (cur.includes(typed)) { p.error = `${typed} is already allowed.`; netRender(); netFocusOriginInput(); return; }
      p.error = "";
      await netProxySave("allowed_origins", { allowed_origins: [...cur, typed] });
    }
    async function netOriginRemove(origin) {
      const d = netStore.data;
      if (!d || !d.reverse_proxy || netStore.proxy.saving) return;
      const cur = (d.reverse_proxy.allowed_origins.value || []).filter((x) => x !== origin);
      await netProxySave("allowed_origins", { allowed_origins: cur });
    }
    function netRender() {
      for (const el of netStore.views.values()) if (el) el.innerHTML = netViewMarkup();
      renderIslands();
    }
    async function netRefresh(opts) {
      const o = opts || {};
      netStore.loading = true;
      if (!o.quiet) netRender();
      try {
        const data = await api(`/api/gateway/network${o.lookupPublic ? "?lookup_public=1" : ""}`, { slow: !!o.lookupPublic });
        if (!data || data.schema !== "gateway_network_v1") throw new Error(`unexpected network payload (schema ${String((data && data.schema) || "none")}, expected gateway_network_v1)`);
        netStore.data = data;
        netStore.error = "";
      } catch (err) {
        netStore.error = err && err.status === 404
          ? "This gateway has no network settings yet (GET /api/gateway/network answered 404): it needs the version with network exposure."
          : String((err && err.message) || err);
      }
      netStore.loading = false;
      netRender();
    }
    async function netChoose(mode) {
      const d = netStore.data;
      if (!d || netStore.busy || netStore.restarting) return;
      const row = (d.modes || []).find((m) => m && m.id === mode) || {};
      netStore.notice = null;
      netStore.refused = null;
      netStore.confirm = null;
      if (mode === (d.configured || {}).mode) { netRender(); return; }
      if (row.allowed === false) {
        // Refused by the gateway's own check (modes[].reason/fix): say why and
        // how to fix it; nothing is sent.
        netStore.refused = { mode, reason: row.reason, fix: row.fix };
        netRender();
        return;
      }
      await netPost({ mode }, mode);
    }
    async function netPost(body, mode) {
      netStore.busy = mode;
      netRender();
      try {
        const res = await api("/api/gateway/network", { method: "POST", body: JSON.stringify(body) });
        const conf = (res && res.configured) || {};
        netStore.confirm = null;
        // Pending a restart, the restart box below says it (with the button
        // or the reason); otherwise the change is already live.
        netStore.notice = res && res.restart_required ? null : { tone: "ok", text: `Saved: ${conf.label || mode}. The gateway already runs this way.` };
      } catch (err) {
        const data = (err && err.data) || {};
        if (err && err.status === 409 && data.reason_code === "acknowledgement_required") {
          netStore.confirm = { mode, reason: data.refused_reason || "", warnings: Array.isArray(data.warnings) ? data.warnings : [] };
        } else if (err && err.status === 409) {
          netStore.refused = { mode, reason: data.refused_reason || String(err.message || err), fix: data.fix || "", details: uiErrorDetails(err) };
        } else {
          netStore.notice = { tone: "err", text: `Could not change who can reach this gateway: ${String((err && err.message) || err)}`, details: uiErrorDetails(err) };
        }
      }
      netStore.busy = "";
      await netRefresh();
    }
    async function netRestart() {
      netStore.restarting = true;
      netStore.notice = { tone: "info", text: "Restarting the gateway... this page reconnects by itself." };
      netRender();
      let res = null;
      try {
        res = await api("/api/gateway/network/restart", { method: "POST", body: JSON.stringify({}) });
      } catch (err) {
        netStore.restarting = false;
        netStore.notice = { tone: "err", text: `The gateway did not restart: ${String((err && err.message) || err)}`, details: uiErrorDetails(err) };
        netRender();
        return;
      }
      const next = (res && res.next) || {};
      const deadline = Date.now() + 90000;
      await new Promise((r) => setTimeout(r, 2000));
      while (Date.now() < deadline) {
        try {
          const d = await api("/api/gateway/network", { timeoutMs: 4000 });
          if (d && d.schema === "gateway_network_v1" && !d.restart_required) {
            netStore.data = d;
            netStore.restarting = false;
            netStore.notice = { tone: "ok", text: `Restarted: ${(d.effective || {}).label || "the new setting"} on port ${(d.effective || {}).port || ""} is live.` };
            netRender();
            return;
          }
        } catch { /* still restarting */ }
        await new Promise((r) => setTimeout(r, 1500));
      }
      netStore.restarting = false;
      netStore.notice = { tone: "warn", text: "The gateway has not answered on this address yet.", hint: next.reconnect_url ? `If the port changed, open ${next.reconnect_url}/console.` : "Check the gateway window or the menu bar icon." };
      netRender();
    }
    async function netAction(action) {
      if (action === "refresh") { netStore.notice = null; netStore.refused = null; await netRefresh(); return; }
      if (action === "ack-cancel") { netStore.confirm = null; netRender(); return; }
      if (action === "ack-internet") { const m = (netStore.confirm && netStore.confirm.mode) || "internet"; await netPost({ mode: m, acknowledge_internet: true }, m); return; }
      if (action === "restart") { await netRestart(); return; }
      if (action === "origin-add") { await netOriginAdd(); return; }
      if (action === "lookup-public") { netStore.publicLoading = true; netRender(); await netRefresh({ lookupPublic: true }); netStore.publicLoading = false; netRender(); }
    }
    function mountNetworkPanel(key, el) {
      if (!el) return;
      netStore.views.set(key, el);
      el.onclick = (event) => {
        const t = event && event.target && event.target.closest ? event.target : null;
        if (!t) return;
        const copy = t.closest("[data-net-copy]");
        if (copy) { uiCopy(copy.dataset.netCopy); return; }
        const mode = t.closest("[data-net-mode]");
        if (mode && !mode.disabled) { netChoose(mode.dataset.netMode); return; }
        const rm = t.closest("[data-net-origin-remove]");
        if (rm && !rm.disabled) { netOriginRemove(rm.dataset.netOriginRemove); return; }
        const act = t.closest("[data-net-action]");
        if (act && !act.disabled) netAction(act.dataset.netAction);
      };
      // Reverse proxy: the typed origin (no re-render per key), the trust
      // switch, and the disclosure's open state (kept across re-renders).
      el.oninput = (event) => {
        const i = event && event.target && event.target.matches && event.target.matches("[data-net-origin-input]") ? event.target : null;
        if (!i) return;
        netStore.proxy.draft = i.value;
        if (netStore.proxy.error) {
          netStore.proxy.error = "";
          i.removeAttribute("aria-invalid");
          const msg = el.querySelector("#net-origin-msg");
          if (msg) { msg.textContent = ""; msg.className = "ui-field-msg"; msg.setAttribute("role", "status"); }
        }
      };
      el.onchange = (event) => {
        const c = event && event.target && event.target.matches && event.target.matches("[data-net-trust]") ? event.target : null;
        if (!c || c.disabled) return;
        netProxySave("trust_proxy", { trust_proxy: !!c.checked });
      };
      el.addEventListener("toggle", (event) => {
        const dt = event && event.target;
        if (dt && dt.matches && dt.matches("[data-net-proxy]")) netStore.proxy.open = !!dt.open;
      }, true);
      // Arrow keys move through the three modes (radio-group convention);
      // Enter/Space choose one (native buttons).
      el.onkeydown = (event) => {
        if (event && event.key === "Enter" && event.target && event.target.matches && event.target.matches("[data-net-origin-input]")) {
          event.preventDefault();
          netOriginAdd();
          return;
        }
        const cur = event && event.target && event.target.closest ? event.target.closest("[data-net-mode]") : null;
        if (!cur || !["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"].includes(event.key)) return;
        const all = Array.from(el.querySelectorAll("[data-net-mode]"));
        const i = all.indexOf(cur);
        const next = all[(i + (event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : all.length - 1)) % all.length];
        if (next) { event.preventDefault(); all.forEach((b) => b.setAttribute("tabindex", b === next ? "0" : "-1")); next.focus(); }
      };
      netRender();
      netRefresh();
    }
    function unmountNetworkPanel(key) { netStore.views.delete(key); }

    // ---- Apps settings (mission Z): the apps.* runtime-config keys ----
    // GET /api/gateway/admin/runtime-config -> apps{name: {key, label, help,
    // placeholder, default, env_name, value, source: stored|env|default,
    // note?, env_shadowed?, invalid_stored?, invalid_env?}} (registry
    // runtime_config.APPS_SETTINGS: rendered as listed, never a hardcoded
    // list). Save = POST {"apps.<name>": value} with only the changed keys; an
    // emptied field clears back to env/default. The gateway validates; its
    // sentence (400 detail) is shown verbatim, as in the TUI and the CLI.
    const appsSetStore = { data: null, error: "", saving: false, saved: null, draft: {}, open: null, views: new Map() };
    function appsSettingsMarkup() {
      const st = appsSetStore;
      if (st.error && !st.data) return `<div class="ui-alert tone-err" role="alert"><strong>Could not read the apps settings.</strong><span>${esc(st.error)}</span></div>`;
      if (!st.data) return `<div class="ui-empty">Reading the apps settings...</div>`;
      const rows = Object.entries(st.data.apps || {});
      if (!rows.length) return "";
      const admin = !!st.data.writable;
      const custom = rows.filter(([, r]) => r.source !== "default").length;
      const open = st.open === null ? custom > 0 : st.open;
      let out = `<details class="ui-details ui-net-proxy" data-apps-settings${open ? " open" : ""}><summary><span class="ui-net-proxy__title">Advanced: apps settings</span>`
        + `<span class="ui-net-proxy__sum">${esc(custom ? `${custom} changed from the default` : "all defaults")}</span></summary><div class="ui-net-proxy__body"><div class="ui-apps-settings__rows">`;
      for (const [name, r] of rows) {
        const stored = r.source === "stored" ? String(r.value ?? "") : "";
        const val = Object.prototype.hasOwnProperty.call(st.draft, name) ? st.draft[name] : stored;
        const pill = r.source === "stored" ? uiPill("Saved setting", "info") : r.source === "env" ? uiPill("From the environment", "warn", r.note || "") : uiPill("Default", "muted");
        const now = String(r.value ?? "") || "(none)";
        out += `<div class="ui-apps-setting" data-apps-setting="${esc(name)}"><div class="ui-apps-setting__head"><label for="apps-set-${esc(name)}">${esc(r.label || r.key || name)}</label>${pill}</div>`
          + `<input type="text" id="apps-set-${esc(name)}" data-apps-input="${esc(name)}" autocomplete="off" spellcheck="false" value="${esc(val)}" placeholder="${esc(r.source === "stored" ? (r.placeholder || "") : now)}"${admin && !st.saving ? "" : " disabled"}>`
          + `<p class="ui-net-proxy__text">${esc(r.help || "")}</p>`
          + (r.source === "env" && r.note ? `<p class="ui-field-msg tone-warn">${esc(r.note)}</p>` : "")
          + (r.env_shadowed && r.note ? `<p class="ui-net-proxy__text">${esc(r.note)}</p>` : "")
          + (r.invalid_stored || r.invalid_env ? `<p class="ui-field-msg tone-warn">Set aside: ${esc(r.invalid_stored || r.invalid_env)}</p>` : "")
          + `<span class="ui-advanced ui-sub"><code>abstractgateway apps config set ${esc(name)} …</code></span></div>`;
      }
      out += `</div>`;
      if (admin) {
        out += `<div class="ui-card__actions"><button type="button" class="ui-btn is-primary" data-apps-settings-save${st.saving ? ' disabled aria-busy="true"' : ""}>${st.saving ? "Saving..." : "Save apps settings"}</button></div>`;
      }
      if (st.saved) out += `<p class="ui-net-proxy__saved tone-${esc(st.saved.tone)}" role="status" data-apps-settings-saved><b>${esc(st.saved.head)}</b><span>${esc(st.saved.text)}</span></p>`;
      else out += `<p class="ui-net-proxy__saved" role="status"><span>${admin ? "Empty = the default (or the value this gateway's environment gives). Applies at the next app start or download." : "Only an admin can change these."}</span></p>`;
      return out + `</div></details>`;
    }
    function appsSettingsRender() { for (const el of appsSetStore.views.values()) if (el) el.innerHTML = appsSettingsMarkup(); }
    async function appsSettingsRefresh() {
      try {
        appsSetStore.data = await api("/api/gateway/admin/runtime-config");
        appsSetStore.error = "";
      } catch (err) {
        appsSetStore.error = String((err && err.message) || err);
      }
      appsSettingsRender();
    }
    async function appsSettingsSave() {
      const st = appsSetStore;
      if (!st.data || st.saving) return;
      const body = {};
      for (const [name, r] of Object.entries(st.data.apps || {})) {
        if (!Object.prototype.hasOwnProperty.call(st.draft, name)) continue;
        const was = r.source === "stored" ? String(r.value ?? "") : "";
        const now = String(st.draft[name] || "").trim();
        if (now !== was) body[r.key || `apps.${name}`] = now;
      }
      if (!Object.keys(body).length) { st.saved = { tone: "ok", head: "Nothing changed", text: "" }; appsSettingsRender(); return; }
      st.saving = true;
      st.saved = null;
      appsSettingsRender();
      try {
        st.data = await api("/api/gateway/admin/runtime-config", { method: "POST", body: JSON.stringify(body) });
        st.draft = {};
        st.saved = { tone: "ok", head: "Saved", text: "Applies at the next app start or download." };
      } catch (err) {
        const data = (err && err.data) || {};
        st.saved = { tone: "err", head: "Not saved", text: String((data && data.detail) || (err && err.message) || err) };
      }
      st.saving = false;
      appsSettingsRender();
    }
    function mountAppsSettings(key, el) {
      if (!el) return;
      appsSetStore.views.set(key, el);
      el.onclick = (event) => {
        const b = event && event.target && event.target.closest ? event.target.closest("[data-apps-settings-save]") : null;
        if (b && !b.disabled) appsSettingsSave();
      };
      el.oninput = (event) => {
        const i = event && event.target && event.target.matches && event.target.matches("[data-apps-input]") ? event.target : null;
        if (i) appsSetStore.draft[i.dataset.appsInput] = i.value;
      };
      el.onkeydown = (event) => {
        if (event && event.key === "Enter" && event.target && event.target.matches && event.target.matches("[data-apps-input]")) { event.preventDefault(); appsSettingsSave(); }
      };
      el.addEventListener("toggle", (event) => {
        const dt = event && event.target;
        if (dt && dt.matches && dt.matches("[data-apps-settings]")) appsSetStore.open = !!dt.open;
      }, true);
      appsSettingsRender();
      appsSettingsRefresh();
    }

    // ---- The kit islands: AfTopBarActions + AfAppearanceDialog ----
    const islands = { lib: null, topbar: null, appearance: null, appearanceOpen: false, phase: "loading", signingOut: false, identity: "" };
    function islandsLib() {
      try {
        const w = typeof window !== "undefined" ? window : null;
        const lib = w && w.AfConsoleIslands;
        return lib && typeof lib.mountTopBar === "function" ? lib : null;
      } catch { return null; }
    }
    function kitAppearance(value) {
      const v = value || {};
      const header = String(v.header_density || "standard");
      return {
        theme: String(v.theme || "dark"),
        font_scale: String(v.font_scale || "md"),
        header_density: header === "large" ? "spacious" : header,
      };
    }
    function topBarIslandProps() {
      const p = state.principal;
      return {
        assistant: p ? { open: !!assistantState.open, onToggle: () => toggleAssistant(), label: "Docs assistant" } : null,
        appearance: { onOpen: openAppearance, label: "Appearance" },
        extras: [
          // The gateway's primary address (GET /network `copy_hint`: the
          // address other devices use), with a copy button beside it.
          { id: "island-address", label: `Gateway address: ${netPrimaryUrl()}`, text: netPrimaryUrl().replace(/^https?:\/\//, ""), hidden: !p || !netPrimaryUrl() },
          { id: "island-address-copy", label: "Copy the gateway address", icon: "copy", hidden: !p || !netPrimaryUrl(), onClick: () => uiCopy(netPrimaryUrl()) },
          { id: "island-open-setup", label: "Setup guide", icon: "settings", hidden: !(p && p.admin), onClick: () => openFirstRunWizard(firstRun.step || "welcome") },
          { id: "island-identity", label: "Signed in as", text: islands.identity, hidden: !p || !islands.identity },
        ],
        connection: {
          phase: islands.phase,
          signingOut: islands.signingOut,
          onConnect: () => { const t = $("login-token"); if (t && t.focus) t.focus(); },
          onDisconnect: () => { islands.signingOut = true; renderIslands(); signOut(); },
        },
      };
    }
    function appearanceIslandProps() {
      return {
        open: islands.appearanceOpen,
        value: kitAppearance(state.appearance),
        onChange: (next) => {
          state.appearance = kitAppearance(next);
          applyAppearanceSettings();
          saveAppearanceSettings();
          renderIslands();
        },
        onClose: () => { islands.appearanceOpen = false; renderIslands(); },
        title: "Appearance",
        note: "Saved in this browser for the gateway console.",
      };
    }
    function renderIslands() {
      if (islands.topbar) islands.topbar.update(topBarIslandProps());
      if (islands.appearance) islands.appearance.update(appearanceIslandProps());
    }
    function islandsSetConnection(ok, text) {
      islands.phase = ok ? "connected" : "disconnected";
      islands.identity = ok ? String(text || "") : "";
      renderIslands();
    }
    function mountConsoleIslands() {
      const lib = islandsLib();
      if (!lib) {
        if (typeof window !== "undefined" && window.document && window.document.getElementById("af-console-islands")) {
          console.error("AbstractGateway console: the abstractuic islands bundle did not load; showing the static top bar.");
        }
        return;
      }
      islands.lib = lib;
      const host = $("af-topbar-root");
      const legacy = $("topbar-static");
      islands.topbar = lib.mountTopBar(host, topBarIslandProps());
      islands.appearance = lib.mountAppearance($("af-appearance-root"), appearanceIslandProps());
      host.classList.remove("hidden");
      legacy.classList.add("hidden");
      applyAppearanceSettings();
    }
"""
