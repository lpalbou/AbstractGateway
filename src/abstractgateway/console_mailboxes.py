"""My email (Accounts → your row → Email): the Mailboxes tab and the ONE mailbox form.

Operator rework (2026-10-25): the dialog has a compact header ("My email" + the address) and three
tabs, **Mailboxes** | **Notifications** | **Agents** (console.py moves the existing cards into them).

- **Mailboxes**: one list — the primary mailbox first (badge "Sends as you"), then the others; each
  row is ONE line "address · provider · state · Read only|Read and write", an **Active** switch, on
  another mailbox a **Read and write** switch (`PUT /me/email/accounts/{id}/access {write}`; the
  primary is always read and write) and a "…" menu (Test, Settings, Remove). **Add mailbox** opens the mailbox form as a dialog, never inline. The primary's **Settings**
  opens the full editor (stored servers, folder, Google/Microsoft client, Test, Disconnect) in its own
  dialog, `#mailbox-settings-backdrop` (console.py holds its markup).
- **The mailbox form** (one component for the primary "Connect" and for another mailbox "Add
  mailbox"): step 1 is one field, **Email address**; `GET /me/email/autodiscover?address=` answers
  `{provider, display_name, imap, smtp|null, login, sign_in[], oauth_available, app_password_url?,
  source}` and one line says what was found. Step 2: **Sign in with Google/Microsoft** when the
  gateway has that client, else a **Password** with the provider's app-password link. **Server
  settings** (IMAP server/port/security, the outgoing server when the mailbox may write, **Sign-in
  name** prefilled with the address) stay collapsed unless nothing was found. **Read and write**
  (another mailbox only, off by default: automations only read it; on: they may mark, move, label,
  delete and send as it) shows the outgoing server (SMTP) settings. No name is asked: the id is
  derived from the address.

Routes: primary = the email UI's base (`myEmailApi()`: `/me/email`, or an entity's own `/accounts/<id>/email`):
PUT base, POST base/test, PUT base/enabled, DELETE base, base/oauth/start|finish|cancel. Others (own account
only): GET /me/email/accounts, PUT|DELETE /me/email/accounts/{id} (`write`), POST …/test, PUT …/enabled,
PUT …/access, …/oauth/start|finish|cancel (`write`). Spliced into the console script scope: `api`, `esc`, `$`, `state`, `afSwitchCreate`,
`afSwitchBind`, `emailErrorText`, `myEmailUi`, `myEmailApi`, `loadMyEmail`, `bindAccountModal`, `trgAgo`,
`trgLoadingMarkup` are the console's own.
"""

from __future__ import annotations

MAILBOXES_CSS = r"""
/* ---- My email: tabs, the mailbox list and the mailbox form (console_mailboxes.py) ---- */
.my-email-head { display: flex; align-items: center; gap: 8px 12px; flex-wrap: wrap; margin-bottom: 4px; }
.my-email-head h2 { margin: 0; }
.my-email-who { margin: 0; color: var(--muted); font-size: var(--font-size-md); flex: 1 1 auto; min-width: 0; overflow-wrap: anywhere; }
.my-email-tabs .af-tabs__panel { padding-top: 14px; display: flex; flex-direction: column; gap: 14px; }
.my-email-tabs .af-tabs__panel[hidden], #mailbox-sheet-go[hidden] { display: none; }
/* Agents tab: Always allowed and Always denied side by side when there is room (the tab fits at 1440×900) */
@media (min-width: 1024px) {
  .my-email-tabs .recipient-rules { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px 24px; align-items: start; }
  .my-email-tabs .recipient-rules > .advanced-sentence, .my-email-tabs .recipient-rules > .af-form__help { grid-column: 1 / -1; }
}
.mbx-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
.mbx-row { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 12px; padding: 10px 2px; border-top: 1px solid var(--line-soft); }
.mbx-row:first-child { border-top: 0; }
.mbx-row__line { flex: 1 1 260px; min-width: 0; overflow-wrap: anywhere; }
.mbx-row__line strong { font-weight: 600; }
.mbx-row__line .mbx-sub { color: var(--muted); }
.mbx-badge { display: inline-flex; align-items: center; padding: 0 8px; margin-left: 6px; border-radius: 999px; border: 1px solid color-mix(in srgb, var(--info, var(--accent)) 50%, transparent); color: var(--info, var(--accent)); font-size: var(--font-size-sm); white-space: nowrap; }
.mbx-row__actions { display: flex; align-items: center; gap: 8px; flex: none; }
.mbx-row__note { flex-basis: 100%; margin: 0; font-size: var(--font-size-md); }
.mbx-row__note.is-ok { color: var(--success, #2f9e44); }
.mbx-row__note.is-failed { color: var(--error, #d6336c); }
.mbx-row .inline-confirm { flex-basis: 100%; }
.mbx-menu { position: relative; }
.mbx-menu > summary { list-style: none; cursor: pointer; min-width: 44px; min-height: 36px; display: inline-flex; align-items: center; justify-content: center; border: 1px solid var(--ui-border-1, var(--line)); border-radius: var(--radius-md); }
.mbx-menu > summary::-webkit-details-marker { display: none; }
.mbx-menu__list { position: absolute; right: 0; z-index: 5; display: flex; flex-direction: column; min-width: 10rem; margin-top: 4px; padding: 4px; border: 1px solid var(--ui-border-1, var(--line)); border-radius: var(--radius-md); background: var(--bg-primary, var(--bg)); box-shadow: 0 6px 18px rgba(0, 0, 0, .12); }
.mbx-menu__list > button { justify-content: flex-start; text-align: left; border: 0; background: transparent; color: var(--text); min-height: 36px; }
.mbx-menu__list > button:hover { background: var(--ui-surface-2, var(--bg-secondary)); }
.mbx-empty { color: var(--muted); margin: 0; }
.mbx-add-row { display: flex; justify-content: flex-start; }
/* the mailbox form (a dialog): one column, generous spacing */
.mbs { display: flex; flex-direction: column; gap: 16px; max-width: 34rem; }
.mbs .af-form__field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.mbs input, .mbs select { width: 100%; box-sizing: border-box; }
.mbs-found { margin: 0; font-size: var(--font-size-md); color: var(--muted); min-height: 1.2em; }
.mbs-found.is-ok { color: var(--text); }
.mbs-signin { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; }
.mbs-error { margin: 0; color: var(--error, #d6336c); font-size: var(--font-size-md); }
.mbs input[aria-invalid="true"] { border-color: var(--error, #d6336c); }
.mbs-servers > summary { cursor: pointer; color: var(--muted); min-height: 28px; }
.mbs-servers[open] > summary { margin-bottom: 10px; }
.mbs-servers__grid { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr) minmax(0, 1fr); gap: 10px 12px; }
.mbs-servers__wide { grid-column: 1 / -1; }
#mailbox-settings-body .account-card { border: 0; padding: 0; background: transparent; box-shadow: none; }
#mailbox-settings-body .account-card > .af-card__header { display: none; }
/* links in the form: the console's link look (as .link-button), never the browser's default blue */
.mbs a, #my-mailboxes-card a { color: var(--text); text-decoration: underline; text-underline-offset: 3px; text-decoration-color: color-mix(in srgb, currentColor 45%, transparent); }
.mbs a:hover, #my-mailboxes-card a:hover { text-decoration-color: currentColor; }
/* the mailbox form is one column: a narrow dialog on wide screens (the kit's phone sheet below 768 px) */
@media (min-width: 768px) { #mailbox-sheet-backdrop .af-modal { width: min(480px, calc(100vw - 32px)); } }
.mbs-actions { display: flex; gap: 8px; margin-left: auto; flex-wrap: wrap; }
@media (max-width: 767.98px) { .mbs-servers__grid { grid-template-columns: minmax(0, 1fr); } .mbx-row__actions { flex-basis: 100%; } }
"""

