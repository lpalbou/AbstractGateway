"""The Accounts table's icon actions and card list, the Runtimes account filter (round 8), and
the workspace folders modals (round 9, DESIGN.md R9.2).

Accounts (console.py renderAccounts): columns Name · Email (ONE column, "address · state") ·
Runtime (a link to the Runtimes page filtered to that account) · Active · Actions. The actions are
44 px icon buttons with the KIT tooltip (`data-af-tip`, bound once by the islands' bindTooltips;
an explicit sentence per button, e.g. "Archive alice (kept, hidden)") — no "⋯" menu, no labels, no
native `title`. The table never scrolls sideways at 1280-2560 px; below ~900 px (or when the table
itself is narrower than its column minimums) the rows become flat cards and the actions wrap.

Runtimes: `#runtimes?account=<id>[&tenant_id=<t>]` = GET /admin/runtimes?account=<id>; the filter
shows as a removable chip ("Account: alice ×"); the link survives a reload.

Workspace folders (round 9; there is NO Workspaces page any more):
- "Shared workspace & allowed folders" (admins, top of Accounts) opens the GATEWAY policy modal:
  GET /workspace/policy, one PUT /workspace/policy per change. Exactly two dimensions (DESIGN R9
  FINAL): the posture (Only allowed folders | Any folder except denied, with ONE default mode for
  everything else under the latter) and ONE folder list whose rows carry Read-only / Read & write /
  Denied; plus the shared workspace (always Read & write). Folder rows apply on BLUR (Enter blurs):
  POST /workspace/path-check first — an invalid path says why and is NOT saved — then the PUT,
  then an inline "Saved". No Save button anywhere.
- The folder icon on every Accounts row (human, entity, admin) opens THAT account's modal: the kit
  WorkspaceChooser (islands mountWorkspaceChooser — the same rows and words as AbstractCode and the
  Assistant), state from GET /workspace/policy/{account}, each change one PUT to the same path.
  The signed-in user's own row uses `me`.

Spliced into the console script scope with the UI layer (console.py _console_owned_sources): `api`,
`esc`, `$`, `state`, `svgIcon`, `ICONS`, `afSwitchCreate`, `afSwitchBind`, `afSwitchSet`,
`setActiveTab`, `loadRuntimes`, `selectRuntime`, `emailErrorText`, `islandsLib`,
`bindAccountModal` are the console's own.
"""

from __future__ import annotations

# The card-list rules, used twice: below ~900 px of viewport, and whenever the table's own
# width is under its column minimums (3 text columns x 110 px + Active 84 + Actions 308 = 722,
# e.g. a large font scale).
_ACCOUNTS_CARD_RULES = r"""
  .accounts-table, .accounts-table tbody, .accounts-table tr { display: block; width: 100%; }
  .accounts-table thead, .accounts-table colgroup { display: none; }
  .accounts-table tr.accounts-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 12px; padding: 12px 12px 12px 14px; border-top: 1px solid var(--line-soft); }
  .accounts-table tr.accounts-row > td { display: block; padding: 0; border: 0; min-width: 0; width: auto; overflow: visible; background: transparent !important; box-shadow: none !important; }
  .accounts-table tr.accounts-row > td::before { content: none; }
  .accounts-table td.accounts-name { grid-column: 1; grid-row: 1; align-self: center; }
  .accounts-table td.accounts-active { grid-column: 2; grid-row: 1; justify-self: end; align-self: center; }
  .accounts-table td.accounts-col-email { grid-column: 1 / -1; grid-row: 2; }
  .accounts-table td.accounts-col-runtime { grid-column: 1 / -1; grid-row: 3; color: var(--af-row-text-muted, var(--muted)); }
  .accounts-table td.accounts-col-runtime::before { content: "Runtime "; color: var(--af-row-text-muted, var(--muted)); }
  .accounts-table td.accounts-col-runtime > .accounts-cell-text { display: inline; }
  .accounts-table td.accounts-actions { grid-column: 1 / -1; grid-row: 4; margin-top: 6px; }
  .accounts-actions__buttons { flex-wrap: wrap; gap: 6px; }
  .accounts-table tr.af-row--admin { background-color: var(--af-row-tint-admin); box-shadow: inset 3px 0 0 var(--af-row-mark-admin); }
  .accounts-table tr.af-row--user { box-shadow: inset 3px 0 0 var(--af-row-mark-user); }
  .accounts-table tr.af-row--entity { background-color: var(--af-row-tint-entity); box-shadow: inset 3px 0 0 var(--af-row-mark-entity); }
  .accounts-table .row-confirm, .accounts-table .row-confirm > td { display: block; padding: 0 0 10px; border: 0; }
"""

