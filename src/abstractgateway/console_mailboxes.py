"""Account → My email: further mailboxes, read-only (root backlog 1044).

The primary mailbox is the "Mailbox" card above (PUT /me/email). This card lists the person's
OTHER mailboxes and adds one: a read-only mailbox (IMAP only, `send: false`) that automations can
watch ("When an email arrives" with that mailbox) and that never sends.

- List: GET /api/gateway/me/email/accounts → `{items: [{account_id, primary, configured, address,
  can_read, can_send, enabled, folder, mailbox: {state, reason}, watcher: {state, last_poll, …}}]}`;
  the non-primary, configured ones are rows: name, address, state, a **Use this mailbox** switch
  (PUT …/{id}/enabled `{enabled}`), **Test** (POST …/{id}/test) and **Delete** (DELETE …/{id}, after
  an inline question).
- Add a mailbox (read-only): name (the mailbox id: 1-32 of a-z, 0-9, `_`, `-`), address, IMAP
  server, port, security, login (optional), password → PUT /api/gateway/me/email/accounts/{id}
  `{address, password, username, imap: {host, port, security}, send: false, test: true}` (the gateway
  signs in first and stores nothing on a failed test).

Shown for your own account only (the routes are `/me/email/accounts`; an entity's email page does
not offer it). Spliced into the console script scope (console.py _console_owned_sources): `api`,
`esc`, `$`, `state`, `afSwitchCreate`, `afSwitchBind`, `emailErrorText`, `myEmailUi` are the
console's own.
"""

from __future__ import annotations

MAILBOXES_CSS = r"""
/* ---- Account → My email: further mailboxes (console_mailboxes.py) ---- */
.mbx-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.mbx-row { display: flex; flex-direction: column; gap: 6px; padding: 10px 12px; border: 1px solid var(--line-soft); border-radius: var(--radius-sm); }
.mbx-row__head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }
.mbx-row__name { font-weight: 600; }
.mbx-row__sub { color: var(--muted); font-size: var(--af-helper-size, var(--font-size-md)); }
.mbx-row__actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.mbx-row__state.is-failed { color: var(--error, #d6336c); }
.mbx-row__state.is-ok { color: var(--success, #2f9e44); }
.mbx-empty { color: var(--muted); margin: 0; }
.mbx-form { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px 16px; margin-top: 8px; }
.mbx-form .af-form__field { min-width: 0; }
.mbx-form .mbx-wide { grid-column: 1 / -1; }
.mbx-form input, .mbx-form select { width: 100%; box-sizing: border-box; }
@media (max-width: 767.98px) { .mbx-form { grid-template-columns: minmax(0, 1fr); } }
"""