MAILBOXES_JS = r"""
    // ---- My email: the Mailboxes tab and the one mailbox form (console_mailboxes.py) ----
    const mbx = { items: null, error: "", confirm: "", note: {}, sheet: null, release: null, settingsRelease: null, rendering: false, oauth: null, timer: null, openAddOnLoad: false, tab: "mailboxes" };
    // Providers that refuse account passwords over IMAP (operator ruling: a direct link, never a menu path).
    const MBX_PROVIDERS = {
      google: { name: "Gmail", url: "https://myaccount.google.com/apppasswords", signIn: "Sign in with Google" },
      microsoft: { name: "Outlook", url: "https://account.live.com/proofs/manage/additional", signIn: "Sign in with Microsoft" },
    };
    const MBX_STATE = {
      connected: "connected", receive_only: "connected, can't send", paused: "paused",
      needs_reconnect: "needs reconnecting", unavailable: "turned off by your admin", not_connected: "not connected",
    };
    function mbxOwnPage() { return !myEmailUi.base; }
    function mbxFurther(items) { return (items || []).filter((m) => m && !m.primary && m.configured); }
    // The provider's help as HTML: "<Name> refuses account passwords. <a>Create an app password</a> and paste it here[, or Sign in with …]."
    function mailboxProviderHelpHtml(provider, signInAvailable, url) {
      const p = MBX_PROVIDERS[provider];
      if (!p) return "";
      return `${esc(p.name)} refuses account passwords. <a href="${esc(url || p.url)}" target="_blank" rel="noopener noreferrer">Create an app password</a> and paste it here${signInAvailable ? `, or ${esc(p.signIn)}` : ""}.`;
    }
    // The one line under Email address, from the autodiscover answer.
    function mailboxFoundLine(d) {
      if (!d) return "";
      if (d.source === "none" || !(d.imap && d.imap.host)) return "We could not find this provider's settings; enter them below.";
      const name = d.display_name || d.provider || "";
      const p = MBX_PROVIDERS[d.provider];
      const ways = Array.isArray(d.sign_in) ? d.sign_in : ["password"];
      if (p && d.oauth_available && ways.includes("oauth")) return ways.includes("app_password") ? `${name} · ${p.signIn}, or use an app password` : `${name} · ${p.signIn}`;
      if (ways.includes("app_password") && !ways.includes("password")) return `${name} · use an app password`;
      if (p && !ways.includes("password")) return `${name} · needs ${p.signIn}`;
      return `${name} · password sign-in`;
    }
    // The id another mailbox is stored under, derived from its address (no name is asked).
    function mailboxIdFromAddress(address, taken) {
      const base = String(address || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 28).replace(/-+$/g, "") || "mailbox";
      const used = new Set((taken || []).map(String));
      used.add("default");
      if (!used.has(base)) return base;
      for (let i = 2; i < 100; i += 1) if (!used.has(`${base}-${i}`)) return `${base}-${i}`;
      return `${base}-${Date.now() % 1000}`;
    }
    // ---- the Mailboxes list: the primary first ("Sends as you"), then the others; one line each ----
    // The provider's name for a row; a password mailbox the gateway reports as plain "imap" names none.
    const MBX_PROVIDER_NAMES = { google: "Gmail", microsoft: "Outlook", yahoo: "Yahoo Mail", icloud: "iCloud Mail", fastmail: "Fastmail" };
    function mbxProviderWord(provider) {
      return MBX_PROVIDER_NAMES[provider] || "";
    }
    // What automations may do with a mailbox, in words (the gateway's `access`; the primary is always read and write).
    const MBX_ACCESS_WORDS = { read: "Read only", write: "Read and write" };
    const MBX_WRITE_HELP = "Off: automations read this mailbox and never change it or send from it. On: they can mark, move, label, delete and send as it.";
    function mbxAccessWords(access) {
      return MBX_ACCESS_WORDS[access] || "";
    }
    function mbxRowMarkup(row, opts) {
      const o = opts || {};
      const key = esc(row.key);
      const parts = [`<strong>${esc(row.name || row.address || row.key)}</strong>`];
      const provider = mbxProviderWord(row.provider);
      if (provider) parts.push(`<span class="mbx-sub">${esc(provider)}</span>`);
      parts.push(`<span class="mbx-sub">${esc(row.enabled === false ? "not in use" : (MBX_STATE[row.state] || row.state || "unknown"))}</span>`);
      if (mbxAccessWords(row.access)) parts.push(`<span class="mbx-sub" data-mbx-access-words="${key}">${esc(mbxAccessWords(row.access))}</span>`);
      const note = o.note && o.note[row.key];
      let html = `<li class="mbx-row" data-mbx-row="${key}"><span class="mbx-row__line">${parts.join(" · ")}${row.primary ? `<span class="mbx-badge">Sends as you</span>` : ""}</span>`
        + `<span class="mbx-row__actions"><span data-mbx-switch="${key}"></span>${row.primary ? "" : `<span data-mbx-access="${key}"></span>`}`
        + `<details class="mbx-menu"><summary aria-label="More for ${esc(row.address || row.key)}">…</summary><div class="mbx-menu__list">`
        + `<button type="button" data-mbx-test="${key}">Test</button><button type="button" data-mbx-settings="${key}">Settings</button><button type="button" data-mbx-delete="${key}">Remove</button>`
        + `</div></details></span>`;
      if (note) html += `<p class="mbx-row__note ${note.ok ? "is-ok" : "is-failed"}" role="status">${esc(note.text)}</p>`;
      if (o.confirm === row.key) {
        html += `<div class="inline-confirm" role="group"><span>${esc(mailboxRemoveQuestion(row))}</span>`
          + `<button type="button" class="danger" data-mbx-delete-now="${key}">Remove</button>`
          + `<button type="button" class="secondary" data-mbx-delete-cancel="${key}">Cancel</button></div>`;
      }
      return html + `</li>`;
    }
    function mailboxRemoveQuestion(row) {
      return row.primary
        ? `Remove ${row.address || "this mailbox"}? Your agents lose email until you connect one again. Policy and limits are kept.`
        : `Remove ${row.address || row.key}? Its stored password and read position are removed; automations that watch it stop.`;
    }
    // The rows: the primary from the email UI's answer (GET base), the others from /me/email/accounts.
    function mbxRows(primary, items) {
      const rows = [];
      const d = primary || {};
      if (d.configured) {
        rows.push({ key: "__primary", primary: true, address: d.address, provider: (d.oauth && d.oauth.provider) || (d.mailbox && d.mailbox.provider) || "", state: (d.mailbox && d.mailbox.state) || "connected", enabled: d.enabled !== false, access: "write" });
      }
      for (const m of mbxFurther(items)) {
        rows.push({ key: m.account_id, primary: false, name: m.name && m.name !== m.address ? `${m.name} (${m.address})` : "", address: m.address, provider: (m.mailbox && m.mailbox.provider) || "", state: (m.mailbox && m.mailbox.state) || "", enabled: !!m.enabled, access: m.access || (m.can_write ? "write" : "read") });
      }
      return rows;
    }
    function mailboxesMarkup(rows, opts) {
      if (!rows.length) return `<p class="mbx-empty">No mailbox yet. Add one so your agents and automations can read your mail.</p>`;
      return `<ul class="mbx-list">${rows.map((r) => mbxRowMarkup(r, opts)).join("")}</ul>`;
    }
    async function loadMyMailboxes() {
      const card = $("my-mailboxes-card");
      if (!card) throw new Error("The account email UI has no #my-mailboxes-card (console_mailboxes.py).");
      mbxRenderHeader();
      if (!(state.myEmail && state.myEmail.configured)) closePrimaryMailboxSettings();  // disconnected: nothing to configure
      if (!mbx.items || mbx.error) { mbx.items = null; mbx.error = ""; renderMyMailboxes(); }  // the loading state, never an empty list
      if (!mbxOwnPage()) { mbx.items = []; renderMyMailboxes(); return; }  // an entity: its own mailbox only (the others are /me routes)
      try {
        const out = await api("/api/gateway/me/email/accounts");
        if (!out || !Array.isArray(out.items)) throw new Error("the gateway's answer has no items list.");
        mbx.items = out.items;
        mbx.error = "";
      } catch (e) {
        mbx.error = emailErrorText(e);
      }
      renderMyMailboxes();
      if (mbx.openAddOnLoad) { mbx.openAddOnLoad = false; openMailboxSheet({ target: state.myEmail && state.myEmail.configured ? "other" : "primary" }); }
    }
    function mbxRenderHeader() {
      const d = state.myEmail || {};
      const who = $("my-email-who");
      if (who) who.textContent = d.email_address || d.address || "";
      const title = $("my-email-title");
      if (title) title.textContent = mbxOwnPage() ? "My email" : "Email";
    }
    function renderMyMailboxes() {
      const list = $("my-mailboxes-list");
      if (mbx.error) { list.innerHTML = `<p class="af-form__error">Could not list your mailboxes: ${esc(mbx.error)}</p>`; return; }
      if (!mbx.items || !state.myEmail) { list.innerHTML = `<p class="mbx-empty">${trgLoadingMarkup("Reading your mailboxes...")}</p>`; return; }
      const rows = mbxRows(state.myEmail, mbx.items);
      list.innerHTML = mailboxesMarkup(rows, { note: mbx.note, confirm: mbx.confirm });
      for (const r of rows) {
        const slot = list.querySelector(`[data-mbx-switch="${CSS.escape(r.key)}"]`);
        if (!slot) continue;
        const sw = afSwitchCreate({ id: `my-mailbox-active-${r.key}`, label: "Active", checked: !!r.enabled, small: true });
        slot.append(...sw.nodes);
        afSwitchBind(sw.button, async (next) => {
          await api(mbxRoute(r.key, "/enabled"), { method: "PUT", body: JSON.stringify({ enabled: next }) });
          await mbxReload(r.key);
          return next;
        }, (e) => { mbx.note[r.key] = { ok: false, text: `Not saved: ${emailErrorText(e)}` }; renderMyMailboxes(); });
        // Another mailbox: Read and write (on) or Read only (off). Turning it on adds and tests its outgoing server.
        const accessSlot = list.querySelector(`[data-mbx-access="${CSS.escape(r.key)}"]`);
        if (!accessSlot) continue;
        const asw = afSwitchCreate({ id: `my-mailbox-write-${r.key}`, label: "Read and write", checked: r.access === "write", small: true });
        accessSlot.append(...asw.nodes);
        afSwitchBind(asw.button, async (next) => {
          if (next) { mbx.note[r.key] = { ok: true, text: "Checking its outgoing server..." }; renderMyMailboxes(); }
          await api(mbxRoute(r.key, "/access"), { method: "PUT", body: JSON.stringify({ write: next }), slow: true });
          delete mbx.note[r.key];
          await mbxReload(r.key);
          return next;
        }, (e) => { mbx.note[r.key] = { ok: false, text: `Not saved: ${emailErrorText(e)}` }; renderMyMailboxes(); });
      }
      $("my-mailbox-add").hidden = state.myEmail && state.myEmail.email_available === false;
    }
    // A row's route: the primary through the email UI's base (own or an entity's), the others by id.
    function mbxRoute(key, sub) {
      return key === "__primary" ? myEmailApi(sub || "") : `/api/gateway/me/email/accounts/${encodeURIComponent(key)}${sub || ""}`;
    }
    async function mbxReload(key) {
      if (key === "__primary") await loadMyEmail();
      else await loadMyMailboxes();
    }
    async function testMyMailbox(key) {
      mbx.note[key] = { ok: true, text: "Signing in..." };
      renderMyMailboxes();
      try {
        const out = await api(mbxRoute(key, "/test"), { method: "POST", body: "{}", slow: true });
        const failed = ["imap", "smtp"].map((k) => out && out[k]).filter((l) => l && l.ok === false);
        const ok = !failed.length && !(out && out.ok === false);
        mbx.note[key] = { ok, text: ok ? "It answered: the mailbox works." : `Test failed: ${failed.length ? `${failed[0].cause}${failed[0].fix ? ` ${failed[0].fix}` : ""}` : ((out && (out.message || out.error)) || "the gateway did not say why.")}` };
      } catch (e) {
        mbx.note[key] = { ok: false, text: `Test failed: ${emailErrorText(e)}` };
      }
      renderMyMailboxes();
    }
    async function removeMyMailbox(key) {
      try {
        await api(mbxRoute(key), { method: "DELETE" });
        mbx.confirm = "";
        delete mbx.note[key];
        await mbxReload(key);
        await loadMyMailboxes();
      } catch (e) {
        mbx.note[key] = { ok: false, text: `Not removed: ${emailErrorText(e)}` };
        renderMyMailboxes();
      }
    }
    // ---- the mailbox form (a dialog): Email address → Sign in / Password → Server settings ----
    // st: {target: "primary"|"other", id, address, found, finding, usePassword, password, login, imap, smtp,
    //      serversOpen, write, errors, problem, saving}
    function mailboxSheetMarkup(st) {
      const e = st.errors || {};
      const err = (k) => e[k] ? `<p class="mbs-error" role="alert">${esc(e[k])}</p>` : "";
      const inv = (k) => e[k] ? ' aria-invalid="true"' : "";
      const d = st.found;
      let html = `<div class="mbs">`
        + `<div class="af-form__field"><label class="af-form__label" for="mbs-address">Email address</label>`
        + `<input id="mbs-address" type="text" inputmode="email" autocomplete="email" spellcheck="false" autofocus data-mbs="address" value="${esc(st.address || "")}"${st.lockAddress ? " readonly" : ""}${inv("address")}>`
        + `<p class="mbs-found${d && d.source !== "none" ? " is-ok" : ""}" data-mbs-found role="status">${esc(st.finding ? "Looking up your provider..." : mailboxFoundLine(d))}</p>${err("address")}</div>`;
      if (d) {
        const p = MBX_PROVIDERS[d.provider];
        const passwordOk = !Array.isArray(d.sign_in) || d.sign_in.includes("password") || d.sign_in.includes("app_password");
        if (p && !d.oauth_available && !passwordOk) {
          // The provider only accepts its own sign-in, and this gateway has no client for it: said plainly.
          const who = d.provider === "microsoft" ? "Microsoft" : "Google";
          html += `<p class="mbs-error" role="status" data-mbs-oauth-missing>${esc(who)} mailboxes need ${esc(p.signIn)}; ask your gateway admin to register a ${esc(who)} sign-in client.</p>`;
        } else if (p && d.oauth_available && !st.usePassword) {
          html += `<div class="mbs-signin"><button type="button" data-mbs-oauth="${esc(d.provider)}">${esc(p.signIn)}</button>`
            + (passwordOk ? `<button type="button" class="link-button" data-mbs-use-password="1">Use an app password instead</button>` : "") + `</div>`;
        } else {
          const ways = Array.isArray(d.sign_in) ? d.sign_in : [];
          const appOnly = ways.includes("app_password") && !ways.includes("password");
          const help = p ? mailboxProviderHelpHtml(d.provider, !!d.oauth_available, d.app_password_url)
            : appOnly ? `${esc(d.display_name || "This provider")} needs an app password${d.app_password_url ? `: <a href="${esc(d.app_password_url)}" target="_blank" rel="noopener noreferrer">Create an app password</a> and paste it here` : " (create one in your account's security settings)"}.`
            : "";
          html += `<div class="af-form__field"><label class="af-form__label" for="mbs-password">Password</label>`
            + `<input id="mbs-password" type="password" autocomplete="new-password" data-mbs="password" value="${esc(st.password || "")}"${inv("password")}>`
            + (help ? `<p class="af-form__help" data-mbs-help>${help}</p>` : `<p class="af-form__help">Use an app password if your provider needs one. Stored encrypted, never shown.</p>`)
            + `${err("password")}</div>`;
        }
        if (st.target === "other") {
          html += `<div data-mbs-write-slot></div>`;
        }
        const im = st.imap || {}, sm = st.smtp || {};
        const showSmtp = st.target === "primary" || st.write;
        html += `<details class="mbs-servers"${st.serversOpen ? " open" : ""}><summary>Server settings</summary><div class="mbs-servers__grid">`
          + `<div class="af-form__field"><label class="af-form__label" for="mbs-imap-host">Incoming server (IMAP)</label><input id="mbs-imap-host" data-mbs="imap.host" autocomplete="off" spellcheck="false" value="${esc(im.host || "")}"${inv("imap.host")}>${err("imap.host")}</div>`
          + `<div class="af-form__field"><label class="af-form__label" for="mbs-imap-port">Port</label><input id="mbs-imap-port" data-mbs="imap.port" inputmode="numeric" value="${esc(im.port || "")}"></div>`
          + `<div class="af-form__field"><label class="af-form__label" for="mbs-imap-security">Security</label><select id="mbs-imap-security" data-mbs="imap.security"><option value="ssl"${im.security !== "starttls" ? " selected" : ""}>SSL</option><option value="starttls"${im.security === "starttls" ? " selected" : ""}>STARTTLS</option></select></div>`
          + (showSmtp
            ? `<div class="af-form__field"><label class="af-form__label" for="mbs-smtp-host">Outgoing server (SMTP)</label><input id="mbs-smtp-host" data-mbs="smtp.host" autocomplete="off" spellcheck="false" value="${esc(sm.host || "")}"${inv("smtp.host")}>${err("smtp.host")}</div>`
              + `<div class="af-form__field"><label class="af-form__label" for="mbs-smtp-port">Port</label><input id="mbs-smtp-port" data-mbs="smtp.port" inputmode="numeric" value="${esc(sm.port || "")}"></div>`
              + `<div class="af-form__field"><label class="af-form__label" for="mbs-smtp-security">Security</label><select id="mbs-smtp-security" data-mbs="smtp.security"><option value="ssl"${sm.security !== "starttls" ? " selected" : ""}>SSL</option><option value="starttls"${sm.security === "starttls" ? " selected" : ""}>STARTTLS</option></select></div>`
            : "")
          + `<div class="af-form__field mbs-servers__wide"><label class="af-form__label" for="mbs-login">Sign-in name</label><input id="mbs-login" data-mbs="login" autocomplete="off" spellcheck="false" value="${esc(st.login || st.address || "")}"></div>`
          + `</div></details>`;
      }
      if (st.problem) html += `<p class="mbs-error" role="alert">${esc(st.problem)}</p>`;
      return html + `</div>`;
    }
    // The connect body (password sign-in), or field errors.
    function mailboxSheetBody(st) {
      const errors = {};
      const address = String(st.address || "").trim();
      if (!address || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(address)) errors.address = "Enter the full email address, e.g. name@example.com.";
      if (!String(st.password || "")) errors.password = "Enter the password (or an app password).";
      const im = st.imap || {}, sm = st.smtp || {};
      if (!String(im.host || "").trim()) errors["imap.host"] = "Enter the incoming server (Server settings).";
      const sending = st.target === "primary" || st.write;
      if (st.target === "primary" && String(im.host || "").trim() && !String(sm.host || "").trim()) errors["smtp.host"] = "Enter the outgoing server too.";
      if (Object.keys(errors).length) return { errors };
      const leg = (l) => {
        const out = { host: String(l.host || "").trim(), security: l.security === "starttls" ? "starttls" : "ssl" };
        if (String(l.port || "").trim()) out.port = Number(l.port);
        return out;
      };
      const body = { address, password: String(st.password), test: true, imap: leg(im) };
      const login = String(st.login || "").trim();
      if (login && login.toLowerCase() !== address.toLowerCase()) body.username = login;
      if (sending && String(sm.host || "").trim()) body.smtp = leg(sm);
      if (st.target === "other") { body.write = !!st.write; body.name = address; }
      return { body, errors: {} };
    }
    function openMailboxSheet(opts) {
      const o = opts || {};
      closeMailboxSheet();
      const d = state.myEmail || {};
      const primary = o.target === "primary";
      const address = primary ? (o.settings ? (d.address || "") : (d.email_address || "")) : (o.address || "");
      mbx.sheet = {
        target: primary ? "primary" : "other", id: o.id || "", address, lockAddress: !!(o.settings && !primary), found: null, finding: false,
        usePassword: false, password: "", login: "", imap: {}, smtp: {}, serversOpen: false, write: !primary && !!o.write, errors: {}, problem: "", saving: false,
      };
      $("mailbox-sheet-title").textContent = primary ? (o.settings ? "Mailbox settings" : "Connect your mailbox") : (o.settings ? "Mailbox settings" : "Add mailbox");
      $("mailbox-sheet-go").textContent = primary ? "Connect" : (o.settings ? "Save" : "Add mailbox");
      renderMailboxSheet();
      const backdrop = $("mailbox-sheet-backdrop");
      backdrop.hidden = false;
      mbx.release = bindAccountModal(backdrop, closeMailboxSheet);
      try { $("mbs-address").focus(); } catch {}
      if (address) discoverMailbox(address);
    }
    function closeMailboxSheet() {
      const backdrop = $("mailbox-sheet-backdrop");
      if (!backdrop || backdrop.hidden) return;
      if (mbx.oauth) cancelMailboxOAuth();
      backdrop.hidden = true;
      $("mailbox-sheet-body").textContent = "";
      const release = mbx.release;
      mbx.release = null;
      if (release) release();
      mbx.sheet = null;
    }
    function mbxCollectSheet() {
      const st = mbx.sheet;
      if (!st) return;
      for (const el of Array.from(document.querySelectorAll("#mailbox-sheet-body [data-mbs]"))) {
        const k = el.dataset.mbs;
        const [a, b] = k.split(".");
        if (b) { st[a] = Object.assign({}, st[a] || {}); st[a][b] = el.value; } else st[a] = el.value;
      }
      const open = document.querySelector("#mailbox-sheet-body details.mbs-servers");
      if (open) st.serversOpen = open.open;
    }
    function renderMailboxSheet() {
      const st = mbx.sheet;
      if (!st) return;
      // Re-rendering replaces the fields: keep the focused one (and its caret), and ignore the blur
      // the replacement itself fires.
      const active = document.activeElement;
      const keep = active && active.id && active.closest && active.closest("#mailbox-sheet-body") ? { id: active.id, a: active.selectionStart, b: active.selectionEnd } : null;
      mbx.rendering = true;
      try { $("mailbox-sheet-body").innerHTML = mailboxSheetMarkup(st); } finally { mbx.rendering = false; }
      if (keep && $(keep.id)) {
        const el = $(keep.id);
        try { el.focus(); if (keep.a != null && el.setSelectionRange) el.setSelectionRange(keep.a, keep.b); } catch {}
      }
      const slot = document.querySelector("#mailbox-sheet-body [data-mbs-write-slot]");
      if (slot) {
        const sw = afSwitchCreate({ id: "mbs-write", label: "Read and write", description: MBX_WRITE_HELP, checked: !!st.write, small: true });
        slot.append(...sw.nodes);
        afSwitchBind(sw.button, async (next) => { mbxCollectSheet(); st.write = next; renderMailboxSheet(); return next; }, () => {});
      }
      const d = st.found;
      const passwordOk = d && (!Array.isArray(d.sign_in) || d.sign_in.includes("password") || d.sign_in.includes("app_password"));
      const oauthOnly = d && MBX_PROVIDERS[d.provider] && ((d.oauth_available && !st.usePassword) || !passwordOk);
      $("mailbox-sheet-go").hidden = !d || oauthOnly;
      $("mailbox-sheet-go").disabled = !!st.saving;
    }
    async function discoverMailbox(address) {
      const st = mbx.sheet;
      if (!st) return;
      const a = String(address || "").trim();
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(a)) return;
      if (st.found && st.found.address === a.toLowerCase()) return;
      st.finding = true;
      st.address = a;
      renderMailboxSheet();
      let d;
      try {
        d = await api(`/api/gateway/me/email/autodiscover?address=${encodeURIComponent(a)}`);
      } catch (e) {
        d = { address: a.toLowerCase(), provider: "other", display_name: a.split("@")[1], imap: { host: "", port: 993, security: "ssl" }, smtp: null, login: a, sign_in: ["password"], oauth_available: false, source: "none" };
      }
      if (mbx.sheet !== st || String(st.address).trim().toLowerCase() !== String(d.address || a).toLowerCase()) return;
      st.finding = false;
      if (st.found && st.found.address !== d.address) { st.password = ""; st.usePassword = false; st.write = false; }  // another address: start step 2 afresh
      st.found = d;
      st.imap = Object.assign({}, d.imap || {});
      st.smtp = Object.assign({}, d.smtp || {});
      st.login = d.login || a;
      st.serversOpen = d.source === "none" || !(d.imap && d.imap.host);
      st.errors = {};
      st.problem = "";
      renderMailboxSheet();
    }
    async function submitMailboxSheet() {
      const st = mbx.sheet;
      if (!st || st.saving) return;
      mbxCollectSheet();
      const { body, errors } = mailboxSheetBody(st);
      st.errors = errors;
      st.problem = "";
      if (Object.keys(errors).length) {
        if (errors["imap.host"] || errors["smtp.host"]) st.serversOpen = true;
        renderMailboxSheet();
        return;
      }
      const taken = (mbx.items || []).map((m) => m.account_id);
      const id = st.target === "primary" ? "" : (st.id || mailboxIdFromAddress(st.address, taken));
      st.saving = true;
      renderMailboxSheet();
      $("mailbox-sheet-note").textContent = "Signing in to the mailbox...";
      try {
        await api(st.target === "primary" ? myEmailApi() : `/api/gateway/me/email/accounts/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body), slow: true });
        $("mailbox-sheet-note").textContent = "";
        const target = st.target;
        closeMailboxSheet();
        if (target === "primary") await loadMyEmail(); else await loadMyMailboxes();
      } catch (e) {
        st.saving = false;
        $("mailbox-sheet-note").textContent = "";
        // A refused password at a provider that refuses account passwords: one short sentence; the
        // provider's own help (with its app-password link) stays under the Password field.
        const p = st.found && MBX_PROVIDERS[st.found.provider];
        const code = e && e.detail && typeof e.detail === "object" ? String(e.detail.reason_code || e.detail.code || "") : "";
        if (p && code === "email_auth_failed") st.problem = `Not connected: ${p.name} refused this password. Use an app password (the link above)${st.found.oauth_available ? `, or ${p.signIn}` : ""}.`;
        else st.problem = `Not connected: ${emailErrorText(e)}`;
        renderMailboxSheet();
      }
    }
    // Sign in with Google / Microsoft: base/oauth/start|finish|cancel (the primary's base, or the other mailbox's).
    async function startMailboxOAuth(provider) {
      const st = mbx.sheet;
      if (!st || mbx.oauth) return;
      mbxCollectSheet();
      const taken = (mbx.items || []).map((m) => m.account_id);
      const base = st.target === "primary" ? myEmailApi("/oauth") : `/api/gateway/me/email/accounts/${encodeURIComponent(st.id || mailboxIdFromAddress(st.address, taken))}/oauth`;
      const note = $("mailbox-sheet-note");
      let out;
      try {
        const start = { provider, address: String(st.address || "").trim() };
        if (st.target === "other") start.write = !!st.write;
        out = await api(`${base}/start`, { method: "POST", body: JSON.stringify(start), slow: true });
      } catch (e) {
        st.problem = `Not signed in: ${emailErrorText(e)}`;
          renderMailboxSheet();
        return;
      }
      const flow = { id: out.flow_id, base, cancelled: false };
      mbx.oauth = flow;
      note.textContent = out.flow === "device"
        ? `Open ${out.verification_uri} in any browser and enter the code ${out.user_code}. Waiting for you to approve...`
        : `Open this link in a browser on the gateway's computer: ${out.authorization_url} — waiting for the sign-in...`;
      while (!flow.cancelled) {
        let res;
        try {
          res = await api(`${base}/finish`, { method: "POST", body: JSON.stringify({ flow_id: flow.id, wait_s: 20 }), timeoutMs: 70000 });
        } catch (e) {
          if (!flow.cancelled && mbx.sheet === st) { st.problem = `Not signed in: ${emailErrorText(e)}`; note.textContent = ""; renderMailboxSheet(); }
          break;
        }
        if (flow.cancelled) break;
        if (res && res.pending) continue;
        note.textContent = "";
        if (mbx.oauth === flow) mbx.oauth = null;
        const target = st.target;
        closeMailboxSheet();
        if (target === "primary") await loadMyEmail(); else await loadMyMailboxes();
        return;
      }
      if (mbx.oauth === flow) mbx.oauth = null;
    }
    async function cancelMailboxOAuth() {
      const flow = mbx.oauth;
      if (!flow) return;
      flow.cancelled = true;
      mbx.oauth = null;
      await api(`${flow.base}/cancel`, { method: "POST", body: JSON.stringify({ flow_id: flow.id }) }).catch(() => {});
    }
    // ---- the primary's Settings: the full editor (stored servers, folder, Google/Microsoft client, Test,
    // Disconnect) in its own dialog; a primary not connected yet opens the mailbox form instead ----
    function openPrimaryMailboxSettings() {
      if (!(state.myEmail && state.myEmail.configured)) { openMailboxSheet({ target: "primary" }); return; }
      const backdrop = $("mailbox-settings-backdrop");
      if (!backdrop.hidden) return;
      backdrop.hidden = false;
      mbx.settingsRelease = bindAccountModal(backdrop, closePrimaryMailboxSettings);
    }
    function closePrimaryMailboxSettings() {
      const backdrop = $("mailbox-settings-backdrop");
      if (!backdrop || backdrop.hidden) return;
      backdrop.hidden = true;
      const release = mbx.settingsRelease;
      mbx.settingsRelease = null;
      if (release) release();
    }
    // ---- the tabs: Mailboxes | Notifications | Agents ----
    const MY_EMAIL_SECTIONS = ["mailboxes", "notifications", "agents"];
    function myEmailShowSection(name) {
      mbx.tab = MY_EMAIL_SECTIONS.includes(name) ? name : "mailboxes";
      for (const s of MY_EMAIL_SECTIONS) {
        const on = s === mbx.tab;
        $(`my-email-sec-${s}`).setAttribute("aria-selected", on ? "true" : "false");
        $(`my-email-sec-${s}`).tabIndex = on ? 0 : -1;
        $(`my-email-pane-${s}`).hidden = !on;
      }
    }
    function bindMyMailboxes() {
      for (const s of MY_EMAIL_SECTIONS) {
        const t = $(`my-email-sec-${s}`);
        t.onclick = () => myEmailShowSection(s);
        t.onkeydown = (e) => {
          if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
          e.preventDefault();
          const i = MY_EMAIL_SECTIONS.indexOf(s);
          const next = MY_EMAIL_SECTIONS[(i + (e.key === "ArrowRight" ? 1 : MY_EMAIL_SECTIONS.length - 1)) % MY_EMAIL_SECTIONS.length];
          myEmailShowSection(next);
          $(`my-email-sec-${next}`).focus();
        };
      }
      $("my-mailbox-add").onclick = () => openMailboxSheet({ target: state.myEmail && state.myEmail.configured ? "other" : "primary" });
      $("my-mailboxes-list").onclick = (e) => {
        const t = e.target && e.target.closest ? e.target.closest("button") : null;
        if (!t) return;
        const menu = t.closest(".mbx-menu");
        if (menu) menu.open = false;
        if (t.dataset.mbxTest) testMyMailbox(t.dataset.mbxTest);
        else if (t.dataset.mbxSettings) {
          const key = t.dataset.mbxSettings;
          const m = (mbx.items || []).find((x) => x.account_id === key);
          if (key === "__primary") openPrimaryMailboxSettings();
          else openMailboxSheet({ target: "other", settings: true, id: key, address: m ? m.address : "", write: !!(m && (m.access === "write" || (!m.access && m.can_write))) });
        }
        else if (t.dataset.mbxDelete) { mbx.confirm = t.dataset.mbxDelete; renderMyMailboxes(); }
        else if (t.dataset.mbxDeleteCancel) { mbx.confirm = ""; renderMyMailboxes(); }
        else if (t.dataset.mbxDeleteNow) removeMyMailbox(t.dataset.mbxDeleteNow);
      };
      $("mailbox-settings-close").onclick = closePrimaryMailboxSettings;
      $("mailbox-sheet-close").onclick = closeMailboxSheet;
      $("mailbox-sheet-cancel").onclick = closeMailboxSheet;
      $("mailbox-sheet-go").onclick = () => submitMailboxSheet();
      const body = $("mailbox-sheet-body");
      body.oninput = (e) => {
        const t = e.target;
        if (!t || !t.dataset || !mbx.sheet) return;
        const k = t.dataset.mbs;
        if (mbx.sheet.errors && mbx.sheet.errors[k]) {
          delete mbx.sheet.errors[k];
          t.removeAttribute("aria-invalid");
          const box = t.closest(".af-form__field");
          const msg = box && box.querySelector(".mbs-error");
          if (msg) msg.remove();
        }
        if (k === "address") {
          if (mbx.timer) clearTimeout(mbx.timer);
          mbx.sheet.address = t.value;
          mbx.timer = setTimeout(() => discoverMailbox(t.value), 600);
        }
      };
      body.addEventListener("focusout", (e) => { const t = e.target; if (!mbx.rendering && t && t.dataset && t.dataset.mbs === "address") discoverMailbox(t.value); });
      body.onclick = (e) => {
        const t = e.target && e.target.closest ? e.target.closest("button") : null;
        if (!t || !mbx.sheet) return;
        if (t.dataset.mbsOauth) startMailboxOAuth(t.dataset.mbsOauth);
        else if (t.dataset.mbsUsePassword) { mbxCollectSheet(); mbx.sheet.usePassword = true; renderMailboxSheet(); try { $("mbs-password").focus(); } catch {} }
      };
    }
"""

