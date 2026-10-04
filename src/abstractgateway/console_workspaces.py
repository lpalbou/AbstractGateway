"""Round 8 (DESIGN.md R8.2): the Accounts table's icon actions and card list, the Runtimes
account filter, and the console's Workspaces page.

Accounts (console.py renderAccounts): columns Name · Email (ONE column, "address · state") ·
Runtime (a link to the Runtimes page filtered to that account) · Active · Actions. The actions are
44 px icon buttons with a CSS tooltip (`.icon-btn[data-tip]`, shared: any console page may use it)
— no "⋯" menu, no labels. The table never scrolls sideways at 1280-2560 px; below ~900 px (or when
the table itself is narrower than its column minimums) the rows become flat cards and the actions
wrap.

Runtimes: `#runtimes?account=<id>[&tenant_id=<t>]` = GET /admin/runtimes?account=<id>; the filter
shows as a removable chip ("Account: alice ×"); the link survives a reload.

Workspaces (sidebar entry right after Accounts): where agents may read and write.
- Admins: the effective summary in one line, the GATEWAY policy (GET/POST /admin/runtime-config:
  workspace_default_mode, trust_client_launch_folder, workspace_allowed_paths,
  workspace_blocked_paths, workspace_root, client_workspace_scope_overrides), then every user's
  OWN policy (GET /admin/runtime-config user_workspace_policies + GET /admin/accounts; writes PUT
  /admin/user-workspace-policy, one whole entry, `{policy: null}` = back to the gateway policy).
- Anyone else: the gateway policy in one read-only line and their own policy (GET/PUT
  /workspace/policy/self).
Controls: access mode = a segmented switch (Allow my list | Allow everything except), launch-folder
trust = a switch, allowed and refused folders = editable rows (add / remove). A row applies on BLUR
(Enter blurs): POST /workspace/path-check first — an invalid path shows its sentence and is NOT
saved — then the write, then an inline "Saved". No Save button anywhere.

Spliced into the console script scope with the UI layer (console.py _console_owned_sources): `api`,
`esc`, `$`, `state`, `svgIcon`, `ICONS`, `afSwitchCreate`, `afSwitchBind`, `afSwitchSet`,
`setActiveTab`, `loadRuntimes`, `selectRuntime`, `emailErrorText` are the console's own.
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
  .accounts-actions__buttons .icon-btn[data-tip]::after { right: auto; left: 0; }
  .accounts-table tr.af-row--admin { background-color: var(--af-row-tint-admin); box-shadow: inset 3px 0 0 var(--af-row-mark-admin); }
  .accounts-table tr.af-row--user { box-shadow: inset 3px 0 0 var(--af-row-mark-user); }
  .accounts-table tr.af-row--entity { background-color: var(--af-row-tint-entity); box-shadow: inset 3px 0 0 var(--af-row-mark-entity); }
  .accounts-table .row-confirm, .accounts-table .row-confirm > td { display: block; padding: 0 0 10px; border: 0; }
"""

ACCOUNTS_CSS = r"""
/* ---- Icon buttons with a tooltip (round 8; shared): 44 px targets, the label in data-tip and
   aria-label. The tooltip is right-aligned to its button so it never widens the page. */
.icon-btn { position: relative; display: inline-flex; align-items: center; justify-content: center; flex: 0 0 44px; width: 44px; height: 44px; min-width: 44px; min-height: 44px; padding: 0; border: 1px solid var(--ui-border-1, var(--line)); border-radius: var(--radius-md); background: var(--bg-primary); color: var(--text-secondary); box-shadow: none; cursor: pointer; }
.icon-btn:hover:not(:disabled) { background: var(--ui-surface-2, var(--bg-secondary)); color: var(--text-primary, var(--text)); filter: none; }
.icon-btn--danger:hover:not(:disabled) { color: var(--error, var(--danger, #c0392b)); border-color: color-mix(in srgb, var(--error, #c0392b) 45%, transparent); }
.icon-btn:focus-visible { outline: 2px solid var(--info, var(--accent)); outline-offset: 2px; }
.icon-btn svg { width: 18px; height: 18px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; pointer-events: none; }
.icon-btn[data-tip]::after { content: attr(data-tip); position: absolute; bottom: calc(100% + 6px); right: 0; display: none; width: max-content; max-width: 240px; padding: 4px 8px; border-radius: var(--radius-sm); background: var(--text-primary, #111); color: var(--bg-primary, #fff); font: 500 var(--font-size-sm, 12px)/1.35 var(--font-sans, system-ui); white-space: normal; text-align: left; pointer-events: none; z-index: 40; box-shadow: 0 2px 8px rgba(0, 0, 0, .2); }
.icon-btn[data-tip]:hover::after, .icon-btn[data-tip]:focus-visible::after { display: block; }

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

/* ---- Runtimes account filter chip (round 8). */
.runtimes-filter { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 10px; }
.runtimes-filter[hidden] { display: none; }
.runtimes-filter__chip { display: inline-flex; align-items: center; gap: 2px; padding: 0 0 0 12px; border: 1px solid var(--accent); border-radius: 999px; background: var(--accent-subtle, transparent); color: var(--text-primary, var(--text)); font-size: var(--font-size-md); font-weight: 600; }
.runtimes-filter__chip .icon-btn { border: 0; background: transparent; width: 36px; height: 36px; min-width: 36px; min-height: 36px; flex-basis: 36px; border-radius: 999px; }
@media (pointer: coarse) { .runtimes-filter__chip .icon-btn { width: 44px; height: 44px; min-width: 44px; min-height: 44px; flex-basis: 44px; } }
"""

