"""The console's Triggers page (root backlog 1041): the signed-in person's trigger CONNECTIONS.

A trigger source is a type (email received, feed item, webhook received); a connection is one
person's instance of it (this mailbox, this feed URL, this webhook and its secret), created once
here and reused by every automation and flow of its owner (Flow's automation dialog lists them).

Page (`/console#triggers`; `#triggers?new=<source_id>` opens the create dialog on that source):
- a table Source · Name · Status · Last event · actions **Events** (expands the row: the connection's
  20 most recent events, each a `<details>` holding the event JSON; webhook rows also show their
  address with a copy button) · **Test** (sends a test event; the result is said in the row) ·
  **Delete** (an inline confirmation under the row, never a dialog);
- **New connection** opens a kit `af-modal`: the sources whose descriptor says
  `needs_connection: true`, a Name, and the source's settings rendered from its
  `connection_schema` (the same JSON Schema subset Flow renders for `config_schema`: string,
  password (`format: password` or `writeOnly`), integer, number, boolean, enum; anything else is
  shown read-only). A webhook's signing secret comes back ONCE in the create answer: the dialog
  then shows the address and the secret with copy buttons and the sentence that it is shown once;
- admins get an **All accounts** switch: every account's connections, with an Account column.

Routes (W2, root backlog 1040/1041/1042): GET /trigger-sources (descriptors with
`needs_connection` + `connection_schema`); GET/POST /triggers/connections (`?all=true` for an
admin: every principal's, rows carry `principal`, the owner string `"<tenant>:<user>"`); DELETE /triggers/connections/{id};
GET /triggers/connections/{id}/events?after=<seq>&limit=; POST /triggers/connections/{id}/test-fire
→ {event_id, sequence}.

Spliced into the console script scope with the UI layer (console.py _console_owned_sources):
`api`, `esc`, `$`, `state`, `afSwitchCreate`, `afSwitchBind`, `setActiveTab`, `bindAccountModal`,
`clipboardWrite` and `uiToast` are the console's own.
"""

from __future__ import annotations

TRIGGERS_CSS = r"""
/* ---- Triggers page (console_triggers.py, root backlog 1041) ---- */
.trg-page { display: flex; flex-direction: column; gap: 12px; }
.trg-purpose { margin: 0; max-width: 90ch; }
.trg-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; }
.trg-toolbar .trg-spacer { flex: 1 1 auto; }
.trg-message:empty { display: none; }
.trg-table { width: 100%; table-layout: fixed; border-collapse: collapse; }
.trg-table th { text-align: left; white-space: nowrap; }
.trg-table td { vertical-align: top; padding-top: 10px; padding-bottom: 10px; overflow-wrap: anywhere; }
.trg-table th.trg-col-name { width: auto; }
.trg-table th.trg-col-account { width: 14%; }
.trg-table th.trg-col-status { width: 15rem; }
.trg-doing { display: block; font-size: var(--font-size-md); margin: 2px 0 4px; }
.trg-table td.trg-status .af-switch { margin-top: 2px; }
.trg-advanced { margin-top: 4px; font-size: var(--font-size-md); }
.trg-error { display: block; margin-top: 4px; color: var(--error, #d6336c); font-size: var(--font-size-md); }
.trg-field-error { margin: 2px 0 0; color: var(--error, #d6336c); font-size: var(--font-size-md); }
.trg-field input[aria-invalid="true"] { border-color: var(--error, #d6336c); }
.trg-duration { display: flex; gap: 8px; }
.trg-duration input { max-width: 8rem; }
.trg-duration select { max-width: 12rem; }
.trg-more > summary { cursor: pointer; color: var(--muted); min-height: 28px; }
.trg-warn-line { margin: 0; color: var(--warning, #c77700); }
.trg-event { display: flex; flex-direction: column; gap: 2px; padding: 4px 0; }
.trg-event > details > summary { cursor: pointer; color: var(--muted); font-size: var(--font-size-md); }
.trg-empty__title { margin: 0 0 8px; font-weight: 600; color: var(--text); }
.trg-empty__actions { display: flex; flex-wrap: wrap; gap: 8px; }
.trg-next { margin: 0; padding: 8px 10px; border-left: 3px solid var(--info, var(--accent)); background: color-mix(in srgb, var(--info, var(--accent)) 8%, transparent); border-radius: var(--radius-sm); }
.trg-hints { margin: 6px 0 0; padding-left: 18px; font-size: var(--font-size-md); color: var(--muted); }
.trg-advanced > summary { cursor: pointer; color: var(--muted); min-height: 24px; }
.trg-table th.trg-col-last { width: 9rem; }
.trg-table th.trg-col-actions { width: 15rem; }
.trg-name { font-weight: 600; }
.trg-sub { display: block; color: var(--muted); font-size: var(--af-helper-size, var(--font-size-md)); font-weight: 400; margin-top: 2px; }
.trg-mono { font-family: var(--font-mono); font-size: var(--font-size-md); overflow-wrap: anywhere; word-break: break-word; }
.trg-chip { display: inline-flex; align-items: center; min-height: 22px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--line); font-size: var(--font-size-sm); font-weight: 500; white-space: nowrap; }
.trg-chip.is-ok { border-color: color-mix(in srgb, var(--success, #2f9e44) 55%, transparent); color: var(--success, #2f9e44); }
.trg-chip.is-warn { border-color: color-mix(in srgb, var(--warning, #c77700) 55%, transparent); color: var(--warning, #c77700); }
.trg-chip.is-err { border-color: color-mix(in srgb, var(--error, #d6336c) 55%, transparent); color: var(--error, #d6336c); }
.trg-chip.is-muted { color: var(--muted); }
.trg-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
@media (min-width: 1024px) { .trg-actions { flex-wrap: nowrap; } }
.trg-actions > button { white-space: nowrap; }
.trg-fire { display: block; margin-top: 6px; font-size: var(--font-size-md); }
.trg-fire.is-ok { color: var(--success, #2f9e44); }
.trg-fire.is-failed { color: var(--error, #d6336c); }
.trg-empty { padding: 14px 2px; color: var(--muted); }
.trg-empty.is-error { color: var(--error, #d6336c); }
.trg-loading { display: inline-flex; align-items: center; gap: 8px; }
.trg-spinner { width: 14px; height: 14px; border-radius: 50%; border: 2px solid color-mix(in srgb, currentColor 25%, transparent); border-top-color: currentColor; animation: trg-spin .8s linear infinite; flex: none; }
@keyframes trg-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .trg-spinner { animation: none; } }
.trg-detail > td { padding: 4px 10px 14px; border-top: 0; }
.trg-detail__box { display: flex; flex-direction: column; gap: 10px; padding: 10px 12px; border: 1px solid var(--line-soft); border-radius: var(--radius-sm); background: var(--ui-surface-1, transparent); }
.trg-detail__head { margin: 0; font-weight: 600; font-size: var(--font-size-base); }
.trg-copy-row { display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
.trg-copy-row > code { flex: 1 1 260px; min-width: 0; padding: 6px 8px; border: 1px solid var(--line-soft); border-radius: var(--radius-sm); overflow-wrap: anywhere; word-break: break-all; }
.trg-copy-row > button { min-height: 44px; white-space: nowrap; }
.trg-events { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
.trg-events details > summary { cursor: pointer; min-height: 28px; display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }
.trg-events details > summary .trg-seq { font-family: var(--font-mono); color: var(--muted); }
.trg-events pre { margin: 6px 0 4px; padding: 8px 10px; max-height: 22rem; overflow: auto; border-radius: var(--radius-sm); background: var(--ui-surface-2, var(--bg-secondary)); font-size: var(--font-size-md); white-space: pre-wrap; overflow-wrap: anywhere; }
.trg-form { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 12px 18px; }
.trg-field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.trg-field--wide { grid-column: 1 / -1; }
.trg-field > label, .trg-field > .trg-label { font-size: var(--font-size-base); font-weight: 600; text-transform: none; letter-spacing: normal; color: var(--text); }
.trg-field input, .trg-field select { width: 100%; box-sizing: border-box; }
.trg-help { margin: 0; color: var(--muted); font-size: var(--af-helper-size, var(--font-size-md)); line-height: 1.4; }
.trg-once { margin: 0; padding: 10px 12px; border-left: 3px solid var(--warning, var(--accent)); background: color-mix(in srgb, var(--warning, var(--accent)) 9%, transparent); border-radius: var(--radius-sm); font-size: var(--font-size-base); line-height: 1.45; }
.trg-footer-actions { display: flex; gap: 8px; margin-left: auto; flex-wrap: wrap; }
.trg-setup { display: flex; flex-direction: column; gap: 8px; padding: 10px 12px; border: 1px solid var(--line-soft); border-radius: var(--radius-sm); }
.trg-setup p { margin: 0; }
.trg-code { flex: 1 1 260px; min-width: 0; margin: 0; padding: 8px 10px; border-radius: var(--radius-sm); background: var(--ui-surface-2, var(--bg-secondary)); font-family: var(--font-mono); font-size: var(--font-size-md); white-space: pre-wrap; overflow-wrap: anywhere; max-height: 22rem; overflow: auto; }
.trg-row--mailbox .link-button { white-space: normal; text-align: right; }
@media (max-width: 1023.98px) {
  .trg-table .trg-th-last, .trg-table td.trg-last { display: none; }
  .trg-fold { display: block; }
}
@media (min-width: 1024px) { .trg-fold { display: none; } }
@media (max-width: 767.98px) {
  .trg-table, .trg-table tbody, .trg-table tr, .trg-table td { display: block; width: 100%; }
  .trg-table thead { display: none; }
  .trg-table tr.trg-row { display: grid; grid-template-columns: minmax(0, 1fr); gap: 4px; padding: 12px 0; border-top: 1px solid var(--line-soft); }
  .trg-table tr.trg-row > td { padding: 0; border: 0; width: auto; }
  .trg-actions { justify-content: flex-start; }
  .trg-actions > button, .trg-toolbar > button { min-height: 44px; }
  .trg-form { grid-template-columns: minmax(0, 1fr); }
}
"""