ACCOUNTS_CSS = r"""
/* ---- Icon buttons (round 8; shared): 44 px targets, the accessible name in aria-label, the
   explanation in data-af-tip (the kit tooltip, round 9: themed, 150 ms, keyboard focus, kept
   inside the viewport). No CSS tooltip and no native title. */
.icon-btn { position: relative; display: inline-flex; align-items: center; justify-content: center; flex: 0 0 44px; width: 44px; height: 44px; min-width: 44px; min-height: 44px; padding: 0; border: 1px solid var(--ui-border-1, var(--line)); border-radius: var(--radius-md); background: var(--bg-primary); color: var(--text-secondary); box-shadow: none; cursor: pointer; }
.icon-btn:hover:not(:disabled) { background: var(--ui-surface-2, var(--bg-secondary)); color: var(--text-primary, var(--text)); filter: none; }
.icon-btn--danger:hover:not(:disabled) { color: var(--error, var(--danger, #c0392b)); border-color: color-mix(in srgb, var(--error, #c0392b) 45%, transparent); }
.icon-btn:focus-visible { outline: 2px solid var(--info, var(--accent)); outline-offset: 2px; }
.icon-btn svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; pointer-events: none; }

/* ---- Accounts table (round 8): Name · Email · Runtime · Active · Actions; table-layout fixed,
   cells wrap (never truncate). Actions = up to 6 icon buttons on one line (6 x 44 + 5 x 4 + 20). */
.accounts-page .users-table-wrap { overflow: visible; container: accounts / inline-size; }
.accounts-table { width: 100%; table-layout: fixed; border-collapse: collapse; }
.accounts-table col.accounts-c-name { width: 24%; }
.accounts-table col.accounts-c-runtime { width: 18%; }
.accounts-table col.accounts-c-active { width: 84px; }
.accounts-table col.accounts-c-actions { width: 308px; }
.accounts-table th { text-align: left; white-space: normal; overflow-wrap: anywhere; }
.accounts-table th, .accounts-table td { padding-left: 10px; padding-right: 10px; }
.accounts-table td { vertical-align: middle; padding-top: 8px; padding-bottom: 8px; overflow: hidden; }
.accounts-cell-text { display: block; min-width: 0; max-width: 100%; white-space: normal; overflow-wrap: anywhere; word-break: normal; }
code.accounts-cell-text { padding: 0; border: 0; background: transparent; box-shadow: none; color: inherit; font-size: var(--font-size-md); }
a.accounts-runtime-link { font-family: var(--font-mono); font-size: var(--font-size-md); color: var(--info, var(--accent)); text-decoration: none; }
a.accounts-runtime-link:hover { text-decoration: underline; }
a.accounts-runtime-link:focus-visible { outline: 2px solid var(--info, var(--accent)); outline-offset: 2px; border-radius: var(--radius-sm); }
.accounts-email__reason { margin-top: 2px; font-size: var(--af-helper-size, var(--font-size-md)); }
.accounts-name__line { display: flex; align-items: center; gap: 4px 8px; min-width: 0; flex-wrap: wrap; }
.accounts-name__line > strong { flex: 0 1 auto; min-width: 0; max-width: 100%; font-weight: 600; }
.accounts-name__line > .af-kind-chip, .accounts-archived-chip { flex: 0 0 auto; }
.accounts-archived-chip { display: inline-flex; align-items: center; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--line); color: var(--muted); font-size: var(--font-size-sm, 12px); font-weight: 600; line-height: 1.5; white-space: nowrap; }
.accounts-row--archived .accounts-name__line > strong { color: var(--muted); }
/* The unavailable reason is the switch's title + aria-describedby (the column stays narrow). */
.accounts-table td.accounts-active .af-switch__reason { display: none; }
.accounts-active__archived { font-size: var(--font-size-md); }
.accounts-table td.accounts-actions { overflow: visible; }
.accounts-actions__buttons { display: flex; flex-wrap: nowrap; align-items: center; gap: 4px; }
/* Cards below ~900 px of viewport: in the drawer range (no docked sidebar, < 1024 px, a named
   breakpoint) the table is the viewport minus ~66 px of gutters, so a table under 834 px = a
   viewport under ~900 px. At 1024 px and up the docked sidebar leaves >= 750 px: the table fits. */
@media (max-width: 1023.98px) {
  @container accounts (max-width: 833.98px) {
""" + _ACCOUNTS_CARD_RULES + r"""
  }
}
@container accounts (max-width: 721.98px) {
""" + _ACCOUNTS_CARD_RULES + r"""
}

/* ---- Accounts head (round 9): the gateway's folder policy button sits first, at the top. */
.accounts-head__actions .accounts-gw-workspace { margin-right: auto; }

/* ---- Runtimes account filter chip (round 8). */
.runtimes-filter { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 10px; }
.runtimes-filter[hidden] { display: none; }
.runtimes-filter__chip { display: inline-flex; align-items: center; gap: 2px; padding: 0 0 0 12px; border: 1px solid var(--accent); border-radius: 999px; background: var(--accent-subtle, transparent); color: var(--text-primary, var(--text)); font-size: var(--font-size-md); font-weight: 600; }
.runtimes-filter__chip .icon-btn { border: 0; background: transparent; width: 36px; height: 36px; min-width: 36px; min-height: 36px; flex-basis: 36px; border-radius: 999px; }
@media (pointer: coarse) { .runtimes-filter__chip .icon-btn { width: 44px; height: 44px; min-width: 44px; min-height: 44px; flex-basis: 44px; } }
"""

