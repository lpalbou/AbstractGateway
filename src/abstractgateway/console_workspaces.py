"""The Accounts table's icon actions and card list, the Runtimes account filter (round 8), and
the Workspaces modals (round 11, DESIGN.md R11.1 FINAL / R11.2).

Accounts (console.py renderAccounts): columns Name · Email (ONE column, "address · state") ·
Runtime (a link to the Runtimes page filtered to that account) · Active · Actions. The actions are
44 px icon buttons with the KIT tooltip (`data-af-tip`, bound once by the islands' bindTooltips;
an explicit sentence per button, e.g. "Archive alice (kept, hidden)") — no "⋯" menu, no labels, no
native `title`. The table never scrolls sideways at 1280-2560 px; below ~900 px (or when the table
itself is narrower than its column minimums) the rows become flat cards and the actions wrap.

Runtimes: `#runtimes?account=<id>[&tenant_id=<t>]` = GET /admin/runtimes?account=<id>; the filter
shows as a removable chip ("Account: alice ×"); the link survives a reload.

Workspaces (round 11; no Workspaces page): both modals mount the
kit WorkspaceChooser (islands mountWorkspaceChooser — the same component, rows and words as
AbstractCode, Flow, Observer and the Assistant), state from the islands' workspaceAsState:
- "Eligible workspaces" (admins, top of Accounts; also the Runtimes "Workspace" cell of the
  default plane) = level "gateway": GET /workspace/policy, one PUT /workspace/policy per change —
  the posture, Everything else, and rows whose mode is the CAP every account stays under;
  built-in refusals shown fixed.
- The workspace icon on every Accounts row the gateway marks `actions.workspace` available
  (humans, entities incl. legacy ones, the admin's own; entities for admins and their creator) =
  level "account": GET/PUT /workspace/policy/{account}, "Follow the gateway policy", the account's
  own posture and rows (a mode above the cap disabled, with the kit tooltip). The signed-in user's
  own row uses `me`.
Every change is ONE PUT; a refusal shows the gateway's sentence + "Not saved."; no Save button.

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

/* ---- Accounts head: the "Eligible workspaces" button (admins) sits first, at the top. */
.accounts-head__actions .accounts-gw-workspace { margin-right: auto; }

/* ---- Runtimes account filter chip (round 8). */
.runtimes-filter { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 10px; }
.runtimes-filter[hidden] { display: none; }
.runtimes-filter__chip { display: inline-flex; align-items: center; gap: 2px; padding: 0 0 0 12px; border: 1px solid var(--accent); border-radius: 999px; background: var(--accent-subtle, transparent); color: var(--text-primary, var(--text)); font-size: var(--font-size-md); font-weight: 600; }
.runtimes-filter__chip .icon-btn { border: 0; background: transparent; width: 36px; height: 36px; min-width: 36px; min-height: 36px; flex-basis: 36px; border-radius: 999px; }
@media (pointer: coarse) { .runtimes-filter__chip .icon-btn { width: 44px; height: 44px; min-width: 44px; min-height: 44px; flex-basis: 44px; } }
"""

