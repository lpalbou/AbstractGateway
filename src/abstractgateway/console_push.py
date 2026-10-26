"""My email → Notifications → **Devices** (framework backlog 1049): the watches and iPhones that get
alerts from this gateway while their app is closed.

Read-only and nothing to configure: an app registers itself when the person signs in (and the
system's notification permission is the switch). One row per device from
`GET /api/gateway/me/push/devices`: the app (Apple Watch | iPhone), its name, when it last
registered, the last alert sent, and the last error in plain words. Your own page only (an
entity's email page hides the card). Spliced into the console script scope: `api`, `esc`, `$`,
`emailAgo`, `emailErrorText`, `mbxOwnPage`, `state` are the console's own.

Beside it, admins only, **Push forwarder**: one row with the gateway-wide address in words
("Built-in forwarder" | "Off" | the URL, plus "set by the launch flag for this run" when
`--push-relay` wins) and an Edit offering the three choices (`GET|PUT /api/gateway/push/settings`);
a save says what the forwarder answered (its apps) or why it could not be reached.
"""

from __future__ import annotations

PUSH_DEVICES_CSS = r"""
/* ---- My email → Notifications → Devices (console_push.py) ---- */
.push-device-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.push-device { display: flex; flex-direction: column; gap: 2px; padding: 8px 0; border-top: 1px solid var(--line-soft); }
.push-device:first-child { border-top: 0; padding-top: 0; }
.push-device__name { font-weight: 600; overflow-wrap: anywhere; }
.push-device__meta { color: var(--muted); font-size: var(--font-size-sm); }
.push-device__error { color: var(--warn, var(--danger)); font-size: var(--font-size-sm); overflow-wrap: anywhere; }
.push-forwarder__row { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; margin: 0; }
.push-forwarder__label { font-weight: 600; }
.push-forwarder__value { overflow-wrap: anywhere; }
.push-forwarder__form { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }
.push-forwarder__form select, .push-forwarder__form input { max-width: 100%; }
.push-forwarder__actions { display: flex; gap: 8px; flex-wrap: wrap; }
"""

