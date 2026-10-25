"""My email → Notifications → **Devices** (framework backlog 1049): the watches and iPhones that get
alerts from this gateway while their app is closed.

Read-only and nothing to configure: an app registers itself when the person signs in (and the
system's notification permission is the switch). One row per device from
`GET /api/gateway/me/push/devices`: the app (Apple Watch | iPhone), its name, when it last
registered, the last alert sent, and the last error in plain words. Your own page only (an
entity's email page hides the card). Spliced into the console script scope: `api`, `esc`, `$`,
`emailAgo`, `emailErrorText`, `mbxOwnPage` are the console's own.
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
"""

PUSH_DEVICES_HTML = r"""
              <section id="my-push-devices" class="af-card account-card" aria-labelledby="my-push-devices-title">
                <div class="af-card__header"><h3 id="my-push-devices-title" class="af-card__title">Devices</h3></div>
                <p class="af-form__help">Your Apple Watch and iPhone get alerts here while the app is closed. Nothing to set up: sign in on the device and allow notifications.</p>
                <ul id="my-push-devices-list" class="push-device-list" aria-labelledby="my-push-devices-title"></ul>
                <p id="my-push-devices-state" class="inline-state" role="status" aria-live="polite" hidden></p>
              </section>
"""

__all__ = ["PUSH_DEVICES_CSS", "PUSH_DEVICES_HTML", "PUSH_DEVICES_JS"]