WORKSPACES_CSS = r"""
/* ---- Workspace folders modals (round 9, console_workspaces.py): the gateway policy and one
   account's folders (the kit WorkspaceChooser). */
.wsm-body { display: grid; gap: 16px; min-width: 0; }
/* Informational: the info tone (never the accent red). */
.wsm-summary { margin: 0; padding: 10px 14px; border-left: 3px solid var(--info, var(--accent)); border-radius: var(--radius-sm); background: color-mix(in srgb, var(--info, var(--accent)) 8%, transparent); font-size: var(--font-size-base); line-height: 1.45; overflow-wrap: anywhere; }
.wsm-loading, .wsm-note { margin: 0; color: var(--text-secondary); font-size: var(--font-size-md); }
.wsm-error { margin: 0; color: var(--error, #c0392b); }
/* The modal's title already says "Workspaces — <id>": the chooser's own heading stays for
   screen readers (it labels the section) but is not shown twice. */
.wsm-chooser > .af-workspace > .af-settings-group__head { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
.wsm-reset { display: grid; gap: 6px; padding-top: 12px; border-top: 1px solid var(--line-soft, var(--ui-border-1)); justify-items: start; }
.wsm-confirm { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; padding: 10px 12px; border-radius: var(--radius-md); background: var(--ui-surface-2, var(--bg-secondary)); }
.ws-field { display: grid; gap: 6px; min-width: 0; padding-top: 12px; border-top: 1px solid var(--line-soft, var(--ui-border-1)); }
.ws-field:first-of-type { border-top: 0; padding-top: 0; }
.ws-field__head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }
.ws-field__label { font-size: var(--font-size-md); font-weight: 600; color: var(--text-primary, var(--text)); }
.ws-field__help { margin: 0; font-size: var(--af-helper-size, var(--font-size-md)); color: var(--text-secondary); }
.ws-saved { font-size: var(--font-size-sm); font-weight: 600; color: var(--success, #2f9e44); }
.ws-saved:empty { display: none; }
.ws-saved.is-error { color: var(--error, #c0392b); }
.ws-folders { display: grid; gap: 6px; margin: 0; padding: 0; list-style: none; }
.ws-folder { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 6px; align-items: center; }
.ws-folder--single { grid-template-columns: minmax(0, 1fr); }
.ws-folder--fixed { grid-template-columns: minmax(0, 1fr) auto; }
/* Rows with a permission: path · remove on the first line, Read-only / Read & write under the path. */
.ws-folder--mode { grid-template-columns: minmax(0, 1fr) auto; }
.ws-folder--mode > .ws-mode { grid-column: 1 / -1; justify-self: start; }
.ws-mode { display: inline-flex; gap: 2px; padding: 2px; border: 1px solid var(--ui-border-2, var(--line)); border-radius: var(--radius-md); background: var(--ui-surface-1, transparent); }
.ws-mode__opt { min-height: 32px; padding: 4px 12px; border: 0; border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary); font: inherit; font-size: var(--font-size-md); font-weight: 500; box-shadow: none; cursor: pointer; }
.ws-mode__opt:hover { background: var(--ui-surface-2, var(--bg-secondary)); filter: none; }
.ws-mode__opt.is-on { background: var(--accent-subtle, var(--ui-surface-2)); color: var(--text-primary, var(--text)); box-shadow: inset 0 0 0 2px var(--accent); font-weight: 600; }
.ws-mode__opt:focus-visible { outline: 2px solid var(--info, var(--accent)); outline-offset: 2px; }
.ws-mode__fixed { font-size: var(--font-size-md); font-weight: 600; color: var(--text-secondary); white-space: nowrap; }
@media (pointer: coarse) { .ws-mode__opt { min-height: 44px; } }
.ws-folder input { width: 100%; min-width: 0; min-height: 40px; font-family: var(--font-mono); font-size: var(--font-size-md); }
.ws-folder input[aria-invalid="true"] { border-color: var(--error, #c0392b); }
.ws-folder__state { grid-column: 1 / -1; margin: 0; font-size: var(--font-size-sm); min-height: 0; }
.ws-folder__state:empty { display: none; }
.ws-folder__state.is-ok { color: var(--success, #2f9e44); font-weight: 600; }
.ws-folder__state.is-error { color: var(--error, #c0392b); }
.ws-add { justify-self: start; min-height: 40px; }
.ws-seg .ui-seg__opt { padding: 10px 12px; justify-items: start; justify-content: stretch; text-align: left; }
@media (max-width: 767.98px) {
  .ws-folder input { min-height: 44px; font-size: var(--font-size-base); }
  .ws-add { min-height: 44px; }
}
"""

