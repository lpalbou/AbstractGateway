"""The console's Skills & MCP page (DESIGN-v3 §6): kit tabs Skills | MCP servers.

Skills: the shelf settings (console_ui.py mountSkillsShelf, moved here from Apps), then one
table of every skill (curated + imported; archived with "Show archived") — Name · What it does ·
Version · Trust · Source · actions View · Export · Archive (imported). The skill modal
(`af-modal af-modal--wide`) shows SKILL.md in a monospace editor with the metadata fields and ONE
primary action: Save (imported), Duplicate to edit (curated), Unarchive (archived).

MCP servers: the gateway's `agents_note` sentence (whether agents are offered tools) and a per-server
kit switch **Enabled for agents** (inline confirmation to turn on; lane mcp-runs owns the route);
a table Name · Transport · Status (last test) · Tools · actions Edit · Test · Archive; the Add /
Edit modal has Name, How to reach it (Command | URL tabs), headers (values masked once saved),
description, **Test connection** (the real handshake, result inline) and primary **Save**.

Routes: GET /skills, GET /skills/{name}, GET /skills/{name}/export, POST /admin/skills/import,
PUT /admin/skills/{name}, POST /admin/skills/{name}/duplicate|archive|unarchive; GET
/mcp/servers, POST /admin/mcp/servers, PUT /admin/mcp/servers/{name}, POST
/admin/mcp/servers/{name}/archive|unarchive|test, POST /admin/mcp/test.

Spliced into the console script scope with the UI layer (console.py _console_owned_sources):
`api`, `esc`, `$`, `state`, `afSwitchCreate`, `afSwitchBind`, `bindAccountModal` and
`mountSkillsShelf` are the console's own.
"""

SKILLS_MCP_CSS = r"""
/* ---- Skills & MCP page (console_skills_mcp.py) ---- */
.skmcp-page { display: flex; flex-direction: column; gap: 14px; }
.skmcp-page .af-tabs__panel { padding-top: 14px; display: flex; flex-direction: column; gap: 12px; }
.skmcp-purpose { margin: 0; max-width: none; }
.skmcp-truth { margin: 0; padding: 10px 12px; max-width: 90ch; border-left: 3px solid var(--warning, var(--accent)); background: color-mix(in srgb, var(--warning, var(--accent)) 9%, transparent); border-radius: var(--radius-sm); font-size: var(--font-size-base); line-height: 1.45; }
.skmcp-shelf { border-top: 1px solid var(--line-soft); padding: 12px 0 0; }
/* Round 8: the shelf folder is ONE inline row (label, field, Refresh curated shelf, status). */
.skmcp-shelf-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; min-width: 0; }
.skmcp-shelf-row > label { margin: 0; font-weight: 600; font-size: var(--font-size-md); color: var(--text-primary); text-transform: none; letter-spacing: 0; white-space: nowrap; }
.skmcp-shelf-row > input { flex: 1 1 260px; min-width: 0; margin: 0; font-family: var(--font-mono); }
.skmcp-shelf-row > .ui-btn { white-space: nowrap; min-height: 44px; }
.skmcp-shelf .ui-field-msg { margin-top: 6px; }
.skmcp-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; }
.skmcp-toolbar input[type="search"] { flex: 1 1 260px; min-width: 0; }
.skmcp-toolbar .skmcp-spacer { flex: 1 1 auto; }
.skmcp-table { width: 100%; table-layout: fixed; border-collapse: collapse; }
.skmcp-table th { white-space: nowrap; text-align: left; }
.skmcp-table td { vertical-align: top; padding-top: 10px; padding-bottom: 10px; overflow-wrap: anywhere; }
.skills-table th.sk-col-name { width: 15%; }
.skills-table th.sk-col-what { width: auto; }
.skills-table th.sk-col-version { width: 6.5rem; }
.skills-table th.sk-col-trust { width: 7.5rem; }
.skills-table th.sk-col-source { width: 8.5rem; }
.skills-table th.sk-col-actions { width: 16rem; }
.mcp-table th.mcp-col-name { width: 18%; }
.mcp-table th.mcp-col-transport { width: auto; }
.mcp-table th.mcp-col-status { width: 26%; }
.mcp-table th.mcp-col-tools { width: 9rem; }
.mcp-table th.mcp-col-actions { width: 14rem; }
.skmcp-name { font-weight: 600; }
.skmcp-sub { display: block; color: var(--muted); font-size: var(--af-helper-size, var(--font-size-md)); font-weight: 400; margin-top: 2px; }
.skmcp-more { cursor: pointer; border-radius: var(--radius-sm); }
.skmcp-more[aria-expanded="false"] { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; }
.skmcp-more:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.skmcp-clamp { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; }
.skmcp-mono { font-family: var(--font-mono); font-size: var(--font-size-md); display: block; overflow-wrap: anywhere; word-break: break-word; }
.skmcp-chip { display: inline-flex; align-items: center; min-height: 22px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--line); font-size: var(--font-size-sm); font-weight: 500; white-space: nowrap; }
.skmcp-chip.is-ok { border-color: color-mix(in srgb, var(--success, #2f9e44) 55%, transparent); color: var(--success, #2f9e44); }
.skmcp-chip.is-warn { border-color: color-mix(in srgb, var(--warning, #c77700) 55%, transparent); color: var(--warning, #c77700); }
.skmcp-chip.is-err { border-color: color-mix(in srgb, var(--error, #d6336c) 55%, transparent); color: var(--error, #d6336c); }
.skmcp-chip.is-muted { color: var(--muted); }
.skmcp-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
@media (min-width: 1024px) { .skmcp-actions { flex-wrap: nowrap; } }
.skmcp-actions > button { white-space: nowrap; }
.skmcp-agents { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; margin-top: 8px; }
.skmcp-confirm { display: flex; flex-direction: column; gap: 6px; padding: 8px 10px; border: 1px solid var(--line); border-radius: var(--radius-sm); font-size: var(--font-size-base); }
.skmcp-confirm__buttons { display: flex; gap: 6px; flex-wrap: wrap; }
.skmcp-status.is-failed { color: var(--error, #d6336c); }
.skmcp-tools > summary { cursor: pointer; min-height: 28px; }
.skmcp-tools ul, .skmcp-test__tools { margin: 6px 0 0; padding-left: 18px; font-size: var(--font-size-md); }
.skmcp-tools li, .skmcp-test__tools li { margin: 2px 0; }
.skmcp-tools li code, .skmcp-test__tools li code { font-weight: 600; }
.skmcp-empty { padding: 14px 2px; color: var(--muted); }
/* Modals (kit af-modal af-modal--wide): body content only. */
.skmcp-form { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px 18px; }
.skmcp-field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.skmcp-field--wide { grid-column: 1 / -1; }
.skmcp-field > label, .skmcp-field > .skmcp-label { font-size: var(--font-size-base); font-weight: 600; text-transform: none; letter-spacing: normal; color: var(--text); }
.skmcp-field input, .skmcp-field textarea { width: 100%; box-sizing: border-box; }
.skmcp-help { margin: 0; color: var(--muted); font-size: var(--af-helper-size, var(--font-size-md)); line-height: 1.4; }
.skmcp-editor { font-family: var(--font-mono); font-size: var(--font-size-md); line-height: 1.5; min-height: 22rem; resize: vertical; tab-size: 2; white-space: pre; overflow-wrap: normal; overflow-x: auto; }
.skmcp-lead { margin: 0 0 14px; padding: 10px 12px; border-left: 3px solid var(--info, var(--accent)); background: color-mix(in srgb, var(--info, var(--accent)) 8%, transparent); border-radius: var(--radius-sm); font-size: var(--font-size-base); line-height: 1.45; }
.skmcp-files { margin: 0; padding-left: 18px; font-size: var(--font-size-md); columns: 2; }
.skmcp-files code { font-size: var(--font-size-md); }
.skmcp-headers { display: flex; flex-direction: column; gap: 8px; }
.skmcp-header-row { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 3fr) auto; gap: 8px; align-items: center; }
.skmcp-test { margin-top: 4px; padding: 10px 12px; border-radius: var(--radius-sm); border: 1px solid var(--line-soft); font-size: var(--font-size-base); }
.skmcp-test.is-ok { border-color: color-mix(in srgb, var(--success, #2f9e44) 50%, transparent); }
.skmcp-test.is-failed { border-color: color-mix(in srgb, var(--error, #d6336c) 50%, transparent); }
.skmcp-test__head { font-weight: 600; margin: 0; }
.skmcp-footer-actions { display: flex; gap: 8px; margin-left: auto; flex-wrap: wrap; }
.skmcp-message:empty { display: none; }
@media (max-width: 1023.98px) {
  .skills-table .sk-th-what, .skills-table td.sk-what, .skills-table .sk-th-source, .skills-table td.sk-source { display: none; }
  .skills-table th.sk-col-name { width: auto; }
  .skmcp-fold { display: block; }
  .mcp-table .mcp-th-transport, .mcp-table td.mcp-transport { display: none; }
  .mcp-table th.mcp-col-name { width: auto; }
}
@media (min-width: 1024px) { .skmcp-fold { display: none; } }
@media (max-width: 767.98px) {
  .skmcp-table, .skmcp-table tbody, .skmcp-table tr, .skmcp-table td { display: block; width: 100%; }
  .skmcp-table thead { display: none; }
  .skmcp-table tr.skmcp-row { display: grid; grid-template-columns: minmax(0, 1fr); gap: 4px; padding: 12px 0; border-top: 1px solid var(--line-soft); }
  .skmcp-table tr.skmcp-row > td { padding: 0; border: 0; width: auto; }
  .skmcp-table td.sk-version, .skmcp-table td.sk-trust, .skmcp-table td.mcp-tools { display: inline; }
  .skmcp-actions { justify-content: flex-start; }
  .skmcp-actions > button, .skmcp-toolbar > button, .skmcp-header-row > button { min-height: 44px; }
  .skmcp-form { grid-template-columns: minmax(0, 1fr); }
  .skmcp-header-row { grid-template-columns: minmax(0, 1fr); }
  .skmcp-files { columns: 1; }
  .skmcp-help { font-size: max(14px, var(--af-helper-size, var(--font-size-md))); }
}
"""