MAILBOXES_HTML = r"""
              <section id="my-mailboxes-card" class="af-card account-card" aria-labelledby="my-mailboxes-title">
                <div class="af-card__header"><h3 id="my-mailboxes-title" class="af-card__title">Mailboxes</h3></div>
                <div id="my-mailboxes-list"></div>
                <div class="mbx-add-row"><button id="my-mailbox-add" class="secondary" type="button">Add mailbox</button></div>
              </section>
"""

MAILBOX_SHEET_HTML = r"""
  <div id="mailbox-sheet-backdrop" class="af-modal-backdrop" hidden>
    <div class="af-modal" role="dialog" aria-modal="true" aria-labelledby="mailbox-sheet-title">
      <div class="af-modal__header">
        <h2 id="mailbox-sheet-title" class="af-modal__title">Add mailbox</h2>
        <button id="mailbox-sheet-close" class="af-modal__close" type="button" aria-label="Close" data-af-tip="Close">×</button>
      </div>
      <div id="mailbox-sheet-body" class="af-modal__body"></div>
      <div class="af-modal__footer">
        <p id="mailbox-sheet-note" class="af-modal__footer-note" role="status" aria-live="polite"></p>
        <div class="mbs-actions"><button id="mailbox-sheet-cancel" class="secondary" type="button">Cancel</button><button id="mailbox-sheet-go" type="button" hidden>Add mailbox</button></div>
      </div>
    </div>
  </div>
"""