PUSH_DEVICES_JS = r"""
    // My email → Notifications → Devices (console_push.py): GET /api/gateway/me/push/devices.
    const PUSH_APP_WORDS = { watch: "Apple Watch", phone: "iPhone" };
    function pushDeviceRow(d) {
      if (!d || typeof d.device_id !== "string" || !(d.app in PUSH_APP_WORDS)) throw new Error("GET /me/push/devices: a row without device_id or a known app (gateway seam).");
      const app = PUSH_APP_WORDS[d.app];
      const name = d.name ? `${d.name} · ${app}` : app;
      const meta = [d.refreshed_at ? `Registered ${emailAgo(d.refreshed_at)}` : "", d.last_sent_at ? `last alert ${emailAgo(d.last_sent_at)}` : "no alert yet"].filter(Boolean).join(" · ");
      const err = d.last_error ? `<span class="push-device__error">${esc(d.last_error)}</span>` : "";
      return `<li class="push-device" data-device="${esc(d.device_id)}"><span class="push-device__name">${esc(name)}</span><span class="push-device__meta">${esc(meta)}</span>${err}</li>`;
    }
    function renderMyPushDevices(rows, error) {
      const list = $("my-push-devices-list"), note = $("my-push-devices-state");
      if (!list || !note) throw new Error("The account email UI has no Devices card (console_push.py).");
      if (error) { list.innerHTML = ""; note.textContent = `Could not list your devices: ${error}`; note.hidden = false; return; }
      if (rows === null) { list.innerHTML = ""; note.textContent = "Reading your devices…"; note.hidden = false; return; }
      list.innerHTML = rows.map(pushDeviceRow).join("");
      note.textContent = rows.length ? "" : "No watch or iPhone yet. Sign in to the AbstractAssistant app on one and allow notifications.";
      note.hidden = !note.textContent;
    }
    async function loadMyPushDevices() {
      const card = $("my-push-devices");
      if (!card) throw new Error("The account email UI has no #my-push-devices card (console_push.py).");
      card.hidden = !mbxOwnPage();  // an entity's page: its devices are not yours to see
      if (card.hidden) return;
      renderMyPushDevices(null, "");
      let rows;
      try {
        rows = await api("/api/gateway/me/push/devices");
        if (!Array.isArray(rows)) throw new Error("the gateway's answer is not a list.");
      } catch (e) {
        renderMyPushDevices([], emailErrorText(e));
        return;
      }
      try { renderMyPushDevices(rows, ""); } catch (e) { consoleSeamFailure(e); }
    }

    // Push forwarder (admins; gateway-wide): GET|PUT /api/gateway/push/settings.
    const PUSH_FORWARDER_FLAG = "set by the launch flag for this run";
    const pushForwarderUi = { saved: null, busy: false };
    function pushForwarderWords(f) {
      if (!f || !["default", "off", "url"].includes(f.mode) || !["flag", "setting", "packaged"].includes(f.source)) {
        throw new Error("GET /push/settings: no forwarder {mode, source} (gateway seam).");
      }
      if (f.source === "flag") return { value: f.effective || "Off", note: PUSH_FORWARDER_FLAG };
      if (f.mode === "off") return { value: "Off", note: "" };
      if (f.mode === "url") return { value: f.url, note: "" };
      return { value: "Built-in forwarder", note: f.effective ? "" : "none in this release yet, so no alerts are sent" };
    }
    function savedChoiceWords(f) {
      return f.mode === "url" ? f.url : (f.mode === "off" ? "Off" : "Built-in forwarder");
    }
    function renderPushForwarder(f) {
      const words = pushForwarderWords(f);
      $("push-forwarder-value").textContent = words.value;
      $("push-forwarder-note").textContent = words.note ? `· ${words.note}` : "";
      $("push-forwarder-note").hidden = !words.note;
      const later = $("push-forwarder-saved");
      later.textContent = f.source === "flag" ? `Your saved choice (${savedChoiceWords(f)}) applies when the gateway starts without the flag.` : "";
      later.hidden = !later.textContent;
    }
    function pushForwarderModeChanged() {
      const url = $("push-forwarder-mode").value === "url";
      $("push-forwarder-url-field").hidden = !url;
    }
    function openPushForwarderEdit() {
      const f = pushForwarderUi.saved;
      if (!f) return;
      $("push-forwarder-mode").value = f.mode;
      $("push-forwarder-url").value = f.url || "";
      $("push-forwarder-error").hidden = true;
      $("push-forwarder-result").hidden = true;
      pushForwarderModeChanged();
      $("push-forwarder-form").hidden = false;
      $("push-forwarder-edit").hidden = true;
    }
    function closePushForwarderEdit() {
      $("push-forwarder-form").hidden = true;
      $("push-forwarder-edit").hidden = false;
    }
    function pushProbeWords(probe) {
      if (!probe) return "Saved.";
      if (probe.reachable) {
        const apps = (probe.topics || []).join(", ");
        return apps ? `Saved. The forwarder answered: it pushes to ${apps}.` : "Saved. The forwarder answered.";
      }
      return `Saved, but the forwarder could not be reached: ${probe.reason}`;
    }
    async function savePushForwarder() {
      if (pushForwarderUi.busy) return;
      const mode = $("push-forwarder-mode").value;
      const body = { mode };
      if (mode === "url") body.url = String($("push-forwarder-url").value || "").trim();
      const err = $("push-forwarder-error"), result = $("push-forwarder-result"), save = $("push-forwarder-save");
      err.hidden = true;
      pushForwarderUi.busy = true; save.disabled = true; save.textContent = "Saving…";
      try {
        const out = await api("/api/gateway/push/settings", { method: "PUT", body: JSON.stringify(body), slow: true });
        pushForwarderUi.saved = out.forwarder;
        renderPushForwarder(out.forwarder);
        closePushForwarderEdit();
        result.textContent = pushProbeWords(out.probe);
        result.hidden = false;
      } catch (e) {
        err.textContent = emailErrorText(e);
        err.hidden = false;
      } finally {
        pushForwarderUi.busy = false; save.disabled = false; save.textContent = "Save";
      }
    }
    async function loadPushForwarder() {
      const card = $("push-forwarder");
      if (!card) throw new Error("The account email UI has no #push-forwarder card (console_push.py).");
      card.hidden = !(mbxOwnPage() && state.principal && state.principal.admin === true);
      if (card.hidden) return;
      $("push-forwarder-edit").onclick = openPushForwarderEdit;
      $("push-forwarder-cancel").onclick = closePushForwarderEdit;
      $("push-forwarder-save").onclick = savePushForwarder;
      $("push-forwarder-mode").onchange = pushForwarderModeChanged;
      $("push-forwarder-value").textContent = "Reading…";
      $("push-forwarder-note").hidden = true;
      let out;
      try {
        out = await api("/api/gateway/push/settings");
      } catch (e) {
        $("push-forwarder-value").textContent = `Could not read it: ${emailErrorText(e)}`;
        $("push-forwarder-edit").hidden = true;
        return;
      }
      $("push-forwarder-edit").hidden = false;
      try { pushForwarderUi.saved = out.forwarder; renderPushForwarder(out.forwarder); } catch (e) { consoleSeamFailure(e); }
    }
"""