WORKSPACES_CSS = r"""
/* ---- Workspaces page (round 8, console_workspaces.py). */
.ws-page { display: flex; flex-direction: column; gap: 16px; min-width: 0; max-width: 980px; }
.ws-summary { margin: 0; padding: 10px 14px; border-left: 3px solid var(--accent); border-radius: var(--radius-sm); background: var(--accent-subtle, transparent); font-size: var(--font-size-base); line-height: 1.45; overflow-wrap: anywhere; }
.ws-loading { margin: 0; color: var(--text-secondary); }
.ws-card .ui-card__note { margin: 2px 0 0; }
.ws-field { display: grid; gap: 6px; min-width: 0; padding-top: 12px; border-top: 1px solid var(--line-soft, var(--ui-border-1)); }
.ws-field:first-of-type { border-top: 0; padding-top: 0; }
.ws-field__head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }
.ws-field__label { font-size: var(--font-size-md); font-weight: 600; color: var(--text-primary, var(--text)); }
.ws-field__help { margin: 0; font-size: var(--af-helper-size, var(--font-size-md)); color: var(--text-secondary); }
.ws-saved { font-size: var(--font-size-sm); font-weight: 600; color: var(--success, #2f9e44); }
.ws-saved:empty { display: none; }
.ws-saved.is-error { color: var(--error, #c0392b); }
.ws-seg { grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr)); }
.ws-seg .ui-seg__opt { padding: 10px 12px; justify-items: start; justify-content: stretch; text-align: left; }
.ws-folder--single { grid-template-columns: minmax(0, 1fr); }
a.ws-link { color: var(--info, var(--accent)); text-decoration: none; }
a.ws-link:hover { text-decoration: underline; }
.ws-folders { display: grid; gap: 6px; margin: 0; padding: 0; list-style: none; }
.ws-folder { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 6px; align-items: center; }
.ws-folder input { width: 100%; min-width: 0; min-height: 40px; font-family: var(--font-mono); font-size: var(--font-size-md); }
.ws-folder input[aria-invalid="true"] { border-color: var(--error, #c0392b); }
.ws-folder__state { grid-column: 1 / -1; margin: 0; font-size: var(--font-size-sm); min-height: 0; }
.ws-folder__state:empty { display: none; }
.ws-folder__state.is-ok { color: var(--success, #2f9e44); font-weight: 600; }
.ws-folder__state.is-error { color: var(--error, #c0392b); }
.ws-add { justify-self: start; min-height: 40px; }
.ws-empty { margin: 0; font-size: var(--font-size-md); color: var(--text-secondary); }
.ws-accounts { display: grid; gap: 0; margin: 0; padding: 0; list-style: none; }
.ws-acc { display: grid; gap: 12px; padding: 12px 0; border-top: 1px solid var(--line-soft, var(--ui-border-1)); }
.ws-acc:first-child { border-top: 0; padding-top: 0; }
.ws-acc.is-focus { box-shadow: inset 3px 0 0 var(--accent); padding-left: 12px; }
.ws-acc__head { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; min-width: 0; }
.ws-acc__name { font-weight: 600; overflow-wrap: anywhere; }
.ws-acc__summary { flex: 1 1 260px; min-width: 0; font-size: var(--font-size-md); color: var(--text-secondary); overflow-wrap: anywhere; }
.ws-acc__switch { margin-left: auto; }
.ws-acc__switch .af-switch__reason { display: none; }
.ws-acc__body { display: grid; gap: 12px; padding-left: 14px; border-left: 2px solid var(--line-soft, var(--ui-border-1)); }
.ws-acc__body:empty { display: none; }
.ws-confirm { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; padding: 10px 12px; border-radius: var(--radius-md); background: var(--ui-surface-2, var(--bg-secondary)); }
.ws-note { margin: 0; font-size: var(--font-size-md); color: var(--text-secondary); }
.ws-error { margin: 0; color: var(--error, #c0392b); }
@media (max-width: 767.98px) {
  .ws-acc__body { padding-left: 10px; }
  .ws-folder input { min-height: 44px; font-size: var(--font-size-base); }
  .ws-add { min-height: 44px; }
}
"""