WORKSPACES_JS = r"""
    // ---- Account icons (round 8): icon-only actions; the explanation is the KIT tooltip
    // (data-af-tip, round 9), the accessible name the aria-label. No native title.
    const ACCOUNT_ICONS = {
      mail: ICONS.mail,
      logs: ICONS.logs,
      folder: ICONS.folder,
      openai: svgIcon('<path d="M4 8h13"></path><path d="m14 4.5 3.5 3.5-3.5 3.5"></path><path d="M20 16H7"></path><path d="m10 12.5-3.5 3.5 3.5 3.5"></path>'),
      manage: svgIcon('<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"></path><circle cx="15" cy="6" r="2"></circle><circle cx="9" cy="12" r="2"></circle><circle cx="17" cy="18" r="2"></circle>'),
      rotate: svgIcon('<circle cx="8" cy="15" r="3.5"></circle><path d="m10.5 12.5 7-7"></path><path d="m15.5 7.5 2 2"></path><path d="M14 4.5a8 8 0 0 1 5.5 5"></path><path d="M20 6v4h-4"></path>'),
      archive: svgIcon('<rect x="3" y="4" width="18" height="4.5" rx="1"></rect><path d="M5 8.5V19a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8.5"></path><path d="M10 12.5h4"></path>'),
      unarchive: svgIcon('<rect x="3" y="4" width="18" height="4.5" rx="1"></rect><path d="M5 8.5V19a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8.5"></path><path d="M12 17.5v-6"></path><path d="m9.5 14 2.5-2.5 2.5 2.5"></path>'),
      close: svgIcon('<path d="M6 6l12 12M18 6 6 18"></path>'),
      remove: svgIcon('<path d="M6 6l12 12M18 6 6 18"></path>'),
    };
    function accountIconButton(icon, tip, aria, danger) {
      const glyph = ACCOUNT_ICONS[icon];
      if (!glyph) throw new Error(`No account icon named ${icon} (console_workspaces.py ACCOUNT_ICONS).`);
      if (!tip) throw new Error(`Account icon ${icon} without a tooltip sentence (R9.2).`);
      const b = document.createElement("button");
      b.type = "button";
      b.className = `icon-btn${danger ? " icon-btn--danger" : ""}`;
      b.setAttribute("aria-label", aria || tip);
      b.setAttribute("data-af-tip", tip);
      b.innerHTML = glyph;
      return b;
    }
    // The tooltip sentences of the Accounts actions (DESIGN.md R9.2), one place.
    const ACCOUNT_TIPS = {
      email: (n) => `Email address and mailbox of ${n}`,
      openai_api: (n) => `OpenAI API access for ${n}`,
      logs: (n) => `Activity log of ${n}`,
      workspace: (n) => `Workspaces ${n}'s agents may use`,
      manage: (n) => `Manage ${n} (mind, voice, prompt…)`,
      rotate: (n) => `Rotate ${n}'s sign-in token`,
      archive: (n) => `Archive ${n} (kept, hidden)`,
      unarchive: (n) => `Unarchive ${n} (comes back inactive)`,
    };

    // ---- Deep link of the Runtimes filter: `#runtimes?account=<id>`.
    function wsHashQuery(tab) {
      let h = "";
      try { h = String(location.hash || ""); } catch { return null; }
      const m = /^#([a-z]+)(?:\?(.*))?$/.exec(h);
      if (!m || m[1] !== tab) return null;
      return new URLSearchParams(m[2] || "");
    }
    function wsSetHash(frag) {
      try {
        if (typeof history !== "undefined" && history && typeof history.replaceState === "function") {
          history.replaceState(null, "", String(location.pathname || "/console") + String(location.search || "") + frag);
        } else {
          location.hash = frag;
        }
      } catch { /* the page still works; only the address bar misses the link */ }
    }
    function wsAccountHref(tab, f) {
      if (!f || !f.account) return `#${tab}`;
      const tenant = f.tenant_id && f.tenant_id !== "default" ? `&tenant_id=${encodeURIComponent(f.tenant_id)}` : "";
      return `#${tab}?account=${encodeURIComponent(f.account)}${tenant}`;
    }
    function wsAccountFromHash(tab) {
      const q = wsHashQuery(tab);
      const account = q ? String(q.get("account") || "").trim() : "";
      if (!account) return null;
      return { account, tenant_id: String(q.get("tenant_id") || "").trim() || null };
    }
    // Leaving the Runtimes page drops its account link (the next reload must not land filtered).
    function wsOnTabChange(tab) {
      if (!state.principal || !state.hashApplied) return;
      if (tab !== "runtimes" && wsHashQuery("runtimes")) wsSetHash("");
    }

    // ---- Runtimes filter (round 8): GET /admin/runtimes?account=<id>, a removable chip.
    function runtimesHref(f) { return wsAccountHref("runtimes", f); }
    function runtimesFilterFromHash() { return wsAccountFromHash("runtimes"); }
    function runtimesClearFilter() { if (wsHashQuery("runtimes")) wsSetHash(""); }
    function openRuntimesFor(f) {
      state.hashApplied = true;
      state.selectedRuntime = null;
      wsSetHash(runtimesHref(f));
      setActiveTab("runtimes");
      loadRuntimes();
    }
    function renderRuntimesFilter(filter) {
      const slot = $("runtimes-filter");
      if (!slot) throw new Error("Runtimes markup has no #runtimes-filter (round-8 seam).");
      slot.textContent = "";
      slot.hidden = !filter;
      if (!filter) return;
      const chip = document.createElement("span");
      chip.className = "runtimes-filter__chip";
      chip.setAttribute("data-runtimes-filter", filter.account);
      const label = document.createElement("span");
      label.textContent = `Account: ${filter.tenant_id && filter.tenant_id !== "default" ? `${filter.tenant_id}/` : ""}${filter.account}`;
      const clear = accountIconButton("close", `Show every runtime, not only ${filter.account}'s`, `Remove the filter: show every runtime, not only ${filter.account}'s`);
      clear.setAttribute("data-runtimes-filter-clear", "");
      clear.onclick = () => {
        wsSetHash("#runtimes");
        state.selectedRuntime = null;
        loadRuntimes();
      };
      chip.append(label, clear);
      slot.append(chip);
    }

    // ---- Workspace folders modals (round 9).
    const wsStore = { gwRelease: null, accRelease: null, accIsland: null, chain: Promise.resolve() };
    function wsEl(tag, cls, text) {
      const el = document.createElement(tag);
      if (cls) el.className = cls;
      if (text !== undefined) el.textContent = text;
      return el;
    }
    function wsPlural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }
    function wsSaved(el, text, error) {
      el.textContent = text || "";
      el.className = `ws-saved${error ? " is-error" : ""}`;
      el.setAttribute("role", error ? "alert" : "status");
      if (el.__t) clearTimeout(el.__t);
      if (text && !error && typeof setTimeout === "function") el.__t = setTimeout(() => { el.textContent = ""; }, 2500);
    }
    // Writes run one after another (a fast blur-then-click never races two PUTs).
    function wsQueue(fn) {
      const next = wsStore.chain.then(fn, fn);
      wsStore.chain = next.catch(() => {});
      return next;
    }
    function wsField(label, help) {
      const box = wsEl("div", "ws-field");
      const head = wsEl("div", "ws-field__head");
      const l = wsEl("span", "ws-field__label", label);
      const saved = wsEl("span", "ws-saved");
      saved.setAttribute("aria-live", "polite");
      head.append(l, saved);
      box.append(head);
      if (help) box.append(wsEl("p", "ws-field__help", help));
      return { box, saved };
    }
    async function wsCheckPath(path) {
      const out = await api("/api/gateway/workspace/path-check", { method: "POST", body: JSON.stringify({ path }) });
      if (!out || typeof out.valid !== "boolean" || typeof out.sentence !== "string") throw new Error("POST /workspace/path-check answered without valid/sentence (round-8 seam).");
      return out;
    }
    function wsRowState(st) {
      return (text, tone) => {
        st.textContent = text || "";
        st.className = `ws-folder__state${tone ? ` is-${tone}` : ""}`;
        st.setAttribute("role", tone === "error" ? "alert" : "status");
        if (st.__t) clearTimeout(st.__t);
        if (tone === "ok" && typeof setTimeout === "function") st.__t = setTimeout(() => { st.textContent = ""; st.className = "ws-folder__state"; }, 2500);
      };
    }
    // Folder rows: an input per folder + a remove icon; "Add folder" adds an empty row. A row
    // applies on BLUR (Enter blurs): path check -> PUT the whole list -> inline "Saved".
    // opts.modes ([[mode, label], …]): rows are {path, mode} with a segmented control per row
    // (Read-only / Read & write; under "Any folder except denied" also Denied); a new row starts at
    // opts.newMode; a mode change is one PUT of the whole list.
    function wsFoldersField(id, label, help, rows, save, opts = {}) {
      const f = wsField(label, help);
      const list = wsEl("ul", "ws-folders");
      list.id = id;
      const modes = Array.isArray(opts.modes) ? opts.modes : null;
      const newMode = opts.newMode || "rw";
      const T = modes ? wsText() : null;
      const saved = () => Array.from(list.querySelectorAll("input")).map((i) => i.dataset.saved || "").filter(Boolean);
      const modeOf = (path) => { const i = Array.from(list.querySelectorAll("input")).find((x) => x.dataset.saved === path); return (i && i.dataset.mode) || newMode; };
      const commit = (next) => wsQueue(() => save(modes ? next.map((path) => ({ path, mode: modeOf(path) })) : next));
      const addRow = (row, focus) => {
        const value = modes ? (row && row.path) || "" : row;
        const li = wsEl("li", "ws-folder");
        const input = document.createElement("input");
        input.type = "text";
        input.spellcheck = false;
        input.autocomplete = "off";
        input.value = value || "";
        input.dataset.saved = value || "";
        if (modes) input.dataset.mode = (row && row.mode) || newMode;
        input.placeholder = "Add a workspace path";
        input.setAttribute("aria-label", `${label}: workspace path`);
        const st = wsEl("p", "ws-folder__state");
        st.setAttribute("aria-live", "polite");
        const say = wsRowState(st);
        const tipFor = (v) => `Remove ${v || "this workspace"} from ${label}`;
        const rm = accountIconButton("remove", tipFor(value), tipFor(value), true);
        rm.setAttribute("data-ws-remove", "");
        input.onkeydown = (ev) => {
          if (ev.key === "Enter") { ev.preventDefault(); input.blur(); }
          if (ev.key === "Escape") { ev.stopPropagation(); input.value = input.dataset.saved || ""; input.removeAttribute("aria-invalid"); say(""); input.blur(); }
        };
        input.onblur = async () => {
          const value = input.value.trim();
          const before = input.dataset.saved || "";
          if (value === before) { if (!value && !before) li.remove(); return; }
          if (!value) {
            // Emptied = removed.
            const next = saved().filter((p, i, all) => !(p === before && all.indexOf(p) === i));
            say("Saving…");
            try { await commit(next); li.remove(); wsSaved(f.saved, "Removed"); }
            catch (e) { input.value = before; say(`${emailErrorText(e)} Not saved.`, "error"); }
            return;
          }
          say("Checking…");
          let check;
          try { check = await wsCheckPath(value); } catch (e) { say(`Not checked: ${emailErrorText(e)}`, "error"); return; }
          if (input.value.trim() !== value) return;  // typed again meanwhile: the next blur decides
          if (!check.valid) { input.setAttribute("aria-invalid", "true"); say(`${check.sentence} Not saved.`, "error"); return; }
          input.removeAttribute("aria-invalid");
          const others = Array.from(list.querySelectorAll("input")).filter((x) => x !== input).map((x) => x.dataset.saved || "").filter(Boolean);
          if (others.includes(check.normalized)) { input.setAttribute("aria-invalid", "true"); say("Already in this list. Not saved.", "error"); return; }
          const next = [];
          for (const x of list.querySelectorAll("input")) {
            if (x === input) next.push(check.normalized);
            else if (x.dataset.saved) next.push(x.dataset.saved);
          }
          say("Saving…");
          const was = input.dataset.saved;
          input.dataset.saved = check.normalized;  // modeOf() reads the row by its saved path
          try {
            await commit(next);
            input.value = check.normalized;
            rm.setAttribute("aria-label", tipFor(check.normalized));
            rm.setAttribute("data-af-tip", tipFor(check.normalized));
            say("Saved", "ok");
          } catch (e) {
            input.dataset.saved = was;
            input.setAttribute("aria-invalid", "true");
            say(`${emailErrorText(e)} Not saved.`, "error");
          }
        };
        rm.onclick = async () => {
          const was = input.dataset.saved || "";
          if (!was) { li.remove(); return; }
          const next = saved().filter((p) => p !== was);
          rm.setAttribute("aria-busy", "true");
          try { await commit(next); li.remove(); wsSaved(f.saved, "Removed"); }
          catch (e) { rm.removeAttribute("aria-busy"); say(`${emailErrorText(e)} Not removed.`, "error"); }
        };
        li.append(input, rm);
        if (modes) {
          // Read-only / Read & write: a two-option segmented control (radio group), one PUT per change.
          const seg = wsEl("div", "ws-mode");
          seg.setAttribute("role", "radiogroup");
          seg.setAttribute("aria-label", `${T.accessLabel}: ${value || "new workspace"}`);
          const opts2 = modes;
          const btns = [];
          const paint = () => { for (const b of btns) { const on = b.dataset.mode === (input.dataset.mode || newMode); b.classList.toggle("is-on", on); b.setAttribute("aria-checked", on ? "true" : "false"); b.tabIndex = on ? 0 : -1; } };
          for (const [m, text] of opts2) {
            const b = wsEl("button", "ws-mode__opt", text);
            b.type = "button";
            b.setAttribute("role", "radio");
            b.dataset.mode = m;
            b.setAttribute("data-ws-mode", m);
            b.onclick = async () => {
              if ((input.dataset.mode || newMode) === m || seg.getAttribute("aria-busy") === "true") return;
              const before = input.dataset.mode || newMode;
              input.dataset.mode = m;
              paint();
              if (!input.dataset.saved) return;  // a new row: the mode goes with its first save
              seg.setAttribute("aria-busy", "true");
              say("Saving…");
              try { await commit(saved()); say("Saved", "ok"); }
              catch (e) { input.dataset.mode = before; paint(); say(`${emailErrorText(e)} Not saved.`, "error"); }
              finally { seg.removeAttribute("aria-busy"); }
            };
            b.onkeydown = (ev) => {
              if (!["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"].includes(ev.key)) return;
              ev.preventDefault();
              const other = btns[(btns.indexOf(b) + 1) % btns.length];
              other.focus();
              other.click();
            };
            btns.push(b);
            seg.append(b);
          }
          paint();
          li.classList.add("ws-folder--mode");
          li.append(seg);
        }
        li.append(st);
        list.append(li);
        if (focus) { try { input.focus(); } catch {} }
        return li;
      };
      for (const r of rows) addRow(r, false);
      const add = wsEl("button", "secondary ws-add", "Add a workspace");
      add.type = "button";
      add.setAttribute("data-ws-add", id);
      add.onclick = () => {
        const empty = Array.from(list.querySelectorAll("input")).find((i) => !i.value.trim());
        if (empty) { try { empty.focus(); } catch {} return; }
        addRow(modes ? { path: "", mode: newMode } : "", true);
      };
      f.box.append(list, add);
      return f.box;
    }
    // The shared workspace: one required folder; same blur rule; empty is refused (kept as was).
    function wsSharedField(id, value, save) {
      const f = wsField(wsText().sharedLabel, `${wsText().sharedHelp} Required.`);
      const row = wsEl("div", "ws-folder ws-folder--single");
      const input = document.createElement("input");
      input.type = "text";
      input.id = id;
      input.spellcheck = false;
      input.autocomplete = "off";
      input.value = value || "";
      input.dataset.saved = value || "";
      input.placeholder = "Add a workspace path";
      input.setAttribute("aria-label", "Shared workspace");
      const st = wsEl("p", "ws-folder__state");
      st.setAttribute("aria-live", "polite");
      const say = wsRowState(st);
      input.onkeydown = (ev) => {
        if (ev.key === "Enter") { ev.preventDefault(); input.blur(); }
        if (ev.key === "Escape") { ev.stopPropagation(); input.value = input.dataset.saved || ""; input.removeAttribute("aria-invalid"); say(""); input.blur(); }
      };
      input.onblur = async () => {
        const v = input.value.trim();
        const before = input.dataset.saved || "";
        if (v === before) return;
        if (!v) { input.value = before; say("The shared workspace is required. Not saved.", "error"); return; }
        say("Checking…");
        let check;
        try { check = await wsCheckPath(v); } catch (e) { say(`Not checked: ${emailErrorText(e)}`, "error"); return; }
        if (input.value.trim() !== v) return;
        if (!check.valid) { input.setAttribute("aria-invalid", "true"); say(`${check.sentence} Not saved.`, "error"); return; }
        input.removeAttribute("aria-invalid");
        say("Saving…");
        try {
          await wsQueue(() => save(check.normalized));
          input.value = check.normalized;
          input.dataset.saved = check.normalized;
          say("Saved", "ok");
        } catch (e) { input.setAttribute("aria-invalid", "true"); say(`${emailErrorText(e)} Not saved.`, "error"); }
      };
      const rw = wsEl("span", "ws-mode__fixed", wsText().accessReadWrite);
      rw.setAttribute("data-ws-shared-mode", "rw");
      row.classList.add("ws-folder--fixed");
      row.append(input, rw, st);
      f.box.append(row);
      return f.box;
    }

    // -- The GATEWAY policy modal ("Shared workspace & allowed folders", admins). Two postures
    // (R9 amendments): "Only allowed folders" (the shared workspace + allowed folders) or "Any
    // folder except denied" (the Never allowed list); the matching list shows under the switch.
    // Words: the kit chooser's ONE wording table (islands workspaceChooserText), so the console, Code
    // and the Assistant say the same thing.
    function wsText() {
      const lib = islandsLib();
      const T = lib && lib.workspaceChooserText;
      if (!T || !T.postureAllowedOnly || !T.postureAnyExceptDenied || !T.accessRead || !T.accessReadWrite || !T.accessDenied || !T.everythingElse || !T.accessLabel || !T.allowedTitle || !T.deniedTitle || !T.sharedLabel) throw new Error("AbstractGateway console: the islands bundle has no workspaceChooserText (ui-kit 0.8.1+ required).");
      return T;
    }
    function wsPostures() {
      const T = wsText();
      return [
        { id: "allowed_only", title: T.postureAllowedOnly, text: T.postureAllowedOnlyHelp },
        { id: "any_except_denied", title: T.postureAnyExceptDenied, text: T.postureAnyExceptDeniedHelp },
      ];
    }
    const WS_POSTURE_IDS = ["allowed_only", "any_except_denied"];
    // The R9 WORKSPACE API (FINAL): {shared_workspace, posture, default_mode, folders: [{path, mode}]},
    // one `folders` list for both postures (mode ro | rw | deny).
    const WS_MODES = ["ro", "rw", "deny"];
    function wsGatewayPolicy(out) {
      const p = out && out.policy;
      if (!p || typeof p.shared_workspace !== "string" || !WS_POSTURE_IDS.includes(p.posture) || !["ro", "rw"].includes(p.default_mode)
        || !Array.isArray(p.folders) || !p.folders.every((r) => r && typeof r.path === "string" && WS_MODES.includes(r.mode))) {
        throw new Error("GET/PUT /workspace/policy answered without the gateway policy fields (R9 WORKSPACE API seam: shared_workspace, posture, default_mode, folders [{path, mode}]).");
      }
      return p;
    }
    // The effective line, byte-exact (DESIGN R9 FINAL, ADVERSARY V15): posture label first (with the
    // default mode under "Any folder except denied"), then the shared workspace, then each folder with
    // its mode. The same format as effective.summary; presentation of the server's values only.
    const WS_MODE_WORD = { rw: "rw", ro: "ro", deny: "refused" };
    function wsGatewaySummary(p) {
      const T = wsText();
      const head = p.posture === "allowed_only" ? T.postureAllowedOnly : `${T.postureAnyExceptDenied} (${WS_MODE_WORD[p.default_mode]})`;
      return [head, `${T.sharedLabel} (rw)`, ...p.folders.map((r) => `${r.path} (${WS_MODE_WORD[r.mode]})`)].join(" · ");
    }
    // The posture: a segmented switch (radio group), applies on click (one PUT).
    function wsPostureField(current, apply) {
      const f = wsField("Workspaces agents may use", "");
      const seg = wsEl("div", "ui-seg ws-seg");
      seg.setAttribute("role", "radiogroup");
      seg.setAttribute("aria-label", "Workspaces agents may use");
      const buttons = [];
      for (const o of wsPostures()) {
        const on = o.id === current;
        const b = wsEl("button", `ui-seg__opt${on ? " is-on" : ""}`);
        b.type = "button";
        b.setAttribute("role", "radio");
        b.setAttribute("aria-checked", on ? "true" : "false");
        b.setAttribute("data-ws-posture", o.id);
        b.tabIndex = on ? 0 : -1;
        b.innerHTML = `<span class="ui-seg__title">${esc(o.title)}</span><span class="ui-seg__text">${esc(o.text)}</span>`;
        b.onclick = async () => {
          if (b.getAttribute("aria-checked") === "true" || seg.getAttribute("aria-busy") === "true") return;
          seg.setAttribute("aria-busy", "true");
          wsSaved(f.saved, "Saving…");
          try { await wsQueue(() => apply(o.id)); }
          catch (e) { seg.removeAttribute("aria-busy"); wsSaved(f.saved, `${emailErrorText(e)} Not saved.`, true); }
        };
        b.onkeydown = (ev) => {
          if (!["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"].includes(ev.key)) return;
          ev.preventDefault();
          buttons[(buttons.indexOf(b) + 1) % buttons.length].focus();
        };
        buttons.push(b);
        seg.append(b);
      }
      f.box.append(seg);
      return f.box;
    }
    function closeGatewayWorkspace() {
      const backdrop = $("gateway-workspace-backdrop");
      if (backdrop.hidden) return;
      $("gateway-workspace-body").textContent = "";
      backdrop.hidden = true;
      const release = wsStore.gwRelease;
      wsStore.gwRelease = null;
      if (release) release();
    }
    function wsRenderGateway(body, p, focusPosture) {
      body.textContent = "";
      const T = wsText();
      const summary = wsEl("p", "wsm-summary", wsGatewaySummary(p));
      summary.setAttribute("data-ws-summary", "");
      summary.setAttribute("role", "status");
      let cur = p;
      const put = async (change) => {
        const out = wsGatewayPolicy(await api("/api/gateway/workspace/policy", { method: "PUT", body: JSON.stringify(change) }));
        cur = out;
        summary.textContent = wsGatewaySummary(cur);
        return out;
      };
      const posture = wsPostureField(p.posture, async (next) => {
        const out = await put({ posture: next });
        wsRenderGateway(body, out, next);
      });
      body.append(summary, posture, wsSharedField("wsg-shared", p.shared_workspace, (path) => put({ shared_workspace: path })));
      // ONE list for both postures; each row Read & write / Read-only / Refused.
      const modes = [["rw", T.accessReadWrite], ["ro", T.accessRead], ["deny", T.accessDenied]];  // the kit chooser's order
      if (p.posture === "allowed_only") {
        body.append(wsFoldersField("wsg-folders", T.allowedTitle, "The workspaces agents may use besides the shared workspace. Refused keeps a workspace out of an allowed one.", p.folders, (list) => put({ folders: list }), { modes, newMode: "rw" }));
      } else {
        body.append(wsDefaultModeField(p.default_mode, (m) => put({ default_mode: m })));
        body.append(wsFoldersField("wsg-folders", T.deniedTitle, "Refused workspaces, or workspaces with their own permission.", p.folders, (list) => put({ folders: list }), { modes, newMode: "deny" }));
      }
      for (const sec of body.querySelectorAll(".ws-field")) sec.setAttribute("data-ws-section", "");
      if (focusPosture) { const b = body.querySelector(`[data-ws-posture="${focusPosture}"]`); try { if (b) b.focus(); } catch {} }
    }
    // Posture (b): ONE permission for everything not listed (Read-only / Read & write).
    function wsDefaultModeField(current, apply) {
      const T = wsText();
      const f = wsField(T.everythingElse, "");
      const seg = wsEl("div", "ws-mode");
      seg.id = "wsg-default";
      seg.setAttribute("role", "radiogroup");
      seg.setAttribute("aria-label", T.everythingElse);
      const btns = [];
      let value = current;
      const paint = () => { for (const b of btns) { const on = b.dataset.mode === value; b.classList.toggle("is-on", on); b.setAttribute("aria-checked", on ? "true" : "false"); b.tabIndex = on ? 0 : -1; } };
      for (const [m, text] of [["rw", T.accessReadWrite], ["ro", T.accessRead]]) {
        const b = wsEl("button", "ws-mode__opt", text);
        b.type = "button";
        b.setAttribute("role", "radio");
        b.dataset.mode = m;
        b.setAttribute("data-ws-default", m);
        b.onclick = async () => {
          if (value === m || seg.getAttribute("aria-busy") === "true") return;
          const before = value;
          value = m;
          paint();
          seg.setAttribute("aria-busy", "true");
          wsSaved(f.saved, "Saving…");
          try { await wsQueue(() => apply(m)); wsSaved(f.saved, "Saved"); }
          catch (e) { value = before; paint(); wsSaved(f.saved, `${emailErrorText(e)} Not saved.`, true); }
          finally { seg.removeAttribute("aria-busy"); }
        };
        b.onkeydown = (ev) => {
          if (!["ArrowRight", "ArrowLeft", "ArrowDown", "ArrowUp"].includes(ev.key)) return;
          ev.preventDefault();
          const other = btns[(btns.indexOf(b) + 1) % btns.length];
          other.focus();
          other.click();
        };
        btns.push(b);
        seg.append(b);
      }
      paint();
      f.box.append(seg);
      return f.box;
    }
    async function openGatewayWorkspace() {
      closeGatewayWorkspace();
      const body = $("gateway-workspace-body");
      body.textContent = "";
      body.append(wsEl("p", "wsm-loading", "Loading…"));
      const backdrop = $("gateway-workspace-backdrop");
      backdrop.hidden = false;
      wsStore.gwRelease = bindAccountModal(backdrop, closeGatewayWorkspace);
      let p;
      try {
        p = wsGatewayPolicy(await api("/api/gateway/workspace/policy"));
      } catch (e) {
        body.textContent = "";
        body.append(wsEl("p", "wsm-error", `The workspace policy could not be loaded: ${emailErrorText(e)}`));
        return;
      }
      wsRenderGateway(body, p, null);
    }

    // -- ONE account's folders: the kit WorkspaceChooser (islands), GET/PUT /workspace/policy/{account}.
    function wsAccountKey(a) {
      if (a.own) return "me";
      return `${a.tenant_id || "default"}:${a.id}`;
    }
    function wsAccountState(out) {
      if (!out || !out.policy || !Array.isArray(out.policy.folders) || !out.effective || typeof out.effective.summary !== "string") {
        throw new Error("GET/PUT /workspace/policy/{account} answered without policy/effective (R9 WORKSPACE API seam).");
      }
      return out;
    }
    function closeAccountWorkspace() {
      const backdrop = $("account-workspace-backdrop");
      if (backdrop.hidden) return;
      if (wsStore.accIsland) { wsStore.accIsland.unmount(); wsStore.accIsland = null; }
      $("account-workspace-body").textContent = "";
      backdrop.hidden = true;
      const release = wsStore.accRelease;
      wsStore.accRelease = null;
      if (release) release();
    }
    async function openAccountWorkspace(a) {
      closeAccountWorkspace();
      const lib = islandsLib();
      if (!lib || typeof lib.mountWorkspaceChooser !== "function") throw new Error("AbstractGateway console: the islands bundle has no mountWorkspaceChooser (ui-kit 0.8.1+ required).");
      const key = wsAccountKey(a);
      const path = `/api/gateway/workspace/policy/${encodeURIComponent(key)}`;
      const kind = a.kind === "entity" ? "entity" : (a.role === "admin" ? "admin" : "user");
      $("account-workspace-title").textContent = `Workspaces — ${a.id}`;
      const body = $("account-workspace-body");
      body.textContent = "";
      body.setAttribute("data-ws-account", a.id);
      body.setAttribute("data-ws-kind", kind);
      const host = wsEl("div", "wsm-chooser");
      body.append(host);
      const props = { state: null, loadError: null, idPrefix: `wsa-${String(a.id).replace(/[^A-Za-z0-9_-]/g, "-")}`, onPut: async (change) => {
        let out;
        try { out = wsAccountState(await api(path, { method: "PUT", body: JSON.stringify(change) })); }
        catch (e) { throw new Error(emailErrorText(e)); }
        props.state = out;
        if (wsStore.accIsland) wsStore.accIsland.update(Object.assign({}, props));
        return out;
      } };
      wsStore.accIsland = lib.mountWorkspaceChooser(host, Object.assign({}, props));
      // "Follow the gateway policy": every allowed folder off and no folders of its own (the
      // shared workspace stays). Asks inline first; one PUT.
      const reset = wsEl("div", "wsm-reset");
      const resetBtn = wsEl("button", "secondary", "Follow the gateway policy");
      resetBtn.type = "button";
      resetBtn.setAttribute("data-ws-reset", "");
      const resetNote = wsEl("p", "wsm-note", "Removes this account's own limits: its agents get exactly what the gateway allows.");
      const resetState = wsEl("p", "ws-folder__state");
      resetState.setAttribute("aria-live", "polite");
      const sayReset = wsRowState(resetState);
      resetBtn.onclick = () => {
        if (reset.querySelector(".wsm-confirm")) return;
        const box = wsEl("div", "wsm-confirm");
        box.setAttribute("role", "group");
        box.append(wsEl("span", "", `Follow the gateway policy for ${a.id}? Their own read-only and denied workspaces are removed.`));
        const yes = wsEl("button", "danger", "Follow");
        yes.type = "button";
        const no = wsEl("button", "secondary", "Cancel");
        no.type = "button";
        no.onclick = () => { box.remove(); try { resetBtn.focus(); } catch {} };
        yes.onclick = async () => {
          yes.disabled = true;
          const change = { default_mode: null, folders: [] };
          try {
            await props.onPut(change);
            box.remove();
            sayReset("Saved", "ok");
          } catch (e) {
            yes.disabled = false;
            sayReset(`${emailErrorText(e)} Not saved.`, "error");
          }
        };
        box.append(yes, no);
        reset.append(box);
        try { no.focus(); } catch {}
      };
      reset.append(resetBtn, resetNote, resetState);
      body.append(reset);
      const backdrop = $("account-workspace-backdrop");
      backdrop.hidden = false;
      wsStore.accRelease = bindAccountModal(backdrop, closeAccountWorkspace);
      try {
        props.state = wsAccountState(await api(path));
      } catch (e) {
        props.loadError = `${a.id}'s workspaces could not be loaded: ${emailErrorText(e)}`;
      }
      if (wsStore.accIsland) wsStore.accIsland.update(Object.assign({}, props));
    }
"""