MAILBOXES_JS = r"""
    // ---- Account → My email: further mailboxes, read-only (console_mailboxes.py) ----
    const mbx = { items: null, error: "", adding: false, saving: false, confirm: "", note: {}, switches: {}, google: false, oauth: null };
    const MBX_GMAIL_HELP = "Gmail refuses account passwords. Use an app password (Google Account → Security → 2-Step Verification → App passwords), or Sign in with Google below.";
    // Provider-aware help for a refused sign-in (Gmail never accepts the account password over IMAP).
    function mailboxProviderHelp(host, address) {
      const h = String(host || "").trim().toLowerCase();
      const a = String(address || "").trim().toLowerCase();
      if (h === "imap.gmail.com" || h.endsWith(".gmail.com") || h.endsWith("googlemail.com") || /@(gmail|googlemail)\.com$/.test(a)) return MBX_GMAIL_HELP;
      return "";
    }
    // "Sign in with Google" is offered when the gateway has a Google client (its own or the built-in one).
    function mbxGoogleAvailable(clients) {
      const g = clients && clients.providers && clients.providers.google;
      return !!(g && (g.configured || g.builtin_available));
    }
    const MBX_STATE = {
      connected: "Connected", receive_only: "Read-only: watched by your automations, never sends", paused: "Paused",
      needs_reconnect: "Needs reconnecting", unavailable: "Mailboxes are turned off for this account", not_connected: "Not connected",
    };
    const MBX_ID_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
    function mbxOwnPage() { return !myEmailUi.base; }
    function mbxFurther(items) { return (items || []).filter((m) => m && !m.primary && m.configured); }
    function mbxStateText(m) {
      if (!m.enabled) return "Not in use: automations do not watch it.";
      const st = String((m.mailbox && m.mailbox.state) || "");
      const reason = m.mailbox && m.mailbox.reason ? ` ${m.mailbox.reason}` : "";
      return `${MBX_STATE[st] || st || "unknown"}${MBX_STATE[st] ? "" : reason}`;
    }
    function mailboxRowMarkup(m, opts) {
      const o = opts || {};
      const id = esc(m.account_id);
      const w = m.watcher || {};
      const sub = [m.address ? esc(m.address) : "", m.folder ? `folder ${esc(m.folder)}` : "", m.can_send ? "reads and sends" : "read-only"].filter(Boolean).join(" · ");
      const note = o.note && o.note[m.account_id];
      let html = `<li class="mbx-row" data-mbx-row="${id}">`
        + `<div class="mbx-row__head"><span class="mbx-row__name">${id}</span><span class="mbx-row__sub">${sub}</span></div>`
        + `<p class="mbx-row__sub">${esc(mbxStateText(m))}${w.last_poll ? ` · last checked ${esc(String(w.last_poll).replace("T", " ").slice(0, 16))}` : ""}</p>`
        + `<div class="mbx-row__actions"><span data-mbx-switch="${id}"></span>`
        + `<button type="button" class="secondary" data-mbx-test="${id}">Test</button>`
        + `<button type="button" class="secondary" data-mbx-delete="${id}">Delete</button></div>`;
      if (note) html += `<p class="mbx-row__state ${note.ok ? "is-ok" : "is-failed"}" role="status">${esc(note.text)}</p>`;
      if (o.confirm === m.account_id) {
        html += `<div class="inline-confirm" role="group"><span>${esc(mailboxDeleteQuestion(m))}</span>`
          + `<button type="button" class="danger" data-mbx-delete-now="${id}">Delete</button>`
          + `<button type="button" class="secondary" data-mbx-delete-cancel="${id}">Cancel</button></div>`;
      }
      return html + `</li>`;
    }
    function mailboxDeleteQuestion(m) {
      return `Delete the mailbox ${m.account_id}${m.address ? ` (${m.address})` : ""}? Its stored password and its read position are removed; automations that watch it stop.`;
    }
    function mailboxesMarkup(items, opts) {
      const list = mbxFurther(items);
      if (!list.length) return `<p class="mbx-empty">No further mailbox yet.</p>`;
      return `<ul class="mbx-list">${list.map((m) => mailboxRowMarkup(m, opts)).join("")}</ul>`;
    }
    // The PUT body of a read-only mailbox (IMAP only), or the problem sentences.
    function mailboxConnectBody(v) {
      const problems = [];
      const id = String(v.id || "").trim();
      if (!id) problems.push("Name is required.");
      else if (id === "default") problems.push("“default” is your primary mailbox: choose another name.");
      else if (!MBX_ID_RE.test(id)) problems.push("Name: 1 to 32 lowercase letters, digits, - or _, starting with a letter or digit.");
      const address = String(v.address || "").trim();
      if (!address || !address.includes("@")) problems.push("Address is required.");
      const host = String(v.host || "").trim();
      if (!host) problems.push("IMAP server is required.");
      const port = String(v.port || "").trim();
      if (port && !/^[0-9]{1,5}$/.test(port)) problems.push("Port must be a number.");
      if (!String(v.password || "")) problems.push("Password is required.");
      if (problems.length) return { problems };
      const imap = { host, security: v.security === "starttls" ? "starttls" : "ssl" };
      if (port) imap.port = Number(port);
      return { id, body: { address, password: String(v.password), username: String(v.username || "").trim(), imap, send: false, test: true }, problems: [] };
    }
    async function loadMailboxOAuthClients() {
      try {
        mbx.google = mbxGoogleAvailable(await api("/api/gateway/me/email/oauth/clients"));
      } catch {
        mbx.google = false;
      }
      $("my-mailbox-google").hidden = !mbx.google;
    }
    function mbxRenderHelp() {
      const help = mailboxProviderHelp($("my-mailbox-imap-host").value, $("my-mailbox-address").value);
      const el = $("my-mailbox-provider-help");
      el.textContent = help;
      el.hidden = !help;
    }
    // Sign in with Google for a further mailbox: /me/email/accounts/{id}/oauth/start|finish|cancel, the
    // primary's /me/email/oauth/* shape (device code or a link on the gateway's computer).
    async function startMailboxGoogle() {
      if (mbx.oauth) return;
      const err = $("my-mailbox-error");
      const id = String($("my-mailbox-id").value || "").trim();
      const address = String($("my-mailbox-address").value || "").trim();
      const problems = [];
      if (!id) problems.push("Name is required.");
      else if (id === "default") problems.push("“default” is your primary mailbox: choose another name.");
      else if (!MBX_ID_RE.test(id)) problems.push("Name: 1 to 32 lowercase letters, digits, - or _, starting with a letter or digit.");
      if (!address || !address.includes("@")) problems.push("Address is required.");
      if (problems.length) { err.textContent = problems.join(" "); err.hidden = false; return; }
      err.hidden = true;
      const base = `/api/gateway/me/email/accounts/${encodeURIComponent(id)}/oauth`;
      const prompt = $("my-mailbox-state");
      let out;
      try {
        out = await api(`${base}/start`, { method: "POST", body: JSON.stringify({ provider: "google", address }), slow: true });
      } catch (e) {
        err.textContent = `Not signed in: ${emailErrorText(e)}`;
        err.hidden = false;
        return;
      }
      const flow = { id: out.flow_id, base, cancelled: false };
      mbx.oauth = flow;
      $("my-mailbox-google-cancel").hidden = false;
      prompt.textContent = out.flow === "device"
        ? `Open ${out.verification_uri} in any browser and enter the code ${out.user_code}. Waiting for you to approve…`
        : `Open this link in a browser on the gateway's computer: ${out.authorization_url} — waiting for the sign-in…`;
      while (!flow.cancelled) {
        let res;
        try {
          res = await api(`${base}/finish`, { method: "POST", body: JSON.stringify({ flow_id: flow.id, wait_s: 20 }), timeoutMs: 70000 });
        } catch (e) {
          if (!flow.cancelled) { err.textContent = `Not signed in: ${emailErrorText(e)}`; err.hidden = false; prompt.textContent = ""; }
          break;
        }
        if (flow.cancelled) break;
        if (res && res.pending) continue;
        prompt.textContent = `Added ${id}: signed in with Google as ${res.address || address}.`;
        mbxShowForm(false);
        await loadMyMailboxes();
        break;
      }
      if (mbx.oauth === flow) mbx.oauth = null;
      $("my-mailbox-google-cancel").hidden = true;
    }
    async function cancelMailboxGoogle() {
      const flow = mbx.oauth;
      if (!flow) return;
      flow.cancelled = true;
      mbx.oauth = null;
      $("my-mailbox-google-cancel").hidden = true;
      $("my-mailbox-state").textContent = "Sign-in cancelled.";
      await api(`${flow.base}/cancel`, { method: "POST", body: JSON.stringify({ flow_id: flow.id }) }).catch(() => {});
    }
    async function loadMyMailboxes() {
      const card = $("my-mailboxes-card");
      if (!card) throw new Error("The account email UI has no #my-mailboxes-card (console_mailboxes.py).");
      card.hidden = !mbxOwnPage();
      if (!mbxOwnPage()) return;
      loadMailboxOAuthClients();
      try {
        const out = await api("/api/gateway/me/email/accounts");
        if (!out || !Array.isArray(out.items)) throw new Error("the gateway's answer has no items list.");
        mbx.items = out.items;
        mbx.error = "";
      } catch (e) {
        mbx.error = emailErrorText(e);
      }
      renderMyMailboxes();
    }
    function renderMyMailboxes() {
      const list = $("my-mailboxes-list");
      if (mbx.error) { list.innerHTML = `<p class="af-form__error">Could not list your mailboxes: ${esc(mbx.error)}</p>`; return; }
      if (!mbx.items) { list.innerHTML = `<p class="mbx-empty">Reading your mailboxes...</p>`; return; }
      list.innerHTML = mailboxesMarkup(mbx.items, { note: mbx.note, confirm: mbx.confirm });
      for (const m of mbxFurther(mbx.items)) {
        const slot = list.querySelector(`[data-mbx-switch="${CSS.escape(m.account_id)}"]`);
        if (!slot) continue;
        const sw = afSwitchCreate({ id: `my-mailbox-enabled-${m.account_id}`, label: "Use this mailbox", checked: !!m.enabled, small: true });
        slot.append(...sw.nodes);
        afSwitchBind(sw.button, async (next) => {
          await api(`/api/gateway/me/email/accounts/${encodeURIComponent(m.account_id)}/enabled`, { method: "PUT", body: JSON.stringify({ enabled: next }) });
          await loadMyMailboxes();
          return next;
        }, (e) => { mbx.note[m.account_id] = { ok: false, text: `Not saved: ${emailErrorText(e)}` }; renderMyMailboxes(); });
      }
    }
    function mbxFormValues() {
      return {
        id: $("my-mailbox-id").value, address: $("my-mailbox-address").value, host: $("my-mailbox-imap-host").value,
        port: $("my-mailbox-imap-port").value, security: $("my-mailbox-imap-security").value,
        username: $("my-mailbox-username").value, password: $("my-mailbox-password").value,
      };
    }
    async function addMyMailbox() {
      if (mbx.saving) return;
      const err = $("my-mailbox-error");
      const out = mailboxConnectBody(mbxFormValues());
      if (out.problems.length) { err.textContent = out.problems.join(" "); err.hidden = false; return; }
      err.hidden = true;
      mbx.saving = true;
      const go = $("my-mailbox-add-go");
      go.disabled = true;
      go.setAttribute("aria-busy", "true");
      $("my-mailbox-state").textContent = "Signing in to the mailbox...";
      try {
        await api(`/api/gateway/me/email/accounts/${encodeURIComponent(out.id)}`, { method: "PUT", body: JSON.stringify(out.body), slow: true });
        $("my-mailbox-state").textContent = `Added ${out.id}: read-only, ready for your automations.`;
        $("my-mailbox-password").value = "";
        mbxShowForm(false);
        await loadMyMailboxes();
      } catch (e) {
        $("my-mailbox-state").textContent = "";
        const help = mailboxProviderHelp(out.body.imap.host, out.body.address);
        err.textContent = help ? `Not added. ${help}` : `Not added: ${emailErrorText(e)}`;
        err.hidden = false;
      } finally {
        mbx.saving = false;
        go.disabled = false;
        go.removeAttribute("aria-busy");
      }
    }
    function mbxShowForm(show) {
      mbx.adding = !!show;
      $("my-mailbox-form").hidden = !mbx.adding;
      $("my-mailbox-add").setAttribute("aria-expanded", mbx.adding ? "true" : "false");
      if (mbx.adding) { try { $("my-mailbox-id").focus(); } catch {} }
    }
    async function testMyMailbox(id) {
      mbx.note[id] = { ok: true, text: "Signing in..." };
      renderMyMailboxes();
      try {
        const out = await api(`/api/gateway/me/email/accounts/${encodeURIComponent(id)}/test`, { method: "POST", body: "{}", slow: true });
        const ok = !out || out.ok !== false;
        mbx.note[id] = { ok, text: ok ? "Signed in: the mailbox answers." : `Test failed: ${(out && (out.message || out.error)) || "the gateway did not say why."}` };
      } catch (e) {
        mbx.note[id] = { ok: false, text: `Test failed: ${emailErrorText(e)}` };
      }
      renderMyMailboxes();
    }
    async function deleteMyMailbox(id) {
      try {
        await api(`/api/gateway/me/email/accounts/${encodeURIComponent(id)}`, { method: "DELETE" });
        mbx.confirm = "";
        delete mbx.note[id];
        $("my-mailbox-state").textContent = `Deleted the mailbox ${id}.`;
        await loadMyMailboxes();
      } catch (e) {
        mbx.note[id] = { ok: false, text: `Not deleted: ${emailErrorText(e)}` };
        renderMyMailboxes();
      }
    }
    function bindMyMailboxes() {
      $("my-mailbox-add").onclick = () => mbxShowForm(!mbx.adding);
      $("my-mailbox-add-cancel").onclick = () => mbxShowForm(false);
      $("my-mailbox-add-go").onclick = () => addMyMailbox();
      $("my-mailbox-google").onclick = () => startMailboxGoogle();
      $("my-mailbox-google-cancel").onclick = () => cancelMailboxGoogle();
      $("my-mailbox-imap-host").oninput = mbxRenderHelp;
      $("my-mailbox-address").oninput = mbxRenderHelp;
      $("my-mailboxes-list").onclick = (e) => {
        const t = e.target && e.target.closest ? e.target.closest("button") : null;
        if (!t) return;
        if (t.dataset.mbxTest) testMyMailbox(t.dataset.mbxTest);
        else if (t.dataset.mbxDelete) { mbx.confirm = t.dataset.mbxDelete; renderMyMailboxes(); }
        else if (t.dataset.mbxDeleteCancel) { mbx.confirm = ""; renderMyMailboxes(); }
        else if (t.dataset.mbxDeleteNow) deleteMyMailbox(t.dataset.mbxDeleteNow);
      };
    }
"""