WORKSPACES_JS = r"""
    // ---- Account icons (round 8): icon-only actions with a tooltip (data-tip) and an aria-label.
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
      const b = document.createElement("button");
      b.type = "button";
      b.className = `icon-btn${danger ? " icon-btn--danger" : ""}`;
      b.setAttribute("aria-label", aria || tip);
      b.setAttribute("data-tip", tip);
      b.innerHTML = glyph;
      return b;
    }

    // ---- Deep links of the two round-8 pages: `#runtimes?account=<id>` and `#workspaces?account=<id>`.
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
    // Leaving a page drops its account link (the next reload must not land filtered).
    function wsOnTabChange(tab) {
      if (!state.principal || !state.hashApplied) return;
      for (const t of ["runtimes", "workspaces"]) {
        if (t !== tab && wsHashQuery(t)) wsSetHash("");
      }
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
      const clear = accountIconButton("close", "Show every runtime", `Remove the filter: show every runtime, not only ${filter.account}'s`);
      clear.setAttribute("data-runtimes-filter-clear", "");
      clear.onclick = () => {
        wsSetHash("#runtimes");
        state.selectedRuntime = null;
        loadRuntimes();
      };
      chip.append(label, clear);
      slot.append(chip);
    }

    // ---- Workspaces page.
    const wsStore = { loading: false, admin: false, cfg: null, accounts: [], self: null, error: "", confirm: "", focus: null, chain: Promise.resolve() };
    function workspacesHref(f) { return wsAccountHref("workspaces", f); }
    function workspacesLink(text, f) {
      const a = document.createElement("a");
      a.className = "ws-link";
      a.href = workspacesHref(f);
      a.textContent = text;
      a.onclick = (ev) => {
        if (ev && (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button === 1)) return;
        if (ev && ev.preventDefault) ev.preventDefault();
        if (ev && ev.stopPropagation) ev.stopPropagation();
        openWorkspacesFor(f);
      };
      return a;
    }
    function openWorkspacesFor(f) {
      state.hashApplied = true;
      wsSetHash(workspacesHref(f));
      setActiveTab("workspaces");
      loadWorkspaces();
    }
    function wsKey(tenant, user) { return `${tenant || "default"}:${user}`; }
    function wsPolicies() {
      const map = wsStore.cfg && wsStore.cfg.user_workspace_policies && wsStore.cfg.user_workspace_policies.value;
      return map && typeof map === "object" ? map : {};
    }
    function wsGateway() {
      const c = wsStore.cfg;
      if (!c) throw new Error("GET /admin/runtime-config did not load (round-8 seam).");
      const mode = c.workspace_default_mode && c.workspace_default_mode.value;
      if (mode !== "whitelist" && mode !== "blacklist") throw new Error("GET /admin/runtime-config has no workspace_default_mode (round-8 seam).");
      return {
        mode,
        trust: Boolean(c.trust_client_launch_folder && c.trust_client_launch_folder.value),
        legacy: Boolean(c.client_workspace_scope_overrides && c.client_workspace_scope_overrides.value),
        allowed: Array.isArray(c.workspace_allowed_paths && c.workspace_allowed_paths.paths) ? c.workspace_allowed_paths.paths : [],
        blocked: Array.isArray(c.workspace_blocked_paths && c.workspace_blocked_paths.paths) ? c.workspace_blocked_paths.paths : [],
        root: c.workspace_root && c.workspace_root.source === "stored" ? String(c.workspace_root.value || "") : "",
        rootDefault: c.workspace_root ? String(c.workspace_root.value || "") : "",
      };
    }
    function wsPlural(n, one, many) { return `${n} ${n === 1 ? one : many}`; }
    // One line each: what the agents may use. Presentation of the server's values only.
    function wsGatewaySentence(g) {
      if (g.legacy) return "Any folder (old clients) is on: agents may use any folder the app names; the folder rules don't apply.";
      if (g.mode === "blacklist") return `Agents may use any folder except ${g.blocked.length ? wsPlural(g.blocked.length, "refused folder", "refused folders") : "none refused yet"}.`;
      const allowed = g.allowed.length ? wsPlural(g.allowed.length, "allowed folder", "allowed folders") : "";
      const start = g.trust ? "the folder they start in" : "";
      const what = [allowed, start].filter(Boolean).join(" and ");
      return what ? `Agents may use only ${what}${g.blocked.length ? `; ${wsPlural(g.blocked.length, "folder", "folders")} refused` : ""}.` : "Agents may not use any folder yet: add an allowed folder or turn on launch-folder trust.";
    }
    function wsOwnSentence(entry, g) {
      if (!entry || !Object.keys(entry).length) return "Follows the gateway policy.";
      if (entry.client_workspace_scope_overrides === true) return "Any folder (old clients) is on for this account.";
      const mode = entry.mode || g.mode;
      const trust = typeof entry.trust_client_launch_folder === "boolean" ? entry.trust_client_launch_folder : g.trust;
      const allowed = (entry.workspace_allowed_paths || []).length;
      const blocked = (entry.workspace_blocked_paths || []).length;
      if (mode === "blacklist") return `Any folder except ${blocked ? wsPlural(blocked, "refused folder", "refused folders") : "the gateway's refused ones"}.`;
      const parts = ["the gateway's allowed folders"];
      if (allowed) parts.push(wsPlural(allowed, "folder of its own", "folders of its own"));
      if (trust) parts.push("the folder it starts in");
      return `Only ${parts.join(", ")}${blocked ? `; ${wsPlural(blocked, "folder", "folders")} refused` : ""}.`;
    }
    async function loadWorkspaces() {
      const root = $("workspaces-root");
      if (!root) throw new Error("Console markup has no #workspaces-root (round-8 seam).");
      if (!state.principal) return;
      wsStore.admin = Boolean(state.principal.admin);
      wsStore.focus = wsAccountFromHash("workspaces");
      if (!wsStore.cfg && !wsStore.self) root.innerHTML = `<p class="ws-loading">Loading…</p>`;
      wsStore.loading = true;
      try {
        if (wsStore.admin) {
          const [cfg, acc] = await Promise.all([api("/api/gateway/admin/runtime-config"), api("/api/gateway/admin/accounts")]);
          if (!cfg || !cfg.user_workspace_policies || !("value" in cfg.user_workspace_policies)) throw new Error("GET /admin/runtime-config has no user_workspace_policies (round-8 seam).");
          if (!acc || !Array.isArray(acc.accounts)) throw new Error("GET /admin/accounts answered without an accounts list (accounts-api seam).");
          wsStore.cfg = cfg;
          wsStore.accounts = acc.accounts;
        } else {
          const self = await api("/api/gateway/workspace/policy/self");
          if (!self || !self.gateway_defaults || !self.effective) throw new Error("GET /workspace/policy/self answered without gateway_defaults/effective (round-8 seam).");
          wsStore.self = self;
        }
        wsStore.error = "";
      } catch (e) {
        wsStore.error = emailErrorText(e);
      } finally {
        wsStore.loading = false;
      }
      renderWorkspaces();
    }
    function wsEl(tag, cls, text) {
      const el = document.createElement(tag);
      if (cls) el.className = cls;
      if (text !== undefined) el.textContent = text;
      return el;
    }
    function wsSaved(el, text, error) {
      el.textContent = text || "";
      el.className = `ws-saved${error ? " is-error" : ""}`;
      el.setAttribute("role", error ? "alert" : "status");
      if (el.__t) clearTimeout(el.__t);
      if (text && !error && typeof setTimeout === "function") el.__t = setTimeout(() => { el.textContent = ""; }, 2500);
    }
    // Writes run one after another (a fast blur-then-click never races two whole-entry PUTs).
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
    // Access mode: the segmented switch (Allow my list | Allow everything except); applies on click.
    function wsModeField(id, current, apply) {
      const f = wsField("Access", "Which folders agents may use.");
      const seg = wsEl("div", "ui-seg ws-seg");
      seg.setAttribute("role", "radiogroup");
      seg.setAttribute("aria-label", "Access");
      const opts = [
        { id: "whitelist", title: "Allow my list", text: "Only the allowed folders below." },
        { id: "blacklist", title: "Allow everything except", text: "Any folder except the refused ones." },
      ];
      const buttons = [];
      for (const o of opts) {
        const b = wsEl("button", `ui-seg__opt${o.id === current ? " is-on" : ""}`);
        b.type = "button";
        b.setAttribute("role", "radio");
        b.setAttribute("aria-checked", o.id === current ? "true" : "false");
        b.setAttribute("data-ws-mode", o.id);
        b.id = `${id}-${o.id}`;
        b.tabIndex = o.id === current ? 0 : -1;
        b.innerHTML = `<span class="ui-seg__title">${esc(o.title)}</span><span class="ui-seg__text">${esc(o.text)}</span>`;
        b.onclick = async () => {
          if (b.getAttribute("aria-checked") === "true" || seg.getAttribute("aria-busy") === "true") return;
          seg.setAttribute("aria-busy", "true");
          wsSaved(f.saved, "Saving…");
          try {
            await wsQueue(() => apply(o.id));
            for (const x of buttons) { const on = x === b; x.classList.toggle("is-on", on); x.setAttribute("aria-checked", on ? "true" : "false"); x.tabIndex = on ? 0 : -1; }
            wsSaved(f.saved, "Saved");
          } catch (e) {
            wsSaved(f.saved, `Not saved: ${emailErrorText(e)}`, true);
          } finally {
            seg.removeAttribute("aria-busy");
          }
        };
        b.onkeydown = (ev) => {
          if (ev.key !== "ArrowRight" && ev.key !== "ArrowLeft" && ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
          ev.preventDefault();
          const other = buttons[(buttons.indexOf(b) + 1) % buttons.length];
          other.focus();
        };
        buttons.push(b);
        seg.append(b);
      }
      f.box.append(seg);
      return f.box;
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
    // Folder rows: an input per folder + a remove icon; "Add folder" adds an empty row. A row
    // applies on BLUR (Enter blurs): path check -> write the whole list -> inline "Saved".
    // `save(list)` writes the list and resolves with the stored list.
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
        input.placeholder = "/full/path/to/folder";
        input.setAttribute("aria-label", `${label}: folder path`);
        const st = wsEl("p", "ws-folder__state");
        st.setAttribute("aria-live", "polite");
        const rm = accountIconButton("remove", "Remove", `Remove ${value || "this folder"} from ${label.toLowerCase()}`, true);
        rm.setAttribute("data-ws-remove", "");
        const say = (text, tone) => { st.textContent = text || ""; st.className = `ws-folder__state${tone ? ` is-${tone}` : ""}`; st.setAttribute("role", tone === "error" ? "alert" : "status"); if (st.__t) clearTimeout(st.__t); if (tone === "ok" && typeof setTimeout === "function") st.__t = setTimeout(() => { st.textContent = ""; st.className = "ws-folder__state"; }, 2500); };
        input.onkeydown = (ev) => {
          if (ev.key === "Enter") { ev.preventDefault(); input.blur(); }
          if (ev.key === "Escape") { input.value = input.dataset.saved || ""; input.removeAttribute("aria-invalid"); say(""); input.blur(); }
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
            catch (e) { input.value = before; say(`Not saved: ${emailErrorText(e)}`, "error"); }
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
          // Keep the row's place in the list.
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
            rm.setAttribute("aria-label", `Remove ${check.normalized} from ${label.toLowerCase()}`);
            say("Saved", "ok");
          } catch (e) {
            input.setAttribute("aria-invalid", "true");
            say(`Not saved: ${emailErrorText(e)}`, "error");
          }
        };
        rm.onclick = async () => {
          const was = input.dataset.saved || "";
          if (!was) { li.remove(); return; }
          const next = saved().filter((p) => p !== was);
          rm.setAttribute("aria-busy", "true");
          try { await commit(next); li.remove(); wsSaved(f.saved, "Removed"); }
          catch (e) { rm.removeAttribute("aria-busy"); say(`Not removed: ${emailErrorText(e)}`, "error"); }
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
    // A single folder (the gateway's default folder): same blur rule; empty = the gateway's own.
    function wsSingleFolderField(id, label, help, value, placeholder, save) {
      const f = wsField(label, help);
      const li = wsEl("div", "ws-folder ws-folder--single");
      const input = document.createElement("input");
      input.type = "text";
      input.id = id;
      input.spellcheck = false;
      input.value = value || "";
      input.dataset.saved = value || "";
      input.placeholder = placeholder || "/full/path/to/folder";
      input.setAttribute("aria-label", label);
      const st = wsEl("p", "ws-folder__state");
      st.setAttribute("aria-live", "polite");
      const say = (text, tone) => { st.textContent = text || ""; st.className = `ws-folder__state${tone ? ` is-${tone}` : ""}`; st.setAttribute("role", tone === "error" ? "alert" : "status"); };
      input.onkeydown = (ev) => { if (ev.key === "Enter") { ev.preventDefault(); input.blur(); } };
      input.onblur = async () => {
        const v = input.value.trim();
        if (v === (input.dataset.saved || "")) return;
        let norm = "";
        if (v) {
          say("Checking…");
          let check;
          try { check = await wsCheckPath(v); } catch (e) { say(`Not checked: ${emailErrorText(e)}`, "error"); return; }
          if (!check.valid) { input.setAttribute("aria-invalid", "true"); say(`${check.sentence} Not saved.`, "error"); return; }
          norm = check.normalized;
        }
        input.removeAttribute("aria-invalid");
        say("Saving…");
        try {
          await wsQueue(() => save(norm || null));
          input.value = norm;
          input.dataset.saved = norm;
          say("Saved", "ok");
          if (typeof setTimeout === "function") setTimeout(() => { if (st.textContent === "Saved") say(""); }, 2500);
        } catch (e) { say(`Not saved: ${emailErrorText(e)}`, "error"); }
      };
      li.append(input, st);
      f.box.append(li);
      return f.box;
    }
    // The editor one policy uses (gateway, an account, or the signed-in user's own).
    function wsEditor(prefix, v, writers, opts) {
      const box = document.createDocumentFragment();
      box.append(wsModeField(`${prefix}-mode`, v.mode, writers.mode));
      box.append(wsSwitchField(`${prefix}-trust`, "Launch-folder trust", "Agents may also use the folder they were started from.", v.trust, writers.trust));
      box.append(wsFoldersField(`${prefix}-allowed`, "Allowed folders", opts.allowedHelp, v.allowed, writers.allowed));
      box.append(wsFoldersField(`${prefix}-blocked`, "Refused folders", opts.blockedHelp, v.blocked, writers.blocked));
      if (writers.root) box.append(wsSingleFolderField(`${prefix}-root`, "Default folder", "Where a run starts when the app names no folder; empty = the gateway's own folder.", v.root, v.rootDefault, writers.root));
      if (writers.legacy) box.append(wsSwitchField(`${prefix}-legacy`, "Any folder (old clients)", "Lets old clients name any folder; the rules above stop applying.", v.legacy, writers.legacy));
      return box;
    }
    async function wsWriteGateway(change) {
      const out = await api("/api/gateway/admin/runtime-config", { method: "POST", body: JSON.stringify(change) });
      if (!out || !out.workspace_default_mode) throw new Error("POST /admin/runtime-config answered without the posture (round-8 seam).");
      wsStore.cfg = Object.assign({}, wsStore.cfg, out);
      wsRenderSummary();
      return out;
    }
    async function wsWriteAccount(a, entry) {
      const tenant = a.tenant_id || "default";
      const body = { policy: entry && Object.keys(entry).length ? entry : null };
      const out = await api(`/api/gateway/admin/user-workspace-policy?tenant_id=${encodeURIComponent(tenant)}&user_id=${encodeURIComponent(a.id)}`, { method: "PUT", body: JSON.stringify(body) });
      if (!out || typeof out.customized !== "boolean" || !out.policy) throw new Error("PUT /admin/user-workspace-policy answered without the entry (round-8 seam).");
      const map = Object.assign({}, wsPolicies());
      if (out.customized) map[wsKey(tenant, a.id)] = out.policy; else delete map[wsKey(tenant, a.id)];
      wsStore.cfg.user_workspace_policies = Object.assign({}, wsStore.cfg.user_workspace_policies, { value: map });
      wsRenderSummary();
      const sum = document.querySelector(`[data-ws-account="${CSS.escape(a.id)}"] .ws-acc__summary`);
      if (sum) sum.textContent = wsOwnSentence(out.customized ? out.policy : null, wsGateway());
      return out;
    }
    async function wsWriteSelf(entry) {
      const out = await api("/api/gateway/workspace/policy/self", { method: "PUT", body: JSON.stringify(entry && Object.keys(entry).length ? entry : {}) });
      if (!out || typeof out.customized !== "boolean" || !out.effective) throw new Error("PUT /workspace/policy/self answered without the entry (round-8 seam).");
      wsStore.self = out;
      wsRenderSummary();
      return out;
    }
    function wsEntryWriters(getEntry, write) {
      const put = (field, value) => {
        const entry = Object.assign({}, getEntry());
        if (Array.isArray(value) && !value.length) delete entry[field]; else entry[field] = value;
        return write(entry);
      };
      return {
        mode: (m) => put("mode", m),
        trust: (on) => put("trust_client_launch_folder", on),
        allowed: (list) => put("workspace_allowed_paths", list),
        blocked: (list) => put("workspace_blocked_paths", list),
      };
    }
    function wsSummaryText() {
      if (wsStore.admin) {
        const own = Object.keys(wsPolicies()).length;
        const tail = own === 0 ? "" : own === 1 ? " 1 account has its own policy." : ` ${own} accounts have their own policy.`;
        return `${wsGatewaySentence(wsGateway())}${tail}`;
      }
      const s = wsStore.self;
      const eff = s.effective;
      const mine = s.customized ? s.policy || {} : {};
      const extra = (mine.workspace_allowed_paths || []).length;
      const refused = (mine.workspace_blocked_paths || []).length;
      const who = s.customized ? "Your own policy" : "The gateway policy";
      if (eff.client_workspace_scope_overrides) return `${who}: your agents may use any folder the app names (old clients).`;
      if (eff.mode === "blacklist") return `${who}: your agents may use any folder except the refused ones${refused ? ` (${refused} of yours)` : ""}.`;
      const parts = ["the gateway's allowed folders"];
      if (extra) parts.push(wsPlural(extra, "folder of yours", "folders of yours"));
      if (eff.trust_client_launch_folder) parts.push("the folder they start in");
      return `${who}: your agents may use only ${parts.join(", ")}${refused ? `; ${wsPlural(refused, "folder", "folders")} refused` : ""}.`;
    }
    function wsRenderSummary() {
      const el = document.querySelector("#workspaces-root [data-ws-summary]");
      if (el) el.textContent = wsSummaryText();
    }
    function wsAccountRow(a, g) {
      const tenant = a.tenant_id || "default";
      const key = wsKey(tenant, a.id);
      const li = wsEl("li", "ws-acc");
      li.setAttribute("data-ws-account", a.id);
      const head = wsEl("div", "ws-acc__head");
      head.append(wsEl("span", "ws-acc__name", tenant !== "default" ? `${tenant}/${a.id}` : a.id));
      const entry = () => wsPolicies()[key] || null;
      head.append(wsEl("span", "ws-acc__summary", wsOwnSentence(entry(), g)));
      const swWrap = wsEl("span", "ws-acc__switch");
      const sw = afSwitchCreate({ id: `ws-own-${tenant}-${a.id}`.replace(/[^A-Za-z0-9_-]/g, "-"), label: "Own policy", ariaLabel: `Own policy: ${a.id}`, checked: Boolean(entry()), small: true });
      swWrap.append(...sw.nodes);
      head.append(swWrap);
      const body = wsEl("div", "ws-acc__body");
      const fillBody = () => {
        body.textContent = "";
        const e = entry();
        if (!e) return;
        const v = {
          mode: e.mode || g.mode,
          trust: typeof e.trust_client_launch_folder === "boolean" ? e.trust_client_launch_folder : g.trust,
          allowed: e.workspace_allowed_paths || [],
          blocked: e.workspace_blocked_paths || [],
          legacy: e.client_workspace_scope_overrides === true,
        };
        const writers = wsEntryWriters(() => entry() || {}, (en) => wsWriteAccount(a, en));
        writers.legacy = (on) => { const en = Object.assign({}, entry() || {}); if (on) en.client_workspace_scope_overrides = true; else delete en.client_workspace_scope_overrides; return wsWriteAccount(a, en); };
        body.append(wsEditor(`ws-acc-${tenant}-${a.id}`.replace(/[^A-Za-z0-9_-]/g, "-"), v, writers, {
          allowedHelp: "Folders this account may use, on top of the gateway's allowed folders.",
          blockedHelp: "Folders this account may never use, on top of the gateway's refused folders.",
        }));
      };
      afSwitchBind(sw.button, async (next) => {
        if (next) {
          // An own policy starts as a copy of the gateway's mode and trust, then diverges.
          await wsQueue(() => wsWriteAccount(a, { mode: g.mode, trust_client_launch_folder: g.trust }));
          fillBody();
          return true;
        }
        // Dropping it forgets this account's folders: ask inline first.
        const box = wsEl("div", "ws-confirm");
        box.setAttribute("role", "group");
        box.append(wsEl("span", "", `Drop ${a.id}'s own policy? Their agents follow the gateway policy again.`));
        const yes = wsEl("button", "danger", "Drop");
        yes.type = "button";
        const no = wsEl("button", "secondary", "Cancel");
        no.type = "button";
        no.onclick = () => box.remove();
        yes.onclick = async () => {
          yes.disabled = true;
          try {
            await wsQueue(() => wsWriteAccount(a, null));
            afSwitchSet(sw.button, { checked: false });
            box.remove();
            fillBody();
          } catch (e) {
            yes.disabled = false;
            box.querySelector("span").textContent = `Not dropped: ${emailErrorText(e)}`;
          }
        };
        box.append(yes, no);
        head.after(box);
        try { no.focus(); } catch {}
        return false;
      }, (e) => { const s = li.querySelector(".ws-acc__summary"); if (s) s.textContent = `Not saved: ${emailErrorText(e)}`; });
      li.append(head, body);
      fillBody();
      return li;
    }
    function renderWorkspaces() {
      const root = $("workspaces-root");
      root.textContent = "";
      if (wsStore.error) {
        root.append(wsEl("p", "ws-error", `Workspaces could not be loaded: ${wsStore.error}`));
        return;
      }
      const summary = wsEl("p", "ws-summary");
      summary.setAttribute("data-ws-summary", "");
      summary.setAttribute("role", "status");
      root.append(summary);
      if (wsStore.admin) {
        const g = wsGateway();
        const card = wsEl("article", "ui-card ws-card");
        card.setAttribute("data-ws-scope", "gateway");
        card.innerHTML = `<div class="ui-card__head"><div class="ui-card__titles"><h3 class="ui-card__title">Gateway policy</h3><p class="ui-card__note">Every account follows it unless it has its own policy below.</p></div></div>`;
        card.append(wsEditor("ws-gw", g, {
          mode: (m) => wsWriteGateway({ workspace_default_mode: m }),
          trust: (on) => wsWriteGateway({ trust_client_launch_folder: on }),
          allowed: (list) => wsWriteGateway({ workspace_allowed_paths: list }),
          blocked: (list) => wsWriteGateway({ workspace_blocked_paths: list }),
          root: (path) => wsWriteGateway({ workspace_root: path }),
          legacy: (on) => wsWriteGateway({ client_workspace_scope_overrides: on }),
        }, {
          allowedHelp: "Folders every account may use.",
          blockedHelp: "Folders no agent may ever use, in either mode.",
        }));
        root.append(card);
        const acc = wsEl("article", "ui-card ws-card");
        acc.setAttribute("data-ws-scope", "accounts");
        acc.innerHTML = `<div class="ui-card__head"><div class="ui-card__titles"><h3 class="ui-card__title">Per-account policies</h3><p class="ui-card__note">Turn on Own policy to give one account different folders.</p></div></div>`;
        const users = wsStore.accounts.filter((a) => a.kind !== "entity" && !a.archived);
        const list = wsEl("ul", "ws-accounts");
        for (const a of users) list.append(wsAccountRow(a, g));
        if (!users.length) acc.append(wsEl("p", "ws-empty", "No accounts yet."));
        acc.append(list);
        if (wsStore.accounts.some((a) => a.kind === "entity")) acc.append(wsEl("p", "ws-note", "Entities: their folders are set in Manage, on the Accounts page."));
        root.append(acc);
      } else {
        const s = wsStore.self;
        const gd = { mode: s.gateway_defaults.mode, trust: Boolean(s.gateway_defaults.trust_client_launch_folder) };
        const card = wsEl("article", "ui-card ws-card");
        card.setAttribute("data-ws-scope", "self");
        card.innerHTML = `<div class="ui-card__head"><div class="ui-card__titles"><h3 class="ui-card__title">Your policy</h3><p class="ui-card__note">Turn on Own policy to choose your own folders; the gateway's refused folders always apply.</p></div></div>`;
        const head = wsEl("div", "ws-acc__head");
        head.append(wsEl("span", "ws-acc__summary", s.customized ? "You have your own policy." : "You follow the gateway policy."));
        const swWrap = wsEl("span", "ws-acc__switch");
        const sw = afSwitchCreate({ id: "ws-own-self", label: "Own policy", ariaLabel: "Own policy", checked: Boolean(s.customized), small: true });
        swWrap.append(...sw.nodes);
        head.append(swWrap);
        const body = wsEl("div", "ws-acc__body");
        const fill = () => {
          body.textContent = "";
          const cur = wsStore.self;
          if (!cur.customized) return;
          const e = cur.policy || {};
          const v = { mode: e.mode || gd.mode, trust: typeof e.trust_client_launch_folder === "boolean" ? e.trust_client_launch_folder : gd.trust, allowed: e.workspace_allowed_paths || [], blocked: e.workspace_blocked_paths || [] };
          // The admin-classed grant is never part of a self write (the route preserves it).
          const own = () => { const en = Object.assign({}, wsStore.self.policy || {}); delete en.client_workspace_scope_overrides; return en; };
          body.append(wsEditor("ws-self", v, wsEntryWriters(own, wsWriteSelf), {
            allowedHelp: "Folders your agents may use, on top of the gateway's allowed folders.",
            blockedHelp: "Folders your agents may never use.",
          }));
        };
        afSwitchBind(sw.button, async (next) => {
          if (next) { await wsQueue(() => wsWriteSelf({ mode: gd.mode, trust_client_launch_folder: gd.trust })); fill(); head.querySelector(".ws-acc__summary").textContent = "You have your own policy."; return true; }
          const box = wsEl("div", "ws-confirm");
          box.setAttribute("role", "group");
          box.append(wsEl("span", "", "Drop your own policy? Your agents follow the gateway policy again."));
          const yes = wsEl("button", "danger", "Drop");
          yes.type = "button";
          const no = wsEl("button", "secondary", "Cancel");
          no.type = "button";
          no.onclick = () => box.remove();
          yes.onclick = async () => {
            yes.disabled = true;
            try { await wsQueue(() => wsWriteSelf(null)); afSwitchSet(sw.button, { checked: false }); box.remove(); fill(); head.querySelector(".ws-acc__summary").textContent = "You follow the gateway policy."; }
            catch (e) { yes.disabled = false; box.querySelector("span").textContent = `Not dropped: ${emailErrorText(e)}`; }
          };
          box.append(yes, no);
          head.after(box);
          try { no.focus(); } catch {}
          return false;
        }, (e) => { head.querySelector(".ws-acc__summary").textContent = `Not saved: ${emailErrorText(e)}`; });
        card.append(head, body);
        fill();
        root.append(card);
      }
      wsRenderSummary();
      // `#workspaces?account=<id>` (the Accounts Workspace icon): that account's row, in view.
      const f = wsStore.focus;
      if (f && wsStore.admin) {
        const row = root.querySelector(`[data-ws-account="${CSS.escape(f.account)}"]`);
        if (row) {
          row.classList.add("is-focus");
          try { row.scrollIntoView({ block: "center" }); } catch {}
          const target = row.querySelector(".ws-acc__body input, .ws-acc__switch [role=switch]");
          try { if (target) target.focus({ preventScroll: true }); } catch {}
        }
      }
    }
"""
