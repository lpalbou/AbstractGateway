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
  GET /workspace/policy, one PUT /workspace/policy per change (shared workspace, allowed folders,
  Allow any folder, Never allowed, launch-folder trust). Folder rows apply on BLUR (Enter blurs):
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
/* The modal's title already says "Workspace folders — <id>": the chooser's own heading stays for
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
.ws-folder input { width: 100%; min-width: 0; min-height: 40px; font-family: var(--font-mono); font-size: var(--font-size-md); }
.ws-folder input[aria-invalid="true"] { border-color: var(--error, #c0392b); }
.ws-folder__state { grid-column: 1 / -1; margin: 0; font-size: var(--font-size-sm); min-height: 0; }
.ws-folder__state:empty { display: none; }
.ws-folder__state.is-ok { color: var(--success, #2f9e44); font-weight: 600; }
.ws-folder__state.is-error { color: var(--error, #c0392b); }
.ws-add { justify-self: start; min-height: 40px; }
.ws-seg .ui-seg__opt { padding: 10px 12px; justify-items: start; justify-content: stretch; text-align: left; }
.ws-builtin { margin: 0; padding: 0; list-style: none; display: grid; gap: 2px; }
.ws-builtin code { font-size: var(--font-size-sm); overflow-wrap: anywhere; color: var(--text-secondary); background: transparent; border: 0; padding: 0; }
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
      workspace: (n) => `Workspace folders ${n}'s agents may use`,
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
    function wsSwitchField(id, label, description, checked, apply) {
      const f = wsField(label, "");
      const sw = afSwitchCreate({ id, label, description, checked });
      f.box.querySelector(".ws-field__label").remove();
      f.box.querySelector(".ws-field__head").prepend(...sw.nodes);
      afSwitchBind(sw.button, async (next) => {
        wsSaved(f.saved, "Saving…");
        await wsQueue(() => apply(next));
        wsSaved(f.saved, "Saved");
        return true;
      }, (e) => wsSaved(f.saved, `Not saved: ${emailErrorText(e)}`, true));
      return f.box;
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
    function wsFoldersField(id, label, help, paths, save) {
      const f = wsField(label, help);
      const list = wsEl("ul", "ws-folders");
      list.id = id;
      const saved = () => Array.from(list.querySelectorAll("input")).map((i) => i.dataset.saved || "").filter(Boolean);
      const commit = (next) => wsQueue(() => save(next));
      const addRow = (value, focus) => {
        const li = wsEl("li", "ws-folder");
        const input = document.createElement("input");
        input.type = "text";
        input.spellcheck = false;
        input.autocomplete = "off";
        input.value = value || "";
        input.dataset.saved = value || "";
        input.placeholder = "/absolute/path/to/folder";
        input.setAttribute("aria-label", `${label}: folder path`);
        const st = wsEl("p", "ws-folder__state");
        st.setAttribute("aria-live", "polite");
        const say = wsRowState(st);
        const tipFor = (v) => `Remove ${v || "this folder"} from ${label}`;
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
          try {
            await commit(next);
            input.value = check.normalized;
            input.dataset.saved = check.normalized;
            rm.setAttribute("aria-label", tipFor(check.normalized));
            rm.setAttribute("data-af-tip", tipFor(check.normalized));
            say("Saved", "ok");
          } catch (e) {
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
        li.append(input, rm, st);
        list.append(li);
        if (focus) { try { input.focus(); } catch {} }
        return li;
      };
      for (const p of paths) addRow(p, false);
      const add = wsEl("button", "secondary ws-add", "Add folder");
      add.type = "button";
      add.setAttribute("data-ws-add", id);
      add.onclick = () => {
        const empty = Array.from(list.querySelectorAll("input")).find((i) => !i.value.trim());
        if (empty) { try { empty.focus(); } catch {} return; }
        addRow("", true);
      };
      f.box.append(list, add);
      return f.box;
    }
    // The shared workspace: one required folder; same blur rule; empty is refused (kept as was).
    function wsSharedField(id, value, save) {
      const f = wsField("Shared workspace", "Every conversation, automation and entity gets its own folder in it. Always on for every account; required.");
      const row = wsEl("div", "ws-folder ws-folder--single");
      const input = document.createElement("input");
      input.type = "text";
      input.id = id;
      input.spellcheck = false;
      input.autocomplete = "off";
      input.value = value || "";
      input.dataset.saved = value || "";
      input.placeholder = "/absolute/path/to/folder";
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
      row.append(input, st);
      f.box.append(row);
      return f.box;
    }

    // -- The GATEWAY policy modal ("Shared workspace & allowed folders", admins). Two postures
    // (R9 amendments): "Only allowed folders" (the shared workspace + allowed folders) or "Any
    // folder except denied" (the Never allowed list); the matching list shows under the switch.
    const WS_POSTURES = [
      { id: "allowed_only", title: "Only allowed folders", text: "Agents use the shared workspace and the folders you allow; each account turns them on." },
      { id: "any_except_denied", title: "Any folder except denied", text: "Accounts may add any folder of their own, except the Never allowed ones." },
    ];
    function wsGatewayPolicy(out) {
      const p = out && out.policy;
      if (!p || typeof p.shared_workspace !== "string" || !Array.isArray(p.allowed_folders) || !WS_POSTURES.some((x) => x.id === p.posture)
        || !Array.isArray(p.never_allowed) || typeof p.launch_folder_trust !== "boolean" || !Array.isArray(p.builtin_never_allowed)) {
        throw new Error("GET/PUT /workspace/policy answered without the gateway policy fields (R9 WORKSPACE API seam: shared_workspace, allowed_folders, posture, never_allowed, launch_folder_trust, builtin_never_allowed).");
      }
      return p;
    }
    // One line: what agents may use, gateway-wide. Presentation of the server's values only.
    function wsGatewaySummary(p) {
      const never = p.never_allowed.length;
      const tail = `Never allowed: ${never ? wsPlural(never, "folder", "folders") : "none"}, plus the gateway's own data and credential folders.`;
      const trust = p.launch_folder_trust ? " and the folder an app starts in" : "";
      if (p.posture === "any_except_denied") return `Agents may use the shared workspace, folders accounts add of their own${trust}. ${tail}`;
      const allowed = p.allowed_folders.length;
      return `Agents may use only the shared workspace${allowed ? ` + ${wsPlural(allowed, "allowed folder", "allowed folders")} (each account turns them on)` : ""}${trust}. ${tail}`;
    }
    // The posture: a segmented switch (radio group), applies on click (one PUT).
    function wsPostureField(current, apply) {
      const f = wsField("Folders agents may use", "");
      const seg = wsEl("div", "ui-seg ws-seg");
      seg.setAttribute("role", "radiogroup");
      seg.setAttribute("aria-label", "Folders agents may use");
      const buttons = [];
      for (const o of WS_POSTURES) {
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
      const builtin = wsField("Always refused", "The gateway's own data folder and credential folders; no setting changes this.");
      const ul = wsEl("ul", "ws-builtin");
      for (const x of p.builtin_never_allowed) { const li = wsEl("li"); li.append(wsEl("code", "", x)); ul.append(li); }
      builtin.box.append(ul);
      const posture = wsPostureField(p.posture, async (next) => {
        const out = await put({ posture: next });
        wsRenderGateway(body, out, next);
      });
      body.append(summary, posture, wsSharedField("wsg-shared", p.shared_workspace, (path) => put({ shared_workspace: path })));
      if (p.posture === "allowed_only") {
        body.append(wsFoldersField("wsg-allowed", "Allowed folders", "Extra folders accounts may turn on for their agents; off for each account until turned on.", p.allowed_folders, (list) => put({ allowed_folders: list })));
      } else {
        body.append(wsFoldersField("wsg-never", "Never allowed", "Folders no agent may use, whatever an account adds.", p.never_allowed, (list) => put({ never_allowed: list })));
      }
      body.append(
        builtin.box,
        wsSwitchField("wsg-launch", "Launch-folder trust", "Agents may also use the folder an app was started from.", p.launch_folder_trust, (on) => put({ launch_folder_trust: on })),
      );
      for (const sec of body.querySelectorAll(".ws-field")) sec.setAttribute("data-ws-section", "");
      if (focusPosture) { const b = body.querySelector(`[data-ws-posture="${focusPosture}"]`); try { if (b) b.focus(); } catch {} }
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
        body.append(wsEl("p", "wsm-error", `The folder policy could not be loaded: ${emailErrorText(e)}`));
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
      if (!out || !out.policy || !Array.isArray(out.policy.enabled_folders) || !Array.isArray(out.policy.own_folders) || typeof out.policy.other_sessions !== "boolean" || !out.effective || typeof out.effective.summary !== "string") {
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
      $("account-workspace-title").textContent = `Workspace folders — ${a.id}`;
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
      const resetNote = wsEl("p", "wsm-note", "Turns every allowed folder and Other sessions off and removes this account's own folders; the shared workspace stays.");
      const resetState = wsEl("p", "ws-folder__state");
      resetState.setAttribute("aria-live", "polite");
      const sayReset = wsRowState(resetState);
      resetBtn.onclick = () => {
        if (reset.querySelector(".wsm-confirm")) return;
        const box = wsEl("div", "wsm-confirm");
        box.setAttribute("role", "group");
        box.append(wsEl("span", "", `Follow the gateway policy for ${a.id}? Their allowed folders and Other sessions turn off and their own folders are removed.`));
        const yes = wsEl("button", "danger", "Follow");
        yes.type = "button";
        const no = wsEl("button", "secondary", "Cancel");
        no.type = "button";
        no.onclick = () => { box.remove(); try { resetBtn.focus(); } catch {} };
        yes.onclick = async () => {
          yes.disabled = true;
          const cur = props.state;
          const change = { enabled_folders: [] };
          if (cur && cur.policy.other_sessions) change.other_sessions = false;
          if (cur && cur.effective && cur.effective.own_folders_allowed && cur.policy.own_folders.length) change.own_folders = [];
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
        props.loadError = `${a.id}'s folders could not be loaded: ${emailErrorText(e)}`;
      }
      if (wsStore.accIsland) wsStore.accIsland.update(Object.assign({}, props));
    }
"""