PUSH_DEVICES_HTML = r"""
              <section id="my-push-devices" class="af-card account-card" aria-labelledby="my-push-devices-title">
                <div class="af-card__header"><h3 id="my-push-devices-title" class="af-card__title">Devices</h3></div>
                <p class="af-form__help">Your Apple Watch and iPhone get alerts here while the app is closed. Nothing to set up: sign in on the device and allow notifications.</p>
                <ul id="my-push-devices-list" class="push-device-list" aria-labelledby="my-push-devices-title"></ul>
                <p id="my-push-devices-state" class="inline-state" role="status" aria-live="polite" hidden></p>
              </section>
              <section id="push-forwarder" class="af-card account-card" aria-labelledby="push-forwarder-title" hidden>
                <div class="af-card__header"><h3 id="push-forwarder-title" class="af-card__title">Push forwarder</h3></div>
                <p class="af-form__help">For the whole gateway (admins): where alerts for watches and iPhones go. Nothing to set unless you run your own forwarder.</p>
                <p class="push-forwarder__row"><span id="push-forwarder-value" class="push-forwarder__value"></span><span id="push-forwarder-note" class="muted" hidden></span><button id="push-forwarder-edit" class="link-button" type="button" aria-label="Edit the push forwarder">Edit</button></p>
                <p id="push-forwarder-saved" class="af-form__help" hidden></p>
                <div id="push-forwarder-form" class="push-forwarder__form" hidden>
                  <label class="af-form__label" for="push-forwarder-mode">Push forwarder</label>
                  <select id="push-forwarder-mode">
                    <option value="default">Built-in forwarder</option>
                    <option value="off">Off</option>
                    <option value="url">Your own forwarder</option>
                  </select>
                  <div id="push-forwarder-url-field" class="af-form__field" hidden>
                    <label class="af-form__label" for="push-forwarder-url">Its address</label>
                    <input id="push-forwarder-url" type="url" autocomplete="off" spellcheck="false" placeholder="https://push.example.org" aria-describedby="push-forwarder-url-help push-forwarder-error">
                    <p id="push-forwarder-url-help" class="af-form__help">https://, or http:// for a forwarder on this computer (e.g. http://127.0.0.1:8095).</p>
                  </div>
                  <p id="push-forwarder-error" class="af-form__error" role="alert" hidden></p>
                  <div class="push-forwarder__actions"><button id="push-forwarder-save" type="button">Save</button><button id="push-forwarder-cancel" class="secondary" type="button">Cancel</button></div>
                </div>
                <p id="push-forwarder-result" class="inline-state" role="status" aria-live="polite" hidden></p>
              </section>
"""

__all__ = ["PUSH_DEVICES_CSS", "PUSH_DEVICES_HTML", "PUSH_DEVICES_JS"]