TRIGGERS_JS = r"""
    // ---- Triggers page (console_triggers.py, root backlog 1041) ----
    const trg = {
      opened: false, all: false, rows: null, error: "", sources: null, sourcesError: "", mailboxes: null, mailboxesError: "", hookTest: null,
      expanded: {}, fire: {}, modal: null, release: null, pendingNew: "",
    };
    const TRG_EVENTS_LIMIT = 20;
    const TRG_STATUS = { active: ["Active", "is-ok"], paused: ["Paused", "is-muted"], error: ["Error", "is-err"], failed: ["Error", "is-err"], disabled: ["Off", "is-muted"] };
    function trgAdmin() { return !!(state.principal && state.principal.admin); }
    function trgErr(e) { return String((e && e.message) || e || "The gateway did not say why."); }
    function trgAgo(iso, nowMs) {
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
    // The sources a connection can be created for: GET /trigger-sources descriptors whose
    // `needs_connection` is true. A served source WITHOUT the boolean is a gateway older than the
    // connections contract: said loudly, never read as "needs none".
    function trgConnectableSources(items) {
      const out = [];
      const missing = [];
      for (const s of items || []) {
        if (!s || !s.available) continue;
        if (typeof s.needs_connection !== "boolean") { missing.push(`${s.id}@${s.version}`); continue; }
        if (!s.needs_connection) continue;
        // A mailbox-backed source (`connection_provider: "email_accounts"`, email received) reads the
        // person's mail accounts: its mailboxes are listed as rows, managed under Account → My email.
        if (s.connection_provider === "email_accounts") continue;
        if (!s.connection_schema || typeof s.connection_schema !== "object" || Array.isArray(s.connection_schema)) {
          throw new Error(`Trigger source ${s.id}@${s.version} needs a connection but serves no connection_schema object.`);
        }
        out.push(s);
      }
      if (missing.length) {
        throw new Error(`This gateway's trigger sources do not say whether they need a connection (needs_connection missing on ${missing.join(", ")}). Update the gateway.`);
      }
      return out;
    }
    // The trigger as the person reads it (the same words as Flow's trigger choices): "When <name> has a
    // new item", "When <name> is called", "When mail arrives in <mailbox>"; never `source_id@version`.
    function trgTriggerPhrase(row, sources) {
      const name = String(row.label || row.connection_id || "");
      if (row.source_id === "feed.item") return `When ${name} has a new item`;
      if (row.source_id === "webhook.received") return `When ${name} is called`;
      return trgSourceLabel(row, sources);
    }
    function trgSourceLabel(row, sources) {
      const s = (sources || []).find((x) => x.id === row.source_id && Number(x.version) === Number(row.source_version));
      return s ? String(s.label || s.id) : String(row.source_id);
    }
    function trgPrincipalText(p) {
      // The store's owner string is "<tenant>:<user>" (W2 principal_key); the default tenant is implied.
      if (typeof p === "string" && p.trim()) return p.trim().replace(/^default:/, "");
      if (p && typeof p === "object" && p.user_id) return p.tenant_id && p.tenant_id !== "default" ? `${p.tenant_id}/${p.user_id}` : String(p.user_id);
      return "";
    }
    function trgStatusChip(status) {
      const key = String(status || "").trim().toLowerCase();
      const known = TRG_STATUS[key];
      if (known) return `<span class="trg-chip ${known[1]}">${esc(known[0])}</span>`;
      return `<span class="trg-chip is-warn">${esc(key || "unknown")}</span>`;
    }
    // What a connection is doing, in words (operator ruling): the Listening switch is the person's
    // (`paused`), the only control; whether an automation uses it (`used_by`) is the system's economy
    // and never moves the switch. `always_listen` ("When nothing uses it") keeps fetching when nothing uses it.
    function trgUsedBy(r) {
      if (Array.isArray(r.used_by)) return r.used_by.length;
      return typeof r.used_by === "number" ? r.used_by : null;
    }
    // UX #8: names, not counts ("Listening · used by Release digest"), up to 3 then "+N"; an unused
    // connection is "Waiting for an automation to use it".
    function trgListeningWords(paused, usedBy, alwaysListen, names) {
      if (paused) return "Paused";
      const list = names || [];
      const count = Math.max(usedBy || 0, list.length);
      if (list.length) {
        const shown = list.slice(0, 3);
        const more = count - shown.length;
        return `Listening · used by ${shown.join(", ")}${more > 0 ? ` +${more}` : ""}`;
      }
      if (usedBy === null) return "Listening";
      if (usedBy > 0) return `Listening · used by ${usedBy} automation${usedBy === 1 ? "" : "s"}`;
      if (alwaysListen) return "Listening · kept on even when nothing uses it";
      return "Waiting for an automation to use it";
    }
    // The titles of your active automations that read a connection (or a mailbox).
    function trgUserNames(automations, match) {
      return (automations || []).filter((a) => a && a.status === "active" && a.trigger && match(a.trigger))
        .map((a) => String(a.title || a.automation_id || "an automation"));
    }
    function trgConnectionUsers(r, automations) {
      return trgUserNames(automations, (t) => !!(t.config && t.config.connection_id === r.connection_id));
    }
    function trgMailboxUsers(m, automations) {
      return trgUserNames(automations, (t) => {
        if (t.source_id !== "email.received") return false;
        const account = String((t.config && t.config.account) || "self");
        return account === m.account_id || (account === "self" && m.primary);
      });
    }
    function trgDoingText(r, automations) {
      if (typeof r.paused !== "boolean") return "";
      return trgListeningWords(r.paused, trgUsedBy(r), r.always_listen === true, trgConnectionUsers(r, automations));
    }
    function trgListenSwitch(r) {
      const id = esc(r.connection_id);
      const on = !r.paused;
      return `<button type="button" role="switch" class="af-switch af-switch--sm" aria-checked="${on ? "true" : "false"}" data-trg-listen="${id}" data-af-tip="${on ? "Pause" : "Resume"} ${esc(r.label || r.connection_id)}">`
        + `<span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">Listening</span></span></button>`;
    }
    function trgStatusCell(r, automations) {
      const doing = trgDoingText(r, automations);
      if (!doing) return `${trgStatusChip(r.status)}<span class="trg-sub">listening state not reported (update the gateway)</span>`;
      const st = String(r.status || "").toLowerCase();
      const chip = st && st !== "active" ? trgStatusChip(r.status) : "";
      return `${chip}<span class="trg-doing" data-trg-doing="${esc(r.connection_id)}">${esc(doing)}</span>${trgListenSwitch(r)}${trgAdvancedMarkup(r)}`;
    }
    // The operator's "Advanced" (collapsed, per connection), content-named per the console's rule (R15 D1,
    // no "Advanced" anywhere): "When nothing uses it" → keep fetching even when no automation uses it.
    function trgAdvancedMarkup(r) {
      if (typeof r.always_listen !== "boolean") return "";
      const id = esc(r.connection_id);
      return `<details class="trg-advanced"><summary>When nothing uses it</summary>`
        + `<button type="button" role="switch" class="af-switch af-switch--sm" aria-checked="${r.always_listen ? "true" : "false"}" data-trg-always="${id}">`
        + `<span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">Keep fetching even when nothing uses it</span></span></button></details>`;
    }
    async function setTriggerAlwaysListen(id, on) {
      try {
        await api(`/api/gateway/triggers/connections/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ always_listen: on }) });
        await loadTriggers();
      } catch (e) {
        trgMessage(`Not saved: ${trgErr(e)}`, "error");
        renderTriggers();
      }
    }
    async function setTriggerPaused(id, paused) {
      try {
        await api(`/api/gateway/triggers/connections/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ paused }) });
        await loadTriggers();
      } catch (e) {
        trgMessage(`${paused ? "Not paused" : "Not resumed"}: ${trgErr(e)}`, "error");
        renderTriggers();
      }
    }
    // UX #9: the stored error (`last_error {code, cause, fix}`), in words and in red.
    const TRG_HTTP_WORDS = { 400: "bad request", 401: "unauthorized", 403: "forbidden", 404: "not found", 410: "gone", 429: "too many requests", 500: "server error", 502: "bad gateway", 503: "unavailable" };
    function trgErrorText(r) {
      const e = r && r.last_error;
      if (!e || typeof e !== "object") return "";
      const code = String(e.code || "");
      const cause = String(e.cause || e.message || "");
      if (r.source_id === "feed.item" || code.startsWith("feed_")) {
        const m = /HTTP (\d{3})/.exec(cause);
        if (m) return `Can't read the feed: the address answered ${m[1]}${TRG_HTTP_WORDS[m[1]] ? ` (${TRG_HTTP_WORDS[m[1]]})` : ""}. Check the feed address.`;
        if (code === "feed_unreachable") return "Can't read the feed: it could not be reached. Check the address and the network.";
        if (code === "feed_unreadable") return "Can't read the feed: the address does not answer with an RSS or Atom feed. Check the feed address.";
        return `Can't read the feed: ${cause}${e.fix ? ` ${e.fix}` : ""}`;
      }
      if (code.startsWith("webhook_")) return `Last call refused: ${webhookRefusalWords(code)}`;
      return `${cause}${e.fix ? ` ${e.fix}` : ""}`.trim();
    }
    function trgFireLine(fire) {
      if (!fire) return "";
      if (fire.pending) return `<span class="trg-fire" role="status">Sending a test event...</span>`;
      if (fire.ok) return `<span class="trg-fire is-ok" role="status">Test event #${esc(fire.sequence)} sent.</span>`;
      return `<span class="trg-fire is-failed" role="status">Test not sent: ${esc(fire.message || "")}</span>`;
    }
    // The person's mailboxes as rows (source "When an email arrives"): an email trigger reads a mail
    // account, created and managed under Account → My email (GET /me/email/accounts).
    const TRG_MAILBOX_STATE = {
      connected: ["Active", "is-ok"], receive_only: ["Read-only", "is-ok"], paused: ["Paused", "is-muted"],
      needs_reconnect: ["Needs reconnecting", "is-warn"], unavailable: ["Unavailable", "is-muted"], not_connected: ["Not connected", "is-muted"],
    };
    function trgMailboxStatusChip(m) {
      const st = String((m.mailbox && m.mailbox.state) || "").trim();
      // A healthy mailbox needs no chip: the listening words say it all.
      if (!m.enabled || st === "connected" || st === "receive_only") return "";
      const known = TRG_MAILBOX_STATE[st];
      if (known) return `<span class="trg-chip ${known[1]}">${esc(known[0])}</span>`;
      return `<span class="trg-chip is-warn">${esc(st || "unknown")}</span>`;
    }
    // A mailbox row: the same three states, with the mailbox's own Active switch (My email → Mailboxes).
    function trgMailboxStatusCell(m, automations) {
      const users = trgMailboxUsers(m, automations);
      const words = trgListeningWords(!m.enabled, automations ? users.length : null, false, users);
      const id = esc(m.account_id);
      return `${trgMailboxStatusChip(m)}<span class="trg-doing">${esc(words)}</span>`
        + `<button type="button" role="switch" class="af-switch af-switch--sm" aria-checked="${m.enabled ? "true" : "false"}" data-trg-mailbox-use="${id}">`
        + `<span class="af-switch__track" aria-hidden="true"><span class="af-switch__thumb"></span></span><span class="af-switch__text"><span class="af-switch__label">Listening</span></span></button>`;
    }
    async function setMailboxInUse(id, enabled) {
      try {
        await api(`/api/gateway/me/email/accounts/${encodeURIComponent(id)}/enabled`, { method: "PUT", body: JSON.stringify({ enabled }) });
        await loadTriggers();
      } catch (e) {
        trgMessage(`Not saved: ${trgErr(e)}`, "error");
        renderTriggers();
      }
    }
    function mailboxRowsMarkup(items, opts) {
      const o = opts || {};
      return (items || []).filter((m) => m && m.configured).map((m) => {
        const id = esc(m.account_id);
        const w = m.watcher || {};
        const last = w.last_poll ? `checked ${trgAgo(w.last_poll, o.now) || w.last_poll}` : "Never";
        const name = m.primary ? `${m.address || m.account_id} (primary)` : m.account_id;
        return `<tr class="trg-row trg-row--mailbox" data-trg-mailbox="${id}">`
          + `<td class="trg-name-cell"><span class="trg-name">${esc(name)}</span><span class="trg-sub">When mail arrives in ${esc(m.address || m.account_id)}${m.can_send ? "" : " · read-only"}</span><span class="trg-sub trg-fold">${esc(last.charAt(0).toUpperCase() + last.slice(1))}</span></td>`
          + `<td class="trg-status">${trgMailboxStatusCell(m, o.automations)}</td>`
          + `<td class="trg-last">${esc(last)}</td>`
          + `<td class="trg-actions-cell"><div class="trg-actions"><button type="button" class="link-button" data-trg-mailbox-manage="${id}" data-af-tip="Your mailboxes are managed in My email">Manage in My email</button></div></td></tr>`;
      }).join("");
    }
    function triggersRowsMarkup(rows, opts) {
      const o = opts || {};
      const cols = o.all ? 5 : 4;
      const list = rows || [];
      const mail = !o.all ? mailboxRowsMarkup(o.mailboxes, o) : "";
      if (!list.length && mail) return mail;
      if (!list.length && !o.all) {
        return `<tr><td colspan="${cols}" class="trg-empty"><p class="trg-empty__title">Nothing to listen to yet.</p>`
          + `<div class="trg-empty__actions"><button type="button" class="secondary" data-trg-empty="mailbox">Add a mailbox (My email)</button>`
          + `<button type="button" class="secondary" data-trg-empty="feed.item">Add a feed</button>`
          + `<button type="button" class="secondary" data-trg-empty="webhook.received">Add a webhook</button></div></td></tr>`;
      }
      if (!list.length) {
        return `<tr><td colspan="${cols}" class="trg-empty">${o.all ? "No account has a connection yet." : "No connection yet. Create one to let your automations listen to a mailbox, a feed or a webhook."}</td></tr>`;
      }
      return list.map((r) => {
        const id = esc(r.connection_id);
        const label = esc(r.label || r.connection_id);
        const phrase = esc(trgTriggerPhrase(r, o.sources));
        const last = r.last_event_at ? trgAgo(r.last_event_at, o.now) || esc(r.last_event_at) : "Never";
        const open = !!(o.expanded && o.expanded[r.connection_id]);
        const owner = o.all ? trgPrincipalText(r.principal) : "";
        const acts = [
          `<button type="button" class="secondary" data-trg-events="${id}" aria-expanded="${open ? "true" : "false"}" data-af-tip="Recent events of ${label}">Events</button>`,
          `<button type="button" class="secondary" data-trg-test="${id}" data-af-tip="Send a test event to ${label}">Test</button>`,
          `<button type="button" class="secondary" data-trg-delete="${id}" data-af-tip="Delete ${label}">Delete</button>`,
        ];
        let html = `<tr class="trg-row" data-trg-row="${id}">`
          + `<td class="trg-name-cell"><span class="trg-name">${label}</span><span class="trg-sub">${phrase}</span>${trgErrorText(r) ? `<span class="trg-error" role="status">${esc(trgErrorText(r))}</span>` : ""}<span class="trg-sub trg-fold">Last event: ${esc(last)}</span>${trgFireLine(o.fire && o.fire[r.connection_id])}</td>`
          + (o.all ? `<td class="trg-account">${owner ? esc(owner) : `<span class="trg-sub">owner not reported</span>`}</td>` : "")
          + `<td class="trg-status">${trgStatusCell(r, o.automations)}</td>`
          + `<td class="trg-last">${esc(last)}</td>`
          + `<td class="trg-actions-cell"><div class="trg-actions">${acts.join("")}</div></td></tr>`;
        if (open) html += `<tr class="trg-detail" data-trg-detail="${id}"><td colspan="${cols}">${triggerDetailMarkup(r, o.expanded[r.connection_id])}</td></tr>`;
        return html;
      }).join("") + mail;
    }
    function trgCopyRow(label, value, key) {
      return `<div class="trg-field trg-field--wide"><span class="trg-label">${esc(label)}</span>`
        + `<div class="trg-copy-row"><code data-trg-value="${esc(key)}">${esc(value)}</code>`
        + `<button type="button" class="secondary" data-trg-copy="${esc(key)}" aria-label="Copy the ${esc(label.toLowerCase())}">Copy</button></div></div>`;
    }
    // The expanded row: a webhook's address (copy), then the recent events, newest first.
    function triggerDetailMarkup(row, detail) {
      const d = detail || {};
      let html = `<div class="trg-detail__box">`;
      if (row.ingress_url) {
        html += trgCopyRow("Webhook address", row.ingress_url, `ingress:${row.connection_id}`)
          + `<p class="trg-help">Senders sign each request with this connection's secret (Standard Webhooks). The secret was shown once, when the connection was created.</p>`;
      }
      html += `<p class="trg-detail__head">Recent events</p>`;
      if (d.loading) return html + `<p class="trg-empty">${trgLoadingMarkup("Reading the events...")}</p></div>`;
      if (d.error) return html + `<p class="message error">Could not read the events: ${esc(d.error)}</p></div>`;
      html += triggerEventsMarkup(d.events || []);
      return html + `</div>`;
    }
    // UX #10: an event as a sentence ("Test event", "New item: Release 0.13", "Call received").
    function triggerEventSentence(e) {
      const d = e && e.data && typeof e.data === "object" ? e.data : {};
      const type = String((e && e.type) || "");
      if (d.test === true) return "Test event";
      if (type.includes("feed")) return `New item: ${d.title || e.subject || "untitled"}`;
      if (type.includes("webhook")) return "Call received";
      if (type.includes("email")) return `New mail: ${e.subject || d.subject || "no subject"}`;
      return e.subject ? `${type || "Event"}: ${e.subject}` : (type || "Event");
    }
    // "just now" / "12 min ago" within the hour, else "09:00" today, else "Oct 24 · 09:00".
    function trgWhen(iso, nowMs) {
      const t = Date.parse(String(iso || ""));
      if (!Number.isFinite(t)) return "";
      const now = nowMs || Date.now();
      if (now - t < 3600 * 1000) return trgAgo(iso, now);
      const d = new Date(t), n = new Date(now);
      const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
      if (d.toDateString() === n.toDateString()) return hm;
      return `${d.toLocaleString("en", { month: "short" })} ${d.getDate()} · ${hm}`;
    }
    function triggerEventsMarkup(events) {
      const list = (events || []).slice().sort((a, b) => Number(b.sequence || 0) - Number(a.sequence || 0)).slice(0, TRG_EVENTS_LIMIT);
      if (!list.length) return `<p class="trg-empty">No event yet. Send a test event to see one here.</p>`;
      const items = list.map((e) => {
        const when = e.time ? trgWhen(e.time) : "";
        return `<li class="trg-event" data-trg-event="${esc(e.id || "")}"><span class="trg-event__line">${esc(triggerEventSentence(e))}${when ? ` · ${esc(when)}` : ""}</span>`
          + `<details><summary>Details</summary><pre>${esc(JSON.stringify(e, null, 2))}</pre></details></li>`;
      }).join("");
      return `<ul class="trg-events">${items}</ul>`;
    }
    function trgMessage(text, tone) {
      const el = $("triggers-message");
      if (!el) return;
      el.textContent = text || "";
      el.className = `message trg-message${tone ? ` ${tone}` : ""}`;
    }
    function openTriggersPage() {
      if (!state.principal) return;
      if (!trg.opened) {
        trg.opened = true;
        const slot = $("triggers-all-slot");
        slot.hidden = !trgAdmin();
        if (trgAdmin()) {
          const sw = afSwitchCreate({ id: "triggers-all", label: "All accounts", checked: false, small: true });
          slot.append(...sw.nodes);
          afSwitchBind(sw.button, async (next) => { trg.all = next; trg.expanded = {}; trg.rows = null; await loadTriggers(); return next; }, (e) => trgMessage(trgErr(e), "error"));
        }
      }
      loadTriggers();
    }
    async function loadTriggerSources() {
      try {
        const out = await api("/api/gateway/trigger-sources");
        trg.sources = (out && out.items) || [];
        trg.sourcesError = "";
      } catch (e) {
        trg.sources = null;
        trg.sourcesError = trgErr(e);
      }
    }
    // The loading state of a list (spinner + "Reading …"), shared by Triggers, Events and My email:
    // a list is never shown empty, nor as its empty state, while its answer is pending.
    function trgLoadingMarkup(text) {
      return `<span class="trg-loading" role="status" aria-busy="true"><span class="trg-spinner" aria-hidden="true"></span>${esc(text)}</span>`;
    }
    async function loadTriggers() {
      // First read (or a new view): the list shows the loading state until connections AND mailboxes answered.
      if (!trg.rows || trg.error) { trg.rows = null; trg.error = ""; if (!trg.all) trg.mailboxes = null; renderTriggers(); }
      await loadTriggerSources();
      try {
        const out = await api(`/api/gateway/triggers/connections${trg.all ? "?all=true" : ""}`);
        if (!out || !Array.isArray(out.connections)) throw new Error("the gateway's answer has no connections list.");
        trg.rows = out.connections;
        trg.error = "";
      } catch (e) {
        trg.error = trgErr(e);
      }
      if (!trg.all) await Promise.all([loadTriggerMailboxes(), loadTriggerAutomations()]);
      renderTriggers();
      for (const id of Object.keys(trg.expanded)) loadTriggerEvents(id);
      if (trg.pendingNew) { const want = trg.pendingNew; trg.pendingNew = ""; openTriggerModal(want); }
    }
    async function loadTriggerAutomations() {
      try {
        const out = await api("/api/gateway/automations?status=active&limit=200");
        trg.automations = out && Array.isArray(out.items) ? out.items : null;
      } catch {
        trg.automations = null;  // the rows fall back to counts ("used by 2 automations")
      }
    }
    async function loadTriggerMailboxes() {
      try {
        const out = await api("/api/gateway/me/email/accounts");
        if (!out || !Array.isArray(out.items)) throw new Error("the gateway's answer has no items list.");
        trg.mailboxes = out.items;
        trg.mailboxesError = "";
      } catch (e) {
        trg.mailboxes = [];
        trg.mailboxesError = trgErr(e);
      }
    }
    // Account → My email: the Accounts page and your own row's Email dialog (where mailboxes live).
    function openMyEmailFromTriggers() {
      const p = state.principal || {};
      $("tab-button-users").click();
      openAccountEmail({ id: String(p.user_id || ""), tenant_id: String(p.tenant_id || "default"), kind: "human", role: p.admin ? "admin" : "user" });
    }
    function renderTriggers() {
      const body = $("triggers-table");
      if (!body) return;
      $("triggers-account-head").hidden = !trg.all;
      const cols = trg.all ? 5 : 4;
      if (trg.error) { body.innerHTML = `<tr><td colspan="${cols}" class="trg-empty is-error" role="alert">Could not list the connections: ${esc(trg.error)}</td></tr>`; return; }
      if (!trg.rows || (!trg.all && trg.mailboxes === null)) { body.innerHTML = `<tr><td colspan="${cols}" class="trg-empty">${trgLoadingMarkup(trg.all ? "Reading the connections..." : "Reading your connections and mailboxes...")}</td></tr>`; return; }
      body.innerHTML = triggersRowsMarkup(trg.rows, { all: trg.all, sources: trg.sources || [], expanded: trg.expanded, fire: trg.fire, mailboxes: trg.mailboxes || [], automations: trg.all ? null : trg.automations });
      if (trg.sourcesError) trgMessage(`Could not read the trigger sources: ${trg.sourcesError}`, "error");
      else if (!trg.all && trg.mailboxesError) trgMessage(`Could not read your mailboxes: ${trg.mailboxesError}`, "error");
    }
    async function loadTriggerEvents(id) {
      if (!trg.expanded[id]) return;
      trg.expanded[id] = Object.assign({}, trg.expanded[id], { loading: !trg.expanded[id].events });
      renderTriggers();
      try {
        const out = await api(`/api/gateway/triggers/connections/${encodeURIComponent(id)}/events?limit=${TRG_EVENTS_LIMIT}`);
        if (!out || !Array.isArray(out.events)) throw new Error("the gateway's answer has no events list.");
        if (trg.expanded[id]) trg.expanded[id] = { events: out.events, error: "", loading: false };
      } catch (e) {
        if (trg.expanded[id]) trg.expanded[id] = { events: null, error: trgErr(e), loading: false };
      }
      renderTriggers();
    }
    function toggleTriggerEvents(id) {
      if (trg.expanded[id]) { delete trg.expanded[id]; renderTriggers(); return; }
      trg.expanded[id] = { loading: true };
      loadTriggerEvents(id);
    }
    async function testFireTrigger(id) {
      trg.fire[id] = { pending: true };
      renderTriggers();
      try {
        const out = await api(`/api/gateway/triggers/connections/${encodeURIComponent(id)}/test-fire`, { method: "POST", body: "{}" });
        if (!out || out.sequence === undefined || !out.event_id) throw new Error("the gateway's answer has no event_id and sequence.");
        trg.fire[id] = { ok: true, sequence: out.sequence, event_id: out.event_id };
      } catch (e) {
        trg.fire[id] = { ok: false, message: trgErr(e) };
      }
      // The test event is in the connection's stream: refresh the row (last event) and its events.
      await loadTriggers();
    }
    function confirmDeleteTrigger(id) {
      const row = (trg.rows || []).find((r) => r.connection_id === id);
      const tr = document.querySelector(`[data-trg-row="${CSS.escape(id)}"]`);
      if (!row || !tr) return;
      for (const old of Array.from(document.querySelectorAll("#triggers-table .row-confirm"))) old.remove();
      const confirmRow = document.createElement("tr");
      confirmRow.className = "row-confirm";
      const td = document.createElement("td");
      td.colSpan = trg.all ? 5 : 4;
      const box = document.createElement("div");
      box.className = "inline-confirm";
      box.setAttribute("role", "group");
      const span = document.createElement("span");
      span.textContent = triggerDeleteQuestion(row.label || row.connection_id);
      const yes = document.createElement("button");
      yes.type = "button";
      yes.className = "danger";
      yes.textContent = "Delete";
      const no = document.createElement("button");
      no.type = "button";
      no.className = "secondary";
      no.textContent = "Cancel";
      no.onclick = () => confirmRow.remove();
      yes.onclick = async () => {
        yes.disabled = true;
        try {
          await api(`/api/gateway/triggers/connections/${encodeURIComponent(id)}`, { method: "DELETE" });
          delete trg.expanded[id];
          delete trg.fire[id];
          trgMessage(`Deleted ${row.label || id}.`, "ok");
          await loadTriggers();
        } catch (e) {
          yes.disabled = false;
          trgMessage(`Not deleted: ${trgErr(e)}`, "error");
        }
      };
      box.append(span, yes, no);
      td.append(box);
      confirmRow.append(td);
      tr.after(confirmRow);
      try { no.focus(); } catch {}
    }
    function triggerDeleteQuestion(label) {
      return `Delete ${label}? Automations that listen to it stop receiving its events.`;
    }

    // ---- the create dialog: the form engine for a source's `connection_schema` (the JSON Schema
    // subset Flow renders for `config_schema`).
    const TRG_FORM_KEYWORDS = new Set(["type", "title", "description", "enum", "default", "pattern", "format", "minimum", "maximum", "minLength", "maxLength", "examples", "writeOnly"]);
    // Labels for the settings of the shipped sources whose schemas carry no `title`.
    const TRG_FIELD_LABELS = {
      subject_path: "Subject (path in the JSON body)",
      tolerance_s: "Accepted clock skew (seconds)",
      url: "Feed address",
      every: "Check every",
      max_items: "Items per check (at most)",
      initial_items: "When added",
    };
    function trgHumanize(key) {
      const words = String(key).replace(/_/g, " ").trim();
      return words ? words.charAt(0).toUpperCase() + words.slice(1) : String(key);
    }
    function trgSentence(text) {
      const t = String(text || "").trim();
      return t ? t.charAt(0).toUpperCase() + t.slice(1) : "";
    }
    function triggerFormFields(schema) {
      const props = schema && typeof schema.properties === "object" && schema.properties ? schema.properties : {};
      const required = new Set(Array.isArray(schema && schema.required) ? schema.required : []);
      return Object.keys(props).filter((key) => {
        // A secret the gateway generates (`x-secret`) is never typed here: it is shown once after Create.
        const p = props[key];
        return !(p && typeof p === "object" && p["x-secret"] === true);
      }).map((key) => {
        const p = props[key] && typeof props[key] === "object" ? props[key] : {};
        let kind = "readonly";
        if (!Object.keys(p).some((k) => !TRG_FORM_KEYWORDS.has(k) && !k.startsWith("x-"))) {
          if (Array.isArray(p.enum)) kind = "enum";
          else if (p.type === "string") kind = (p.format === "password" || p.writeOnly === true) ? "password" : "string";
          else if (p.type === "integer" || p.type === "number" || p.type === "boolean") kind = p.type;
        }
        const label = String(p.title || TRG_FIELD_LABELS[key] || trgHumanize(key));
        return { key, kind, label, description: trgSentence(p.description), required: required.has(key), schema: p };
      });
    }
    // UX #11/#12/#13: human controls (a number + unit for a duration, words for known choices), the
    // values kept across a refusal, each refusal beside its field in red, optional settings collapsed.
    const TRG_DURATION_UNITS = [["s", "seconds"], ["m", "minutes"], ["h", "hours"], ["d", "days"]];
    const TRG_ENUM_WORDS = { initial_items: { skip: "Ignore what is already there", deliver: "Also deliver the latest items" } };
    const TRG_MORE_FIELDS = new Set(["subject_path", "tolerance_s", "max_items"]);
    function trgIsDuration(f) {
      return f.kind === "string" && (f.schema.format === "duration" || f.schema.pattern === "^[1-9][0-9]*[smhd]$");
    }
    function trgFieldMarkup(f, values, errors) {
      const id = `trg-f-${f.key.replace(/[^A-Za-z0-9_-]/g, "-")}`;
      const label = `<label for="${id}">${esc(f.label)}${f.required ? " *" : ""}</label>`;
      const help = f.description ? `<p class="trg-help">${esc(f.description)}</p>` : "";
      const err = errors && errors[f.key] ? `<p class="trg-field-error" role="alert">${esc(errors[f.key])}</p>` : "";
      const dflt = f.schema.default;
      const has = values && values[f.key] !== undefined;
      const cur = has ? String(values[f.key]) : (dflt === undefined ? "" : (typeof dflt === "string" ? dflt : JSON.stringify(dflt)));
      let control;
      if (trgIsDuration(f)) {
        const m = /^([1-9][0-9]*)([smhd])$/.exec(cur) || [null, "", "m"];
        control = `<span class="trg-duration"><input id="${id}" type="number" min="1" step="1" data-trg-dur-n="${esc(f.key)}" value="${esc(m[1])}" aria-label="${esc(f.label)}">`
          + `<select data-trg-dur-u="${esc(f.key)}" aria-label="${esc(f.label)} unit">${TRG_DURATION_UNITS.map(([u, w]) => `<option value="${u}"${u === m[2] ? " selected" : ""}>${w}</option>`).join("")}</select></span>`;
      } else if (f.kind === "enum") {
        const words = TRG_ENUM_WORDS[f.key] || {};
        const sel = has ? cur : (dflt === undefined ? "" : JSON.stringify(dflt));
        control = `<select id="${id}" data-trg-field="${esc(f.key)}">${dflt === undefined ? `<option value="">(not set)</option>` : ""}`
          + f.schema.enum.map((v) => `<option value="${esc(JSON.stringify(v))}"${JSON.stringify(v) === sel ? " selected" : ""}>${esc(words[v] || (typeof v === "string" ? v : JSON.stringify(v)))}</option>`).join("")
          + `</select>`;
      } else if (f.kind === "boolean") {
        const b = has ? cur : (dflt === undefined ? "" : String(dflt));
        control = `<select id="${id}" data-trg-field="${esc(f.key)}"><option value="">(not set)</option><option value="true"${b === "true" ? " selected" : ""}>Yes</option><option value="false"${b === "false" ? " selected" : ""}>No</option></select>`;
      } else if (f.kind === "integer" || f.kind === "number") {
        control = `<input id="${id}" type="number" step="${f.kind === "integer" ? "1" : "any"}" data-trg-field="${esc(f.key)}" value="${esc(cur)}">`;
      } else if (f.kind === "string" || f.kind === "password") {
        const ex = Array.isArray(f.schema.examples) && f.schema.examples.length ? ` placeholder="${esc(f.schema.examples[0])}"` : "";
        const v = f.kind === "password" && !has ? "" : cur;
        control = `<input id="${id}" type="${f.kind === "password" ? "password" : "text"}" autocomplete="off" data-trg-field="${esc(f.key)}" value="${esc(v)}"${ex}${err ? ' aria-invalid="true"' : ""}>`;
      } else {
        control = `<pre class="trg-help">Not editable here: ${esc(JSON.stringify(f.schema))}</pre>`;
      }
      return `<div class="trg-field trg-field--wide">${label}${control}${err}${help}</div>`;
    }
    function triggerFormMarkup(fields, values, errors) {
      const main = fields.filter((f) => f.required || !TRG_MORE_FIELDS.has(f.key));
      const more = fields.filter((f) => !f.required && TRG_MORE_FIELDS.has(f.key));
      let html = main.map((f) => trgFieldMarkup(f, values, errors)).join("");
      if (more.length) {
        const open = more.some((f) => errors && errors[f.key]) ? " open" : "";
        html += `<details class="trg-more trg-field--wide"${open}><summary>More options</summary>${more.map((f) => trgFieldMarkup(f, values, errors)).join("")}</details>`;
      }
      return html;
    }
    // Form values -> the connection's config; returns {config, problems}. Empty = not set (the
    // source applies its default); the gateway validates the whole config again on create.
    function triggerConfigFromValues(fields, values) {
      const config = {};
      const problems = [];
      const byField = {};
      const problem = (f, text) => { problems.push(text); byField[f.key] = text; };
      for (const f of fields) {
        if (f.kind === "readonly") continue;
        const raw = String(values[f.key] === undefined ? "" : values[f.key]).trim();
        if (!raw) { if (f.required) problem(f, `${f.label} is required.`); continue; }
        if (f.kind === "integer" || f.kind === "number") {
          const n = Number(raw);
          if (!Number.isFinite(n) || (f.kind === "integer" && !Number.isInteger(n))) { problem(f, `${f.label} must be ${f.kind === "integer" ? "a whole number" : "a number"}.`); continue; }
          config[f.key] = n;
        } else if (f.kind === "boolean") config[f.key] = raw === "true";
        else if (f.kind === "enum") config[f.key] = JSON.parse(raw);
        else {
          if (typeof f.schema.pattern === "string" && !new RegExp(f.schema.pattern, "u").test(raw)) { problem(f, trgFieldHint(f) || `${f.label} does not have the expected form.`); continue; }
          config[f.key] = raw;
        }
      }
      return { config, problems, byField };
    }
    // The fix for a refused field, in words (UX #12).
    function trgFieldHint(f) {
      if (f.key === "url") return "Feed address: start with https:// (e.g. https://example.org/feed.xml)";
      if (trgIsDuration(f)) return `${f.label}: a whole number of seconds, minutes, hours or days.`;
      return "";
    }
    // A gateway refusal for one field (`detail.field` "config.url") → its sentence beside that field.
    function trgRefusalByField(e, fields) {
      const d = e && e.detail;
      const key = d && typeof d.field === "string" ? d.field.replace(/^(trigger\.)?config\./, "") : "";
      const f = (fields || []).find((x) => x.key === key);
      if (!f) return null;
      return { [f.key]: trgFieldHint(f) || `${f.label}: ${trgErr(e)}` };
    }
    function closeTriggerModal() {
      const backdrop = $("trigger-modal-backdrop");
      if (backdrop.hidden) return;
      backdrop.hidden = true;
      $("trigger-modal-body").textContent = "";
      const release = trg.release;
      trg.release = null;
      if (release) release();
      trg.modal = null;
    }
    function renderTriggerModal() {
      const m = trg.modal;
      const body = $("trigger-modal-body");
      const note = $("trigger-modal-note");
      const actions = $("trigger-modal-actions");
      if (m.created) {
        const c = m.created;
        $("trigger-modal-title").textContent = `Connection created — ${c.label || c.connection_id}`;
        let html = `<div class="trg-form">`;
        if (c.ingress_url) html += trgCopyRow("Webhook address", c.ingress_url, "created:ingress");
        if (c.secret) {
          html += `<p class="trg-once trg-field--wide">The signing secret is shown once. Copy it now: the gateway keeps it sealed and never shows it again.</p>`
            + trgCopyRow("Signing secret", c.secret, "created:secret");
        }
        html += `<p class="trg-next trg-field--wide">Next: in Flow, + → When something arrives → ${esc(c.label || c.connection_id)}.</p>`;
        if (c.ingress_url && c.secret) html += webhookSetupMarkup(c, trg.hookTest);
        if (!c.ingress_url && !c.secret) html += `<p class="trg-help trg-field--wide">Your automations and flows can now listen to it.</p>`;
        body.innerHTML = html + `</div>`;
        note.textContent = "";
        actions.innerHTML = `<button type="button" data-trg-done="1">Done</button>`;
        return;
      }
      $("trigger-modal-title").textContent = "New connection";
      const sources = m.sources;
      if (!sources.length) {
        body.innerHTML = `<p class="trg-empty">No trigger source on this gateway needs a connection.</p>`;
        actions.innerHTML = "";
        return;
      }
      const s = sources.find((x) => `${x.id}@${x.version}` === m.sourceKey) || null;
      const options = sources.map((x) => `<option value="${esc(`${x.id}@${x.version}`)}"${s && x === s ? " selected" : ""}>${esc(x.label || x.id)}</option>`).join("");
      let html = `<div class="trg-form">`
        + `<div class="trg-field"><label for="trg-f-source">What to listen to</label><select id="trg-f-source" data-trg-source><option value="">Choose…</option>${options}</select>${trgSourceHintsMarkup(sources, s)}</div>`
        + `<div class="trg-field"><label for="trg-f-label">Name *</label><input id="trg-f-label" type="text" autocomplete="off" data-trg-label value="${esc(m.label || "")}" placeholder="Release feed, Orders webhook…">${m.errors && m.errors.__name ? `<p class="trg-field-error" role="alert">${esc(m.errors.__name)}</p>` : ""}<p class="trg-help">How you find it in Flow and in your automations.</p></div>`;
      if (s) html += triggerFormMarkup(m.fields, m.values, m.errors);
      body.innerHTML = html + `</div>`;
      note.textContent = m.problem || "";
      actions.innerHTML = `<button type="button" class="secondary" data-trg-cancel="1">Cancel</button>`
        + (s ? `<button type="button" data-trg-create="1"${m.saving ? " disabled" : ""}>Create</button>` : "");
    }
    // UX #18: one line per option, what it listens to.
    const TRG_SOURCE_HINTS = {
      "feed.item": "Runs when a feed (RSS or Atom) publishes a new item.",
      "webhook.received": "Runs when another service (GitHub, Zapier…) calls an address you give it.",
    };
    function trgSourceHintsMarkup(sources, chosen) {
      if (chosen) return TRG_SOURCE_HINTS[chosen.id] ? `<p class="trg-help">${esc(TRG_SOURCE_HINTS[chosen.id])}</p>` : "";
      const lines = sources.filter((x) => TRG_SOURCE_HINTS[x.id]).map((x) => `<li><strong>${esc(x.label || x.id)}</strong> — ${esc(TRG_SOURCE_HINTS[x.id])}</li>`).join("");
      return lines ? `<ul class="trg-hints">${lines}</ul>` : "";
    }
    async function openTriggerModal(sourceId) {
      closeTriggerModal();
      const backdrop = $("trigger-modal-backdrop");
      $("trigger-modal-title").textContent = "New connection";
      $("trigger-modal-body").innerHTML = `<p class="trg-empty">Reading the trigger sources...</p>`;
      $("trigger-modal-note").textContent = "";
      $("trigger-modal-actions").textContent = "";
      backdrop.hidden = false;
      trg.release = bindAccountModal(backdrop, closeTriggerModal);
      if (!trg.sources) await loadTriggerSources();
      let sources;
      try {
        if (trg.sourcesError) throw new Error(`Could not read the trigger sources: ${trg.sourcesError}`);
        sources = trgConnectableSources(trg.sources);
      } catch (e) {
        $("trigger-modal-body").innerHTML = `<p class="message error">${esc(trgErr(e))}</p>`;
        return;
      }
      const pick = sources.find((x) => x.id === sourceId || `${x.id}@${x.version}` === sourceId) || (sources.length === 1 ? sources[0] : null);
      trg.hookTest = null;
      trg.modal = { sources, sourceKey: pick ? `${pick.id}@${pick.version}` : "", fields: pick ? triggerFormFields(pick.connection_schema) : [], label: "", problem: "", saving: false, created: null };
      renderTriggerModal();
    }
    function trgModalValues() {
      const values = {};
      for (const el of Array.from(document.querySelectorAll("#trigger-modal-body [data-trg-field]"))) values[el.dataset.trgField] = el.value;
      for (const el of Array.from(document.querySelectorAll("#trigger-modal-body [data-trg-dur-n]"))) {
        const unit = document.querySelector(`#trigger-modal-body [data-trg-dur-u="${CSS.escape(el.dataset.trgDurN)}"]`);
        const n = String(el.value || "").trim();
        values[el.dataset.trgDurN] = n ? `${n}${unit ? unit.value : "m"}` : "";
      }
      return values;
    }
    async function createTriggerConnection() {
      const m = trg.modal;
      if (!m || m.saving) return;
      const s = m.sources.find((x) => `${x.id}@${x.version}` === m.sourceKey);
      if (!s) return;
      m.label = String($("trg-f-label").value || "").trim();
      m.values = trgModalValues();
      const { config, byField } = triggerConfigFromValues(m.fields, m.values);
      if (!m.label) byField.__name = "Name is required.";
      m.errors = byField;
      m.problem = "";
      if (Object.keys(byField).length) { renderTriggerModal(); return; }
      m.saving = true;
      $("trigger-modal-note").textContent = "Creating...";
      try {
        const created = await api("/api/gateway/triggers/connections", { method: "POST", body: JSON.stringify({ source_id: s.id, source_version: s.version, label: m.label, config }) });
        if (!created || !created.connection_id) throw new Error("the gateway's answer has no connection_id.");
        m.created = created;
        m.saving = false;
        renderTriggerModal();
        await loadTriggers();
      } catch (e) {
        m.saving = false;
        const one = trgRefusalByField(e, m.fields);
        if (one) { m.errors = one; m.problem = ""; }
        else m.problem = `Not created: ${trgErr(e)}`;
        renderTriggerModal();
      }
    }
    // ---- the webhook setup panel (after Create): a signed call with the REAL address and secret, the
    // GitHub / Zapier recipe, and a signed test call made in the browser (WebCrypto HMAC-SHA256).
    function webhookCurlExample(ingressUrl, secret) {
      return [
        `SECRET='${secret}'`,
        `URL='${ingressUrl}'`,
        `KEY=$(printf %s "\${SECRET#whsec_}" | base64 -d | xxd -p -c 256)`,
        `ID=msg_$(date +%s); TS=$(date +%s); BODY='{"hello": "world"}'`,
        `SIG=$(printf %s "$ID.$TS.$BODY" | openssl dgst -sha256 -mac HMAC -macopt hexkey:$KEY -binary | base64)`,
        `curl -sS -X POST "$URL" -H "webhook-id: $ID" -H "webhook-timestamp: $TS" \\`,
        `  -H "webhook-signature: v1,$SIG" -H "Content-Type: application/json" --data "$BODY"`,
      ].join("\n");
    }
    function webhookTestResultMarkup(t) {
      if (!t) return "";
      if (t.pending) return `<p class="trg-help" role="status">Sending a signed test call...</p>`;
      if (t.error) return `<p class="message error" role="status">${esc(t.error)}</p>`;
      const ok = t.status >= 200 && t.status < 300;
      const name = t.name || "The webhook";
      if (ok) {
        let html = `<p class="trg-fire is-ok" role="status">It worked: ${esc(name)} received the call · see it under Events.</p>`;
        if (t.event) html += `<details><summary>Details</summary><pre class="trg-code">${esc(JSON.stringify(t.event, null, 2))}</pre></details>`;
        return html;
      }
      return `<p class="trg-fire is-failed" role="status">The call was refused: ${esc(webhookRefusalWords(t.reason))} <span class="trg-sub" style="display:inline">(HTTP ${esc(t.status)})</span></p>`;
    }
    // Why the ingress refused a call, in words (the gateway's reason codes).
    function webhookRefusalWords(reason) {
      const r = String(reason || "");
      if (r.includes("signature")) return "the signature did not match the signing secret.";
      if (r.includes("unsigned")) return "the call was not signed.";
      if (r.includes("stale")) return "its time stamp is too far from now.";
      if (r.includes("replayed")) return "this call was already received.";
      if (r.includes("disabled") || r.includes("paused")) return "the connection is paused (turn Listening on).";
      return r || "the gateway did not say why.";
    }
    // UX #15: an address only this computer (or its network) can reach.
    function trgLocalAddress(url) {
      let host = "";
      try { host = new URL(String(url)).hostname.toLowerCase().replace(/^\[|\]$/g, ""); } catch { return false; }
      if (host === "localhost" || host === "::1" || host === "0.0.0.0" || host.endsWith(".local") || host.endsWith(".localhost")) return true;
      const m = /^(\d+)\.(\d+)\.\d+\.\d+$/.exec(host);
      if (!m) return false;
      const a = Number(m[1]), b = Number(m[2]);
      return a === 127 || a === 10 || (a === 192 && b === 168) || (a === 172 && b >= 16 && b <= 31) || (a === 169 && b === 254);
    }
    function webhookSetupMarkup(c, test) {
      const local = trgLocalAddress(c.ingress_url);
      return `<section class="trg-field trg-field--wide trg-setup" aria-labelledby="trg-setup-title">`
        + `<h3 id="trg-setup-title" class="trg-detail__head">Set up the sender</h3>`
        + (local
          ? `<p class="trg-warn-line" role="status">GitHub can't reach this computer yet. Turn on internet access under Network first. <button type="button" class="link-button" data-trg-open-network="1">Open Network</button></p>`
          : `<p class="trg-help">GitHub, Zapier and other services: paste the webhook address as the webhook URL, the signing secret as the secret, and choose JSON as the content type.</p>`)
        + `<p class="trg-help">Any other sender signs each call per Standard Webhooks. From a shell (bash, openssl):</p>`
        + `<div class="trg-copy-row"><pre class="trg-code" data-trg-value="created:curl">${esc(webhookCurlExample(c.ingress_url, c.secret))}</pre>`
        + `<button type="button" class="secondary" data-trg-copy="created:curl" aria-label="Copy the signed call">Copy</button></div>`
        + `<p><button type="button" class="secondary" data-trg-hook-test="1">Send a signed test call</button></p>`
        + `<div data-trg-hook-result>${webhookTestResultMarkup(test)}</div>`
        + `</section>`;
    }
    function trgB64ToBytes(b64) {
      const bin = atob(b64);
      const out = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
      return out;
    }
    function trgBytesToB64(bytes) {
      let bin = "";
      for (const b of new Uint8Array(bytes)) bin += String.fromCharCode(b);
      return btoa(bin);
    }
    // The Standard Webhooks signature of one call: base64 HMAC-SHA256 of "<id>.<timestamp>.<body>",
    // keyed with the secret's base64 part.
    async function webhookSignature(secret, id, ts, body) {
      const subtle = typeof crypto !== "undefined" && crypto && crypto.subtle;
      if (!subtle) throw new Error("This browser cannot sign here (signing needs https or localhost): use the command above.");
      const key = await subtle.importKey("raw", trgB64ToBytes(String(secret).replace(/^whsec_/, "")), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
      const sig = await subtle.sign("HMAC", key, new TextEncoder().encode(`${id}.${ts}.${body}`));
      return `v1,${trgBytesToB64(sig)}`;
    }
    function trgIngressPath(url) {
      try { return new URL(String(url), location.href).pathname; } catch { return String(url); }
    }
    async function sendSignedTestCall() {
      const c = trg.modal && trg.modal.created;
      if (!c || !c.ingress_url || !c.secret) return;
      const box = () => document.querySelector("[data-trg-hook-result]");
      const show = () => { const el = box(); if (el) el.innerHTML = webhookTestResultMarkup(trg.hookTest); };
      trg.hookTest = { pending: true };
      show();
      try {
        const ts = Math.floor(Date.now() / 1000);
        const id = `msg_console_${ts}_${Math.random().toString(36).slice(2, 8)}`;
        const body = JSON.stringify({ test: true, from: "console", sent_at: new Date(ts * 1000).toISOString() });
        const signature = await webhookSignature(c.secret, id, ts, body);
        const res = await fetch(trgIngressPath(c.ingress_url), {
          method: "POST", credentials: "omit",
          headers: { "Content-Type": "application/json", "webhook-id": id, "webhook-timestamp": String(ts), "webhook-signature": signature },
          body,
        });
        const answer = await res.json().catch(() => ({}));
        const detail = answer && answer.detail;
        const reason = (detail && (detail.reason_code || detail.message)) || "";
        let event = null;
        if (res.ok) {
          const out = await api(`/api/gateway/triggers/connections/${encodeURIComponent(c.connection_id)}/events?limit=1`);
          event = out && Array.isArray(out.events) && out.events.length ? out.events[out.events.length - 1] : null;
        }
        trg.hookTest = { status: res.status, reason, event, name: c.label || c.connection_id };
        loadTriggers();
      } catch (e) {
        trg.hookTest = { error: `Test call not sent: ${trgErr(e)}` };
      }
      show();
    }
    async function trgCopy(key) {
      const el = document.querySelector(`[data-trg-value="${CSS.escape(key)}"]`);
      if (!el) return;
      const ok = await clipboardWrite(el.textContent || "");
      uiToast(ok ? "Copied" : "Copy failed — select and copy");
    }
    // `#triggers?new=<source_id>` (Flow's "New connection…" link): open the dialog on that source.
    function triggersNewFromHash() {
      const h = String(location.hash || "");
      const m = /^#triggers\?(.*)$/.exec(h);
      if (!m) return "";
      return String(new URLSearchParams(m[1]).get("new") || "").trim();
    }
    function bindTriggersPage() {
      $("tab-button-triggers").onclick = () => { setActiveTab("triggers"); };
      $("triggers-new").onclick = () => openTriggerModal("");
      $("triggers-refresh").onclick = () => loadTriggers();
      $("triggers-table").onclick = (e) => {
        const t = e.target && e.target.closest ? e.target.closest("button") : null;
        if (!t) return;
        if (t.dataset.trgEvents) toggleTriggerEvents(t.dataset.trgEvents);
        else if (t.dataset.trgTest) testFireTrigger(t.dataset.trgTest);
        else if (t.dataset.trgDelete) confirmDeleteTrigger(t.dataset.trgDelete);
        else if (t.dataset.trgCopy) trgCopy(t.dataset.trgCopy);
        else if (t.dataset.trgMailboxManage) openMyEmailFromTriggers();
        else if (t.dataset.trgEmpty === "mailbox") { mbx.openAddOnLoad = true; openMyEmailFromTriggers(); }
        else if (t.dataset.trgEmpty) openTriggerModal(t.dataset.trgEmpty);
        else if (t.dataset.trgAlways) {
          const row = (trg.rows || []).find((x) => x.connection_id === t.dataset.trgAlways);
          if (row) { t.setAttribute("aria-busy", "true"); setTriggerAlwaysListen(row.connection_id, !row.always_listen); }
        }
        else if (t.dataset.trgMailboxUse) {
          const m = (trg.mailboxes || []).find((x) => x.account_id === t.dataset.trgMailboxUse);
          if (m) { t.setAttribute("aria-busy", "true"); setMailboxInUse(m.account_id, !m.enabled); }
        }
        else if (t.dataset.trgListen) {
          const row = (trg.rows || []).find((x) => x.connection_id === t.dataset.trgListen);
          if (row) { t.setAttribute("aria-busy", "true"); setTriggerPaused(row.connection_id, !row.paused); }
        }
      };
      $("trigger-modal-close").onclick = closeTriggerModal;
      $("trigger-modal-body").onchange = (e) => {
        const t = e.target;
        if (!t || !trg.modal || t.dataset.trgSource === undefined) return;
        trg.modal.label = String($("trg-f-label").value || "");
        trg.modal.sourceKey = t.value;
        const s = trg.modal.sources.find((x) => `${x.id}@${x.version}` === t.value);
        trg.modal.fields = s ? triggerFormFields(s.connection_schema) : [];
        trg.modal.values = {};
        trg.modal.errors = {};
        trg.modal.problem = "";
        renderTriggerModal();
      };
      // A field being corrected loses its refusal (UX #12: the text stays, the red goes once you edit it).
      $("trigger-modal-body").oninput = (e) => {
        const t = e.target;
        const key = t && t.dataset ? (t.dataset.trgField || t.dataset.trgDurN || (t.dataset.trgLabel !== undefined ? "__name" : "")) : "";
        if (!key || !trg.modal || !trg.modal.errors || !trg.modal.errors[key]) return;
        delete trg.modal.errors[key];
        t.removeAttribute("aria-invalid");
        const box = t.closest ? t.closest(".trg-field") : null;
        const msg = box && box.querySelector(".trg-field-error");
        if (msg) msg.remove();
      };
      $("trigger-modal-body").onclick = (e) => {
        const t = e.target && e.target.closest ? e.target.closest("button") : null;
        if (t && t.dataset.trgCopy) trgCopy(t.dataset.trgCopy);
        else if (t && t.dataset.trgHookTest) sendSignedTestCall();
        else if (t && t.dataset.trgOpenNetwork) { closeTriggerModal(); $("tab-button-network").click(); }
      };
      $("trigger-modal-actions").onclick = (e) => {
        const t = e.target && e.target.closest ? e.target.closest("button") : null;
        if (!t) return;
        if (t.dataset.trgCreate) createTriggerConnection();
        else if (t.dataset.trgCancel) closeTriggerModal();
        else if (t.dataset.trgDone) closeTriggerModal();
      };
    }
"""