WORKSPACES_CSS = r"""
/* ---- Workspaces modals (round 11, console_workspaces.py): "Eligible workspaces" and one
   account's workspaces, both the kit WorkspaceChooser. */
.wsm-body { display: grid; gap: 16px; min-width: 0; }
.wsm-chooser { min-width: 0; }
/* The modal's title already names it ("Eligible workspaces" / "Workspaces — <id>"): the
   chooser's own heading stays for screen readers (it labels the section) but is not shown twice;
   its one-line help stays visible. */
.wsm-chooser > .af-workspace > .af-settings-group__head { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
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

    // ---- Workspaces modals (round 11, DESIGN R11.1 FINAL): ONE kit WorkspaceChooser (islands
    // mountWorkspaceChooser — the same component, rows and words as AbstractCode, Flow, Observer
    // and the Assistant) at two levels: "Eligible workspaces" (level "gateway", admins: GET/PUT
    // /workspace/policy) and one account's default (level "account": GET/PUT
    // /workspace/policy/{account|me}). The state is the islands' workspaceAsState(answer, level)
    // (it refuses an older answer loudly); every change is ONE PUT of the chooser's payload; a
    // refusal shows the gateway's sentence + "Not saved." under the control. No Save button.
    const wsStore = { gwRelease: null, gwIsland: null, accRelease: null, accIsland: null };
    function wsEl(tag, cls, text) {
      const el = document.createElement(tag);
      if (cls) el.className = cls;
      if (text !== undefined) el.textContent = text;
      return el;
    }
    function wsLib() {
      const lib = islandsLib();
      const T = lib && lib.workspaceChooserText;
      if (!lib || typeof lib.mountWorkspaceChooser !== "function" || typeof lib.workspaceAsState !== "function" || !T || !T.gatewayTitle) {
        throw new Error("AbstractGateway console: the islands bundle has no round-11 WorkspaceChooser (mountWorkspaceChooser + workspaceAsState; ui-kit 0.8.2+ required).");
      }
      return lib;
    }
    function wsText() { return wsLib().workspaceChooserText; }
    // Mount the chooser for one level into a modal body; loads the level's route and PUTs each change.
    async function wsOpenLevel(o) {
      const lib = wsLib();
      const body = $(o.bodyId);
      body.textContent = "";
      const host = wsEl("div", "wsm-chooser");
      host.setAttribute("data-ws-level", o.level);
      body.append(host);
      const props = {
        level: o.level,
        state: null,
        loadError: null,
        idPrefix: o.idPrefix,
        save: async (payload) => {
          let out;
          try { out = await api(o.path, { method: "PUT", body: JSON.stringify(payload) }); }
          catch (e) { throw new Error(emailErrorText(e)); }
          const next = lib.workspaceAsState(out, o.level);
          props.state = next;
          const island = wsStore[o.island];
          if (island) island.update(Object.assign({}, props));
          return next;
        },
      };
      wsStore[o.island] = lib.mountWorkspaceChooser(host, Object.assign({}, props));
      const backdrop = $(o.backdropId);
      backdrop.hidden = false;
      wsStore[o.release] = bindAccountModal(backdrop, o.close);
      try {
        props.state = lib.workspaceAsState(await api(o.path), o.level);
      } catch (e) {
        props.loadError = `${o.what} could not be loaded: ${emailErrorText(e)}`;
      }
      const island = wsStore[o.island];
      if (island) island.update(Object.assign({}, props));
    }
    function wsClose(backdropId, bodyId, islandKey, releaseKey) {
      const backdrop = $(backdropId);
      if (backdrop.hidden) return;
      if (wsStore[islandKey]) { wsStore[islandKey].unmount(); wsStore[islandKey] = null; }
      $(bodyId).textContent = "";
      backdrop.hidden = true;
      const release = wsStore[releaseKey];
      wsStore[releaseKey] = null;
      if (release) release();
    }

    // -- "Eligible workspaces" (admins): the gateway level — the posture, Everything else, and the
    // rows whose mode is the CAP for every account; built-in refusals shown fixed.
    function closeGatewayWorkspace() { wsClose("gateway-workspace-backdrop", "gateway-workspace-body", "gwIsland", "gwRelease"); }
    async function openGatewayWorkspace() {
      closeGatewayWorkspace();
      $("gateway-workspace-title").textContent = wsText().gatewayTitle;
      await wsOpenLevel({
        level: "gateway", path: "/api/gateway/workspace/policy", idPrefix: "wsg", what: "The eligible workspaces",
        bodyId: "gateway-workspace-body", backdropId: "gateway-workspace-backdrop", island: "gwIsland", release: "gwRelease", close: closeGatewayWorkspace,
      });
    }

    // -- ONE account's default (humans and entities; the signed-in user's own row = `me`): the
    // account level — "Follow the gateway policy", the account's own posture and rows <= the caps.
    function wsAccountKey(a) {
      if (a.own) return "me";
      return `${a.tenant_id || "default"}:${a.id}`;
    }
    function closeAccountWorkspace() { wsClose("account-workspace-backdrop", "account-workspace-body", "accIsland", "accRelease"); }
    async function openAccountWorkspace(a) {
      closeAccountWorkspace();
      const kind = a.kind === "entity" ? "entity" : (a.role === "admin" ? "admin" : "user");
      $("account-workspace-title").textContent = `${wsText().title} — ${a.id}`;
      const body = $("account-workspace-body");
      body.setAttribute("data-ws-account", a.id);
      body.setAttribute("data-ws-kind", kind);
      await wsOpenLevel({
        level: "account", path: `/api/gateway/workspace/policy/${encodeURIComponent(wsAccountKey(a))}`,
        idPrefix: `wsa-${String(a.id).replace(/[^A-Za-z0-9_-]/g, "-")}`, what: `${a.id}'s workspaces`,
        bodyId: "account-workspace-body", backdropId: "account-workspace-backdrop", island: "accIsland", release: "accRelease", close: closeAccountWorkspace,
      });
    }
"""
