from __future__ import annotations


def gateway_console_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AbstractGateway Console</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #080b12;
      --panel: #111827;
      --panel-2: #0c1220;
      --panel-3: #172033;
      --line: #2b3a55;
      --line-soft: rgba(148, 163, 184, .16);
      --text: #f3f6fc;
      --muted: #aab6ca;
      --subtle: #77849a;
      --accent: #2dd4bf;
      --accent-2: #38bdf8;
      --danger: #f43f5e;
      --danger-2: #a92645;
      --ok: #34d399;
      --warn: #f59e0b;
      --button: #2563eb;
      --button-2: #25324a;
      --shadow: 0 18px 48px rgba(0, 0, 0, .26);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      min-height: 62px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 0 22px;
      border-bottom: 1px solid var(--line-soft);
      background: rgba(8, 11, 18, .92);
      backdrop-filter: blur(14px);
      position: sticky;
      top: 0;
      z-index: 4;
    }
    h1 { font-size: 17px; margin: 0; letter-spacing: -.01em; }
    h2 { font-size: 15px; margin: 0; letter-spacing: -.01em; }
    p { margin: 0; }
    main { max-width: 1440px; margin: 0 auto; padding: 22px; }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand-mark {
      width: 32px;
      height: 32px;
      display: grid;
      place-items: center;
      border: 1px solid rgba(45, 212, 191, .34);
      border-radius: 8px;
      color: var(--accent);
      background: rgba(45, 212, 191, .08);
      font-weight: 900;
    }
    .brand-subtitle { color: var(--subtle); font-size: 12px; margin-top: 1px; }
    .status {
      display: flex;
      align-items: center;
      gap: 10px;
      color: var(--muted);
      min-width: 0;
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--warn); display: inline-block; }
    .dot.ok { background: var(--ok); }
    .dot.bad { background: var(--danger); }
    .layout { display: grid; grid-template-columns: minmax(320px, 420px) minmax(0, 1fr); gap: 18px; align-items: start; }
    .stack { display: grid; gap: 16px; }
    section {
      border: 1px solid var(--line-soft);
      background: linear-gradient(180deg, rgba(17, 24, 39, .98), rgba(13, 19, 32, .98));
      border-radius: 8px;
      padding: 16px;
      box-shadow: var(--shadow);
    }
    .section-head { display: flex; align-items: start; justify-content: space-between; gap: 14px; margin-bottom: 16px; }
    .section-title { display: flex; align-items: center; gap: 9px; }
    .section-icon {
      width: 28px;
      height: 28px;
      display: inline-grid;
      place-items: center;
      border: 1px solid var(--line-soft);
      border-radius: 8px;
      color: var(--accent);
      background: rgba(45, 212, 191, .08);
      font-size: 14px;
      flex: 0 0 auto;
    }
    .section-note { color: var(--muted); font-size: 12px; max-width: 620px; }
    label {
      display: grid;
      gap: 6px;
      margin-bottom: 12px;
      color: var(--muted);
      font-weight: 800;
      text-transform: uppercase;
      font-size: 11px;
      letter-spacing: .04em;
    }
    input, select, textarea {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-2);
      color: var(--text);
      padding: 8px 10px;
      font: inherit;
    }
    textarea { min-height: 76px; resize: vertical; }
    input[type="checkbox"] { width: auto; min-height: auto; }
    input:focus, select:focus, textarea:focus { outline: 2px solid rgba(32, 199, 223, .65); outline-offset: 0; }
    .field-help { color: var(--subtle); font-size: 12px; line-height: 1.35; text-transform: none; letter-spacing: 0; font-weight: 600; }
    .inline { display: flex; gap: 10px; align-items: end; flex-wrap: wrap; }
    .inline > label { flex: 1 1 150px; }
    button {
      border: 0;
      border-radius: 6px;
      min-height: 36px;
      padding: 8px 12px;
      background: var(--button);
      color: white;
      font-weight: 800;
      cursor: pointer;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 7px;
    }
    button.secondary { background: var(--button-2); }
    button.danger { background: var(--danger-2); }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .button-icon { font-size: 14px; line-height: 1; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid var(--line); padding: 10px 8px; text-align: left; vertical-align: top; }
    th { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
    tbody tr:hover { background: rgba(255, 255, 255, .025); }
    code { background: #101629; border: 1px solid var(--line); border-radius: 6px; padding: 2px 6px; }
    .muted { color: var(--muted); }
    .message { margin-top: 12px; color: var(--muted); overflow-wrap: anywhere; }
    .message.error { color: #ff9caf; }
    .message.ok { color: var(--ok); }
    .issued { margin: 12px 0; border: 1px solid rgba(32, 199, 223, .45); background: rgba(32, 199, 223, .08); border-radius: 8px; padding: 10px; overflow-wrap: anywhere; }
	    .hidden { display: none !important; }
	    body:not(.signed-in) main.layout {
	      min-height: calc(100vh - 61px);
	      grid-template-columns: minmax(320px, 900px);
	      justify-content: center;
	      align-content: center;
	      align-items: center;
	      padding-block: clamp(24px, 7vh, 72px);
	    }
	    body:not(.signed-in) .session-only { display: none !important; }
	    body.signed-in #login-section { display: none !important; }
	    .pill { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; color: var(--muted); margin: 0 6px 6px 0; }
	    .badge, .state-pill { display: inline-flex; align-items: center; border: 1px solid var(--line); border-radius: 999px; padding: 2px 8px; color: var(--muted); font-size: 12px; }
	    .state-pill.ok { color: var(--ok); border-color: rgba(52, 211, 153, .34); background: rgba(52, 211, 153, .08); }
	    .state-pill.off { color: var(--warn); border-color: rgba(245, 158, 11, .35); background: rgba(245, 158, 11, .08); }
	    .account-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
	    .metric {
	      border: 1px solid var(--line-soft);
	      border-radius: 8px;
	      padding: 10px;
	      background: rgba(255, 255, 255, .025);
	      min-width: 0;
	    }
	    .metric span { display: block; color: var(--subtle); font-size: 11px; font-weight: 800; text-transform: uppercase; letter-spacing: .04em; }
	    .metric strong { display: block; margin-top: 4px; overflow-wrap: anywhere; }
    .actions { display: flex; gap: 6px; flex-wrap: wrap; }
    .actions select { width: auto; min-width: 150px; }
    .empty { color: var(--muted); padding: 12px 8px; }
    .danger-text { color: #ff9caf; }
	    .model-picker { display: grid; gap: 8px; }
	    .model-picker__head {
	      display: flex;
	      justify-content: space-between;
	      align-items: center;
	      gap: 10px;
	      flex-wrap: wrap;
	    }
	    .model-picker__actions { display: flex; gap: 8px; flex-wrap: wrap; }
	    select.model-picker__select { min-height: 150px; }
	    .model-picker__note { color: var(--muted); font-size: 12px; line-height: 1.4; text-transform: none; letter-spacing: 0; }
	    .model-summary {
	      border: 1px solid var(--line);
	      border-radius: 8px;
	      padding: 10px 12px;
	      background: rgba(255, 255, 255, .025);
	      color: var(--muted);
	    }
	    .advanced-panel {
	      border: 1px solid var(--line-soft);
	      border-radius: 8px;
	      padding: 0;
	      background: rgba(0, 0, 0, .12);
	    }
	    .advanced-panel summary {
	      cursor: pointer;
	      padding: 10px 12px;
	      color: var(--muted);
	      font-weight: 800;
	      list-style-position: inside;
	    }
	    .advanced-panel__body { padding: 0 12px 12px; display: grid; gap: 10px; }
	    .af-gateway-signin {
	      width: min(900px, 100%);
	      border: 1px solid rgba(32, 199, 223, .24);
	      border-radius: 8px;
	      padding: 24px;
	      background:
	        linear-gradient(135deg, rgba(18, 31, 61, .98), rgba(22, 20, 42, .98)),
	        var(--panel);
	      box-shadow: 0 28px 90px rgba(0, 0, 0, .45);
	    }
	    .af-gateway-signin h2 { margin: 0; font-size: 24px; line-height: 1.25; }
	    .af-gateway-signin p { margin: 14px 0 0; color: var(--muted); font-size: 18px; line-height: 1.45; }
	    .af-gateway-signin__hero { display: flex; justify-content: space-between; gap: 24px; margin-bottom: 24px; }
	    .af-gateway-signin__kicker {
	      margin-bottom: 8px;
	      color: rgba(32, 199, 223, .9);
	      font-size: 13px;
	      font-weight: 800;
	      letter-spacing: .14em;
	      text-transform: uppercase;
	    }
	    .af-gateway-signin__mark {
	      width: 72px;
	      height: 72px;
	      display: grid;
	      place-items: center;
	      flex: 0 0 auto;
	      border-radius: 24px;
	      border: 1px solid rgba(32, 199, 223, .28);
	      background: linear-gradient(135deg, rgba(32, 199, 223, .18), rgba(238, 65, 104, .22));
	      color: rgba(255, 255, 255, .86);
	      font-size: 32px;
	      font-weight: 900;
	    }
	    .af-gateway-signin__status-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }
	    .af-gateway-signin__status {
	      display: inline-flex;
	      align-items: center;
	      min-height: 26px;
	      padding: 4px 12px;
	      border-radius: 999px;
	      font-size: 12px;
	      font-weight: 800;
	      border: 1px solid rgba(255, 255, 255, .12);
	      background: rgba(255, 255, 255, .06);
	    }
	    .af-gateway-signin__status--ok { color: rgba(120, 255, 190, .95); border-color: rgba(59, 217, 154, .38); }
	    .af-gateway-signin__status--warn { color: rgba(255, 210, 120, .95); border-color: rgba(255, 183, 86, .42); }
	    .af-gateway-signin__status--err { color: rgba(255, 115, 135, .95); border-color: rgba(238, 65, 104, .45); }
	    .af-gateway-signin__source { color: var(--muted); font-size: 13px; }
	    .af-gateway-signin__form { display: grid; grid-template-columns: 160px minmax(0, 1fr); gap: 14px; align-items: center; }
	    .af-gateway-signin__label { margin: 0; }
	    .af-gateway-signin__token-input { display: flex; align-items: center; gap: 8px; }
	    .af-gateway-signin__token-input input { flex: 1; }
	    .af-gateway-signin__checkbox {
	      display: flex;
	      align-items: center;
	      gap: 10px;
	      margin: 0;
	      color: var(--muted);
	      font-size: 18px;
	      font-weight: 700;
	      text-transform: none;
	      letter-spacing: 0;
	    }
	    .af-gateway-signin__actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 22px; flex-wrap: wrap; }
	    .af-gateway-signin__primary { background: var(--danger); }
	    .af-gateway-signin__secondary { background: var(--button); }
	    .modal-backdrop {
	      position: fixed;
	      inset: 0;
      z-index: 10;
      display: grid;
      place-items: center;
      padding: 22px;
      background: rgba(2, 7, 18, .72);
    }
    .modal {
      width: min(520px, 100%);
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 0 28px 70px rgba(0, 0, 0, .46);
      padding: 18px;
    }
    .modal h2 { margin-bottom: 8px; }
    .modal p { color: var(--muted); margin-bottom: 16px; overflow-wrap: anywhere; }
    .modal-actions { display: flex; justify-content: flex-end; gap: 10px; }
	    @media (max-width: 940px) {
	      .layout { grid-template-columns: 1fr; }
	      body:not(.signed-in) main.layout { grid-template-columns: 1fr; }
	      header { align-items: flex-start; flex-direction: column; padding: 14px 18px; }
	      main { padding: 16px; }
	    }
	    @media (max-width: 680px) {
	      body:not(.signed-in) main.layout { align-content: start; padding-block: 18px; }
	      .af-gateway-signin { padding: 18px; }
	      .af-gateway-signin__hero { gap: 16px; }
	      .af-gateway-signin__mark { width: 56px; height: 56px; border-radius: 18px; font-size: 26px; }
	      .af-gateway-signin__form { grid-template-columns: 1fr; }
	    }
	  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">↔</div>
      <div>
        <h1>AbstractGateway Console</h1>
        <div class="brand-subtitle">Users, runtimes, defaults, and provider endpoints</div>
      </div>
    </div>
    <div class="status"><span id="status-dot" class="dot"></span><span id="status-text">Signed out</span><button id="sign-out" class="secondary hidden"><span class="button-icon" aria-hidden="true">×</span><span>Sign out</span></button></div>
  </header>
  <main class="layout">
    <div class="stack">
	      <section id="login-section" class="af-gateway-signin">
	        <div class="af-gateway-signin__hero">
	          <div>
	            <div class="af-gateway-signin__kicker">AbstractGateway Console</div>
	            <h2>Connect to AbstractGateway</h2>
	            <p>Sign in with the Gateway user token assigned by the Gateway admin.</p>
	          </div>
	          <div class="af-gateway-signin__mark" aria-hidden="true">↔</div>
	        </div>
	        <div class="af-gateway-signin__status-row">
	          <span id="login-status" class="af-gateway-signin__status af-gateway-signin__status--warn">Gateway token missing</span>
	          <span id="login-source" class="af-gateway-signin__source">token: missing</span>
	        </div>
	        <form id="login-form">
	          <div class="af-gateway-signin__form">
	            <label class="af-gateway-signin__label" for="login-user">Gateway user</label>
	            <input id="login-user" autocomplete="username" value="admin">
	            <label class="af-gateway-signin__label" for="login-token">Gateway token</label>
	            <div class="af-gateway-signin__token-input">
	              <input id="login-token" autocomplete="current-password" type="password" placeholder="Paste Gateway user token">
	              <button id="toggle-token" class="af-gateway-signin__secondary" type="button">Show</button>
	            </div>
	            <label class="af-gateway-signin__label">Browser session</label>
	            <label class="af-gateway-signin__checkbox"><input id="login-remember" type="checkbox"> Remember this browser</label>
	          </div>
	          <div class="af-gateway-signin__actions">
	            <button id="login-button" class="af-gateway-signin__primary" type="submit">Sign in</button>
	          </div>
	        </form>
	        <div id="login-message" class="message"></div>
	      </section>
	      <section class="session-only">
	        <div class="section-head">
	          <h2 class="section-title"><span class="section-icon" aria-hidden="true">◎</span><span>Account</span></h2>
	        </div>
	        <div id="account">No active session.</div>
	      </section>
	      <section class="session-only">
	        <div class="section-head">
	          <div>
            <h2 class="section-title"><span class="section-icon" aria-hidden="true">◆</span><span>Capability Defaults</span></h2>
            <p id="defaults-scope" class="section-note">Sign in to edit Gateway or user capability defaults.</p>
          </div>
          <button id="refresh-catalog" class="secondary"><span class="button-icon" aria-hidden="true">↻</span><span>Refresh</span></button>
        </div>
        <label>Route<select id="default-route"></select></label>
        <label>Provider<select id="default-provider"></select></label>
        <label>Model<select id="default-model"></select></label>
        <label>Base URL<input id="default-base-url" placeholder="optional"></label>
        <div class="inline">
          <button id="save-default"><span class="button-icon" aria-hidden="true">✓</span><span>Save default</span></button>
          <button id="clear-default" class="secondary"><span class="button-icon" aria-hidden="true">×</span><span>Clear</span></button>
        </div>
        <div id="defaults-message" class="message"></div>
      </section>
	      <section class="session-only">
	        <div class="section-head">
	          <div>
	            <h2 class="section-title"><span class="section-icon" aria-hidden="true">◇</span><span>Provider Endpoints</span></h2>
	            <p class="section-note">Create a reusable endpoint profile. Gateway stores the key server-side and exposes only a virtual provider to workflows.</p>
	          </div>
	        </div>
	        <input id="endpoint-id" type="hidden">
	        <div class="inline">
	          <label>Profile id<input id="endpoint-profile-id" placeholder="office-vllm"></label>
	          <label>Name<input id="endpoint-name" placeholder="Office vLLM"></label>
	        </div>
	        <label>Description<textarea id="endpoint-description" placeholder="What this endpoint is for, who owns it, and when to use it."></textarea></label>
	        <div class="inline">
	          <label>API type<select id="endpoint-provider-family"></select></label>
	          <label>Visibility<select id="endpoint-scope"></select></label>
	        </div>
	        <label>Base URL<input id="endpoint-base-url" placeholder="https://endpoint.example/v1"></label>
	        <label>API key<input id="endpoint-api-key" type="password" placeholder="leave blank to keep existing key"></label>
	        <label class="af-gateway-signin__checkbox"><input id="endpoint-clear-api-key" type="checkbox"> Clear stored API key</label>
	        <div class="model-picker">
	          <div class="model-picker__head">
	            <div>
	              <label for="endpoint-models">Models</label>
	              <p class="field-help">Gateway asks the endpoint for its model list. AbstractCore handles model capability metadata.</p>
	            </div>
	            <div class="model-picker__actions">
	              <button id="discover-endpoint-models" type="button" class="secondary"><span class="button-icon" aria-hidden="true">↻</span><span>Discover</span></button>
	            </div>
	          </div>
	          <div id="endpoint-model-summary" class="model-summary">No models discovered yet.</div>
	          <details class="advanced-panel">
	            <summary>Advanced: restrict visible models</summary>
	            <div class="advanced-panel__body">
	              <p class="field-help">Optional. Leave the list empty to keep live endpoint discovery. Select models only when this profile should expose a fixed allowlist.</p>
	              <select id="endpoint-models" class="model-picker__select" multiple size="7"></select>
	              <button id="clear-endpoint-models" type="button" class="secondary"><span class="button-icon" aria-hidden="true">×</span><span>Clear restriction</span></button>
	            </div>
	          </details>
	        </div>
	        <label class="af-gateway-signin__checkbox"><input id="endpoint-enabled" type="checkbox" checked> Enabled</label>
	        <div class="inline">
	          <button id="save-endpoint-profile"><span class="button-icon" aria-hidden="true">✓</span><span>Save endpoint</span></button>
	          <button id="clear-endpoint-profile" class="secondary"><span class="button-icon" aria-hidden="true">×</span><span>Clear</span></button>
	        </div>
	        <div id="endpoint-message" class="message"></div>
	      </section>
    </div>
    <div class="stack">
	      <section id="users-section" class="session-only hidden">
        <div class="section-head">
          <div>
            <h2 class="section-title"><span class="section-icon" aria-hidden="true">+</span><span>Users</span></h2>
            <p class="section-note">Admins create user tokens and bind each user to one runtime data plane.</p>
          </div>
        </div>
        <div class="inline">
          <label>Tenant<input id="new-tenant" value="default"></label>
          <label>User<input id="new-user" placeholder="user id"></label>
          <label>Email<input id="new-email" type="email" placeholder="optional"></label>
          <label>Runtime<input id="new-runtime" placeholder="defaults to user id"></label>
          <label>Roles<input id="new-roles" value="user"></label>
        </div>
        <button id="create-user"><span class="button-icon" aria-hidden="true">+</span><span>Create user</span></button>
        <div id="issued-token" class="issued hidden"></div>
        <div id="users-message" class="message"></div>
        <table>
          <thead><tr><th>Tenant</th><th>User</th><th>Email</th><th>Runtime</th><th>Roles</th><th>State</th><th>Actions</th></tr></thead>
          <tbody id="users-table"></tbody>
        </table>
      </section>
	      <section id="runtime-reservations-section" class="session-only hidden">
        <div class="section-head">
          <div>
            <h2 class="section-title"><span class="section-icon" aria-hidden="true">◌</span><span>Retained Runtimes</span></h2>
            <p class="section-note">Deleted or reassigned users leave retained runtime reservations. Transfer only when the new owner should inherit the data; purge permanently deletes the retained runtime directory.</p>
          </div>
        </div>
        <div id="reservations-message" class="message"></div>
        <table>
          <thead><tr><th>Tenant</th><th>Runtime</th><th>Owner</th><th>Reason</th><th>Data</th><th>Actions</th></tr></thead>
          <tbody id="runtime-reservations-table"></tbody>
        </table>
      </section>
	      <section class="session-only">
        <div class="section-head">
          <div>
            <h2 class="section-title"><span class="section-icon" aria-hidden="true">◇</span><span>Provider Endpoint Profiles</span></h2>
            <p class="section-note">Use the virtual provider id in Flow nodes and Gateway defaults. Raw keys are never shown after save.</p>
          </div>
        </div>
        <table>
          <thead><tr><th>Name</th><th>Routing</th><th>Models</th><th>Status</th><th>Actions</th></tr></thead>
          <tbody id="endpoint-profiles-table"></tbody>
        </table>
      </section>
	      <section class="session-only">
        <div class="section-head">
          <h2 class="section-title"><span class="section-icon" aria-hidden="true">◆</span><span>Current Defaults</span></h2>
        </div>
        <table>
          <thead><tr><th>Route</th><th>Provider</th><th>Model</th><th>Source</th></tr></thead>
          <tbody id="defaults-table"></tbody>
        </table>
      </section>
    </div>
  </main>
  <div id="confirm-backdrop" class="modal-backdrop hidden" role="presentation">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
      <h2 id="confirm-title">Confirm</h2>
      <p id="confirm-message"></p>
      <div class="modal-actions">
        <button id="confirm-cancel" class="secondary">Cancel</button>
        <button id="confirm-ok">Confirm</button>
      </div>
    </div>
  </div>
  <script>
	    const state = { principal: null, users: [], defaults: [], providers: [], providerLabels: new Map(), providerModels: new Map(), endpointProfiles: [], endpointModelOptions: [], confirmResolve: null };
	    const $ = (id) => document.getElementById(id);
	    const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
	    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch] || ch);
    function csrf() {
      return document.cookie.split(";").map((p) => p.trim()).find((p) => p.startsWith("abstractgateway_csrf="))?.slice("abstractgateway_csrf=".length) || "";
    }
    async function api(path, options = {}) {
      const headers = new Headers(options.headers || {});
      headers.set("Accept", "application/json");
      if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
      const token = csrf();
      if (token && ["POST", "PUT", "PATCH", "DELETE"].includes(String(options.method || "GET").toUpperCase())) {
        headers.set("X-AbstractGateway-CSRF", decodeURIComponent(token));
      }
      const res = await fetch(path, { ...options, headers, credentials: "same-origin" });
      const text = await res.text();
      let data = {};
      try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
      if (!res.ok) throw new Error(data.detail || data.reason_code || `HTTP ${res.status}`);
      return data;
    }
	    function setStatus(ok, text) {
	      $("status-dot").className = `dot ${ok ? "ok" : ""}`;
	      $("status-text").textContent = text;
	      $("sign-out").classList.toggle("hidden", !ok);
	    }
	    function setLoginStatus(label, tone = "warn", source = "token: missing") {
	      $("login-status").textContent = label;
	      $("login-status").className = `af-gateway-signin__status af-gateway-signin__status--${tone}`;
	      $("login-source").textContent = source;
	    }
    function confirmAction({ title, message, confirmLabel = "Confirm", danger = false }) {
      $("confirm-title").textContent = title;
      $("confirm-message").textContent = message;
      $("confirm-ok").textContent = confirmLabel;
      $("confirm-ok").className = danger ? "danger" : "";
      $("confirm-backdrop").classList.remove("hidden");
      return new Promise((resolve) => { state.confirmResolve = resolve; });
    }
    function finishConfirm(value) {
      $("confirm-backdrop").classList.add("hidden");
      if (state.confirmResolve) state.confirmResolve(Boolean(value));
      state.confirmResolve = null;
    }
    function parseProviderItems(payload) {
      const names = [];
      const add = (value) => {
        if (typeof value === "string" && value.trim()) names.push(value.trim());
        else if (value && typeof value === "object") {
          const name = value.name || value.provider || value.id || value.provider_id;
          if (typeof name === "string" && name.trim()) {
            const providerName = name.trim();
            names.push(providerName);
            const label = value.display_name || value.label || value.name || providerName;
            if (typeof label === "string" && label.trim() && label.trim() !== providerName) {
              state.providerLabels.set(providerName, `${label.trim()} (${providerName})`);
            } else {
              state.providerLabels.set(providerName, providerName);
            }
          }
        }
      };
      for (const key of ["items", "providers", "available_providers", "provider_details"]) {
        const values = payload?.[key];
        if (Array.isArray(values)) values.forEach(add);
      }
      return [...new Set(names)].sort((a, b) => a.localeCompare(b));
    }
    function parseModelItems(payload) {
      const models = [];
      const add = (value) => {
        if (typeof value === "string" && value.trim()) models.push(value.trim());
        else if (value && typeof value === "object") {
          const id = value.id || value.model || value.name;
          if (typeof id === "string" && id.trim()) models.push(id.trim());
        }
      };
      for (const key of ["items", "models", "available_models", "provider_models"]) {
        const values = payload?.[key];
        if (Array.isArray(values)) values.forEach(add);
      }
      return [...new Set(models)].sort((a, b) => a.localeCompare(b));
    }
    function setSelectOptions(select, values, { emptyLabel, disabled = false, selected = "" } = {}) {
      select.textContent = "";
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = emptyLabel || "Select...";
      select.append(empty);
      for (const value of values) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = state.providerLabels.get(value) || value;
        select.append(opt);
      }
      select.disabled = disabled;
      if (selected && values.includes(selected)) select.value = selected;
      else select.value = "";
    }
    function setEndpointModelOptions(values, selectedValues = []) {
      const select = $("endpoint-models");
      const selected = new Set((Array.isArray(selectedValues) ? selectedValues : []).map((value) => String(value || "").trim()).filter(Boolean));
      const models = [...new Set((Array.isArray(values) ? values : []).map((value) => String(value || "").trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b));
      state.endpointModelOptions = models;
      select.textContent = "";
      if (!models.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "Discover models from the endpoint...";
        opt.disabled = true;
        select.append(opt);
        select.disabled = true;
        updateEndpointModelSummary();
        return;
      }
      for (const model of models) {
        const opt = document.createElement("option");
        opt.value = model;
        opt.textContent = model;
        opt.selected = selected.has(model);
        select.append(opt);
      }
      select.disabled = false;
      updateEndpointModelSummary();
    }
    function selectedEndpointModels() {
      return [...$("endpoint-models").selectedOptions].map((opt) => opt.value).filter(Boolean);
    }
    function updateEndpointModelSummary() {
      const models = state.endpointModelOptions || [];
      const selectedCount = selectedEndpointModels().length;
      if (!models.length) {
        $("endpoint-model-summary").textContent = "No model list loaded. Save now to use live discovery, or discover models first.";
      } else if (selectedCount) {
        $("endpoint-model-summary").textContent = `${models.length} models available. ${selectedCount} model${selectedCount === 1 ? "" : "s"} restricted by this profile.`;
      } else {
        $("endpoint-model-summary").textContent = `${models.length} models available. This profile will keep live endpoint discovery.`;
      }
    }
    async function loadProviders() {
      $("defaults-message").className = "message";
      $("defaults-message").textContent = "Loading provider catalog...";
      try {
        const payload = await api("/api/gateway/discovery/providers?include_models=false");
        state.providerLabels.clear();
        state.providers = parseProviderItems(payload);
        setSelectOptions($("default-provider"), state.providers, {
          emptyLabel: state.providers.length ? "Select provider..." : "No providers discovered",
          disabled: !state.providers.length,
        });
        $("defaults-message").textContent = state.providers.length ? "" : "No providers were discovered by the Gateway runtime.";
      } catch (err) {
        state.providers = [];
        setSelectOptions($("default-provider"), [], { emptyLabel: "Provider discovery failed", disabled: true });
        setSelectOptions($("default-model"), [], { emptyLabel: "Select provider first", disabled: true });
        $("defaults-message").textContent = String(err.message || err);
        $("defaults-message").className = "message error";
      }
    }
    async function loadModels(provider, selected = "") {
      if (!provider) {
        setSelectOptions($("default-model"), [], { emptyLabel: "Select provider first", disabled: true });
        return;
      }
      if (!state.providerModels.has(provider)) {
        setSelectOptions($("default-model"), [], { emptyLabel: "Loading models...", disabled: true });
        const payload = await api(`/api/gateway/discovery/providers/${encodeURIComponent(provider)}/models`);
        state.providerModels.set(provider, parseModelItems(payload));
      }
      const models = state.providerModels.get(provider) || [];
      setSelectOptions($("default-model"), models, {
        emptyLabel: models.length ? "Select model..." : "No models discovered",
        disabled: !models.length,
        selected,
      });
      if (selected && !models.includes(selected)) {
        $("defaults-message").textContent = `Configured model "${selected}" is not currently in the discovered ${provider} catalog.`;
        $("defaults-message").className = "message error";
      }
    }
    function initEndpointProfileFormOptions() {
      const familySelect = $("endpoint-provider-family");
      const familySelected = familySelect.value || "openai-compatible";
      familySelect.textContent = "";
      for (const item of [
        ["openai-compatible", "OpenAI-compatible"],
        ["openai", "OpenAI"],
        ["openrouter", "OpenRouter"],
        ["anthropic", "Anthropic"],
      ]) {
        const opt = document.createElement("option");
        opt.value = item[0];
        opt.textContent = item[1];
        familySelect.append(opt);
      }
      familySelect.value = familySelected;
      const scopeValues = state.principal?.admin ? ["user", "gateway"] : ["user"];
      const scopeSelect = $("endpoint-scope");
      const scopeSelected = scopeSelect.value || "user";
      scopeSelect.textContent = "";
      for (const value of scopeValues) {
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = value === "gateway" ? "Gateway-wide" : "Only me";
        scopeSelect.append(opt);
      }
      scopeSelect.value = scopeValues.includes(scopeSelected) ? scopeSelected : "user";
    }
    function clearEndpointProfileForm() {
      $("endpoint-id").value = "";
      $("endpoint-profile-id").value = "";
      $("endpoint-profile-id").disabled = false;
      $("endpoint-name").value = "";
      $("endpoint-description").value = "";
      $("endpoint-provider-family").value = "openai-compatible";
      $("endpoint-scope").value = "user";
      $("endpoint-base-url").value = "";
      $("endpoint-api-key").value = "";
      $("endpoint-clear-api-key").checked = false;
      setEndpointModelOptions([], []);
      $("endpoint-enabled").checked = true;
      $("endpoint-message").textContent = "";
      $("endpoint-message").className = "message";
    }
    function fillEndpointProfileForm(profile) {
      initEndpointProfileFormOptions();
      $("endpoint-id").value = profile.id || "";
      $("endpoint-profile-id").value = profile.id || "";
      $("endpoint-profile-id").disabled = true;
      $("endpoint-name").value = profile.display_name || profile.id || "";
      $("endpoint-description").value = profile.description || "";
      $("endpoint-provider-family").value = profile.provider_family || "openai-compatible";
      $("endpoint-scope").value = profile.scope || "user";
      $("endpoint-base-url").value = profile.base_url || "";
      $("endpoint-api-key").value = "";
      $("endpoint-clear-api-key").checked = false;
      const allowedModels = Array.isArray(profile.allowed_models) ? profile.allowed_models : [];
      setEndpointModelOptions(allowedModels, allowedModels);
      $("endpoint-enabled").checked = profile.enabled !== false;
      $("endpoint-message").textContent = `Editing ${profile.virtual_provider || "endpoint:" + profile.id}. Leave API key blank to keep the stored key.`;
      $("endpoint-message").className = "message";
    }
    function renderEndpointProfiles(profiles) {
      state.endpointProfiles = Array.isArray(profiles) ? profiles : [];
      const tbody = $("endpoint-profiles-table");
      tbody.textContent = "";
      if (!state.endpointProfiles.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="5" class="empty">No provider endpoint profiles configured.</td>`;
        tbody.append(tr);
        return;
      }
      for (const p of state.endpointProfiles) {
        const tr = document.createElement("tr");
        const endpoint = p.base_url_configured ? esc(p.base_url || "") : "provider default";
        const keyState = p.api_key_set ? `key ${String(p.api_key_fingerprint || "").slice(0, 8)}` : "no key";
        const models = Array.isArray(p.allowed_models) && p.allowed_models.length
          ? `${p.allowed_models.length} restricted`
          : "live discovery";
        tr.innerHTML = `
          <td><strong>${esc(p.display_name || p.id)}</strong><div class="muted">${esc(p.description || "No description")}</div></td>
          <td><code>${esc(p.virtual_provider || "endpoint:" + p.id)}</code><div class="muted">${esc(p.provider_family || "")} · ${endpoint}</div></td>
          <td><span class="badge">${esc(models)}</span></td>
          <td><span class="state-pill ${p.enabled ? "ok" : "off"}">${p.enabled ? "enabled" : "disabled"}</span><div class="muted">${esc(p.scope || "user")} · ${esc(keyState)}</div></td>
        `;
        const actions = document.createElement("td");
        actions.className = "actions";
        const edit = document.createElement("button");
        edit.innerHTML = `<span class="button-icon" aria-hidden="true">✎</span><span>Edit</span>`;
        edit.className = "secondary";
        edit.onclick = () => fillEndpointProfileForm(p);
        const del = document.createElement("button");
        del.innerHTML = `<span class="button-icon" aria-hidden="true">×</span><span>Delete</span>`;
        del.className = "danger";
        del.onclick = () => deleteEndpointProfile(p);
        actions.append(edit, del);
        tr.append(actions);
        tbody.append(tr);
      }
    }
    async function loadEndpointProfiles() {
      const payload = await api("/api/gateway/config/provider-endpoint-profiles");
      renderEndpointProfiles(payload.profiles || []);
      initEndpointProfileFormOptions();
      return payload;
    }
    async function saveEndpointProfile() {
      $("endpoint-message").textContent = "";
      const editingId = $("endpoint-id").value.trim();
      const profileId = editingId || $("endpoint-profile-id").value.trim();
      if (!profileId) {
        $("endpoint-message").textContent = "Profile id is required.";
        $("endpoint-message").className = "message error";
        return;
      }
      const payload = {
        display_name: $("endpoint-name").value.trim() || profileId,
        description: $("endpoint-description").value.trim(),
        provider_family: $("endpoint-provider-family").value || "openai-compatible",
        base_url: $("endpoint-base-url").value.trim() || null,
        scope: $("endpoint-scope").value || "user",
        allowed_models: selectedEndpointModels(),
        enabled: $("endpoint-enabled").checked,
      };
      if (!editingId) payload.id = profileId;
      const apiKey = $("endpoint-api-key").value.trim();
      if (apiKey && $("endpoint-clear-api-key").checked) {
        $("endpoint-message").textContent = "Choose either a new API key or clear the stored key, not both.";
        $("endpoint-message").className = "message error";
        return;
      }
      if (apiKey) payload.api_key = apiKey;
      if (editingId && $("endpoint-clear-api-key").checked) payload.clear_api_key = true;
      const path = editingId
        ? `/api/gateway/config/provider-endpoint-profiles/${encodeURIComponent(editingId)}`
        : "/api/gateway/config/provider-endpoint-profiles";
      const method = editingId ? "PUT" : "POST";
      try {
        const res = await api(path, { method, body: JSON.stringify(payload) });
        $("endpoint-api-key").value = "";
        $("endpoint-message").textContent = `Saved. Use ${res.profile?.virtual_provider || "endpoint:" + profileId} as the provider in Flow.`;
        $("endpoint-message").className = "message ok";
        renderEndpointProfiles(res.profiles || []);
        state.providerModels.clear();
        await loadProviders();
        await fillDefaultForm();
      } catch (err) {
        $("endpoint-message").textContent = String(err.message || err);
        $("endpoint-message").className = "message error";
      }
    }
    async function discoverEndpointModels() {
      $("endpoint-message").textContent = "";
      const editingId = $("endpoint-id").value.trim();
      const profileId = editingId || $("endpoint-profile-id").value.trim();
      const apiKey = $("endpoint-api-key").value.trim();
      if (apiKey && $("endpoint-clear-api-key").checked) {
        $("endpoint-message").textContent = "Choose either a new API key or clear the stored key before discovering models.";
        $("endpoint-message").className = "message error";
        return;
      }
      const previousSelection = selectedEndpointModels();
      const payload = {
        provider_family: $("endpoint-provider-family").value || "openai-compatible",
        base_url: $("endpoint-base-url").value.trim() || null,
      };
      if (profileId) payload.profile_id = profileId;
      if (apiKey) payload.api_key = apiKey;
      $("discover-endpoint-models").disabled = true;
      $("endpoint-message").textContent = "Discovering models from endpoint...";
      $("endpoint-message").className = "message";
      try {
        const res = await api("/api/gateway/config/provider-endpoint-profiles/discover-models", { method: "POST", body: JSON.stringify(payload) });
        const models = parseModelItems(res);
        setEndpointModelOptions(models, previousSelection.filter((model) => models.includes(model)));
        if (models.length) {
          $("endpoint-message").textContent = `Discovered ${models.length} model${models.length === 1 ? "" : "s"}.`;
          $("endpoint-message").className = "message ok";
        } else {
          $("endpoint-message").textContent = res.error || "No models were returned by this endpoint.";
          $("endpoint-message").className = "message error";
        }
      } catch (err) {
        $("endpoint-message").textContent = String(err.message || err);
        $("endpoint-message").className = "message error";
      } finally {
        $("discover-endpoint-models").disabled = false;
      }
    }
    function clearEndpointModelAllowlist() {
      for (const option of $("endpoint-models").options) option.selected = false;
      updateEndpointModelSummary();
    }
    async function deleteEndpointProfile(profile) {
      const ok = await confirmAction({
        title: "Delete provider endpoint",
        message: `Delete ${profile.virtual_provider || "endpoint:" + profile.id}? Existing workflows that select this virtual provider will stop working until they are remapped.`,
        confirmLabel: "Delete endpoint",
        danger: true,
      });
      if (!ok) return;
      try {
        const res = await api(`/api/gateway/config/provider-endpoint-profiles/${encodeURIComponent(profile.id)}`, { method: "DELETE" });
        renderEndpointProfiles(res.profiles || []);
        clearEndpointProfileForm();
        state.providerModels.clear();
        await loadProviders();
        await fillDefaultForm();
      } catch (err) {
        $("endpoint-message").textContent = String(err.message || err);
        $("endpoint-message").className = "message error";
      }
    }
    function routeKey(row) { return `${row.kind || ""}.${row.modality || ""}`; }
    function renderAccount(me) {
      const p = me?.principal;
      state.principal = p || null;
	      if (!p) {
	        document.body.classList.remove("signed-in");
	        $("account").textContent = "No active session.";
	        $("users-section").classList.add("hidden");
	        $("runtime-reservations-section").classList.add("hidden");
	        $("defaults-scope").textContent = "Sign in to edit Gateway or user capability defaults.";
	        setLoginStatus("Gateway token missing", "warn", "token: missing");
	        setStatus(false, "Signed out");
	        return;
	      }
	      document.body.classList.add("signed-in");
	      $("account").innerHTML = `
        <div class="account-grid">
          <div class="metric"><span>Tenant</span><strong>${esc(p.tenant_id)}</strong></div>
          <div class="metric"><span>User</span><strong>${esc(p.user_id)}</strong></div>
          <div class="metric"><span>Runtime</span><strong>${esc(p.runtime_id || p.user_id)}</strong></div>
          <div class="metric"><span>Roles</span><strong>${esc((p.roles || []).join(", ") || "none")}</strong></div>
        </div>
      `;
      $("users-section").classList.toggle("hidden", !p.admin);
      $("runtime-reservations-section").classList.toggle("hidden", !p.admin);
      $("defaults-scope").textContent = p.admin
        ? "Editing as admin changes Gateway defaults. Users inherit these unless they save their own override."
        : "Editing here changes only your user defaults. Gateway defaults remain inherited where you have no override.";
      initEndpointProfileFormOptions();
      setStatus(true, `${p.tenant_id}/${p.user_id}`);
    }
    function renderUsers(users) {
      const tbody = $("users-table");
      tbody.textContent = "";
      if (!users || !users.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="7" class="empty">No users registered.</td>`;
        tbody.append(tr);
        return;
      }
      for (const u of users || []) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${esc(u.tenant_id)}</td><td>${esc(u.user_id)}</td><td>${esc(u.email || "")}</td><td>${esc(u.runtime_id || u.user_id)}</td><td>${esc((u.roles || []).join(", "))}</td><td>${u.enabled ? "enabled" : "disabled"}</td>`;
        const actions = document.createElement("td");
        actions.className = "actions";
        const rotate = document.createElement("button");
        rotate.innerHTML = `<span class="button-icon" aria-hidden="true">↻</span><span>Rotate</span>`;
        rotate.className = "secondary";
        rotate.onclick = () => rotateUser(u);
        const toggle = document.createElement("button");
        toggle.innerHTML = `<span class="button-icon" aria-hidden="true">${u.enabled ? "×" : "✓"}</span><span>${u.enabled ? "Disable" : "Enable"}</span>`;
        toggle.className = "secondary";
        toggle.onclick = () => updateUser(u, { enabled: !u.enabled });
        const del = document.createElement("button");
        del.innerHTML = `<span class="button-icon" aria-hidden="true">×</span><span>Delete</span>`;
        del.className = "danger";
        del.onclick = () => deleteUser(u);
        actions.append(rotate, toggle, del);
        tr.append(actions);
        tbody.append(tr);
      }
    }
    function renderRuntimeReservations(reservations) {
      const tbody = $("runtime-reservations-table");
      tbody.textContent = "";
      if (!reservations || !reservations.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6" class="empty">No retained runtime reservations.</td>`;
        tbody.append(tr);
        return;
      }
      for (const r of reservations || []) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td>${esc(r.tenant_id)}</td><td><code>${esc(r.runtime_id)}</code></td><td>${esc(r.owner_key || r.owner_user_id || "")}</td><td>${esc(r.reason || "")}</td><td>${r.data_exists ? "retained" : "no files found"}</td>`;
        const actions = document.createElement("td");
        actions.className = "actions";
        const transferTarget = document.createElement("select");
        const empty = document.createElement("option");
        empty.value = "";
        empty.textContent = "Transfer to...";
        transferTarget.append(empty);
        for (const u of state.users.filter((u) => u.tenant_id === r.tenant_id)) {
          const opt = document.createElement("option");
          opt.value = u.user_id;
          opt.textContent = `${u.user_id} (${u.runtime_id || u.user_id})`;
          transferTarget.append(opt);
        }
        const transfer = document.createElement("button");
        transfer.innerHTML = `<span class="button-icon" aria-hidden="true">→</span><span>Transfer</span>`;
        transfer.className = "secondary";
        transfer.onclick = () => transferRuntimeReservation(r, transferTarget.value);
        const purge = document.createElement("button");
        purge.innerHTML = `<span class="button-icon" aria-hidden="true">×</span><span>Purge</span>`;
        purge.className = "danger";
        purge.onclick = () => purgeRuntimeReservation(r);
        actions.append(transferTarget, transfer, purge);
        tr.append(actions);
        tbody.append(tr);
      }
    }
    async function renderDefaults(payload) {
      const rows = Array.isArray(payload?.routes) ? payload.routes : [];
      state.defaults = rows;
      const select = $("default-route");
      const previous = select.value;
      select.textContent = "";
      const tbody = $("defaults-table");
      tbody.textContent = "";
      for (const row of rows) {
        const key = row.key || routeKey(row);
        if (!key || key === ".") continue;
        const opt = document.createElement("option");
        opt.value = key;
        opt.textContent = `${key}${row.label ? " - " + row.label : ""}`;
        select.append(opt);
        if (row.configured || row.provider || row.model) {
          const tr = document.createElement("tr");
          tr.innerHTML = `<td><code>${esc(key)}</code></td><td>${esc(row.provider || "")}</td><td>${esc(row.model || "")}</td><td><span class="badge">${esc(row.source || payload.source || "")}</span></td>`;
          tbody.append(tr);
        }
      }
      if (!tbody.children.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="4" class="empty">No capability defaults configured.</td>`;
        tbody.append(tr);
      }
      if (previous) select.value = previous;
      await fillDefaultForm();
    }
    async function fillDefaultForm() {
      $("defaults-message").className = "message";
      const key = $("default-route").value;
      const row = state.defaults.find((r) => (r.key || routeKey(r)) === key) || {};
      $("default-base-url").value = row.base_url || "";
      if (!state.providers.length) await loadProviders();
      setSelectOptions($("default-provider"), state.providers, {
        emptyLabel: state.providers.length ? "Select provider..." : "No providers discovered",
        disabled: !state.providers.length,
        selected: row.provider || "",
      });
      if (row.provider && !state.providers.includes(row.provider)) {
        $("defaults-message").textContent = `Configured provider "${row.provider}" is not currently discovered by Gateway.`;
        $("defaults-message").className = "message error";
        setSelectOptions($("default-model"), [], { emptyLabel: "Select discovered provider first", disabled: true });
        return;
      }
      await loadModels($("default-provider").value, row.model || "");
    }
    async function refresh() {
      try {
        const me = await api("/api/gateway/me");
        renderAccount(me);
        $("login-section").classList.add("hidden");
        if (!state.providers.length) await loadProviders();
        await loadEndpointProfiles();
        const defaults = await api("/api/gateway/config/capability-defaults");
        await renderDefaults(defaults);
        if (me.principal?.admin) {
          const users = await api("/api/gateway/admin/users");
          state.users = users.users || [];
          renderUsers(state.users);
          const reservations = await api("/api/gateway/admin/runtime-reservations");
          renderRuntimeReservations(reservations.runtime_reservations || []);
        }
      } catch (err) {
        renderAccount(null);
        $("login-section").classList.remove("hidden");
      }
    }
    async function login() {
	      $("login-message").textContent = "";
	      $("login-button").disabled = true;
	      setLoginStatus("Signing in...", "warn", "token: checking");
	      try {
	        const user = $("login-user").value.trim();
	        const token = $("login-token").value.trim();
	        if (!user || !token) throw new Error("Gateway user and token are required.");
	        await api("/api/gateway/session/login", {
	          method: "POST",
	          body: JSON.stringify({ user_id: user, token, remember: $("login-remember").checked })
	        });
	        $("login-token").value = "";
	        setLoginStatus("Signed in", "ok", "token: browser session");
	        await refresh();
	      } catch (err) {
	        $("login-message").textContent = String(err.message || err);
	        $("login-message").className = "message error";
	        setLoginStatus("Could not sign in", "err", "token: rejected");
	      } finally {
	        $("login-button").disabled = false;
	      }
	    }
    async function signOut() {
      try { await api("/api/gateway/session/logout", { method: "POST" }); } catch {}
      location.reload();
    }
    async function createUser() {
      $("users-message").textContent = "";
      $("issued-token").classList.add("hidden");
      const payload = {
        tenant_id: $("new-tenant").value.trim() || "default",
        user_id: $("new-user").value.trim(),
        email: $("new-email").value.trim() || null,
        runtime_id: $("new-runtime").value.trim() || null,
        roles: $("new-roles").value.split(",").map((x) => x.trim()).filter(Boolean)
      };
      if (!payload.user_id) {
        $("users-message").textContent = "User id is required.";
        $("users-message").className = "message error";
        return;
      }
      const runtime = payload.runtime_id || payload.user_id;
      const ok = await confirmAction({
        title: "Create Gateway user",
        message: `Create ${payload.tenant_id}/${payload.user_id} with runtime ${runtime}? The token is shown once after creation.`,
        confirmLabel: "Create user",
      });
      if (!ok) return;
      try {
        const res = await api("/api/gateway/admin/users", { method: "POST", body: JSON.stringify(payload) });
        $("issued-token").textContent = `Issued token for ${res.user.tenant_id}/${res.user.user_id}: ${res.token}`;
        $("issued-token").classList.remove("hidden");
        $("new-user").value = "";
        $("new-email").value = "";
        $("new-runtime").value = "";
        await refresh();
      } catch (err) {
        $("users-message").textContent = String(err.message || err);
        $("users-message").className = "message error";
      }
    }
    async function updateUser(u, payload) {
      await api(`/api/gateway/admin/users/${encodeURIComponent(u.user_id)}?tenant_id=${encodeURIComponent(u.tenant_id)}`, { method: "PATCH", body: JSON.stringify(payload) });
      await refresh();
    }
    async function rotateUser(u) {
      const res = await api(`/api/gateway/admin/users/${encodeURIComponent(u.user_id)}?tenant_id=${encodeURIComponent(u.tenant_id)}`, { method: "PATCH", body: JSON.stringify({ rotate_token: true }) });
      $("issued-token").textContent = `Issued token for ${res.user.tenant_id}/${res.user.user_id}: ${res.token}`;
      $("issued-token").classList.remove("hidden");
      await refresh();
    }
    async function deleteUser(u) {
      const ok = await confirmAction({
        title: "Delete Gateway user",
        message: `Delete ${u.tenant_id}/${u.user_id}? This removes the account and token access to runtime ${u.runtime_id || u.user_id}. Runtime data is retained and the runtime id stays reserved for this user.`,
        confirmLabel: "Delete user",
        danger: true,
      });
      if (!ok) return;
      await api(`/api/gateway/admin/users/${encodeURIComponent(u.user_id)}?tenant_id=${encodeURIComponent(u.tenant_id)}`, { method: "DELETE" });
      await refresh();
    }
    async function transferRuntimeReservation(r, targetUserId) {
      $("reservations-message").textContent = "";
      if (!targetUserId) {
        $("reservations-message").textContent = "Select a target user before transferring.";
        $("reservations-message").className = "message error";
        return;
      }
      const ok = await confirmAction({
        title: "Transfer retained runtime",
        message: `Transfer retained runtime ${r.tenant_id}/${r.runtime_id} to ${targetUserId}? The target user will inherit this runtime data, and their previous runtime id will be reserved.`,
        confirmLabel: "Transfer runtime",
        danger: true,
      });
      if (!ok) return;
      try {
        await api(`/api/gateway/admin/runtime-reservations/${encodeURIComponent(r.runtime_id)}/transfer`, {
          method: "POST",
          body: JSON.stringify({ tenant_id: r.tenant_id, target_user_id: targetUserId, confirm_runtime_id: r.runtime_id })
        });
        $("reservations-message").textContent = "Runtime transferred.";
        $("reservations-message").className = "message ok";
        await refresh();
      } catch (err) {
        $("reservations-message").textContent = String(err.message || err);
        $("reservations-message").className = "message error";
      }
    }
    async function purgeRuntimeReservation(r) {
      $("reservations-message").textContent = "";
      const ok = await confirmAction({
        title: "Purge retained runtime",
        message: `Permanently delete retained runtime data for ${r.tenant_id}/${r.runtime_id} and release the runtime id? This cannot be undone.`,
        confirmLabel: "Purge runtime",
        danger: true,
      });
      if (!ok) return;
      try {
        await api(`/api/gateway/admin/runtime-reservations/${encodeURIComponent(r.runtime_id)}/purge`, {
          method: "POST",
          body: JSON.stringify({ tenant_id: r.tenant_id, confirm_runtime_id: r.runtime_id, delete_data: true })
        });
        $("reservations-message").textContent = "Runtime purged.";
        $("reservations-message").className = "message ok";
        await refresh();
      } catch (err) {
        $("reservations-message").textContent = String(err.message || err);
        $("reservations-message").className = "message error";
      }
    }
    async function saveDefault() {
      $("defaults-message").textContent = "";
      try {
        const provider = $("default-provider").value;
        const model = $("default-model").value;
        if (!provider || !model) throw new Error("Select a discovered provider and model before saving.");
        const [kind, modality] = $("default-route").value.split(".", 2);
        await api(`/api/gateway/config/capability-defaults/${encodeURIComponent(kind)}/${encodeURIComponent(modality)}`, {
          method: "PUT",
          body: JSON.stringify({
            provider,
            model,
            base_url: $("default-base-url").value.trim() || null,
            options: {}
          })
        });
        $("defaults-message").textContent = "Saved.";
        $("defaults-message").className = "message ok";
        await renderDefaults(await api("/api/gateway/config/capability-defaults"));
      } catch (err) {
        $("defaults-message").textContent = String(err.message || err);
        $("defaults-message").className = "message error";
      }
    }
    async function clearDefault() {
      const [kind, modality] = $("default-route").value.split(".", 2);
      await api(`/api/gateway/config/capability-defaults/${encodeURIComponent(kind)}/${encodeURIComponent(modality)}`, { method: "DELETE" });
      await renderDefaults(await api("/api/gateway/config/capability-defaults"));
    }
	    $("login-form").onsubmit = (event) => { event.preventDefault(); login(); };
	    $("toggle-token").onclick = () => {
	      const input = $("login-token");
	      const visible = input.type === "text";
	      input.type = visible ? "password" : "text";
	      $("toggle-token").textContent = visible ? "Show" : "Hide";
	    };
	    $("sign-out").onclick = signOut;
    $("confirm-cancel").onclick = () => finishConfirm(false);
    $("confirm-ok").onclick = () => finishConfirm(true);
    $("confirm-backdrop").onclick = (event) => { if (event.target === $("confirm-backdrop")) finishConfirm(false); };
    $("create-user").onclick = createUser;
    $("default-route").onchange = fillDefaultForm;
    $("default-provider").onchange = () => loadModels($("default-provider").value);
    $("refresh-catalog").onclick = async () => {
      state.providerModels.clear();
      await loadProviders();
      await fillDefaultForm();
    };
    $("save-default").onclick = saveDefault;
    $("clear-default").onclick = clearDefault;
    $("save-endpoint-profile").onclick = saveEndpointProfile;
    $("clear-endpoint-profile").onclick = clearEndpointProfileForm;
    $("discover-endpoint-models").onclick = discoverEndpointModels;
    $("clear-endpoint-models").onclick = clearEndpointModelAllowlist;
    $("endpoint-models").onchange = updateEndpointModelSummary;
    initEndpointProfileFormOptions();
    setEndpointModelOptions([], []);
    refresh();
  </script>
</body>
</html>"""