MAILBOXES_HTML = r"""
              <section id="my-mailboxes-card" class="af-card account-card" aria-labelledby="my-mailboxes-title" hidden>
                <div class="af-card__header">
                  <h3 id="my-mailboxes-title" class="af-card__title">Further mailboxes</h3>
                  <p class="af-card__desc">Other mailboxes your automations can watch ("When an email arrives"). Read-only: they are never used to send.</p>
                </div>
                <div id="my-mailboxes-list"></div>
                <p id="my-mailbox-state" class="inline-state" role="status" aria-live="polite"></p>
                <div class="af-form__actions"><button id="my-mailbox-add" class="secondary" type="button" aria-expanded="false" aria-controls="my-mailbox-form">Add a mailbox (read-only)</button></div>
                <div id="my-mailbox-form" class="mbx-form" hidden>
                  <div class="af-form__field"><label class="af-form__label" for="my-mailbox-id">Name</label><input id="my-mailbox-id" autocomplete="off" spellcheck="false" placeholder="work" aria-describedby="my-mailbox-id-help"><p id="my-mailbox-id-help" class="af-form__help">How automations name it: lowercase letters, digits, - or _.</p></div>
                  <div class="af-form__field"><label class="af-form__label" for="my-mailbox-address">Address</label><input id="my-mailbox-address" type="email" autocomplete="off" spellcheck="false"></div>
                  <div class="af-form__field"><label class="af-form__label" for="my-mailbox-imap-host">IMAP server</label><input id="my-mailbox-imap-host" autocomplete="off" spellcheck="false" placeholder="imap.example.com"></div>
                  <div class="af-form__field"><label class="af-form__label" for="my-mailbox-imap-port">Port</label><input id="my-mailbox-imap-port" inputmode="numeric" placeholder="993"></div>
                  <div class="af-form__field"><label class="af-form__label" for="my-mailbox-imap-security">Security</label><select id="my-mailbox-imap-security"><option value="ssl">SSL</option><option value="starttls">STARTTLS</option></select></div>
                  <div class="af-form__field"><label class="af-form__label" for="my-mailbox-username">Login</label><input id="my-mailbox-username" autocomplete="off" spellcheck="false" aria-describedby="my-mailbox-username-help"><p id="my-mailbox-username-help" class="af-form__help">Empty: the address.</p></div>
                  <div class="af-form__field mbx-wide"><label class="af-form__label" for="my-mailbox-password">Password</label><input id="my-mailbox-password" type="password" autocomplete="new-password" aria-describedby="my-mailbox-password-help"><p id="my-mailbox-password-help" class="af-form__help">Use an app password if your provider needs one. Stored encrypted, never shown.</p></div>
                  <p id="my-mailbox-provider-help" class="af-form__help mbx-wide" hidden></p>
                  <p id="my-mailbox-error" class="af-form__error mbx-wide" role="alert" hidden></p>
                  <div class="af-form__actions mbx-wide"><button id="my-mailbox-add-cancel" class="secondary" type="button">Cancel</button><button id="my-mailbox-google" class="secondary" type="button" hidden>Sign in with Google</button><button id="my-mailbox-google-cancel" class="secondary" type="button" hidden>Cancel sign-in</button><button id="my-mailbox-add-go" type="button">Add mailbox</button></div>
                </div>
              </section>
"""