SKILLS_MCP_JS = r"""
    // ---- Skills & MCP page (console_skills_mcp.py, DESIGN-v3 §6) ----
    const skmcp = {
      tab: "skills", opened: false,
      skills: null, skillsError: "", skillsArchived: false, search: "",
      mcp: null, mcpError: "", mcpArchived: false, agentsPending: "",
      skillModal: null, mcpModal: null, release: null,
    };
    const SKILL_TRUST_TEXT = { first_party: "First party", audited: "Audited", adopted: "Adopted", community: "Community", unverified: "Unverified", blocked: "Blocked" };
    const SKILL_TRUST_TONE = { first_party: "is-ok", audited: "is-ok", adopted: "is-ok", community: "is-muted", unverified: "is-warn", blocked: "is-err" };
    function skmcpAdmin() { return !!(state.principal && state.principal.admin); }
    function skmcpErr(e) { return String((e && e.message) || e || "The gateway did not say why."); }
    function skmcpAgo(iso, nowMs) {
      const t = Date.parse(String(iso || ""));
      if (!Number.isFinite(t)) return "";
      const s = Math.max(0, Math.round(((nowMs || Date.now()) - t) / 1000));
      if (s < 60) return "just now";
      const m = Math.round(s / 60);
      if (m < 60) return `${m} min ago`;
      const h = Math.round(m / 60);
      if (h < 24) return `${h} h ago`;
      return `${Math.round(h / 24)} d ago`;
    }
    // A skill row's Trust cell: the abstractskill level, its reasons on hover.
    function skillTrustCell(row) {
      if (row.archived) return `<span class="skmcp-chip is-muted">Archived</span>`;
      const level = row.blocked ? "blocked" : String(row.trust_level || "unverified");
      const reasons = (row.reasons || []).map((r) => String(r)).join("\n");
      return `<span class="skmcp-chip ${SKILL_TRUST_TONE[level] || "is-muted"}"${reasons ? ` title="${esc(reasons)}"` : ""}>${esc(SKILL_TRUST_TEXT[level] || level)}</span>`;
    }
    function skillsRowsMarkup(rows, opts) {
      const admin = !!(opts && opts.admin);
      const q = String((opts && opts.search) || "").trim().toLowerCase();
      const list = (rows || []).filter((r) => !q || `${r.name} ${r.description || ""}`.toLowerCase().includes(q));
      if (!list.length) {
        return `<tr><td colspan="6" class="skmcp-empty">${q ? "No skill matches this search." : "No skills on this shelf yet."}</td></tr>`;
      }
      return list.map((r) => {
        const n = esc(r.name);
        const version = r.version ? esc(r.version) : "—";
        const source = esc(r.source_label || (r.origin === "imported" ? "Imported" : "Curated registry"));
        const acts = [`<button type="button" class="secondary" data-skill-view="${n}">View</button>`];
        if (!r.archived) acts.push(`<button type="button" class="secondary" data-skill-export="${n}">Export</button>`);
        if (admin && r.origin === "imported" && !r.archived) acts.push(`<button type="button" class="secondary" data-skill-archive="${n}">Archive</button>`);
        if (admin && r.archived) acts.push(`<button type="button" class="secondary" data-skill-unarchive="${n}">Unarchive</button>`);
        return `<tr class="skmcp-row" data-skill-row="${n}">`
          + `<td class="sk-name"><span class="skmcp-name">${n}</span><div class="skmcp-sub skmcp-fold">${skmcpMore(r.description || "")}</div><span class="skmcp-sub skmcp-fold">${source}</span></td>`
          + `<td class="sk-what">${skmcpMore(r.description || "")}</td>`
          + `<td class="sk-version">${version}</td>`
          + `<td class="sk-trust">${skillTrustCell(r)}</td>`
          + `<td class="sk-source">${source}</td>`
          + `<td class="sk-actions"><div class="skmcp-actions">${acts.join("")}</div></td></tr>`;
      }).join("");
    }
    // A long cell: clamped to two lines, expanded in place on click / Enter / Space (aria-expanded).
    function skmcpMore(text, extraClass) {
      return `<div class="skmcp-more${extraClass ? ` ${extraClass}` : ""}" role="button" tabindex="0" aria-expanded="false" data-skmcp-more>${esc(text)}</div>`;
    }
    function mcpTransportText(s) {
      if (s.transport === "stdio") return [s.command].concat(s.args || []).join(" ").trim();
      return String(s.url || "");
    }
    function mcpStatusText(lastTest, nowMs) {
      if (!lastTest) return { text: "Not tested", failed: false };
      const ago = skmcpAgo(lastTest.at, nowMs);
      if (lastTest.ok) {
        const n = (lastTest.tools || []).length;
        return { text: `OK · ${n} tool${n === 1 ? "" : "s"}${ago ? ` · ${ago}` : ""}`, failed: false };
      }
      return { text: `Failed: ${String(lastTest.message || "no reason given")}`, failed: true };
    }
    function mcpToolsCell(lastTest) {
      if (!lastTest || !lastTest.ok) return `<span class="skmcp-sub">—</span>`;
      const tools = lastTest.tools || [];
      if (!tools.length) return "0";
      const items = tools.map((t) => `<li><code>${esc(t.name)}</code>${t.description ? ` — ${esc(t.description)}` : ""}</li>`).join("");
      return `<details class="skmcp-tools"><summary>${tools.length} tool${tools.length === 1 ? "" : "s"}</summary><ul>${items}</ul></details>`;
    }
    // "Enabled for agents" (lane mcp-runs: POST /admin/mcp/servers/{name}/agents {enabled}): a kit
    // af-switch labelled by the feature. Turning it ON asks inline first (the tools become callable by
    // agents); OFF applies at once. ON is refused, with the reason shown, for an archived server or one
    // whose last test did not succeed; a switch that is on can always be turned off.
    function mcpAgentsBlockReason(s) {
      if (!s) return "Save and test the server first: agents get the tools a successful test lists.";
      if (s.enabled_for_agents) return "";
      if (s.archived) return "Archived: unarchive it first.";
      if (!(s.last_test && s.last_test.ok)) return "Test the connection first: agents get the tools a successful test lists.";
      return "";
    }
    function mcpAgentsControlMarkup(s, opts) {
      const where = (opts && opts.where) || "row";
      const n = s ? esc(s.name) : "";
      const on = !!(s && s.enabled_for_agents);
      const reason = mcpAgentsBlockReason(s);
      const rid = `mcp-agents-${where}-${n || "new"}-reason`;
      const sw = `<button type="button" role="switch" class="af-switch af-switch--sm" aria-checked="${on ? "true" : "false"}" data-mcp-agents="${n}"`
        + (reason ? ` aria-disabled="true" title="${esc(reason)}" aria-describedby="${rid}"` : "")
        + `><span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">Enabled for agents</span></span></button>`;
      const line = reason
        ? `<span id="${rid}" class="skmcp-sub skmcp-agents__why">${esc(reason)}</span>`
        : `<span class="skmcp-sub skmcp-agents__status" data-mcp-agents-status>${esc((s && s.agents_status) || "")}</span>`;
      let confirm = "";
      if (s && opts && opts.pending === s.name && !on) {
        const k = ((s.last_test && s.last_test.tools) || []).length;
        confirm = `<div class="skmcp-confirm" role="group" aria-label="Confirm Enabled for agents"><span>Offer its ${k} tool${k === 1 ? "" : "s"} to your agents? Each call asks for approval unless a run allows all tools.</span>`
          + `<span class="skmcp-confirm__buttons"><button type="button" data-mcp-agents-confirm="${n}">Turn on</button><button type="button" class="secondary" data-mcp-agents-cancel="${n}">Cancel</button></span></div>`;
      }
      return `<div class="skmcp-agents">${sw}${line}${confirm}</div>`;
    }
    function mcpRowsMarkup(rows, opts) {
      const admin = !!(opts && opts.admin);
      const archived = !!(opts && opts.archived);
      const list = (rows || []).filter((s) => archived || !s.archived);
      if (!list.length) {
        return `<tr><td colspan="5" class="skmcp-empty">No MCP server registered yet.${admin ? " Add one to check that the gateway can reach it." : ""}</td></tr>`;
      }
      return list.map((s) => {
        const n = esc(s.name);
        const st = mcpStatusText(s.last_test, opts && opts.now);
        const reach = esc(mcpTransportText(s));
        const how = s.transport === "stdio" ? "Command" : "URL";
        const acts = [];
        if (admin && !s.archived) {
          acts.push(`<button type="button" class="secondary" data-mcp-edit="${n}">Edit</button>`);
          acts.push(`<button type="button" class="secondary" data-mcp-test="${n}">Test</button>`);
          acts.push(`<button type="button" class="secondary" data-mcp-archive="${n}">Archive</button>`);
        }
        if (admin && s.archived) acts.push(`<button type="button" class="secondary" data-mcp-unarchive="${n}">Unarchive</button>`);
        return `<tr class="skmcp-row" data-mcp-row="${n}">`
          + `<td class="mcp-name"><span class="skmcp-name">${n}</span>${s.archived ? ` <span class="skmcp-chip is-muted">Archived</span>` : ""}`
          + `${s.description ? `<span class="skmcp-sub">${esc(s.description)}</span>` : ""}`
          + `<span class="skmcp-sub skmcp-fold">${how}</span><div class="skmcp-fold">${skmcpMore(mcpTransportText(s), "skmcp-mono")}</div></td>`
          + `<td class="mcp-transport"><span class="skmcp-sub">${how}</span>${skmcpMore(mcpTransportText(s), "skmcp-mono")}</td>`
          + `<td class="mcp-status"><span class="skmcp-status${st.failed ? " is-failed" : ""}">${esc(st.text)}</span>${admin ? mcpAgentsControlMarkup(s, { where: "row", pending: opts && opts.pending }) : ""}</td>`
          + `<td class="mcp-tools">${mcpToolsCell(s.last_test)}</td>`
          + `<td class="mcp-actions"><div class="skmcp-actions">${acts.join("")}</div></td></tr>`;
      }).join("");
    }
    function mcpTestResultMarkup(result) {
      if (!result) return "";
      if (result.pending) return `<div class="skmcp-test" role="status"><p class="skmcp-test__head">Connecting (up to 10 seconds)...</p></div>`;
      if (!result.ok) return `<div class="skmcp-test is-failed" role="status"><p class="skmcp-test__head">Connection failed</p><p class="skmcp-help">${esc(result.message || "")}</p></div>`;
      const tools = (result.tools || []).map((t) => `<li><code>${esc(t.name)}</code>${t.description ? ` — ${esc(t.description)}` : ""}</li>`).join("");
      return `<div class="skmcp-test is-ok" role="status"><p class="skmcp-test__head">${esc(result.message || "Connected.")}</p>${tools ? `<ul class="skmcp-test__tools">${tools}</ul>` : ""}</div>`;
    }
    function skmcpMessage(id, text, tone) {
      const el = $(id);
      if (!el) return;
      el.textContent = text || "";
      el.className = `message skmcp-message${tone ? ` ${tone}` : ""}`;
    }
    function skmcpSetTab(tab) {
      skmcp.tab = tab === "mcp" ? "mcp" : "skills";
      for (const [id, pane] of [["skmcp-tab-skills", "skmcp-pane-skills"], ["skmcp-tab-mcp", "skmcp-pane-mcp"]]) {
        const on = (id === "skmcp-tab-mcp") === (skmcp.tab === "mcp");
        $(id).setAttribute("aria-selected", on ? "true" : "false");
        $(id).tabIndex = on ? 0 : -1;
        $(pane).hidden = !on;
      }
    }
    function openSkillsMcpPage() {
      if (!state.principal || !skmcpAdmin()) return;
      if (!skmcp.opened) {
        skmcp.opened = true;
        // The skills shelf setting lives at the top of the Skills tab (it moved here from Apps).
        mountSkillsShelf("skills", $("skills-settings-root"));
        const sw = afSwitchCreate({ id: "skills-show-archived", label: "Show archived", checked: false, small: true });
        $("skills-archived-slot").append(...sw.nodes);
        afSwitchBind(sw.button, async (next) => { skmcp.skillsArchived = next; await loadSkillsList(); return next; }, (e) => skmcpMessage("skills-message", skmcpErr(e), "error"));
        const sw2 = afSwitchCreate({ id: "mcp-show-archived", label: "Show archived", checked: false, small: true });
        $("mcp-archived-slot").append(...sw2.nodes);
        afSwitchBind(sw2.button, async (next) => { skmcp.mcpArchived = next; renderMcpList(); return next; }, () => {});
        $("skills-import-zip").hidden = !skmcpAdmin();
        $("skills-import-folder").hidden = !skmcpAdmin();
        $("mcp-add").hidden = !skmcpAdmin();
      }
      skmcpSetTab(skmcp.tab);
      loadSkillsList();
      loadMcpList();
    }
    async function loadSkillsList() {
      try {
        skmcp.skills = await api(`/api/gateway/skills${skmcp.skillsArchived ? "?include_archived=1" : ""}`);
        skmcp.skillsError = "";
      } catch (e) {
        skmcp.skillsError = skmcpErr(e);
      }
      renderSkillsList();
    }
    function renderSkillsList() {
      const body = $("skills-table");
      if (!body) return;
      if (skmcp.skillsError) {
        body.innerHTML = `<tr><td colspan="6" class="skmcp-empty">Could not list the skills: ${esc(skmcp.skillsError)}</td></tr>`;
        return;
      }
      if (!skmcp.skills) { body.innerHTML = `<tr><td colspan="6" class="skmcp-empty">Reading the skills...</td></tr>`; return; }
      body.innerHTML = skillsRowsMarkup(skmcp.skills.skills || [], { admin: skmcpAdmin(), search: skmcp.search });
      const warn = (skmcp.skills.warnings || []).filter((w) => !String(w).startsWith("#FALLBACK"));
      $("skills-warnings").textContent = warn.join(" ");
    }
    async function loadMcpList() {
      try {
        skmcp.mcp = await api("/api/gateway/mcp/servers");
        skmcp.mcpError = "";
      } catch (e) {
        skmcp.mcpError = skmcpErr(e);
      }
      renderMcpList();
    }
    function renderMcpList() {
      const body = $("mcp-table");
      if (!body) return;
      if (skmcp.mcpError) { body.innerHTML = `<tr><td colspan="5" class="skmcp-empty">Could not list the MCP servers: ${esc(skmcp.mcpError)}</td></tr>`; return; }
      if (!skmcp.mcp) { body.innerHTML = `<tr><td colspan="5" class="skmcp-empty">Reading the MCP servers...</td></tr>`; return; }
      // The truth sentence comes from the gateway (agents_note), never a client-side claim.
      $("mcp-truth").textContent = String(skmcp.mcp.agents_note || "");
      body.innerHTML = mcpRowsMarkup(skmcp.mcp.servers || [], { admin: skmcpAdmin(), archived: skmcp.mcpArchived, pending: skmcp.agentsPending });
    }
    function exportSkill(name) {
      // A plain navigation: the gateway's Content-Disposition names the zip.
      window.location.href = `/api/gateway/skills/${encodeURIComponent(name)}/export`;
    }
    async function importSkillUpload(form, label) {
      skmcpMessage("skills-message", `Importing ${label}...`, "");
      try {
        const res = await fetch("/api/gateway/admin/skills/import", { method: "POST", body: form, credentials: "same-origin", headers: skmcpUploadHeaders() });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          const d = data && data.detail;
          throw new Error((d && d.message) || d || `HTTP ${res.status}`);
        }
        skmcpMessage("skills-message", `Imported ${data.name}. It is Unverified until a reviewer adds it to the shelf's validations; open it to read or edit it.`, "ok");
        await loadSkillsList();
      } catch (e) {
        skmcpMessage("skills-message", `Not imported: ${skmcpErr(e)}`, "error");
      }
    }
    function skmcpUploadHeaders() {
      // Multipart: the browser sets the boundary; the CSRF header rides like api()'s.
      const h = { Accept: "application/json" };
      const token = csrf();
      if (token) h["X-AbstractGateway-CSRF"] = decodeURIComponent(token);
      return h;
    }
    async function skillAction(kind, name) {
      try {
        await api(`/api/gateway/admin/skills/${encodeURIComponent(name)}/${kind}`, { method: "POST", body: "{}" });
        skmcpMessage("skills-message", kind === "archive" ? `Archived ${name}: runs no longer see it. Turn on Show archived to find it again.` : `Unarchived ${name}: it is back on the shelf.`, "ok");
        await loadSkillsList();
      } catch (e) {
        skmcpMessage("skills-message", `${kind === "archive" ? "Not archived" : "Not unarchived"}: ${skmcpErr(e)}`, "error");
      }
    }
    // ---- the skill modal (af-modal af-modal--wide) ----
    function skillModalMarkup(d, opts) {
      const admin = !!(opts && opts.admin);
      const fm = d.frontmatter || {};
      const ro = !d.editable || !admin;
      const roAttr = ro ? " readonly" : "";
      const files = (d.files || []).map((f) => `<li><code>${esc(f.path)}</code> <span class="skmcp-sub" style="display:inline">${esc(String(f.size))} B</span></li>`).join("");
      let lead = "";
      if (!d.editable) lead = `<p class="skmcp-lead">${esc(d.read_only_reason || "This skill is read-only.")}</p>`;
      else if (!admin) lead = `<p class="skmcp-lead">Only an admin can edit skills.</p>`;
      return lead + `<div class="skmcp-form">`
        + `<div class="skmcp-field"><span class="skmcp-label">Name</span><code>${esc(d.name)}</code><p class="skmcp-help">How agents refer to this skill; it never changes (duplicate to rename).</p></div>`
        + `<div class="skmcp-field"><label for="skill-f-version">Version</label><input id="skill-f-version" type="text" autocomplete="off" value="${esc(d.version || "")}"${roAttr}></div>`
        + `<div class="skmcp-field skmcp-field--wide"><label for="skill-f-description">What it does</label><input id="skill-f-description" type="text" autocomplete="off" value="${esc(fm.description || "")}"${roAttr}><p class="skmcp-help">One sentence agents read to decide when to load this skill.</p></div>`
        + `<div class="skmcp-field"><label for="skill-f-license">License</label><input id="skill-f-license" type="text" autocomplete="off" value="${esc(fm.license || "")}"${roAttr}></div>`
        + `<div class="skmcp-field"><span class="skmcp-label">Source</span><span>${esc(d.origin === "imported" ? "Imported" : d.origin === "archived" ? "Imported (archived)" : "Curated registry")}</span></div>`
        + `<div class="skmcp-field skmcp-field--wide"><label for="skill-f-md">SKILL.md</label><textarea id="skill-f-md" class="skmcp-editor" spellcheck="false"${roAttr}>${esc(d.skill_md || "")}</textarea><p class="skmcp-help">What agents read when they load the skill; the fields above are written into its frontmatter on Save.</p></div>`
        + `<div class="skmcp-field skmcp-field--wide"><span class="skmcp-label">Files</span><ul class="skmcp-files">${files}</ul></div>`
        + `</div>`;
    }
    function closeSkillModal() {
      const backdrop = $("skill-modal-backdrop");
      if (backdrop.hidden) return;
      backdrop.hidden = true;
      $("skill-modal-body").textContent = "";
      const release = skmcp.release;
      skmcp.release = null;
      if (release) release();
      skmcp.skillModal = null;
    }
    function renderSkillModalFooter() {
      const d = skmcp.skillModal && skmcp.skillModal.detail;
      const box = $("skill-modal-actions");
      box.textContent = "";
      if (!d || !skmcpAdmin()) return;
      const btn = document.createElement("button");
      btn.type = "button";
      if (d.editable) { btn.textContent = "Save"; btn.dataset.skillSave = "1"; }
      else if (d.origin === "curated") { btn.textContent = "Duplicate to edit"; btn.dataset.skillDuplicate = d.name; }
      else if (d.origin === "archived") { btn.textContent = "Unarchive"; btn.dataset.skillModalUnarchive = d.name; }
      else return;
      box.append(btn);
    }
    function showSkillDetail(d) {
      skmcp.skillModal = { detail: d };
      $("skill-modal-title").textContent = `Skill — ${d.name}`;
      $("skill-modal-body").innerHTML = skillModalMarkup(d, { admin: skmcpAdmin() });
      $("skill-modal-note").textContent = d.problem || "";
      renderSkillModalFooter();
    }
    async function openSkillModal(name) {
      closeSkillModal();
      const backdrop = $("skill-modal-backdrop");
      $("skill-modal-title").textContent = `Skill — ${name}`;
      $("skill-modal-body").innerHTML = `<p class="skmcp-empty">Reading ${esc(name)}...</p>`;
      $("skill-modal-note").textContent = "";
      $("skill-modal-actions").textContent = "";
      backdrop.hidden = false;
      skmcp.release = bindAccountModal(backdrop, closeSkillModal);
      try {
        showSkillDetail(await api(`/api/gateway/skills/${encodeURIComponent(name)}`));
      } catch (e) {
        $("skill-modal-body").innerHTML = `<p class="message error">Could not open ${esc(name)}: ${esc(skmcpErr(e))}</p>`;
      }
    }
    async function saveSkillModal() {
      const d = skmcp.skillModal && skmcp.skillModal.detail;
      if (!d) return;
      const fm = d.frontmatter || {};
      const body = {};
      const md = $("skill-f-md").value;
      if (md !== d.skill_md) body.skill_md = md;
      const pairs = [["description", "skill-f-description", fm.description || ""], ["version", "skill-f-version", d.version || ""], ["license", "skill-f-license", fm.license || ""]];
      for (const [key, id, was] of pairs) { const now = $(id).value; if (now !== String(was)) body[key] = now; }
      if (!Object.keys(body).length) { $("skill-modal-note").textContent = "Nothing changed."; return; }
      $("skill-modal-note").textContent = "Saving...";
      try {
        const saved = await api(`/api/gateway/admin/skills/${encodeURIComponent(d.name)}`, { method: "PUT", body: JSON.stringify(body) });
        showSkillDetail(saved);
        $("skill-modal-note").textContent = "Saved. New runs read this version.";
        loadSkillsList();
      } catch (e) {
        $("skill-modal-note").textContent = `Not saved: ${skmcpErr(e)}`;
      }
    }
    async function duplicateSkillFromModal(name) {
      $("skill-modal-note").textContent = "Duplicating...";
      try {
        const copy = await api(`/api/gateway/admin/skills/${encodeURIComponent(name)}/duplicate`, { method: "POST", body: JSON.stringify({ name: `${name}-copy` }) });
        showSkillDetail(copy);
        $("skill-modal-note").textContent = `Copied to ${copy.name}: this copy is yours to edit (Unverified until reviewed).`;
        loadSkillsList();
      } catch (e) {
        $("skill-modal-note").textContent = `Not duplicated: ${skmcpErr(e)}`;
      }
    }
    // ---- the MCP server modal (af-modal af-modal--wide) ----
    function mcpModalMarkup(m) {
      const editing = !!m.existing;
      const s = m.existing || {};
      const transport = m.transport;
      const savedHeaders = s.headers || {};
      const headerRows = (m.headers || []).map((h, i) => {
        const saved = h.saved && savedHeaders[h.name];
        const ph = saved ? `Saved · fingerprint ${saved.fingerprint}; type to replace` : "Value";
        return `<div class="skmcp-header-row" data-mcp-header="${i}"><input type="text" aria-label="Header name" placeholder="Name, e.g. Authorization" value="${esc(h.name)}" data-mcp-header-name="${i}" autocomplete="off" spellcheck="false">`
          + `<input type="password" aria-label="Header value" placeholder="${esc(ph)}" value="${esc(h.value || "")}" data-mcp-header-value="${i}" autocomplete="new-password">`
          + `<button type="button" class="secondary" data-mcp-header-remove="${i}" aria-label="Remove this header">Remove</button></div>`;
      }).join("");
      const args = (m.args || []).join("\n");
      return `<div class="skmcp-form">`
        + `<div class="skmcp-field">${editing ? `<span class="skmcp-label">Name</span><code>${esc(s.name)}</code>` : `<label for="mcp-f-name">Name</label><input id="mcp-f-name" type="text" autocomplete="off" spellcheck="false" value="${esc(m.name || "")}">`}<p class="skmcp-help">A short name for this server (letters, digits, - _ .); its tools will be named after it.</p></div>`
        + `<div class="skmcp-field"><label for="mcp-f-description">Description</label><input id="mcp-f-description" type="text" autocomplete="off" value="${esc(m.description || "")}"><p class="skmcp-help">What this server is for, for the admins who read this list.</p></div>`
        + `<div class="skmcp-field skmcp-field--wide"><span class="skmcp-label">Agents</span>${mcpAgentsControlMarkup(m.existing || null, { where: "modal", pending: m.agentsPending })}</div>`
        + `<div class="skmcp-field skmcp-field--wide"><span class="skmcp-label" id="mcp-how-label">How to reach it</span>`
        + `<div class="af-tabs"><div class="af-tabs__list" role="tablist" aria-labelledby="mcp-how-label">`
        + `<button type="button" class="af-tabs__tab" role="tab" id="mcp-how-stdio" data-mcp-how="stdio" aria-controls="mcp-pane-stdio" aria-selected="${transport === "stdio"}"${transport === "stdio" ? "" : ' tabindex="-1"'}>Command</button>`
        + `<button type="button" class="af-tabs__tab" role="tab" id="mcp-how-http" data-mcp-how="http" aria-controls="mcp-pane-http" aria-selected="${transport === "http"}"${transport === "http" ? "" : ' tabindex="-1"'}>URL</button></div>`
        + `<div id="mcp-pane-stdio" class="af-tabs__panel" role="tabpanel" aria-labelledby="mcp-how-stdio"${transport === "stdio" ? "" : " hidden"}><div class="skmcp-form">`
        + `<p class="skmcp-help skmcp-field--wide">The gateway starts the server on this computer and talks to it over stdin/stdout.</p>`
        + `<div class="skmcp-field"><label for="mcp-f-command">Command</label><input id="mcp-f-command" type="text" autocomplete="off" spellcheck="false" placeholder="npx" value="${esc(m.command || "")}"><p class="skmcp-help">The program that starts the server, for example npx or uvx.</p></div>`
        + `<div class="skmcp-field"><label for="mcp-f-cwd">Working folder</label><input id="mcp-f-cwd" type="text" autocomplete="off" spellcheck="false" placeholder="A scratch folder" value="${esc(m.cwd || "")}"><p class="skmcp-help">Where the command runs. Empty: a scratch folder removed after each test.</p></div>`
        + `<div class="skmcp-field skmcp-field--wide"><label for="mcp-f-args">Arguments</label><textarea id="mcp-f-args" rows="3" spellcheck="false" placeholder="-y&#10;@modelcontextprotocol/server-everything">${esc(args)}</textarea><p class="skmcp-help">One argument per line, passed to the command as they are.</p></div>`
        + `</div></div>`
        + `<div id="mcp-pane-http" class="af-tabs__panel" role="tabpanel" aria-labelledby="mcp-how-http"${transport === "http" ? "" : " hidden"}><div class="skmcp-form">`
        + `<p class="skmcp-help skmcp-field--wide">The server already runs somewhere and answers MCP over HTTP.</p>`
        + `<div class="skmcp-field skmcp-field--wide"><label for="mcp-f-url">URL</label><input id="mcp-f-url" type="url" autocomplete="off" spellcheck="false" placeholder="https://example.com/mcp" value="${esc(m.url || "")}"><p class="skmcp-help">The server's MCP endpoint.</p></div>`
        + `<div class="skmcp-field skmcp-field--wide"><span class="skmcp-label">Headers</span><div class="skmcp-headers">${headerRows}</div>`
        + `<div><button type="button" class="secondary" data-mcp-header-add="1">Add header</button></div>`
        + `<p class="skmcp-help">Sent with every request, for example Authorization: Bearer &lt;token&gt;. Values are stored encrypted and never shown again.</p></div>`
        + `</div></div></div>`
        + `<div class="skmcp-field skmcp-field--wide" id="mcp-test-result">${mcpTestResultMarkup(m.result)}</div>`
        + `</div>`;
    }
    function mcpModalCollect() {
      const m = skmcp.mcpModal;
      if (!m) return;
      const v = (id) => ($(id) ? $(id).value : undefined);
      if (!m.existing && $("mcp-f-name")) m.name = v("mcp-f-name");
      if ($("mcp-f-description")) m.description = v("mcp-f-description");
      if ($("mcp-f-command")) m.command = v("mcp-f-command");
      if ($("mcp-f-cwd")) m.cwd = v("mcp-f-cwd");
      if ($("mcp-f-args")) m.args = String(v("mcp-f-args") || "").split("\n").map((a) => a.trim()).filter(Boolean);
      if ($("mcp-f-url")) m.url = v("mcp-f-url");
      m.headers = (m.headers || []).map((h, i) => {
        const nameEl = document.querySelector(`[data-mcp-header-name="${i}"]`);
        const valEl = document.querySelector(`[data-mcp-header-value="${i}"]`);
        return { ...h, name: nameEl ? nameEl.value : h.name, value: valEl ? valEl.value : h.value };
      });
    }
    function mcpModalBody() {
      const m = skmcp.mcpModal;
      const body = { transport: m.transport, description: String(m.description || "").trim() };
      if (!m.existing) body.name = String(m.name || "").trim(); else body.name = m.existing.name;
      if (m.transport === "stdio") {
        body.command = String(m.command || "").trim();
        body.args = m.args || [];
        body.cwd = String(m.cwd || "").trim();
      } else {
        body.url = String(m.url || "").trim();
        body.headers = {};
        for (const h of m.headers || []) {
          const k = String(h.name || "").trim();
          if (!k) continue;
          // A saved header the admin did not retype keeps its stored value (null).
          body.headers[k] = h.value ? String(h.value) : (h.saved ? null : "");
        }
      }
      return body;
    }
    function renderMcpModal() {
      const m = skmcp.mcpModal;
      if (!m) return;
      // The agents switch shows the server's current row (the list is re-read after each change).
      if (m.existing) m.existing = { ...m.existing, ...(mcpServerRow(m.existing.name) || {}) };
      m.agentsPending = skmcp.agentsPending;
      $("mcp-modal-title").textContent = m.existing ? `MCP server — ${m.existing.name}` : "Add MCP server";
      $("mcp-modal-body").innerHTML = mcpModalMarkup(m);
    }
    function closeMcpModal() {
      const backdrop = $("mcp-modal-backdrop");
      if (backdrop.hidden) return;
      backdrop.hidden = true;
      $("mcp-modal-body").textContent = "";
      const release = skmcp.release;
      skmcp.release = null;
      if (release) release();
      skmcp.mcpModal = null;
    }
    function openMcpModal(existing) {
      closeMcpModal();
      const s = existing || null;
      skmcp.mcpModal = {
        existing: s,
        transport: s ? s.transport : "stdio",
        name: s ? s.name : "",
        description: s ? s.description || "" : "",
        command: s ? s.command || "" : "",
        args: s ? (s.args || []).slice() : [],
        cwd: s ? s.cwd || "" : "",
        url: s ? s.url || "" : "",
        headers: s ? Object.keys(s.headers || {}).map((k) => ({ name: k, value: "", saved: true })) : [],
        result: null,
      };
      $("mcp-modal-note").textContent = "";
      renderMcpModal();
      const backdrop = $("mcp-modal-backdrop");
      backdrop.hidden = false;
      skmcp.release = bindAccountModal(backdrop, closeMcpModal);
    }
    async function testMcpModal() {
      mcpModalCollect();
      const m = skmcp.mcpModal;
      m.result = { pending: true };
      $("mcp-test-result").innerHTML = mcpTestResultMarkup(m.result);
      try {
        m.result = await api("/api/gateway/admin/mcp/test", { method: "POST", body: JSON.stringify(mcpModalBody()), timeoutMs: 30000 });
      } catch (e) {
        m.result = { ok: false, message: skmcpErr(e) };
      }
      if (skmcp.mcpModal === m) $("mcp-test-result").innerHTML = mcpTestResultMarkup(m.result);
    }
    async function saveMcpModal() {
      mcpModalCollect();
      const m = skmcp.mcpModal;
      $("mcp-modal-note").textContent = "Saving...";
      try {
        const body = mcpModalBody();
        const path = m.existing ? `/api/gateway/admin/mcp/servers/${encodeURIComponent(m.existing.name)}` : "/api/gateway/admin/mcp/servers";
        await api(path, { method: m.existing ? "PUT" : "POST", body: JSON.stringify(body) });
        closeMcpModal();
        skmcpMessage("mcp-message", `Saved ${body.name}. Test it to record whether the gateway can reach it.`, "ok");
        await loadMcpList();
      } catch (e) {
        $("mcp-modal-note").textContent = `Not saved: ${skmcpErr(e)}`;
      }
    }
    function mcpServerRow(name) {
      return (((skmcp.mcp && skmcp.mcp.servers) || []).find((x) => x.name === name)) || null;
    }
    async function setMcpAgents(name, enabled) {
      skmcp.agentsPending = "";
      try {
        const row = await api(`/api/gateway/admin/mcp/servers/${encodeURIComponent(name)}/agents`, { method: "POST", body: JSON.stringify({ enabled }) });
        skmcpMessage("mcp-message", `${name}: ${row.agents_status || (enabled ? "Offered to agents" : "Not offered to agents")}.`, "ok");
        if (skmcp.mcpModal && skmcp.mcpModal.existing && skmcp.mcpModal.existing.name === name) skmcp.mcpModal.existing = { ...skmcp.mcpModal.existing, ...row };
      } catch (e) {
        skmcpMessage("mcp-message", `${name}: ${skmcpErr(e)}`, "error");
        if (skmcp.mcpModal) $("mcp-modal-note").textContent = `${name}: ${skmcpErr(e)}`;
      }
      await loadMcpList();
      if (skmcp.mcpModal) { mcpModalCollect(); renderMcpModal(); }
    }
    // One click handler for the switch and its inline confirmation (table rows and the modal).
    function mcpAgentsClick(t) {
      if (t.dataset.mcpAgentsConfirm) { setMcpAgents(t.dataset.mcpAgentsConfirm, true); return true; }
      if (t.dataset.mcpAgentsCancel !== undefined && t.dataset.mcpAgentsCancel !== "" ) { skmcp.agentsPending = ""; renderMcpList(); if (skmcp.mcpModal) { mcpModalCollect(); renderMcpModal(); } return true; }
      if (t.dataset.mcpAgents === undefined) return false;
      const name = t.dataset.mcpAgents;
      if (t.getAttribute("aria-disabled") === "true") {
        skmcpMessage("mcp-message", `${name || "This server"}: ${t.title}`, "error");
        return true;
      }
      const row = mcpServerRow(name);
      if (row && row.enabled_for_agents) { setMcpAgents(name, false); return true; }
      skmcp.agentsPending = name;
      renderMcpList();
      if (skmcp.mcpModal) { mcpModalCollect(); renderMcpModal(); }
      return true;
    }
    async function mcpRowAction(kind, name) {
      if (kind === "edit") {
        const s = ((skmcp.mcp && skmcp.mcp.servers) || []).find((x) => x.name === name);
        if (s) openMcpModal(s);
        return;
      }
      if (kind === "test") skmcpMessage("mcp-message", `Testing ${name} (up to 10 seconds)...`, "");
      try {
        const out = await api(`/api/gateway/admin/mcp/servers/${encodeURIComponent(name)}/${kind}`, { method: "POST", body: "{}", timeoutMs: 30000 });
        if (kind === "test") skmcpMessage("mcp-message", `${name}: ${out.message}`, out.ok ? "ok" : "error");
        else skmcpMessage("mcp-message", kind === "archive" ? `Archived ${name}. Turn on Show archived to find it again.` : `Unarchived ${name}.`, "ok");
        await loadMcpList();
      } catch (e) {
        skmcpMessage("mcp-message", `${name}: ${skmcpErr(e)}`, "error");
      }
    }
    function skmcpToggleMore(e) {
      const t = e.target && e.target.closest ? e.target.closest("[data-skmcp-more]") : null;
      if (!t) return false;
      if (e.type === "keydown" && e.key !== "Enter" && e.key !== " ") return false;
      if (e.type === "keydown") e.preventDefault();
      t.setAttribute("aria-expanded", t.getAttribute("aria-expanded") === "true" ? "false" : "true");
      return true;
    }
    function bindSkillsMcpPage() {
      for (const id of ["skills-table", "mcp-table"]) $(id).onkeydown = skmcpToggleMore;
      $("tab-button-skills").onclick = () => { setActiveTab("skills"); };
      const tabs = [$("skmcp-tab-skills"), $("skmcp-tab-mcp")];
      for (const t of tabs) {
        t.onclick = () => skmcpSetTab(t.dataset.skmcpTab);
        t.onkeydown = (e) => {
          if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
          e.preventDefault();
          const next = t.dataset.skmcpTab === "skills" ? "mcp" : "skills";
          skmcpSetTab(next);
          $(`skmcp-tab-${next}`).focus();
        };
      }
      $("skills-search").oninput = (e) => { skmcp.search = e.target.value; renderSkillsList(); };
      $("skills-import-zip").onclick = () => $("skills-import-zip-file").click();
      $("skills-import-folder").onclick = () => $("skills-import-folder-file").click();
      $("skills-import-zip-file").onchange = async (ev) => {
        const f = ev.target.files && ev.target.files[0];
        if (f) { const form = new FormData(); form.append("file", f, f.name); await importSkillUpload(form, f.name); }
        ev.target.value = "";
      };
      $("skills-import-folder-file").onchange = async (ev) => {
        const files = Array.from(ev.target.files || []);
        if (files.length) {
          const form = new FormData();
          for (const f of files) { form.append("files", f, f.name); form.append("paths", f.webkitRelativePath || f.name); }
          await importSkillUpload(form, (files[0].webkitRelativePath || files[0].name).split("/")[0]);
        }
        ev.target.value = "";
      };
      $("skills-table").onclick = (e) => {
        if (skmcpToggleMore(e)) return;
        const t = e.target && e.target.closest ? e.target.closest("button") : null;
        if (!t) return;
        if (t.dataset.skillView) openSkillModal(t.dataset.skillView);
        else if (t.dataset.skillExport) exportSkill(t.dataset.skillExport);
        else if (t.dataset.skillArchive) skillAction("archive", t.dataset.skillArchive);
        else if (t.dataset.skillUnarchive) skillAction("unarchive", t.dataset.skillUnarchive);
      };
      $("skill-modal-close").onclick = closeSkillModal;
      $("skill-modal-actions").onclick = (e) => {
        const t = e.target && e.target.closest ? e.target.closest("button") : null;
        if (!t) return;
        if (t.dataset.skillSave) saveSkillModal();
        else if (t.dataset.skillDuplicate) duplicateSkillFromModal(t.dataset.skillDuplicate);
        else if (t.dataset.skillModalUnarchive) { const n = t.dataset.skillModalUnarchive; skillAction("unarchive", n).then(() => openSkillModal(n)); }
      };
      $("mcp-add").onclick = () => openMcpModal(null);
      $("mcp-table").onclick = (e) => {
        if (skmcpToggleMore(e)) return;
        const t = e.target && e.target.closest ? e.target.closest("button") : null;
        if (!t) return;
        if (mcpAgentsClick(t)) return;
        if (t.dataset.mcpEdit) mcpRowAction("edit", t.dataset.mcpEdit);
        else if (t.dataset.mcpTest) mcpRowAction("test", t.dataset.mcpTest);
        else if (t.dataset.mcpArchive) mcpRowAction("archive", t.dataset.mcpArchive);
        else if (t.dataset.mcpUnarchive) mcpRowAction("unarchive", t.dataset.mcpUnarchive);
      };
      $("mcp-modal-close").onclick = closeMcpModal;
      $("mcp-modal-test").onclick = testMcpModal;
      $("mcp-modal-save").onclick = saveMcpModal;
      $("mcp-modal-body").onclick = (e) => {
        const t = e.target && e.target.closest ? e.target.closest("button") : null;
        if (!t || !skmcp.mcpModal) return;
        if (mcpAgentsClick(t)) return;
        if (t.dataset.mcpHow) { mcpModalCollect(); skmcp.mcpModal.transport = t.dataset.mcpHow; renderMcpModal(); const b = $(`mcp-how-${t.dataset.mcpHow}`); if (b) b.focus(); }
        else if (t.dataset.mcpHeaderAdd) { mcpModalCollect(); skmcp.mcpModal.headers.push({ name: "", value: "", saved: false }); renderMcpModal(); }
        else if (t.dataset.mcpHeaderRemove) { mcpModalCollect(); skmcp.mcpModal.headers.splice(Number(t.dataset.mcpHeaderRemove), 1); renderMcpModal(); }
      };
    }
"""
