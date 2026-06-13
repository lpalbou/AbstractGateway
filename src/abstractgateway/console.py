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
	      --font-scale: 1;
	      --header-density: 1;
	      --font-base: calc(14px * var(--font-scale));
	      --font-sm: calc(12px * var(--font-scale));
	      --font-xs: calc(11px * var(--font-scale));
	      --bg-primary: var(--bg);
	      --bg-secondary: var(--panel);
	      --bg-tertiary: var(--line);
	      --text-primary: var(--text);
	      --text-secondary: var(--muted);
	      --text-muted: var(--subtle);
	      --accent-primary: var(--accent);
	      --font-sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
	      --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
	      --font-size-xs: var(--font-xs);
	      --font-size-sm: var(--font-sm);
	      --font-size-md: calc(13px * var(--font-scale));
	      --font-size-base: var(--font-base);
	      --font-size-lg: calc(16px * var(--font-scale));
	      --radius-sm: 4px;
	      --radius-md: 8px;
	    }
	    :root.theme-light {
	      color-scheme: light;
	      --bg: #f5f7fb;
	      --panel: #ffffff;
	      --panel-2: #f0f4fa;
	      --panel-3: #e6edf7;
	      --line: #cbd5e1;
	      --line-soft: rgba(15, 23, 42, .14);
	      --text: #0f172a;
	      --muted: #475569;
	      --subtle: #64748b;
	      --accent: #e94560;
	      --accent-2: #2563eb;
	      --button: #2563eb;
	      --button-2: #dbe5f4;
	      --danger-2: #be123c;
	      --shadow: 0 16px 40px rgba(15, 23, 42, .12);
	    }
	    :root.theme-tokyo-night {
	      --bg: #1a1b26;
	      --panel: #24283b;
	      --panel-2: #16161e;
	      --panel-3: #2f3549;
	      --line: #414868;
	      --accent: #7aa2f7;
	      --accent-2: #2ac3de;
	      --button: #3659b8;
	      --button-2: #2f3549;
	      --danger: #f7768e;
	      --danger-2: #b84862;
	      --ok: #9ece6a;
	    }
	    :root.theme-nord {
	      --bg: #2e3440;
	      --panel: #3b4252;
	      --panel-2: #26303d;
	      --panel-3: #434c5e;
	      --line: #4c566a;
	      --text: #eceff4;
	      --muted: #d8dee9;
	      --subtle: #81a1c1;
	      --accent: #88c0d0;
	      --accent-2: #5e81ac;
	      --button: #5e81ac;
	      --button-2: #434c5e;
	      --danger: #bf616a;
	      --danger-2: #a94f5b;
	      --ok: #a3be8c;
	      --warn: #ebcb8b;
	    }
	    :root.theme-catppuccin-mocha {
	      --bg: #1e1e2e;
	      --panel: #181825;
	      --panel-2: #11111b;
	      --panel-3: #313244;
	      --line: #45475a;
	      --text: #cdd6f4;
	      --muted: #bac2de;
	      --subtle: #7f849c;
	      --accent: #cba6f7;
	      --accent-2: #89b4fa;
	      --button: #585b9b;
	      --button-2: #313244;
	      --danger: #f38ba8;
	      --danger-2: #b84d6d;
	      --ok: #a6e3a1;
	      --warn: #f9e2af;
	    }
	    :root.theme-rose-pine {
	      --bg: #191724;
	      --panel: #1f1d2e;
	      --panel-2: #12101a;
	      --panel-3: #26233a;
	      --line: #403d52;
	      --text: #e0def4;
	      --muted: #c9c6e3;
	      --subtle: #908caa;
	      --accent: #c4a7e7;
	      --accent-2: #9ccfd8;
	      --button: #31748f;
	      --button-2: #26233a;
	      --danger: #eb6f92;
	      --danger-2: #b64f70;
	      --ok: #9ccfd8;
	      --warn: #f6c177;
	    }
	    * { box-sizing: border-box; }
	    body {
	      margin: 0;
	      min-height: 100vh;
	      background: var(--bg);
	      color: var(--text);
	      font: var(--font-base)/1.45 var(--font-sans);
	    }
	    body.font-sm { --font-scale: .92; }
	    body.font-lg { --font-scale: 1.08; }
	    body.header-compact { --header-density: .84; }
	    body.header-large { --header-density: 1.18; }
	    header {
	      min-height: calc(62px * var(--header-density));
	      display: flex;
	      align-items: center;
	      justify-content: space-between;
	      gap: 18px;
	      padding: 0 22px;
	      border-bottom: 1px solid var(--line-soft);
	      background: var(--bg);
	      background: color-mix(in srgb, var(--bg) 92%, transparent);
	      backdrop-filter: blur(14px);
	      position: sticky;
	      top: 0;
      z-index: 4;
    }
    h1 { font-size: 17px; margin: 0; letter-spacing: -.01em; }
    h2 { font-size: 15px; margin: 0; letter-spacing: -.01em; }
    h3 { font-size: 14px; margin: 0; letter-spacing: -.01em; }
    p { margin: 0; }
	    main { max-width: 1560px; margin: 0 auto; padding: 22px; }
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
		    .console-shell { min-height: calc(100vh - 62px); }
		    .workspace-shell { display: grid; gap: 16px; }
		    .console-tabs-bar {
		      position: sticky;
		      top: calc(62px * var(--header-density));
		      z-index: 3;
		      display: flex;
		      align-items: center;
		      min-height: 48px;
		      padding: 6px 22px;
		      border-bottom: 1px solid var(--line-soft);
		      background: var(--bg);
		      background: color-mix(in srgb, var(--bg) 94%, transparent);
		      backdrop-filter: blur(14px);
		    }
		    .console-tabs {
		      display: flex;
		      width: 100%;
		      max-width: 100%;
		      gap: 4px;
		      padding: 4px;
		      border: 0;
		      border-radius: 0;
		      background: transparent;
		      overflow-x: auto;
		    }
		    .tab-button {
		      min-height: 38px;
		      padding: 8px 14px;
		      background: transparent;
		      color: var(--muted);
		      border: 1px solid transparent;
		      border-radius: 6px;
		      font-size: 13px;
		    }
	    .tab-button:hover,
	    .tab-button.active {
	      color: var(--text);
	      border-color: var(--line-soft);
	      background: var(--panel-3);
	    }
	    .tab-panel { display: none; }
	    .tab-panel.active { display: block; }
	    .tab-grid {
	      display: grid;
	      grid-template-columns: minmax(330px, 430px) minmax(0, 1fr);
	      gap: 18px;
	      align-items: start;
	    }
	    .tab-grid-wide { grid-template-columns: minmax(0, 1fr); }
	    .tab-stack { display: grid; gap: 16px; }
	    .session-summary {
	      display: flex;
	      align-items: center;
	      justify-content: space-between;
	      gap: 12px;
	      flex-wrap: wrap;
	      padding: 10px 12px;
	      margin-bottom: 14px;
	      border: 1px solid var(--line-soft);
	      border-radius: 8px;
	      background: rgba(255, 255, 255, .025);
	      color: var(--muted);
	    }
	    section {
	      border: 1px solid var(--line-soft);
	      background: var(--panel);
	      background: linear-gradient(180deg, color-mix(in srgb, var(--panel) 96%, white 4%), color-mix(in srgb, var(--panel) 92%, var(--bg) 8%));
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
      min-height: 34px;
      border: 1px solid var(--bg-tertiary);
      border-radius: var(--radius-sm);
      background: var(--bg-primary);
      color: var(--text-primary);
      padding: 7px 9px;
      font: inherit;
    }
    select {
      min-height: 34px;
      line-height: 1.2;
      padding: 6px 30px 6px 9px;
    }
    select:not([multiple]) {
      appearance: none;
      background-image:
        linear-gradient(45deg, transparent 50%, var(--text-secondary) 50%),
        linear-gradient(135deg, var(--text-secondary) 50%, transparent 50%);
      background-position:
        calc(100% - 14px) 50%,
        calc(100% - 9px) 50%;
      background-size: 5px 5px, 5px 5px;
      background-repeat: no-repeat;
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
	    button.secondary { background: var(--button-2); color: var(--text); }
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
	    body:not(.signed-in) .console-shell {
	      display: grid;
	      place-items: center;
	      padding-block: clamp(24px, 7vh, 72px);
	    }
	    body:not(.signed-in) .session-only { display: none !important; }
	    body.signed-in #login-section { display: none !important; }
	    .pill { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; color: var(--muted); margin: 0 6px 6px 0; }
	    .badge, .state-pill { display: inline-flex; align-items: center; border: 1px solid var(--line); border-radius: 999px; padding: 2px 8px; color: var(--muted); font-size: 12px; }
	    .state-pill.ok { color: var(--ok); border-color: rgba(52, 211, 153, .34); background: rgba(52, 211, 153, .08); }
	    .state-pill.off { color: var(--warn); border-color: rgba(245, 158, 11, .35); background: rgba(245, 158, 11, .08); }
	    .state-pill.covered { color: var(--cyan); border-color: rgba(32, 199, 223, .36); background: rgba(32, 199, 223, .08); }
	    .capability-derived td { color: var(--muted); }
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
	    .connection-wizard { display: grid; gap: 16px; }
	    .wizard-step {
	      border: 1px solid var(--line-soft);
	      border-radius: 8px;
	      padding: 12px;
	      background: rgba(255, 255, 255, .02);
	    }
	    .step-kicker {
	      color: var(--accent);
	      font-size: 11px;
	      font-weight: 900;
	      letter-spacing: .08em;
	      text-transform: uppercase;
	      margin-bottom: 4px;
	    }
		    .provider-preset-grid {
		      display: grid;
		      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
		      gap: 8px;
		      margin-top: 12px;
		    }
		    .provider-preset {
		      min-height: 64px;
	      display: grid;
	      gap: 6px;
	      justify-content: stretch;
	      align-items: start;
		      text-align: left;
		      background: var(--panel-3);
		      background: color-mix(in srgb, var(--panel-3) 82%, transparent);
		      border: 1px solid var(--line-soft);
		      padding: 10px;
		      white-space: normal;
		    }
	    .provider-preset:hover, .provider-preset.active {
	      border-color: rgba(45, 212, 191, .55);
	      background: rgba(45, 212, 191, .1);
	    }
	    .provider-preset strong { display: block; color: var(--text); line-height: 1.25; }
	    .provider-preset span { display: block; color: var(--muted); font-size: 12px; font-weight: 700; line-height: 1.3; margin-top: 3px; }
	    .setup-summary {
	      border: 1px solid rgba(45, 212, 191, .22);
	      border-radius: 8px;
	      padding: 10px 12px;
	      background: rgba(45, 212, 191, .06);
	      color: var(--muted);
	      margin-bottom: 12px;
	    }
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
	      max-height: calc(100vh - 44px);
	      overflow: auto;
	      border: 1px solid var(--line);
	      border-radius: 8px;
	      background: var(--panel);
	      box-shadow: 0 28px 70px rgba(0, 0, 0, .46);
	      padding: 18px;
	    }
	    .modal.wide { width: min(680px, 100%); }
	    .modal.flow-modal {
	      width: min(520px, calc(100vw - 48px));
	      max-width: min(520px, calc(100vw - 48px));
	      padding: 0;
	      display: flex;
	      flex-direction: column;
	      overflow: hidden;
	      border: 1px solid rgba(255, 255, 255, .12);
	      border-radius: 8px;
	      background: var(--bg-secondary);
	      box-shadow: 0 24px 80px rgba(0, 0, 0, .55);
	    }
	    .flow-modal .modal-header {
	      flex: 0 0 auto;
	      padding: 16px 18px 12px;
	      border-bottom: 1px solid rgba(255, 255, 255, .08);
	      background: rgba(255, 255, 255, .02);
	    }
	    .flow-modal .modal-header h2 {
	      margin: 0 0 5px;
	      font-size: var(--font-size-lg);
	      line-height: 1.25;
	    }
	    .flow-modal .modal-header p {
	      margin: 0;
	      color: var(--text-secondary);
	      font-size: var(--font-size-sm);
	      line-height: 1.35;
	    }
	    .flow-modal .modal-body {
	      flex: 1 1 auto;
	      min-height: 0;
	      overflow: auto;
	      padding: 14px 18px 16px;
	    }
	    .flow-modal .modal-actions {
	      flex: 0 0 auto;
	      position: static;
	      margin: 0;
	      padding: 12px 18px;
	      border-top: 1px solid rgba(255, 255, 255, .08);
	      background: rgba(255, 255, 255, .02);
	    }
	    .default-modal {
	      width: min(500px, calc(100vw - 48px));
	      max-width: min(500px, calc(100vw - 48px));
	    }
	    .modal.provider-modal {
	      width: min(720px, 100%);
	      padding: 14px 14px 0;
	    }
	    .modal h2 { margin-bottom: 8px; }
	    .modal p { color: var(--muted); margin-bottom: 16px; overflow-wrap: anywhere; }
	    .modal-actions {
	      position: sticky;
	      bottom: -18px;
	      display: flex;
	      justify-content: flex-end;
	      gap: 10px;
	      margin: 10px -18px -18px;
	      padding: 12px 18px 18px;
	      background: linear-gradient(180deg, rgba(22, 27, 44, .78), var(--panel) 34%);
	    }
	    .capability-table td { vertical-align: middle; }
	    .capability-route { display: grid; gap: 3px; }
	    .capability-route code { width: fit-content; }
	    .default-config-form, .provider-config-form { display: grid; gap: 12px; margin-top: 16px; }
	    .provider-modal .provider-config-form {
	      grid-template-columns: repeat(2, minmax(0, 1fr));
	      gap: 8px 12px;
	      margin-top: 8px;
	    }
	    .provider-modal input,
	    .provider-modal select {
	      min-height: 36px;
	      padding: 7px 10px;
	      line-height: 1.35;
	    }
	    .provider-modal .inline {
	      display: contents;
	    }
	    .provider-modal label {
	      margin-bottom: 0;
	      gap: 5px;
	    }
	    .provider-modal textarea {
	      min-height: 42px;
	      max-height: 60px;
	    }
	    .provider-modal .field-help {
	      margin: -2px 0 0;
	      font-size: 11px;
	    }
	    .provider-modal .field-span-2 {
	      grid-column: 1 / -1;
	    }
	    .provider-modal .provider-toggle-row {
	      grid-column: 1 / -1;
	      display: flex;
	      align-items: center;
	      justify-content: space-between;
	      gap: 12px;
	      flex-wrap: wrap;
	      padding-top: 2px;
	    }
	    .provider-modal .provider-toggle-row .af-gateway-signin__checkbox {
	      font-size: 13px;
	    }
	    .provider-modal .advanced-panel summary {
	      padding: 7px 10px;
	    }
	    .provider-modal .advanced-panel__body {
	      padding: 0 10px 10px;
	    }
	    .provider-modal .modal-actions {
	      margin: 8px -14px 0;
	      padding: 12px 14px 14px;
	      border-top: 1px solid rgba(255, 255, 255, .08);
	      background: rgba(255, 255, 255, .02);
	    }
	    .default-config-form {
	      gap: 10px;
	      margin-top: 0;
	    }
	    .default-config-form label {
	      gap: 5px;
	      margin-bottom: 0;
	      color: var(--text-secondary);
	      font-size: var(--font-size-xs);
	    }
	    .default-config-form select {
	      min-height: 32px;
	      padding-top: 5px;
	      padding-bottom: 5px;
	    }
	    .default-modal-route {
	      display: inline-flex;
	      width: fit-content;
	      margin-top: 10px;
	      border: 1px solid rgba(255, 255, 255, .14);
	      border-radius: 999px;
	      padding: 4px 10px;
	      color: var(--text-secondary);
	      background: rgba(255, 255, 255, .05);
	      font-size: var(--font-size-xs);
	      font-weight: 700;
	      text-transform: none;
	      letter-spacing: 0;
	    }
	    .provider-modal-grid { display: grid; gap: 12px; }
	    .providers-workspace { display: grid; gap: 16px; }
	    .providers-workspace #provider-preset-grid { grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); }
	    .sandbox-workspace {
	      display: grid;
	      grid-template-columns: minmax(0, 1fr);
	      gap: 18px;
	      align-items: stretch;
	    }
	    .sandbox-mode-grid {
	      display: flex;
	      flex-wrap: wrap;
	      justify-content: flex-end;
	      gap: 7px;
	      margin: 0;
	    }
	    .sandbox-mode {
	      display: inline-grid;
	      place-items: center;
	      position: relative;
	      width: 38px;
	      min-width: 38px;
	      height: 38px;
	      min-height: 38px;
	      padding: 0;
	      border: 1px solid var(--line-soft);
	      border-radius: 13px;
	      background: rgba(255, 255, 255, .03);
	      color: var(--text-secondary);
	      text-align: center;
	    }
	    .sandbox-mode:hover:not(:disabled) {
	      border-color: rgba(47, 229, 218, .45);
	      background: rgba(47, 229, 218, .08);
	      color: var(--text-primary);
	    }
	    .sandbox-mode.active {
	      border-color: rgba(47, 229, 218, .75);
	      background: rgba(47, 229, 218, .10);
	      color: var(--text-primary);
	    }
	    .sandbox-mode:disabled {
	      opacity: .46;
	      cursor: not-allowed;
	    }
	    .sandbox-mode-icon {
	      display: inline-grid;
	      place-items: center;
	      width: 30px;
	      height: 30px;
	      border-radius: 11px;
	      background: rgba(47, 229, 218, .10);
	      color: var(--accent);
	      font-weight: 900;
	    }
	    .sandbox-mode-icon svg {
	      width: 18px;
	      height: 18px;
	      stroke: currentColor;
	      stroke-width: 2;
	      stroke-linecap: round;
	      stroke-linejoin: round;
	      fill: none;
	    }
	    .sandbox-mode-icon svg.fill {
	      fill: currentColor;
	      stroke: none;
	    }
	    .sandbox-mode-copy {
	      position: absolute;
	      width: 1px;
	      height: 1px;
	      overflow: hidden;
	      clip: rect(0 0 0 0);
	      white-space: nowrap;
	    }
	    .sandbox-mode-main {
	      display: none;
	      overflow: hidden;
	      text-overflow: ellipsis;
	      white-space: nowrap;
	      font-weight: 900;
	    }
	    .sandbox-mode-sub {
	      display: none;
	      overflow: hidden;
	      text-overflow: ellipsis;
	      white-space: nowrap;
	      color: var(--text-muted);
	      font-size: var(--font-size-xs);
	      font-weight: 700;
	      margin-top: 2px;
	    }
	    .sandbox-chat {
	      min-height: 560px;
	      max-height: calc(100vh - 205px);
	      display: grid;
	      grid-template-rows: auto minmax(260px, 1fr) auto;
	      overflow: hidden;
	    }
	    .sandbox-chat .section-head {
	      margin-bottom: 0;
	      padding-bottom: 12px;
	      border-bottom: 1px solid var(--line-soft);
	    }
	    .sandbox-transcript {
	      min-height: 0;
	      max-height: none;
	      overflow-y: auto;
	      padding: 18px;
	      background:
	        linear-gradient(180deg, rgba(255,255,255,.025), rgba(255,255,255,0)),
	        rgba(0, 0, 0, .10);
	    }
	    .sandbox-message {
	      display: flex;
	      margin: 0 0 12px;
	    }
	    .sandbox-message.user {
	      justify-content: flex-end;
	    }
	    .sandbox-message.assistant,
	    .sandbox-message.system,
	    .sandbox-message.error {
	      justify-content: flex-start;
	    }
	    .sandbox-bubble {
	      width: min(760px, 88%);
	      border: 1px solid var(--line-soft);
	      border-radius: 12px;
	      padding: 10px 12px;
	      background: rgba(255, 255, 255, .035);
	      box-shadow: 0 10px 28px rgba(0, 0, 0, .12);
	    }
	    .sandbox-message.user .sandbox-bubble {
	      border-color: rgba(58, 108, 255, .38);
	      background: rgba(58, 108, 255, .13);
	    }
	    .sandbox-message.error .sandbox-bubble {
	      border-color: rgba(255, 80, 124, .48);
	      background: rgba(255, 80, 124, .08);
	    }
	    .sandbox-message-meta {
	      display: flex;
	      align-items: center;
	      gap: 8px;
	      margin-bottom: 6px;
	      color: var(--text-muted);
	      font-size: var(--font-size-xs);
	      font-weight: 800;
	    }
	    .sandbox-message-role {
	      color: var(--accent);
	      font-size: var(--font-size-sm);
	      font-weight: 900;
	    }
	    .sandbox-message-spacer {
	      flex: 1;
	    }
	    .sandbox-message-body {
	      white-space: pre-wrap;
	      color: var(--text-primary);
	      line-height: 1.45;
	    }
	    .sandbox-message-body.markdown {
	      white-space: normal;
	    }
	    .sandbox-message-body.markdown > :first-child {
	      margin-top: 0;
	    }
	    .sandbox-message-body.markdown > :last-child {
	      margin-bottom: 0;
	    }
	    .sandbox-message-body.markdown p {
	      margin: 0 0 10px;
	    }
	    .sandbox-message-body.markdown h1,
	    .sandbox-message-body.markdown h2,
	    .sandbox-message-body.markdown h3 {
	      margin: 12px 0 8px;
	      color: var(--text-primary);
	      font-weight: 900;
	      letter-spacing: 0;
	      line-height: 1.2;
	    }
	    .sandbox-message-body.markdown h1 { font-size: 1.18em; }
	    .sandbox-message-body.markdown h2 { font-size: 1.10em; }
	    .sandbox-message-body.markdown h3 { font-size: 1.03em; }
	    .sandbox-message-body.markdown ul,
	    .sandbox-message-body.markdown ol {
	      margin: 8px 0 10px 22px;
	      padding: 0;
	    }
	    .sandbox-message-body.markdown li {
	      margin: 4px 0;
	    }
	    .sandbox-message-body.markdown blockquote {
	      margin: 10px 0;
	      border-left: 3px solid rgba(47, 229, 218, .45);
	      padding: 4px 0 4px 12px;
	      color: var(--text-secondary);
	    }
	    .sandbox-message-body.markdown code {
	      border: 1px solid rgba(255, 255, 255, .10);
	      border-radius: 6px;
	      padding: 1px 5px;
	      background: rgba(0, 0, 0, .26);
	      color: var(--text-primary);
	      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
	      font-size: .93em;
	    }
	    .sandbox-message-body.markdown pre {
	      overflow: auto;
	      margin: 10px 0;
	      border: 1px solid var(--line-soft);
	      border-radius: 10px;
	      padding: 10px 12px;
	      background: rgba(0, 0, 0, .28);
	    }
	    .sandbox-message-body.markdown pre code {
	      border: 0;
	      padding: 0;
	      background: transparent;
	    }
	    .sandbox-message-body.markdown a {
	      color: var(--accent-2);
	      font-weight: 800;
	    }
	    .sandbox-speak {
	      width: 30px;
	      min-width: 30px;
	      height: 28px;
	      padding: 0;
	    }
	    .sandbox-speak.speaking {
	      border-color: rgba(47, 229, 218, .70);
	      color: var(--accent);
	    }
	    .sandbox-progress {
	      display: grid;
	      gap: 8px;
	      margin-top: 8px;
	    }
	    .sandbox-progress-bar {
	      position: relative;
	      height: 6px;
	      overflow: hidden;
	      border-radius: 999px;
	      background: rgba(255, 255, 255, .08);
	    }
	    .sandbox-progress-bar::before {
	      content: "";
	      position: absolute;
	      inset: 0;
	      width: 38%;
	      border-radius: inherit;
	      background: linear-gradient(90deg, var(--accent), var(--accent-2));
	      animation: sandbox-progress 1.25s ease-in-out infinite;
	    }
	    @keyframes sandbox-progress {
	      0% { transform: translateX(-110%); }
	      100% { transform: translateX(280%); }
	    }
	    .sandbox-artifact {
	      display: grid;
	      gap: 8px;
	      margin-top: 10px;
	    }
	    .sandbox-artifact img,
	    .sandbox-artifact video {
	      max-width: 100%;
	      max-height: min(440px, 48vh);
	      border: 1px solid var(--line-soft);
	      border-radius: 10px;
	      background: rgba(0, 0, 0, .22);
	      object-fit: contain;
	    }
	    .sandbox-artifact audio {
	      width: 100%;
	      min-width: 260px;
	    }
	    .sandbox-media-error {
	      border: 1px solid rgba(255, 80, 124, .35);
	      border-radius: 8px;
	      padding: 8px 10px;
	      color: #ff9db2;
	      background: rgba(255, 80, 124, .08);
	      font-size: var(--font-size-sm);
	      font-weight: 800;
	    }
	    .sandbox-artifact-link {
	      display: inline-flex;
	      align-items: center;
	      gap: 6px;
	      width: fit-content;
	      color: var(--accent-2);
	      font-weight: 900;
	    }
	    .sandbox-composer {
	      border-top: 1px solid var(--line-soft);
	      padding: 14px 16px 16px;
	      background: rgba(0, 0, 0, .16);
	    }
	    .sandbox-composer-toolbar {
	      display: grid;
	      grid-template-columns: minmax(0, 1fr);
	      gap: 10px;
	      align-items: end;
	      margin-bottom: 10px;
	    }
	    .sandbox-system-compact {
	      margin: 0;
	    }
	    .sandbox-system-compact input {
	      min-height: 34px;
	      padding-top: 6px;
	      padding-bottom: 6px;
	    }
	    .sandbox-dropzone {
	      display: grid;
	      grid-template-columns: auto minmax(0, 1fr) auto;
	      gap: 12px;
	      align-items: end;
	      border: 1px solid var(--line-soft);
	      border-radius: 26px;
	      padding: 10px 12px;
	      background:
	        linear-gradient(180deg, rgba(255, 255, 255, .035), rgba(255, 255, 255, .015)),
	        rgba(2, 8, 24, .78);
	      box-shadow: inset 0 1px 0 rgba(255, 255, 255, .05);
	    }
	    .sandbox-dropzone:focus-within {
	      border-color: rgba(47, 229, 218, .62);
	      box-shadow: 0 0 0 3px rgba(47, 229, 218, .10), inset 0 1px 0 rgba(255, 255, 255, .05);
	    }
	    .sandbox-dropzone.dragover {
	      border-color: rgba(47, 229, 218, .75);
	      box-shadow: 0 0 0 3px rgba(47, 229, 218, .12);
	    }
	    .sandbox-input-area {
	      display: grid;
	      min-width: 0;
	    }
	    .sandbox-dropzone textarea {
	      min-height: 56px;
	      max-height: 170px;
	      resize: vertical;
	      border: 0;
	      padding: 8px 2px;
	      background: transparent;
	      color: var(--text-primary);
	      font-size: var(--font-size-md);
	      line-height: 1.45;
	      box-shadow: none;
	    }
	    .sandbox-dropzone textarea:focus {
	      box-shadow: none;
	    }
	    .sandbox-composer-side {
	      display: grid;
	      gap: 8px;
	      align-items: end;
	      justify-items: end;
	      align-self: stretch;
	      align-content: end;
	    }
	    .sandbox-composer-actions {
	      display: flex;
	      align-items: center;
	      justify-content: flex-end;
	      gap: 8px;
	    }
	    .sandbox-composer-icon,
	    .sandbox-send {
	      width: 40px;
	      min-width: 40px;
	      height: 40px;
	      min-height: 40px;
	      border-radius: 14px;
	      padding: 0;
	    }
	    .sandbox-send {
	      background: linear-gradient(135deg, #2563eb, #3a6cff);
	      border-color: rgba(96, 165, 250, .36);
	    }
	    .sandbox-send .button-icon,
	    .sandbox-composer-icon .button-icon {
	      margin: 0;
	    }
	    .sandbox-attachments {
	      display: flex;
	      flex-wrap: wrap;
	      gap: 6px;
	      margin: 8px 0 0;
	    }
	    .sandbox-attachment {
	      display: inline-flex;
	      align-items: center;
	      gap: 6px;
	      max-width: 260px;
	      border: 1px solid var(--line-soft);
	      border-radius: 999px;
	      padding: 4px 8px;
	      color: var(--text-secondary);
	      background: rgba(255, 255, 255, .04);
	      font-size: var(--font-size-xs);
	      font-weight: 800;
	    }
	    .sandbox-attachment span {
	      overflow: hidden;
	      text-overflow: ellipsis;
	      white-space: nowrap;
	    }
	    .appearance-form {
	      display: grid;
	      grid-template-columns: 140px minmax(0, 1fr);
	      gap: 12px;
	      align-items: center;
	      margin: 18px 0;
	    }
	    .appearance-form label {
	      margin: 0;
	    }
	    .appearance-control {
	      display: grid;
	      gap: 8px;
	    }
	    .theme-swatches { display: flex; gap: 4px; flex-wrap: wrap; }
	    .theme-swatch {
	      width: 16px;
	      height: 16px;
	      border-radius: 4px;
	      border: 1px solid rgba(255, 255, 255, .16);
	    }
	    .icon-only {
	      width: 36px;
	      min-width: 36px;
	      padding: 0;
	    }
	    @media (max-width: 940px) {
	      .tab-grid { grid-template-columns: 1fr; }
	      .sandbox-workspace { grid-template-columns: 1fr; }
	      .sandbox-composer-toolbar { grid-template-columns: 1fr; }
	      .sandbox-mode-grid { justify-content: flex-start; }
	      header { align-items: flex-start; flex-direction: column; padding: 14px 18px; }
	      .console-tabs-bar { top: 0; }
	      main { padding: 16px; }
	    }
	    @media (max-width: 680px) {
	      body:not(.signed-in) .console-shell { align-content: start; padding-block: 18px; }
	      .af-gateway-signin { padding: 18px; }
	      .af-gateway-signin__hero { gap: 16px; }
	      .af-gateway-signin__mark { width: 56px; height: 56px; border-radius: 18px; font-size: 26px; }
	      .af-gateway-signin__form { grid-template-columns: 1fr; }
	      .appearance-form { grid-template-columns: 1fr; }
	      .provider-modal .provider-config-form { grid-template-columns: 1fr; }
	      .provider-modal .field-span-2 { grid-column: auto; }
	      .sandbox-dropzone { grid-template-columns: auto minmax(0, 1fr); }
	      .sandbox-composer-side {
	        grid-column: 1 / -1;
	        grid-template-columns: minmax(0, 1fr) auto;
	        align-items: center;
	        justify-items: stretch;
	      }
	      .sandbox-composer-actions { justify-content: flex-end; }
	    }
	  </style>
</head>
<body>
	  <header>
	    <div class="brand">
	      <div class="brand-mark" aria-hidden="true">↔</div>
	      <div>
	        <h1>AbstractGateway Console</h1>
	        <div class="brand-subtitle">Users, runtimes, provider connections, and multimodal capabilities</div>
	      </div>
	    </div>
	    <div class="status">
	      <span id="status-dot" class="dot"></span>
	      <span id="status-text">Signed out</span>
	      <button id="open-appearance" class="secondary icon-only" title="Appearance" aria-label="Appearance">◐</button>
	      <button id="sign-out" class="secondary hidden"><span class="button-icon" aria-hidden="true">×</span><span>Sign out</span></button>
	    </div>
	  </header>
	  <div id="console-tabs-bar" class="console-tabs-bar session-only">
	    <nav class="console-tabs" aria-label="Gateway console sections">
	      <button id="tab-button-users" class="tab-button" type="button"><span class="button-icon" aria-hidden="true">◎</span><span>Users &amp; Runtimes</span></button>
	      <button id="tab-button-providers" class="tab-button" type="button"><span class="button-icon" aria-hidden="true">◇</span><span>Providers</span></button>
	      <button id="tab-button-defaults" class="tab-button" type="button"><span class="button-icon" aria-hidden="true">◆</span><span>Multimodal Capabilities</span></button>
	      <button id="tab-button-sandbox" class="tab-button" type="button"><span class="button-icon" aria-hidden="true">▶</span><span>Sandbox</span></button>
	    </nav>
	  </div>
	  <main class="console-shell">
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

	    <div id="workspace-shell" class="workspace-shell session-only">
	      <div id="tab-users" class="tab-panel">
	        <div id="account" class="session-summary">No active session.</div>
	        <div class="tab-grid tab-grid-wide">
	          <div class="tab-stack">
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
	          </div>
	        </div>
	      </div>

	      <div id="tab-providers" class="tab-panel">
	        <div class="providers-workspace">
	          <section id="provider-setup-section" class="session-only">
	            <div class="section-head">
	              <div>
	                <h2 class="section-title"><span class="section-icon" aria-hidden="true">◇</span><span>Provider Connections</span></h2>
	                <p class="section-note">Pick a provider type to configure. Gateway stores API keys and endpoint URLs server-side, then exposes the connection as an available provider for Flow nodes and Core capability defaults.</p>
	              </div>
	            </div>
	            <div id="provider-preset-grid" class="provider-preset-grid"></div>
	          </section>

	          <section class="session-only">
	            <div class="section-head">
	              <div>
	                <h2 class="section-title"><span class="section-icon" aria-hidden="true">◇</span><span>Available Providers</span></h2>
	                <p class="section-note">Configured providers available to this Gateway principal. Use their provider ids in Flow nodes and Core capability defaults.</p>
	              </div>
	            </div>
	            <table>
	              <thead><tr><th>Name</th><th>Provider ID</th><th>Type</th><th>Models</th><th>Status</th><th>Actions</th></tr></thead>
	              <tbody id="endpoint-profiles-table"></tbody>
	            </table>
	          </section>
	        </div>
	      </div>

	      <div id="tab-defaults" class="tab-panel">
	        <section id="defaults-section" class="session-only">
	          <div class="section-head">
	            <div>
	              <h2 class="section-title"><span class="section-icon" aria-hidden="true">◆</span><span>Multimodal Capabilities</span></h2>
	              <p id="defaults-scope" class="section-note">Sign in to edit provider/model defaults for this Gateway runtime.</p>
	            </div>
	            <button id="refresh-catalog" class="secondary"><span class="button-icon" aria-hidden="true">↻</span><span>Refresh</span></button>
	          </div>
		          <table class="capability-table">
		            <thead><tr><th>Route</th><th>Capability</th><th>Provider</th><th>Model</th><th>Source</th><th>Status</th><th>Actions</th></tr></thead>
		            <tbody id="defaults-table"></tbody>
		          </table>
	          <div id="defaults-message" class="message"></div>
	        </section>
	      </div>

	      <div id="tab-sandbox" class="tab-panel">
	        <div class="sandbox-workspace">
	          <section class="session-only sandbox-chat">
	            <div class="section-head">
	              <div>
	                <h2 class="section-title"><span class="section-icon" aria-hidden="true">◌</span><span>Sandbox Chat</span></h2>
	                <p id="sandbox-context" class="section-note">Select a provider/model and run a smoke test.</p>
	              </div>
	            </div>
	            <div id="sandbox-transcript" class="sandbox-transcript"></div>
	            <div class="sandbox-composer">
	              <label class="hidden">Capability<select id="sandbox-capability"></select></label>
	              <label id="sandbox-provider-label" class="hidden">Provider<select id="sandbox-provider"></select></label>
	              <label id="sandbox-model-label" class="hidden">Model<select id="sandbox-model"></select></label>
	              <div class="sandbox-composer-toolbar">
	                <label id="sandbox-system-label" class="sandbox-system-compact">System prompt<input id="sandbox-system" placeholder="optional"></label>
	              </div>
	              <div id="sandbox-dropzone" class="sandbox-dropzone">
	                <button id="sandbox-attach" class="secondary icon-only sandbox-composer-icon" title="Attach files" aria-label="Attach files"><span class="button-icon" aria-hidden="true">＋</span></button>
	                <div class="sandbox-input-area">
	                  <textarea id="sandbox-prompt" placeholder="Ask a question, describe an image/video/music request, or drop files here."></textarea>
	                  <div id="sandbox-attachments" class="sandbox-attachments"></div>
	                </div>
	                <div class="sandbox-composer-side">
	                  <div id="sandbox-output-modes" class="sandbox-mode-grid" role="radiogroup" aria-label="Sandbox output mode"></div>
	                  <div class="sandbox-composer-actions">
	                    <button id="sandbox-clear" class="secondary icon-only sandbox-composer-icon" title="Clear chat" aria-label="Clear chat"><span class="button-icon" aria-hidden="true">×</span></button>
	                    <button id="sandbox-run" class="sandbox-send icon-only" title="Send" aria-label="Send"><span class="button-icon" aria-hidden="true">▶</span></button>
	                  </div>
	                </div>
	              </div>
	              <input id="sandbox-file-input" class="hidden" type="file" multiple>
	              <div id="sandbox-message" class="message"></div>
	            </div>
	          </section>
	        </div>
	      </div>
	    </div>
	  </main>
	  <div id="default-modal-backdrop" class="modal-backdrop hidden" role="presentation">
	    <div class="modal flow-modal default-modal" role="dialog" aria-modal="true" aria-labelledby="default-modal-title">
	      <div class="modal-header">
	        <h2 id="default-modal-title">Configure capability default</h2>
	        <p id="default-modal-description">Select a provider and one of its discovered models.</p>
	        <div id="default-modal-route" class="default-modal-route"></div>
	      </div>
	      <div class="modal-body">
	        <div class="default-config-form">
	          <label>Provider<select id="modal-default-provider"></select></label>
	          <label>Model<select id="modal-default-model"></select></label>
	          <label id="modal-default-voice-label" class="hidden">Voice<select id="modal-default-voice"></select></label>
	          <div id="default-modal-message" class="message"></div>
	        </div>
	      </div>
	      <div class="modal-actions">
	        <button id="close-default-modal" class="secondary">Cancel</button>
	        <button id="clear-default" class="secondary"><span class="button-icon" aria-hidden="true">×</span><span>Clear</span></button>
	        <button id="save-default"><span class="button-icon" aria-hidden="true">✓</span><span>Save</span></button>
	      </div>
	    </div>
	  </div>
	  <div id="provider-modal-backdrop" class="modal-backdrop hidden" role="presentation">
	    <div class="modal wide provider-modal" role="dialog" aria-modal="true" aria-labelledby="provider-modal-title">
	      <h2 id="provider-modal-title">Configure provider</h2>
	      <p id="provider-modal-description">Gateway stores endpoint details and keys server-side, then exposes this connection as an available provider.</p>
	      <input id="endpoint-id" type="hidden">
	      <div class="provider-config-form">
	        <div class="inline">
	          <label>Provider type<select id="endpoint-provider-family"></select></label>
	          <label>Who can use it?<select id="endpoint-scope"></select></label>
	        </div>
	        <div class="inline">
	          <label>Provider ID<input id="endpoint-profile-id" placeholder="openai"></label>
	          <label>Name<input id="endpoint-name" placeholder="OpenAI"></label>
	        </div>
	        <label class="field-span-2">Description<textarea id="endpoint-description" placeholder="What this provider is for, who owns it, and when to use it."></textarea></label>
	        <label>Base URL<input id="endpoint-base-url" placeholder="optional; leave blank for provider default"></label>
	        <label>API key<input id="endpoint-api-key" type="password" placeholder="leave blank to keep existing key"></label>
	        <p id="endpoint-base-url-help" class="field-help"></p>
	        <p id="endpoint-api-key-help" class="field-help"></p>
	        <div class="provider-toggle-row">
	          <label class="af-gateway-signin__checkbox"><input id="endpoint-clear-api-key" type="checkbox"> Clear stored API key</label>
	          <label class="af-gateway-signin__checkbox"><input id="endpoint-enabled" type="checkbox" checked> Enabled</label>
	        </div>
	        <div class="model-picker field-span-2">
	          <details class="advanced-panel">
	            <summary>Advanced: restrict visible models after testing</summary>
	            <div class="advanced-panel__body">
	              <p class="field-help">Optional. Use Test to preview discovery, then select models only when this provider should expose a fixed allowlist.</p>
	              <div id="endpoint-model-summary" class="model-summary">Not tested yet.</div>
	              <select id="endpoint-models" class="model-picker__select" multiple size="7"></select>
	              <button id="clear-endpoint-models" type="button" class="secondary"><span class="button-icon" aria-hidden="true">×</span><span>Clear restriction</span></button>
	            </div>
	          </details>
	        </div>
	        <div id="endpoint-message" class="message field-span-2"></div>
	      </div>
	      <div class="modal-actions">
	        <button id="cancel-endpoint-profile" class="secondary">Cancel</button>
	        <button id="discover-endpoint-models" type="button" class="secondary"><span class="button-icon" aria-hidden="true">↻</span><span>Test</span></button>
	        <button id="save-endpoint-profile"><span class="button-icon" aria-hidden="true">✓</span><span>Confirm</span></button>
	      </div>
	    </div>
	  </div>
	  <div id="appearance-backdrop" class="modal-backdrop hidden" role="presentation">
	    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="appearance-title">
	      <h2 id="appearance-title">Appearance</h2>
	      <p>Theme and typography are stored locally in this browser.</p>
	      <div class="appearance-form">
	        <label for="appearance-theme">Theme</label>
	        <div class="appearance-control">
	          <select id="appearance-theme"></select>
	          <div id="appearance-swatches" class="theme-swatches" aria-hidden="true"></div>
	        </div>
	        <label for="appearance-font-size">Font size</label>
	        <select id="appearance-font-size">
	          <option value="sm">Compact</option>
	          <option value="md">Medium</option>
	          <option value="lg">Large</option>
	        </select>
	        <label for="appearance-header-size">Header size</label>
	        <select id="appearance-header-size">
	          <option value="compact">Compact</option>
	          <option value="standard">Standard</option>
	          <option value="large">Large</option>
	        </select>
	      </div>
	      <div class="modal-actions">
	        <button id="appearance-close" class="secondary">Close</button>
	      </div>
	    </div>
	  </div>
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
		    const state = { principal: null, users: [], defaults: [], providers: [], providerLabels: new Map(), voiceLabels: new Map(), providerModels: new Map(), endpointProfiles: [], endpointModelOptions: [], sandboxMessages: [], sandboxAttachments: [], sandboxObjectUrls: [], activeProviderPreset: "openai", activeTab: "providers", activeDefaultRow: null, confirmResolve: null, appearance: null };
		    const $ = (id) => document.getElementById(id);
		    const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
		    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch] || ch);
	    function renderMarkdownInline(value) {
	      const renderPlain = (text) => {
	        const codeParts = [];
	        const marker = (index) => `%%AF_CODE_${index}%%`;
	        let tokenized = String(text || "").replace(/`([^`\\n]+)`/g, (_match, code) => {
	          const key = marker(codeParts.length);
	          codeParts.push(`<code>${esc(code)}</code>`);
	          return key;
	        });
	        let html = esc(tokenized);
	        html = html.replace(/[*][*]([^*\\n]+)[*][*]/g, "<strong>$1</strong>");
	        html = html.replace(/(^|[ (])[*]([^*\\n]+)[*]/g, "$1<em>$2</em>");
	        for (let i = 0; i < codeParts.length; i += 1) {
	          html = html.replace(marker(i), codeParts[i]);
	        }
	        return html;
	      };
	      const parts = [];
	      const source = String(value || "");
	      const linkRe = /\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)/g;
	      let cursor = 0;
	      let match = null;
	      while ((match = linkRe.exec(source))) {
	        parts.push(renderPlain(source.slice(cursor, match.index)));
	        parts.push(`<a href="${esc(match[2])}" target="_blank" rel="noopener">${renderPlain(match[1])}</a>`);
	        cursor = match.index + match[0].length;
	      }
	      parts.push(renderPlain(source.slice(cursor)));
	      return parts.join("");
	    }
	    function renderMarkdown(value) {
	      const lines = String(value ?? "").replace(/\\r\\n?/g, "\\n").split("\\n");
	      const out = [];
	      let paragraph = [];
	      let listType = "";
	      let inCode = false;
	      let codeLines = [];
	      const closeParagraph = () => {
	        if (!paragraph.length) return;
	        out.push(`<p>${renderMarkdownInline(paragraph.join(" "))}</p>`);
	        paragraph = [];
	      };
	      const closeList = () => {
	        if (!listType) return;
	        out.push(`</${listType}>`);
	        listType = "";
	      };
	      const openList = (type) => {
	        if (listType === type) return;
	        closeList();
	        listType = type;
	        out.push(`<${type}>`);
	      };
	      for (const rawLine of lines) {
	        const line = String(rawLine || "");
	        const trimmed = line.trim();
	        if (trimmed.startsWith("```")) {
	          if (inCode) {
	            out.push(`<pre><code>${esc(codeLines.join("\\n"))}</code></pre>`);
	            codeLines = [];
	            inCode = false;
	          } else {
	            closeParagraph();
	            closeList();
	            inCode = true;
	          }
	          continue;
	        }
	        if (inCode) {
	          codeLines.push(line);
	          continue;
	        }
	        if (!trimmed) {
	          closeParagraph();
	          closeList();
	          continue;
	        }
	        const heading = trimmed.match(/^(#{1,3})\\s+(.+)$/);
	        if (heading) {
	          closeParagraph();
	          closeList();
	          const level = Math.min(3, heading[1].length);
	          out.push(`<h${level}>${renderMarkdownInline(heading[2])}</h${level}>`);
	          continue;
	        }
	        const bullet = trimmed.match(/^[-*]\\s+(.+)$/);
	        if (bullet) {
	          closeParagraph();
	          openList("ul");
	          out.push(`<li>${renderMarkdownInline(bullet[1])}</li>`);
	          continue;
	        }
	        const ordered = trimmed.match(/^\\d+[.)]\\s+(.+)$/);
	        if (ordered) {
	          closeParagraph();
	          openList("ol");
	          out.push(`<li>${renderMarkdownInline(ordered[1])}</li>`);
	          continue;
	        }
	        const quote = trimmed.match(/^>\\s?(.+)$/);
	        if (quote) {
	          closeParagraph();
	          closeList();
	          out.push(`<blockquote>${renderMarkdownInline(quote[1])}</blockquote>`);
	          continue;
	        }
	        closeList();
	        paragraph.push(line);
	      }
	      if (inCode) out.push(`<pre><code>${esc(codeLines.join("\\n"))}</code></pre>`);
	      closeParagraph();
	      closeList();
	      return out.join("") || "";
	    }
	    function setSandboxMessageBody(body, content, { markdown = false } = {}) {
	      if (!body) return;
	      if (markdown) {
	        body.classList.add("markdown");
	        body.innerHTML = renderMarkdown(content);
	      } else {
	        body.classList.remove("markdown");
	        body.textContent = String(content || "");
	      }
	    }
	    const UI_SETTINGS_KEY = "abstractgateway_ui_settings_v1";
	    const ACTIVE_TAB_KEY = "abstractgateway_active_tab_v1";
	    const TABS = ["users", "providers", "defaults", "sandbox"];
	    const THEME_SPECS = [
	      { id: "dark", label: "Dark (Abstract)", swatches: ["#1a1a2e", "#16213e", "#0f3460", "#e94560", "#60a5fa", "#27ae60"] },
	      { id: "tokyo-night", label: "Tokyo Night", swatches: ["#1a1b26", "#24283b", "#414868", "#7aa2f7", "#2ac3de", "#9ece6a"] },
	      { id: "catppuccin-mocha", label: "Catppuccin Mocha", swatches: ["#1e1e2e", "#181825", "#313244", "#cba6f7", "#89b4fa", "#a6e3a1"] },
	      { id: "rose-pine", label: "Rose Pine", swatches: ["#191724", "#1f1d2e", "#26233a", "#c4a7e7", "#31748f", "#9ccfd8"] },
	      { id: "nord", label: "Nord", swatches: ["#2e3440", "#3b4252", "#434c5e", "#88c0d0", "#5e81ac", "#a3be8c"] },
	      { id: "light", label: "Light", swatches: ["#f7f7fb", "#ffffff", "#e6e8f0", "#e94560", "#2563eb", "#16a34a"] },
	    ];
	    function readJsonSetting(key, fallback) {
	      try {
	        const raw = localStorage.getItem(key);
	        return raw ? JSON.parse(raw) : fallback;
	      } catch {
	        return fallback;
	      }
	    }
	    function writeJsonSetting(key, value) {
	      try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
	    }
	    function readStringSetting(key, fallback) {
	      try { return localStorage.getItem(key) || fallback; } catch { return fallback; }
	    }
	    function writeStringSetting(key, value) {
	      try { localStorage.setItem(key, value); } catch {}
	    }
	    function applyAppearanceSettings() {
	      const value = state.appearance || { theme: "dark", font_scale: "md", header_density: "standard" };
	      const root = document.documentElement || document.body;
	      for (const theme of THEME_SPECS) root.classList.remove(`theme-${theme.id}`);
	      root.classList.add(`theme-${value.theme || "dark"}`);
	      for (const cls of ["font-sm", "font-md", "font-lg", "header-compact", "header-standard", "header-large"]) document.body.classList.remove(cls);
	      document.body.classList.add(`font-${value.font_scale || "md"}`);
	      document.body.classList.add(`header-${value.header_density || "standard"}`);
	      renderThemeSwatches(value.theme || "dark");
	    }
	    function loadAppearanceSettings() {
	      const value = readJsonSetting(UI_SETTINGS_KEY, null);
	      return {
	        theme: String(value?.theme || "dark").trim() || "dark",
	        font_scale: String(value?.font_scale || "md").trim() || "md",
	        header_density: String(value?.header_density || "standard").trim() || "standard",
	      };
	    }
	    function saveAppearanceSettings() {
	      writeJsonSetting(UI_SETTINGS_KEY, state.appearance);
	    }
	    function initAppearanceControls() {
	      const themeSelect = $("appearance-theme");
	      themeSelect.textContent = "";
	      for (const theme of THEME_SPECS) {
	        const option = document.createElement("option");
	        option.value = theme.id;
	        option.textContent = theme.label;
	        themeSelect.append(option);
	      }
	      themeSelect.value = state.appearance.theme;
	      $("appearance-font-size").value = state.appearance.font_scale;
	      $("appearance-header-size").value = state.appearance.header_density;
	      renderThemeSwatches(state.appearance.theme);
	    }
	    function renderThemeSwatches(themeId) {
	      const target = $("appearance-swatches");
	      if (!target) return;
	      const theme = THEME_SPECS.find((item) => item.id === themeId) || THEME_SPECS[0];
	      target.textContent = "";
	      for (const color of theme.swatches) {
	        const swatch = document.createElement("span");
	        swatch.className = "theme-swatch";
	        if (swatch.style) swatch.style.background = color;
	        target.append(swatch);
	      }
	    }
	    function updateAppearanceFromForm() {
	      state.appearance = {
	        theme: $("appearance-theme").value || "dark",
	        font_scale: $("appearance-font-size").value || "md",
	        header_density: $("appearance-header-size").value || "standard",
	      };
	      applyAppearanceSettings();
	      saveAppearanceSettings();
	    }
	    function openAppearance() {
	      initAppearanceControls();
	      $("appearance-backdrop").classList.remove("hidden");
	    }
	    function closeAppearance() {
	      $("appearance-backdrop").classList.add("hidden");
	    }
	    function setActiveTab(tab) {
	      const next = TABS.includes(tab) ? tab : "providers";
	      state.activeTab = next;
	      for (const id of TABS) {
	        const panel = $(`tab-${id}`);
	        const button = $(`tab-button-${id}`);
	        if (panel) panel.classList.toggle("active", id === next);
	        if (button) button.classList.toggle("active", id === next);
	      }
	      writeStringSetting(ACTIVE_TAB_KEY, next);
	    }
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
    function textValue(value) {
      return typeof value === "string" && value.trim() ? value.trim() : "";
    }
    function arrayValue(value) {
      return Array.isArray(value) ? value : [];
    }
    function objectValue(value) {
      return value && typeof value === "object" && !Array.isArray(value) ? value : null;
    }
    function dedupeOptionValues(values) {
      const seen = new Set();
      const out = [];
      for (const value of values || []) {
        const clean = textValue(value);
        const key = clean.toLowerCase();
        if (!clean || seen.has(key)) continue;
        seen.add(key);
        out.push(clean);
      }
      return out.sort((a, b) => a.localeCompare(b));
    }
    function catalogProviderFromItem(item, fallback = "") {
      if (typeof item === "string") return item.trim();
      const rec = objectValue(item);
      if (!rec) return String(fallback || "").trim();
      return textValue(rec.provider)
        || textValue(rec.provider_id)
        || textValue(rec.provider_name)
        || textValue(rec.backend_id)
        || textValue(rec.backend)
        || textValue(rec.id)
        || textValue(rec.name)
        || String(fallback || "").trim();
    }
    function catalogModelFromItem(item, fallback = "") {
      if (typeof item === "string") return item.trim();
      const rec = objectValue(item);
      if (!rec) return String(fallback || "").trim();
      return textValue(rec.model)
        || textValue(rec.model_id)
        || textValue(rec.routed_model)
        || textValue(rec.id)
        || textValue(rec.name)
        || String(fallback || "").trim();
    }
    function catalogVoiceFromItem(item, fallback = "") {
      if (typeof item === "string") return item.trim();
      const rec = objectValue(item);
      if (!rec) return String(fallback || "").trim();
      const params = objectValue(rec.params);
      return textValue(rec.voice_id)
        || textValue(params?.voice)
        || textValue(rec.voice)
        || textValue(rec.profile_id)
        || textValue(rec.id)
        || textValue(rec.name)
        || String(fallback || "").trim();
    }
    function catalogLabelFromItem(item, fallback) {
      const rec = objectValue(item);
      return textValue(rec?.label)
        || textValue(rec?.display_name)
        || textValue(rec?.title)
        || textValue(rec?.name)
        || String(fallback || "").trim();
    }
    function providerOptionsFromCatalog(payload, providerKeys = [], mapKeys = []) {
      const out = [];
      const add = (value) => {
        const provider = catalogProviderFromItem(value);
        if (!provider) return;
        const label = catalogLabelFromItem(value, provider);
        state.providerLabels.set(provider, label && label !== provider ? `${label} (${provider})` : provider);
        out.push(provider);
      };
      const items = arrayValue(payload?.items);
      if (items.length) items.forEach(add);
      else {
        for (const key of providerKeys) arrayValue(payload?.[key]).forEach(add);
      }
      for (const key of mapKeys) {
        const map = objectValue(payload?.[key]);
        if (!map) continue;
        Object.keys(map).forEach(add);
      }
      return dedupeOptionValues(out);
    }
    function modelOptionsFromCatalog(payload, provider, valueKeys = [], mapKeys = []) {
      const wanted = String(provider || "").trim().toLowerCase();
      const out = [];
      const add = (value, providerFallback = provider) => {
        const itemProvider = (typeof value === "string" ? String(providerFallback || "").trim() : catalogProviderFromItem(value, providerFallback)).toLowerCase();
        if (wanted && itemProvider && itemProvider !== wanted) return;
        const model = catalogModelFromItem(value);
        if (model) out.push(model);
      };
      const items = arrayValue(payload?.items);
      if (items.length) items.forEach((item) => add(item));
      else {
        for (const key of valueKeys) arrayValue(payload?.[key]).forEach((item) => add(item));
      }
      for (const key of mapKeys) {
        const map = objectValue(payload?.[key]);
        if (!map) continue;
        for (const [mapProvider, values] of Object.entries(map)) {
          arrayValue(values).forEach((item) => add(item, mapProvider));
        }
      }
      return dedupeOptionValues(out);
    }
    function voiceOptionsFromCatalog(payload, provider, model = "") {
      const wantedProvider = String(provider || "").trim().toLowerCase();
      const wantedModel = String(model || "").trim().toLowerCase();
      const out = [];
      const add = (value, providerFallback = provider, modelFallback = "") => {
        const rec = objectValue(value);
        const params = objectValue(rec?.params);
        const tags = objectValue(rec?.tags);
        const itemProvider = (rec
          ? textValue(rec.provider)
            || textValue(rec.provider_id)
            || textValue(rec.engine_id)
            || textValue(rec.engine)
            || textValue(tags?.provider)
            || textValue(params?.provider)
            || textValue(params?.engine)
            || String(providerFallback || "").trim()
          : String(providerFallback || "").trim()).toLowerCase();
        if (wantedProvider && itemProvider && itemProvider !== wantedProvider) return;
        const itemModel = (rec
          ? textValue(rec.model)
            || textValue(rec.model_id)
            || textValue(params?.model)
            || textValue(params?.model_id)
            || textValue(params?.model_filename)
            || String(modelFallback || "").trim()
          : String(modelFallback || "").trim()).toLowerCase();
        if (wantedModel && itemModel && itemModel !== wantedModel) return;
        const voice = catalogVoiceFromItem(value);
        if (!voice) return;
        const label = catalogLabelFromItem(value, voice) || voice;
        state.voiceLabels.set(voice, label);
        out.push(voice);
      };
      for (const key of ["items", "profiles", "voices", "cloned_voices"]) {
        arrayValue(payload?.[key]).forEach((item) => add(item));
      }
      for (const key of ["tts_voices_by_provider", "tts_profiles_by_provider"]) {
        const map = objectValue(payload?.[key]);
        if (!map) continue;
        for (const [mapProvider, values] of Object.entries(map)) {
          arrayValue(values).forEach((item) => add(item, mapProvider));
        }
      }
      return dedupeOptionValues(out);
    }
    function withQuery(path, params = {}) {
      const query = Object.entries(params)
        .filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== "")
        .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
        .join("&");
      return query ? `${path}?${query}` : path;
    }
    function defaultCatalogForRow(row) {
      const { kind, modality, task } = defaultRowKindModality(row || {});
      const key = `${kind}.${modality}`;
      if (key === "embedding.text") {
        return {
          scope: "embedding.text",
          providerPath: () => withQuery("/api/gateway/embeddings/models", { providers_only: true }),
          modelPath: (provider) => withQuery("/api/gateway/embeddings/models", { provider }),
          providerKeys: ["providers", "available_providers", "embedding_providers", "provider_details"],
          modelKeys: ["embedding_models", "models", "data", "provider_models"],
          mapKeys: ["models_by_provider", "embedding_models_by_provider"],
          emptyProviders: "No embedding providers discovered",
          emptyModels: "No embedding models discovered",
        };
      }
      if (key === "embedding.image") {
        return textCatalog("image embeddings", { capability_route: "input.image,embedding.image" });
      }
      if (key === "output.image") {
        if (task === "image_to_image") return visionCatalog("image edit", "image_to_image");
        if (task === "image_upscale") return visionCatalog("image restore / upscale", "image_upscale");
        return visionCatalog("image generation", "text_to_image");
      }
      if (key === "output.video") {
        if (task === "image_to_video") return visionCatalog("image to video", "image_to_video");
        return visionCatalog("video generation", "text_to_video");
      }
      if (key === "input.image") {
        return textCatalog("image input", { capability_route: "input.image,output.text" });
      }
      if (key === "input.video") {
        return textCatalog("video input", { capability_route: "input.video,output.text" });
      }
      if (key === "input.sound" || key === "input.audio") {
        return textCatalog("audio input", { capability_route: "input.sound,output.text" });
      }
      if (key === "input.music") {
        return textCatalog("music input", { capability_route: "input.music,output.text" });
      }
      if (key === "output.voice" || key === "output.audio") {
        return {
          scope: "voice generation",
          providerPath: () => withQuery("/api/gateway/voice/voices", { providers_only: true, compact: true }),
          modelPath: (provider) => withQuery("/api/gateway/audio/speech/models", { provider }),
          providerKeys: ["tts_providers", "providers", "available_providers"],
          modelKeys: ["tts_models", "models", "data", "provider_models"],
          mapKeys: ["models_by_provider", "tts_models_by_provider"],
          emptyProviders: "No voice generation providers discovered",
          emptyModels: "No voice generation models discovered",
        };
      }
      if (key === "input.voice") {
        return audioCatalog("speech transcription", "/api/gateway/audio/transcriptions/models", ["stt_providers", "providers", "available_providers"], ["stt_models", "models", "data", "provider_models"], ["models_by_provider", "stt_models_by_provider"]);
      }
      if (key === "output.sound") {
        return {
          scope: "sound effects generation",
          providerPath: () => withQuery("/api/gateway/audio/music/providers", { task: "text_to_audio" }),
          modelPath: (provider) => withQuery("/api/gateway/audio/music/models", { task: "text_to_audio", provider }),
          providerKeys: ["music_providers", "providers", "available_providers", "provider_details"],
          modelKeys: ["music_models", "models", "items", "data", "provider_models"],
          mapKeys: ["models_by_provider", "music_models_by_provider"],
          emptyProviders: "No sound effects providers discovered",
          emptyModels: "No sound effects models discovered",
        };
      }
      if (key === "output.music") {
        return {
          scope: "music generation",
          providerPath: () => withQuery("/api/gateway/audio/music/providers", { task: "text_to_music" }),
          modelPath: (provider) => withQuery("/api/gateway/audio/music/models", { task: "text_to_music", provider }),
          providerKeys: ["music_providers", "providers", "available_providers", "provider_details"],
          modelKeys: ["music_models", "models", "items", "data", "provider_models"],
          mapKeys: ["models_by_provider", "music_models_by_provider"],
          emptyProviders: "No music providers discovered",
          emptyModels: "No music models discovered",
        };
      }
      return textCatalog("text generation", { capability_route: "output.text" });
    }
    function textCatalog(scope, filters = {}) {
      return {
        scope,
        providerPath: () => "/api/gateway/discovery/providers",
        modelPath: (provider) => withQuery(`/api/gateway/discovery/providers/${encodeURIComponent(provider)}/models`, filters),
        providerKeys: ["items", "providers", "available_providers", "provider_details"],
        modelKeys: ["models", "items", "data", "provider_models"],
        mapKeys: ["models_by_provider"],
        emptyProviders: "No text providers discovered",
        emptyModels: "No compatible text models discovered",
        useConfiguredProviderFallback: true,
      };
    }
    function visionCatalog(scope, task) {
      return {
        scope,
        providerPath: () => withQuery("/api/gateway/vision/provider_models", { task, providers_only: true }),
        modelPath: (provider) => withQuery("/api/gateway/vision/provider_models", { task, provider }),
        providerKeys: ["providers", "available_providers", "image_providers"],
        modelKeys: ["models", "items", "available_models", "local_models", "provider_models"],
        mapKeys: ["models_by_provider"],
        emptyProviders: `No ${scope} providers discovered`,
        emptyModels: `No ${scope} models discovered`,
      };
    }
	    function audioCatalog(scope, endpoint, providerKeys, modelKeys, mapKeys) {
	      return {
	        scope,
	        providerPath: () => withQuery(endpoint, { providers_only: true }),
        modelPath: (provider) => withQuery(endpoint, { provider }),
        providerKeys,
        modelKeys,
        mapKeys,
        emptyProviders: `No ${scope} providers discovered`,
	        emptyModels: `No ${scope} models discovered`,
	      };
	    }
	    function isVoiceOutputDefault(row) {
	      const { kind, modality } = defaultRowKindModality(row || {});
	      return kind === "output" && (modality === "voice" || modality === "audio");
	    }
	    function defaultVoiceValue(row) {
	      const options = row && typeof row.options === "object" && !Array.isArray(row.options) ? row.options : {};
	      return textValue(options.voice) || textValue(options.profile);
	    }
	    function setSelectOptions(select, values, { emptyLabel, disabled = false, selected = "", labelMap = null } = {}) {
	      select.textContent = "";
	      const empty = document.createElement("option");
	      empty.value = "";
	      empty.textContent = emptyLabel || "Select...";
	      select.append(empty);
	      for (const value of values) {
	        const opt = document.createElement("option");
	        opt.value = value;
	        opt.textContent = labelMap?.get(value) || state.providerLabels.get(value) || value;
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
	    function providerValueForEndpointProfile(profile) {
	      if (!profile || typeof profile !== "object") return "";
	      const direct = String(profile.provider_id || "").trim();
	      if (direct) return direct;
	      const value = String(profile.virtual_provider || (profile.id ? `endpoint:${profile.id}` : "") || "").trim();
	      return value;
	    }
	    function configuredProviderOptions() {
	      const values = [];
	      state.providerLabels.clear();
	      for (const profile of state.endpointProfiles || []) {
	        if (profile?.enabled === false) continue;
	        const value = providerValueForEndpointProfile(profile);
	        if (!value) continue;
	        const name = String(profile.display_name || profile.id || value).trim();
	        state.providerLabels.set(value, `${name} (${value})`);
	        values.push(value);
	      }
	      return [...new Set(values)].sort((a, b) => {
	        const left = state.providerLabels.get(a) || a;
	        const right = state.providerLabels.get(b) || b;
	        return left.localeCompare(right);
	      });
	    }
	    async function loadProviders() {
	      $("defaults-message").className = "message";
	      state.providers = configuredProviderOptions();
	      setSelectOptions($("modal-default-provider"), state.providers, {
	        emptyLabel: state.providers.length ? "Select provider..." : "Configure a provider first",
	        disabled: !state.providers.length,
	      });
	      setSelectOptions($("modal-default-model"), [], { emptyLabel: "Select provider first", disabled: true });
	      $("defaults-message").textContent = state.providers.length ? "" : "Configure a provider on the Providers tab before setting multimodal capability defaults.";
	      syncSandboxProviderOptions();
	    }
	    async function fetchProviderModels(provider) {
	      if (!provider) return [];
	      const cacheKey = `text::${provider}`;
	      if (!state.providerModels.has(cacheKey)) {
	        const payload = await api(withQuery(`/api/gateway/discovery/providers/${encodeURIComponent(provider)}/models`, { capability_route: "output.text" }));
	        state.providerModels.set(cacheKey, parseModelItems(payload));
	      }
	      return state.providerModels.get(cacheKey) || [];
	    }
	    async function loadModels(provider, selected = "") {
	      await loadDefaultModels(provider, selected, state.activeDefaultRow || null);
	    }
	    async function fetchDefaultProviders(row) {
	      const catalog = defaultCatalogForRow(row || {});
	      const path = catalog.providerPath();
	      const cacheKey = `providers::${catalog.scope}::${path}`;
	      if (!state.providerModels.has(cacheKey)) {
	        const payload = await api(path);
	        let providers = providerOptionsFromCatalog(payload, catalog.providerKeys, catalog.mapKeys);
	        if (!providers.length && catalog.useConfiguredProviderFallback) providers = configuredProviderOptions();
	        state.providerModels.set(cacheKey, providers);
	      }
	      return state.providerModels.get(cacheKey) || [];
	    }
	    async function fetchDefaultModels(provider, row) {
	      if (!provider) return [];
	      const catalog = defaultCatalogForRow(row || {});
	      const path = catalog.modelPath(provider);
	      const cacheKey = `models::${catalog.scope}::${provider}::${path}`;
	      if (!state.providerModels.has(cacheKey)) {
	        const promise = api(path).then((payload) => modelOptionsFromCatalog(payload, provider, catalog.modelKeys, catalog.mapKeys));
	        state.providerModels.set(cacheKey, promise);
	      }
	      const cached = await state.providerModels.get(cacheKey);
	      const models = Array.isArray(cached) ? cached : [];
	      state.providerModels.set(cacheKey, models);
	      return models;
	    }
	    async function fetchDefaultVoices(provider, model, row) {
	      if (!provider || !isVoiceOutputDefault(row)) return [];
	      const path = withQuery("/api/gateway/voice/voices", { provider, model, compact: true });
	      const cacheKey = `voices::${provider}::${model || ""}::${path}`;
	      if (!state.providerModels.has(cacheKey)) {
	        const payload = await api(path);
	        state.providerModels.set(cacheKey, voiceOptionsFromCatalog(payload, provider, model));
	      }
	      return state.providerModels.get(cacheKey) || [];
	    }
	    async function loadDefaultModels(provider, selected = "", row = null) {
	      if (!provider) {
	        setSelectOptions($("modal-default-model"), [], { emptyLabel: "Select provider first", disabled: true });
	        return;
	      }
	      setSelectOptions($("modal-default-model"), [], { emptyLabel: "Loading models...", disabled: true });
	      const catalog = defaultCatalogForRow(row || {});
	      const models = await fetchDefaultModels(provider, row || {});
	      setSelectOptions($("modal-default-model"), models, {
	        emptyLabel: models.length ? "Select model..." : catalog.emptyModels,
	        disabled: !models.length,
	        selected,
	      });
	      if (selected && !models.includes(selected)) {
	        $("default-modal-message").textContent = `Configured model "${selected}" is not currently in the discovered ${catalog.scope} catalog for ${provider}.`;
	        $("default-modal-message").className = "message error";
	      }
	    }
	    async function loadDefaultVoices(provider, model = "", selected = "", row = null) {
	      const label = $("modal-default-voice-label");
	      const select = $("modal-default-voice");
	      if (!isVoiceOutputDefault(row)) {
	        label.classList.add("hidden");
	        setSelectOptions(select, [], { emptyLabel: "No voice selector for this route", disabled: true, labelMap: state.voiceLabels });
	        return;
	      }
	      label.classList.remove("hidden");
	      if (!provider) {
	        setSelectOptions(select, [], { emptyLabel: "Select provider first", disabled: true, labelMap: state.voiceLabels });
	        return;
	      }
	      if (!model) {
	        setSelectOptions(select, [], { emptyLabel: "Select model first", disabled: true, labelMap: state.voiceLabels });
	        return;
	      }
	      setSelectOptions(select, [], { emptyLabel: "Loading voices...", disabled: true, labelMap: state.voiceLabels });
	      const voices = await fetchDefaultVoices(provider, model, row || {});
	      setSelectOptions(select, voices, {
	        emptyLabel: voices.length ? "Use provider default voice" : "No voices discovered",
	        disabled: !voices.length,
	        selected,
	        labelMap: state.voiceLabels,
	      });
	      if (selected && !voices.includes(selected)) {
	        $("default-modal-message").textContent = `Configured voice "${selected}" is not currently in the discovered voice catalog for ${provider}/${model}.`;
	        $("default-modal-message").className = "message error";
	      }
	    }
    const ENDPOINT_FAMILIES = [
      {
        id: "openai",
        label: "OpenAI",
        presetId: "openai",
        defaultName: "OpenAI",
        summary: "OpenAI API or an OpenAI-compatible OpenAI deployment.",
        description: "OpenAI account connection for GPT and embedding models.",
        basePlaceholder: "default: https://api.openai.com/v1",
        baseHelp: "Optional. Leave empty to use the normal OpenAI API URL.",
        keyPlaceholder: "OpenAI API key",
        keyHelp: "Paste an OpenAI API key. Leave blank while editing to keep the stored key.",
        requiresBaseUrl: false,
      },
      {
        id: "anthropic",
        label: "Anthropic",
        presetId: "anthropic",
        defaultName: "Anthropic",
        summary: "Anthropic Claude API or a Claude-compatible Anthropic proxy.",
        description: "Anthropic account connection for Claude models.",
        basePlaceholder: "default: https://api.anthropic.com/v1",
        baseHelp: "Optional. Leave empty to use the normal Anthropic API URL. Use a /v1 base URL for Anthropic-compatible proxies.",
        keyPlaceholder: "Anthropic API key",
        keyHelp: "Paste an Anthropic API key. Leave blank while editing to keep the stored key.",
        requiresBaseUrl: false,
      },
      {
        id: "openrouter",
        label: "OpenRouter",
        presetId: "openrouter",
        defaultName: "OpenRouter",
        summary: "OpenRouter account connection for multi-provider routing.",
        description: "OpenRouter connection for hosted model routing.",
        basePlaceholder: "default: https://openrouter.ai/api/v1",
        baseHelp: "Optional. Leave empty to use the normal OpenRouter API URL.",
        keyPlaceholder: "OpenRouter API key",
        keyHelp: "Paste an OpenRouter API key. Leave blank while editing to keep the stored key.",
        requiresBaseUrl: false,
      },
      {
        id: "portkey",
        label: "Portkey",
        presetId: "portkey",
        defaultName: "Portkey",
        summary: "Portkey gateway connection for governed provider routing.",
        description: "Portkey connection for gateway-managed provider routing.",
        basePlaceholder: "default: https://api.portkey.ai/v1",
        baseHelp: "Optional. Leave empty to use the normal Portkey OpenAI-compatible URL.",
        keyPlaceholder: "Portkey API key",
        keyHelp: "Paste the Portkey API key or gateway key expected by your account.",
        requiresBaseUrl: false,
      },
      {
        id: "lmstudio",
        label: "LM Studio",
        presetId: "lmstudio",
        defaultName: "LM Studio",
        summary: "Local or remote LM Studio server.",
        description: "LM Studio server connection for local or LAN model serving.",
        basePlaceholder: "http://127.0.0.1:1234/v1",
        baseHelp: "Usually the LM Studio OpenAI-compatible server URL. In Docker, use the host-reachable URL instead of 127.0.0.1.",
        keyPlaceholder: "optional",
        keyHelp: "Optional. Only fill this if your LM Studio proxy requires a key.",
        requiresBaseUrl: false,
      },
      {
        id: "ollama",
        label: "Ollama",
        presetId: "ollama",
        defaultName: "Ollama",
        summary: "Local or remote Ollama server.",
        description: "Ollama server connection for local or LAN model serving.",
        basePlaceholder: "http://127.0.0.1:11434",
        baseHelp: "Usually the Ollama server URL. In Docker, use the host-reachable URL instead of 127.0.0.1.",
        keyPlaceholder: "optional",
        keyHelp: "Optional. Most local Ollama installs do not require a key.",
        requiresBaseUrl: false,
      },
      {
        id: "openai-compatible",
        label: "Custom OpenAI-compatible",
        presetId: "custom-openai-compatible",
        defaultName: "Custom endpoint",
        summary: "Any generic /v1 endpoint such as vLLM, llama.cpp, LocalAI, or a private gateway.",
        description: "Custom OpenAI-compatible endpoint connection.",
        basePlaceholder: "https://endpoint.example/v1",
        baseHelp: "Required for custom OpenAI-compatible endpoints such as vLLM, llama.cpp, or a private inference gateway.",
        keyPlaceholder: "endpoint API key, if required",
        keyHelp: "Paste the key required by this endpoint. Leave blank while editing to keep the stored key.",
        requiresBaseUrl: true,
      },
    ];
    function endpointFamilyInfo(id) {
      return ENDPOINT_FAMILIES.find((item) => item.id === id) || ENDPOINT_FAMILIES[ENDPOINT_FAMILIES.length - 1];
    }
    function defaultProfileIdForFamily(family) {
      const info = endpointFamilyInfo(family);
      if (info.id === "openai-compatible") return "custom-endpoint";
      return info.id;
    }
    function renderProviderPresets() {
      const grid = $("provider-preset-grid");
      if (!grid) return;
      grid.textContent = "";
      for (const item of ENDPOINT_FAMILIES) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `provider-preset ${state.activeProviderPreset === item.id ? "active" : ""}`;
        button.innerHTML = `<strong>${esc(item.label)}</strong><span>${esc(item.summary || "")}</span>`;
	        button.onclick = () => openEndpointModalForFamily(item.id);
        grid.append(button);
      }
    }
    function updateEndpointFamilyHints() {
      const family = endpointFamilyInfo($("endpoint-provider-family").value || "openai-compatible");
      $("endpoint-base-url").placeholder = family.basePlaceholder;
      $("endpoint-api-key").placeholder = family.keyPlaceholder || "leave blank to keep existing key";
      $("endpoint-base-url-help").textContent = family.baseHelp;
      $("endpoint-api-key-help").textContent = family.keyHelp;
	      $("provider-modal-title").textContent = `Configure ${family.label}`;
	      $("provider-modal-description").textContent = family.summary || "Gateway stores endpoint details and keys server-side, then exposes this connection as an available provider.";
      state.activeProviderPreset = family.id;
      renderProviderPresets();
    }
    function handleEndpointFamilyChange() {
      const previous = endpointFamilyInfo(state.activeProviderPreset || "openai");
      const family = endpointFamilyInfo($("endpoint-provider-family").value || "openai-compatible");
      const createMode = !$("endpoint-id").value.trim();
      if (createMode) {
        const previousDefaultId = defaultProfileIdForFamily(previous.id);
        const currentId = $("endpoint-profile-id").value.trim();
        if (!currentId || currentId === previousDefaultId) {
          $("endpoint-profile-id").value = defaultProfileIdForFamily(family.id);
        }
        const currentName = $("endpoint-name").value.trim();
        if (!currentName || currentName === (previous.defaultName || previous.label)) {
          $("endpoint-name").value = family.defaultName || family.label;
        }
        const currentDescription = $("endpoint-description").value.trim();
        if (!currentDescription || currentDescription === (previous.description || "")) {
          $("endpoint-description").value = family.description || "";
        }
        setEndpointModelOptions([], []);
      }
      updateEndpointFamilyHints();
    }
    function selectProviderPreset(familyId) {
      const family = endpointFamilyInfo(familyId || "openai-compatible");
      state.activeProviderPreset = family.id;
      $("endpoint-provider-family").value = family.id;
      if (!$("endpoint-id").value.trim()) {
        $("endpoint-profile-id").value = defaultProfileIdForFamily(family.id);
        $("endpoint-profile-id").disabled = false;
        $("endpoint-name").value = family.defaultName || family.label;
        $("endpoint-description").value = family.description || "";
        $("endpoint-base-url").value = "";
        $("endpoint-api-key").value = "";
        $("endpoint-clear-api-key").checked = false;
        setEndpointModelOptions([], []);
        $("endpoint-enabled").checked = true;
        $("endpoint-message").textContent = "";
        $("endpoint-message").className = "message";
      }
      updateEndpointFamilyHints();
    }
    function initEndpointProfileFormOptions() {
      const familySelect = $("endpoint-provider-family");
      const familySelected = familySelect.value || state.activeProviderPreset || "openai";
      familySelect.textContent = "";
      for (const item of ENDPOINT_FAMILIES) {
        const opt = document.createElement("option");
        opt.value = item.id;
        opt.textContent = item.label;
        familySelect.append(opt);
      }
      familySelect.value = ENDPOINT_FAMILIES.some((item) => item.id === familySelected) ? familySelected : "openai-compatible";
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
      updateEndpointFamilyHints();
      renderProviderPresets();
    }
    function clearEndpointProfileForm() {
      $("endpoint-id").value = "";
      $("endpoint-profile-id").value = "";
      $("endpoint-profile-id").disabled = false;
      $("endpoint-name").value = "";
      $("endpoint-description").value = "";
      $("endpoint-provider-family").value = "openai";
      $("endpoint-scope").value = "user";
      $("endpoint-base-url").value = "";
      $("endpoint-api-key").value = "";
      $("endpoint-clear-api-key").checked = false;
      setEndpointModelOptions([], []);
      $("endpoint-enabled").checked = true;
      $("endpoint-message").textContent = "";
	      $("endpoint-message").className = "message";
	      selectProviderPreset("openai");
	    }
	    function openEndpointModalForFamily(familyId) {
	      initEndpointProfileFormOptions();
	      clearEndpointProfileForm();
	      selectProviderPreset(familyId || "openai");
	      $("provider-modal-backdrop").classList.remove("hidden");
	    }
	    function openEndpointModalFromConfiguredProvider(profile) {
	      initEndpointProfileFormOptions();
	      clearEndpointProfileForm();
	      const family = String(profile.provider_family || profile.provider_id || profile.id || "openai").trim();
	      selectProviderPreset(family);
	      const providerId = String(profile.provider_id || profile.id || defaultProfileIdForFamily(family)).trim();
	      $("endpoint-profile-id").value = providerId;
	      $("endpoint-profile-id").disabled = false;
	      $("endpoint-name").value = profile.display_name || providerId;
	      $("endpoint-description").value = profile.description || "";
	      $("endpoint-base-url").value = profile.base_url || "";
	      $("endpoint-api-key").value = "";
	      $("endpoint-clear-api-key").checked = false;
	      $("endpoint-scope").value = state.principal?.admin ? "gateway" : "user";
	      $("endpoint-enabled").checked = true;
	      setEndpointModelOptions([], []);
	      $("endpoint-message").textContent = "This provider is already available from Core config or environment. Confirm only if you want an explicit Gateway provider connection override.";
	      $("endpoint-message").className = "message";
	      updateEndpointFamilyHints();
	      $("provider-modal-backdrop").classList.remove("hidden");
	    }
	    function closeEndpointModal() {
	      $("provider-modal-backdrop").classList.add("hidden");
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
	      state.activeProviderPreset = $("endpoint-provider-family").value || "openai-compatible";
	      updateEndpointFamilyHints();
	      $("provider-modal-backdrop").classList.remove("hidden");
	    }
    function renderEndpointProfiles(profiles) {
      state.endpointProfiles = Array.isArray(profiles) ? profiles : [];
      const tbody = $("endpoint-profiles-table");
      tbody.textContent = "";
	      if (!state.endpointProfiles.length) {
	        const tr = document.createElement("tr");
	        tr.innerHTML = `<td colspan="6" class="empty">No available providers configured yet.</td>`;
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
        const providerId = providerValueForEndpointProfile(p);
	        tr.innerHTML = `
	          <td><strong>${esc(p.display_name || p.id)}</strong><div class="muted">${esc(p.description || "No description")}</div></td>
	          <td><code>${esc(providerId || p.id)}</code><div class="muted">${endpoint}</div></td>
	          <td>${esc(endpointFamilyInfo(p.provider_family || "openai-compatible").label)}</td>
	          <td><span class="badge">${esc(models)}</span></td>
	          <td><span class="state-pill ${p.enabled ? "ok" : "off"}">${p.enabled ? "enabled" : "disabled"}</span><div class="muted">${esc(p.scope || "user")} · ${esc(keyState)}</div></td>
	        `;
        const actions = document.createElement("td");
        actions.className = "actions";
        if (p.managed === false || p.synthetic === true) {
          const override = document.createElement("button");
          override.innerHTML = `<span class="button-icon" aria-hidden="true">✎</span><span>Override</span>`;
          override.className = "secondary";
          override.onclick = () => openEndpointModalFromConfiguredProvider(p);
          actions.append(override);
        } else {
          const edit = document.createElement("button");
          edit.innerHTML = `<span class="button-icon" aria-hidden="true">✎</span><span>Edit</span>`;
          edit.className = "secondary";
          edit.onclick = () => fillEndpointProfileForm(p);
          const del = document.createElement("button");
          del.innerHTML = `<span class="button-icon" aria-hidden="true">×</span><span>Delete</span>`;
          del.className = "danger";
          del.onclick = () => deleteEndpointProfile(p);
          actions.append(edit, del);
        }
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
        $("endpoint-message").textContent = "Connection id is required.";
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
      if (endpointFamilyInfo(payload.provider_family).requiresBaseUrl && !payload.base_url) {
        $("endpoint-message").textContent = "Custom OpenAI-compatible connections need a Base URL.";
        $("endpoint-message").className = "message error";
        return;
      }
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
        $("endpoint-message").textContent = `Saved. Use ${res.profile?.virtual_provider || "endpoint:" + profileId} as the provider in Flow nodes or Core capability defaults.`;
        $("endpoint-message").className = "message ok";
		        renderEndpointProfiles(res.profiles || []);
		        state.providerModels.clear();
		        await loadProviders();
		        await renderDefaults(await api("/api/gateway/config/capability-defaults"));
		        closeEndpointModal();
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
      if (endpointFamilyInfo(payload.provider_family).requiresBaseUrl && !payload.base_url) {
        $("endpoint-message").textContent = "Custom OpenAI-compatible discovery needs a Base URL.";
        $("endpoint-message").className = "message error";
        return;
      }
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
	        await renderDefaults(await api("/api/gateway/config/capability-defaults"));
      } catch (err) {
        $("endpoint-message").textContent = String(err.message || err);
        $("endpoint-message").className = "message error";
      }
    }
	    function canonicalDefaultRouteTask(value) {
	      const task = String(value || "").trim().toLowerCase().replaceAll("-", "_");
	      return ["text_to_image", "image_to_image", "image_upscale", "text_to_video", "image_to_video"].includes(task) ? task : "";
	    }
	    function routeKey(row) {
	      const base = `${row.kind || ""}.${row.modality || ""}`;
	      const task = canonicalDefaultRouteTask(row.route_task || row.default_task || row.capability_task || row.task);
	      return task ? `${base}.${task}` : base;
	    }
	    function defaultRowKey(row) {
	      const key = row?.key || routeKey(row || {});
	      return key && key !== "." ? key : "";
	    }
	    function defaultRowConfigured(row) {
	      return Boolean(row?.provider && row?.model);
	    }
	    function defaultRowCapability(row) {
	      if (row?.label) return row.label;
	      const kind = row?.kind ? String(row.kind) : "";
	      const modality = row?.modality ? String(row.modality) : "";
	      return `${kind} ${modality}`.trim() || "Capability";
	    }
	    function defaultRowKindModality(row) {
	      const key = defaultRowKey(row);
	      const parts = key.split(".");
	      const task = parts[2] || canonicalDefaultRouteTask(row?.route_task || row?.default_task || row?.capability_task || "");
	      return { kind: parts[0] || row?.kind || "", modality: parts[1] || row?.modality || "", task };
	    }
	    function findDefaultRow(rows, key) {
	      return (rows || []).find((row) => defaultRowKey(row) === key) || null;
	    }
	    function rowHasProviderModel(row) {
	      return Boolean(row?.provider && row?.model);
	    }
	    function visibleCapabilityDefaultRow(row) {
	      const key = defaultRowKey(row || {});
	      if (key === "output.image" || key === "output.video") return false;
	      const { modality } = defaultRowKindModality(row || {});
	      return String(modality || "").toLowerCase() !== "scene3d";
	    }
	    async function textDefaultCoversInput(row, rows) {
	      const key = defaultRowKey(row);
	      if (!["input.image", "input.video", "input.sound", "input.music"].includes(key)) return false;
	      const textRow = findDefaultRow(rows, "input.text");
	      if (!rowHasProviderModel(textRow)) return false;
	      try {
	        const models = await fetchDefaultModels(textRow.provider, row);
	        return models.includes(textRow.model);
	      } catch {
	        return false;
	      }
	    }
	    async function displayDefaultRow(row, rows) {
	      const key = defaultRowKey(row);
	      if (key === "output.text") {
	        const textRow = findDefaultRow(rows, "input.text");
	        if (rowHasProviderModel(textRow)) {
	          return {
	            ...row,
	            provider: textRow.provider,
	            model: textRow.model,
	            base_url: textRow.base_url,
	            options: textRow.options || {},
	            configured: true,
	            source: textRow.source || row.source || "abstractcore.capability_defaults",
	            derived_from: "input.text",
	            read_only: true,
	          };
	        }
	        return { ...row, derived_from: "input.text", read_only: true };
	      }
	      if (["input.image", "input.video", "input.sound", "input.music"].includes(key)) {
	        if (row?.covered_by === "input.text" || await textDefaultCoversInput(row, rows)) {
	          const textRow = findDefaultRow(rows, "input.text") || row;
	          const overrideable = key !== "input.image";
	          return {
	            ...row,
	            provider: textRow.provider || row.provider,
	            model: textRow.model || row.model,
	            base_url: textRow.base_url || row.base_url,
	            options: textRow.options || row.options || {},
	            configured: Boolean(textRow.provider && textRow.model) || defaultRowConfigured(row),
	            source: textRow.source || row.source || "abstractcore.capability_defaults",
	            covered_by: "input.text",
	            coverage_mode: key === "input.video" ? "video_frames" : row.coverage_mode,
	            overrideable,
	            read_only: !overrideable,
	          };
	        }
	      }
	      return row;
	    }
	    function defaultRowReadOnly(row) {
	      return Boolean(row?.read_only || row?.derived_from || (row?.covered_by && !row?.overrideable));
	    }
	    function defaultRowStatus(row) {
	      if (row?.covered_by === "input.text") return { label: "covered", cls: "covered" };
	      if (row?.derived_from === "input.text") return { label: rowHasProviderModel(row) ? "linked" : "not configured", cls: rowHasProviderModel(row) ? "covered" : "off" };
	      return defaultRowConfigured(row)
	        ? { label: "configured", cls: "ok" }
	        : { label: "not configured", cls: "off" };
	    }
	    function defaultRowActionLabel(row) {
	      if (row?.covered_by === "input.text") return row?.overrideable ? "Override" : "Covered";
	      if (row?.derived_from === "input.text") return "Linked";
	      return defaultRowConfigured(row) ? "Edit" : "Configure";
	    }
	    function defaultSourceLabel(source) {
	      const value = String(source || "").trim();
	      const labels = {
	        "abstractcore.runtime": "Runtime override",
	        "abstractcore.gateway_runtime": "Gateway baseline",
	        "abstractcore.local": "Local Core config",
	        "abstractcore.server": "Core server",
	        "abstractcore.capability_defaults": "Core config",
	        "abstractcore.capability_defaults.input_text_multimodal": "Text model handles it",
	        "not_configured": "Not configured",
	      };
	      return labels[value] || value;
	    }
	    function renderAccount(me) {
	      const p = me?.principal;
	      state.principal = p || null;
		      if (!p) {
		        document.body.classList.remove("signed-in");
		        $("account").textContent = "No active session.";
	        $("users-section").classList.add("hidden");
	        $("runtime-reservations-section").classList.add("hidden");
	      $("defaults-scope").textContent = "Sign in to edit provider/model defaults for this Gateway runtime.";
	        setLoginStatus("Gateway token missing", "warn", "token: missing");
	        setStatus(false, "Signed out");
		        return;
		      }
		      document.body.classList.add("signed-in");
		      $("account").innerHTML = `
	        <span><span class="muted">Runtime</span> <code>${esc(p.tenant_id)}/${esc(p.runtime_id || p.user_id)}</code></span>
	        <span><span class="muted">Roles</span> ${(p.roles || []).map((role) => `<span class="badge">${esc(role)}</span>`).join(" ") || '<span class="badge">none</span>'}</span>
	      `;
	      $("users-section").classList.toggle("hidden", !p.admin);
	      $("runtime-reservations-section").classList.toggle("hidden", !p.admin);
      $("defaults-scope").textContent = p.admin
        ? "Editing as admin changes the Gateway multimodal capability defaults. Users inherit these unless they set their own runtime defaults."
        : "Editing here changes your runtime multimodal capability defaults. Unset routes inherit the Gateway defaults.";
	      initEndpointProfileFormOptions();
	      setStatus(true, `${p.tenant_id}/${p.user_id}`);
	      setActiveTab(state.activeTab);
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
	      const rawRows = (Array.isArray(payload?.routes) ? payload.routes : []).filter(visibleCapabilityDefaultRow);
	      const rows = [];
	      for (const rawRow of rawRows) rows.push(await displayDefaultRow(rawRow, rawRows));
	      state.defaults = rows;
	      const tbody = $("defaults-table");
	      tbody.textContent = "";
	      for (const row of rows) {
	        const key = defaultRowKey(row);
	        if (!key || key === ".") continue;
	        const configured = defaultRowConfigured(row);
	        const status = defaultRowStatus(row);
	        const source = row.covered_by === "input.text"
	          ? "Text Input"
	          : row.derived_from === "input.text"
	            ? "Text Input"
	            : defaultSourceLabel(row.source || (configured ? payload.source || "" : ""));
	        const tr = document.createElement("tr");
	        if (defaultRowReadOnly(row)) tr.classList.add("capability-derived");
	        tr.innerHTML = `
	          <td><span class="capability-route"><code>${esc(key)}</code></span></td>
	          <td>${esc(defaultRowCapability(row))}</td>
	          <td>${row.provider ? esc(state.providerLabels.get(row.provider) || row.provider) : "-"}</td>
	          <td>${row.model ? esc(row.model) : "-"}</td>
	          <td>${source ? `<span class="badge">${esc(source)}</span>` : "-"}</td>
	          <td><span class="state-pill ${esc(status.cls)}">${esc(status.label)}</span></td>
	        `;
	        const actions = document.createElement("td");
	        actions.className = "actions";
	        const configure = document.createElement("button");
	        configure.className = "secondary";
	        configure.disabled = defaultRowReadOnly(row);
	        configure.innerHTML = `<span class="button-icon" aria-hidden="true">${configured && !defaultRowReadOnly(row) ? "✎" : "+"}</span><span>${esc(defaultRowActionLabel(row))}</span>`;
	        configure.onclick = () => {
	          if (!defaultRowReadOnly(row)) openDefaultModal(row);
	        };
	        actions.append(configure);
	        if (configured && !defaultRowReadOnly(row)) {
	          const clear = document.createElement("button");
	          clear.className = "secondary";
	          clear.innerHTML = `<span class="button-icon" aria-hidden="true">×</span><span>Clear</span>`;
	          clear.onclick = () => clearDefault(row);
	          actions.append(clear);
	        }
	        tr.append(actions);
	        tbody.append(tr);
	      }
	      if (!tbody.children.length) {
	        const tr = document.createElement("tr");
	        tr.innerHTML = `<td colspan="7" class="empty">No capability routes were returned by Gateway.</td>`;
	        tbody.append(tr);
	      }
	      renderSandboxCapabilityOptions();
	    }
	    function sandboxRouteMode(key) {
	      const value = String(key || "").trim().toLowerCase();
	      if (value === "input.text" || value === "output.text") return "text";
	      if (value === "output.image" || value === "output.image.text_to_image") return "image";
	      if (value === "output.voice") return "voice";
	      if (value === "output.sound") return "sound";
	      if (value === "output.music") return "music";
	      if (value === "output.video" || value === "output.video.text_to_video") return "video";
	      return "text";
	    }
	    function sandboxRouteIcon(mode) {
	      return { text: "T", image: "▣", voice: "◉", sound: "S", music: "♫", video: "▻" }[mode] || "T";
	    }
	    function svgIcon(paths, className = "") {
	      return `<svg class="${esc(className)}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${paths}</svg>`;
	    }
	    function sandboxRouteIconMarkup(mode) {
	      const icons = {
	        text: svgIcon('<path d="M5 6h14"></path><path d="M12 6v12"></path><path d="M8 18h8"></path>'),
	        image: svgIcon('<rect x="4" y="5" width="16" height="14" rx="2"></rect><path d="m7 15 3-3 3 3 2-2 3 4"></path><circle cx="9" cy="9" r="1.2"></circle>'),
	        voice: svgIcon('<path d="M5 10v4h4l5 4V6l-5 4H5z"></path><path d="M17 9c1 1 1.5 2 1.5 3s-.5 2-1.5 3"></path><path d="M19.5 7.5c1.7 1.7 2.5 3.5 2.5 5.5s-.8 3.8-2.5 5.5"></path>'),
	        sound: svgIcon('<path d="M4 12h2l2-5 4 10 3-7 2 2h3"></path><path d="M3 18h18"></path>'),
	        music: svgIcon('<path d="M10 18V6l9-2v12"></path><circle cx="7" cy="18" r="3"></circle><circle cx="16" cy="16" r="3"></circle>'),
	        video: svgIcon('<rect x="4" y="6" width="11" height="12" rx="2"></rect><path d="m15 10 5-3v10l-5-3z"></path>'),
	      };
	      return icons[mode] || icons.text;
	    }
	    function sandboxRouteShortLabel(row) {
	      const mode = sandboxRouteMode(defaultRowKey(row));
	      return { text: "Text", image: "Image", voice: "Voice", sound: "SFX", music: "Music", video: "Video" }[mode] || defaultRowCapability(row);
	    }
	    function sandboxRouteLabel(row) {
	      const key = defaultRowKey(row);
	      if (sandboxRouteMode(key) === "text") return "Text Chat";
	      return `${key} - ${defaultRowCapability(row)}`;
	    }
	    function sandboxCandidateRows() {
	      const wanted = new Set(["input.text", "output.text", "output.image.text_to_image", "output.voice", "output.sound", "output.music", "output.video.text_to_video"]);
	      const byKey = new Map();
	      for (const row of state.defaults || []) {
	        if (!visibleCapabilityDefaultRow(row)) continue;
	        const key = defaultRowKey(row);
	        if (wanted.has(key) && !byKey.has(key)) byKey.set(key, row);
	      }
	      const textRow = byKey.get("input.text") || byKey.get("output.text") || { key: "input.text", kind: "input", modality: "text", label: "Text Chat" };
	      const ordered = [textRow];
	      for (const key of ["output.image.text_to_image", "output.voice", "output.music", "output.sound", "output.video.text_to_video"]) {
	        if (byKey.has(key)) ordered.push(byKey.get(key));
	      }
	      return ordered;
	    }
	    function renderSandboxCapabilityOptions() {
	      const select = $("sandbox-capability");
	      if (!select) return;
	      const previous = select.value;
	      select.textContent = "";
	      const rows = sandboxCandidateRows();
	      const modes = $("sandbox-output-modes");
	      if (modes) modes.textContent = "";
	      for (const row of rows) {
	        const opt = document.createElement("option");
	        opt.value = defaultRowKey(row);
	        opt.textContent = sandboxRouteLabel(row);
	        select.append(opt);
	        if (modes) {
	          const mode = sandboxRouteMode(defaultRowKey(row));
	          const configured = defaultRowConfigured(row);
	          const btn = document.createElement("button");
	          btn.type = "button";
	          btn.className = "sandbox-mode";
	          btn.disabled = !configured;
	          btn.setAttribute?.("role", "radio");
	          const label = sandboxRouteShortLabel(row);
	          btn.title = `${label}: ${configured ? `${state.providerLabels.get(row.provider) || row.provider || ""} / ${row.model || ""}` : "not configured"}`;
	          btn.setAttribute?.("aria-label", btn.title);
	          btn.innerHTML = `<span class="sandbox-mode-icon" aria-hidden="true">${sandboxRouteIconMarkup(mode)}</span><span class="sandbox-mode-copy"><span class="sandbox-mode-main">${esc(label)}</span><span class="sandbox-mode-sub">${configured ? esc(row.model || "configured") : "not configured"}</span></span>`;
	          btn.onclick = () => {
	            if (btn.disabled) return;
	            select.value = defaultRowKey(row);
	            updateSandboxControls();
	          };
	          modes.append(btn);
	        }
	      }
	      select.value = rows.some((row) => defaultRowKey(row) === previous) ? previous : (rows[0] ? defaultRowKey(rows[0]) : "input.text");
	      updateSandboxControls();
	    }
	    function selectedSandboxRoute() {
	      const key = $("sandbox-capability")?.value || "input.text";
	      const rows = sandboxCandidateRows();
	      const exact = rows.find((row) => defaultRowKey(row) === key);
	      if (exact) return exact;
	      if (String(key).toLowerCase() === "output.text") {
	        const text = rows.find((row) => sandboxRouteMode(defaultRowKey(row)) === "text");
	        if (text) return text;
	      }
	      return rows[0] || { key, label: key };
	    }
	    function syncSandboxProviderOptions() {
	      const providerSelect = $("sandbox-provider");
	      if (!providerSelect) return;
	      setSelectOptions(providerSelect, state.providers || [], {
	        emptyLabel: state.providers.length ? "Select provider..." : "Configure a provider first",
	        disabled: !state.providers.length,
	        selected: providerSelect.value || "",
	      });
	      if (!providerSelect.value && state.providers.length) providerSelect.value = state.providers[0];
	      updateSandboxControls();
	    }
	    async function loadSandboxModels(selected = "") {
	      const provider = $("sandbox-provider").value;
	      if (!provider) {
	        setSelectOptions($("sandbox-model"), [], { emptyLabel: "Select provider first", disabled: true });
	        return;
	      }
	      setSelectOptions($("sandbox-model"), [], { emptyLabel: "Loading models...", disabled: true });
	      try {
	        const models = await fetchProviderModels(provider);
	        setSelectOptions($("sandbox-model"), models, {
	          emptyLabel: models.length ? "Select model..." : "No models discovered",
	          disabled: !models.length,
	          selected,
	        });
	      } catch (err) {
	        setSelectOptions($("sandbox-model"), [], { emptyLabel: "Model discovery failed", disabled: true });
	        $("sandbox-message").textContent = String(err.message || err);
	        $("sandbox-message").className = "message error";
	      }
	    }
	    function updateSandboxControls() {
	      const row = selectedSandboxRoute();
	      const mode = sandboxRouteMode(defaultRowKey(row));
	      $("sandbox-provider-label").classList.add("hidden");
	      $("sandbox-model-label").classList.add("hidden");
	      $("sandbox-system-label").classList.toggle("hidden", mode !== "text");
	      const configured = defaultRowConfigured(row);
	      const prov = row.provider ? (state.providerLabels.get(row.provider) || row.provider) : "";
	      $("sandbox-context").textContent = configured
	        ? `${sandboxRouteLabel(row)} will use ${prov} / ${row.model}.`
	        : `${sandboxRouteLabel(row)} is not configured yet. Configure it in Multimodal Capabilities first.`;
	      const prompt = $("sandbox-prompt");
	      if (prompt) {
	        prompt.placeholder = {
	          text: "Ask a question. Drop files here to include images, audio, video, PDFs, markdown, or text documents.",
	          image: "Describe the image you want to generate.",
	          voice: "Type the sentence to synthesize.",
	          sound: "Describe the sound effect you want to generate.",
	          music: "Describe the music you want to generate.",
	          video: "Describe the video you want to generate.",
	        }[mode] || "Type your request.";
	      }
	      const run = $("sandbox-run");
	      if (run) run.disabled = !configured;
	      const buttons = $("sandbox-output-modes")?.children || [];
	      for (const btn of buttons) {
	        const label = btn.children?.[1]?.children?.[0]?.textContent || "";
	        btn.classList.toggle("active", label === sandboxRouteShortLabel(row));
	        btn.setAttribute?.("aria-checked", label === sandboxRouteShortLabel(row) ? "true" : "false");
	      }
	    }
	    function sandboxNow() {
	      const d = new Date();
	      return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
	    }
	    function sandboxClientContext() {
	      const now = new Date();
	      const pad = (n) => String(Math.trunc(Math.abs(Number(n) || 0))).padStart(2, "0");
	      const offsetMinutes = -now.getTimezoneOffset();
	      const offsetSign = offsetMinutes >= 0 ? "+" : "-";
	      const offset = `${offsetSign}${pad(offsetMinutes / 60)}:${pad(offsetMinutes % 60)}`;
	      const localDatetime = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}${offset}`;
	      let timezone = "";
	      try { timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch {}
	      const nav = typeof navigator !== "undefined" ? navigator : {};
	      const languages = Array.isArray(nav.languages) ? nav.languages.filter(Boolean) : [];
	      const locale = String(languages[0] || nav.language || "").trim();
	      let localeCountry = "";
	      try {
	        if (locale && typeof Intl !== "undefined" && typeof Intl.Locale === "function") {
	          localeCountry = String(new Intl.Locale(locale).region || "").trim().toUpperCase();
	        }
	      } catch {}
	      if (!localeCountry && locale) {
	        const match = locale.match(/[-_]([A-Za-z]{2})(?:[-_]|$)/);
	        if (match) localeCountry = String(match[1] || "").toUpperCase();
	      }
	      const ctx = {
	        local_datetime: localDatetime,
	        utc_datetime: now.toISOString(),
	        timezone_offset_minutes: offsetMinutes,
	        source: "browser",
	      };
	      if (timezone) ctx.timezone = timezone;
	      if (locale) ctx.locale = locale;
	      if (localeCountry) ctx.locale_country = localeCountry;
	      return ctx;
	    }
	    function formatSandboxDuration(ms) {
	      const n = Number(ms || 0);
	      if (!Number.isFinite(n) || n <= 0) return "";
	      return n < 1000 ? `${Math.round(n)}ms` : `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}s`;
	    }
	    function sandboxUsageLabel(usage, elapsedMs) {
	      const elapsed = formatSandboxDuration(elapsedMs);
	      const data = objectValue(usage);
	      const tokens = Number(data?.completion_tokens ?? data?.output_tokens ?? data?.generated_tokens ?? data?.total_tokens ?? 0);
	      const parts = [];
	      if (elapsed) parts.push(elapsed);
	      if (Number.isFinite(tokens) && tokens > 0) {
	        parts.push(`${Math.round(tokens)} tok`);
	        const sec = Number(elapsedMs || 0) / 1000;
	        if (sec > 0) parts.push(`${Math.max(1, Math.round(tokens / sec))} tok/s`);
	      }
	      return parts.join(" · ");
	    }
	    function sandboxVoiceDefaultRow() {
	      return (state.defaults || []).find((row) => defaultRowKey(row) === "output.voice" && defaultRowConfigured(row));
	    }
	    function sandboxArtifactUrl(runId, ref) {
	      if (!ref || typeof ref !== "object") return "";
	      const id = String(ref.$artifact || ref.artifact_id || ref.id || "").trim();
	      if (!id) return "";
	      return `/api/gateway/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(id)}/content`;
	    }
	    function sandboxRememberObjectUrl(url) {
	      if (!url) return url;
	      state.sandboxObjectUrls.push(url);
	      return url;
	    }
	    function sandboxRevokeObjectUrls() {
	      if (typeof URL === "undefined" || !URL.revokeObjectURL) {
	        state.sandboxObjectUrls = [];
	        return;
	      }
	      for (const url of state.sandboxObjectUrls || []) {
	        try { URL.revokeObjectURL(url); } catch {}
	      }
	      state.sandboxObjectUrls = [];
	    }
	    function addSandboxMediaError(target, message) {
	      if (!target || target._sandboxMediaErrorShown) return;
	      target._sandboxMediaErrorShown = true;
	      const note = document.createElement("div");
	      note.className = "sandbox-media-error";
	      note.textContent = message;
	      target.append(note);
	    }
	    async function setSandboxMediaSource(mediaEl, wrap, runId, ref, label) {
	      const url = sandboxArtifactUrl(runId, ref);
	      if (!url || !mediaEl) return "";
	      if (mediaEl.dataset) mediaEl.dataset.artifactUrl = url;
	      else mediaEl.artifactUrl = url;
	      const rawContentType = String(ref?.content_type || "").trim();
	      mediaEl.onerror = () => addSandboxMediaError(wrap, `${label || "Artifact"} could not be decoded by this browser. Use the raw artifact link to inspect it.`);
	      if (typeof URL === "undefined" || !URL.createObjectURL || typeof fetch !== "function") {
	        mediaEl.src = url;
	        return url;
	      }
	      try {
	        const res = await fetch(url, { method: "GET", credentials: "same-origin" });
	        if (!res.ok) {
	          let detail = "";
	          try {
	            const text = await res.text();
	            detail = text ? `: ${text.slice(0, 160)}` : "";
	          } catch {}
	          throw new Error(`artifact download failed (${res.status})${detail}`);
	        }
	        const blob = await res.blob();
	        const typedBlob = rawContentType && (!blob.type || blob.type === "application/octet-stream")
	          ? new Blob([blob], { type: rawContentType })
	          : blob;
	        mediaEl.src = sandboxRememberObjectUrl(URL.createObjectURL(typedBlob));
	        return mediaEl.src;
	      } catch (err) {
	        mediaEl.src = url;
	        addSandboxMediaError(wrap, String(err.message || err));
	        return url;
	      }
	    }
	    function renderSandboxArtifact(target, { runId = "", ref = null, mode = "", label = "" } = {}) {
	      const url = sandboxArtifactUrl(runId, ref);
	      if (!url || !target) return;
	      const wrap = document.createElement("div");
	      wrap.className = "sandbox-artifact";
	      const contentType = String(ref?.content_type || "").toLowerCase();
	      const kind = mode || (contentType.startsWith("image/") ? "image" : contentType.startsWith("video/") ? "video" : contentType.startsWith("audio/") ? "audio" : "");
	      if (kind === "image") {
	        const img = document.createElement("img");
	        img.alt = label || "Generated image";
	        img.loading = "lazy";
	        wrap.append(img);
	        setSandboxMediaSource(img, wrap, runId, ref, label || "Image");
	      } else if (kind === "video") {
	        const video = document.createElement("video");
	        video.controls = true;
	        video.playsInline = true;
	        wrap.append(video);
	        setSandboxMediaSource(video, wrap, runId, ref, label || "Video");
	      } else if (kind === "voice" || kind === "music" || kind === "sound" || kind === "audio") {
	        const audio = document.createElement("audio");
	        audio.controls = true;
	        audio.preload = "metadata";
	        wrap.append(audio);
	        setSandboxMediaSource(audio, wrap, runId, ref, label || "Audio");
	      }
	      const link = document.createElement("a");
	      link.href = url;
	      link.target = "_blank";
	      link.rel = "noopener";
	      link.className = "sandbox-artifact-link";
	      link.textContent = label || "Open artifact";
	      wrap.append(link);
	      target.append(wrap);
	    }
	    function appendSandboxMessage(role, content, options = {}) {
	      const target = $("sandbox-transcript");
	      const div = document.createElement("div");
	      const kind = String(options.kind || role || "").toLowerCase();
	      div.className = `sandbox-message ${kind.includes("you") || kind === "user" ? "user" : kind.includes("error") ? "error" : kind.includes("system") ? "system" : "assistant"}`;
	      const bubble = document.createElement("div");
	      bubble.className = "sandbox-bubble";
	      const meta = document.createElement("div");
	      meta.className = "sandbox-message-meta";
	      const roleEl = document.createElement("span");
	      roleEl.className = "sandbox-message-role";
	      roleEl.textContent = role;
	      const timeEl = document.createElement("span");
	      timeEl.textContent = options.meta || sandboxNow();
	      const spacer = document.createElement("span");
	      spacer.className = "sandbox-message-spacer";
	      meta.append(roleEl, timeEl, spacer);
	      const body = document.createElement("div");
	      body.className = "sandbox-message-body";
	      const messageIsAssistant = String(div.className || "").includes("assistant");
	      setSandboxMessageBody(body, content, { markdown: options.markdown === true || (options.markdown !== false && messageIsAssistant) });
	      if (options.speakable && content && sandboxVoiceDefaultRow()) {
	        const speak = document.createElement("button");
	        speak.type = "button";
	        speak.className = "secondary sandbox-speak";
	        speak.title = "Speak this message";
	        speak.setAttribute?.("aria-label", "Speak this message");
	        speak.innerHTML = `<span aria-hidden="true">&#128266;</span>`;
	        speak.onclick = () => speakSandboxText(String(content || ""), bubble, speak);
	        meta.append(speak);
	      }
	      bubble.append(meta, body);
	      if (Array.isArray(options.attachments) && options.attachments.length) {
	        const chips = document.createElement("div");
	        chips.className = "sandbox-attachments";
	        for (const item of options.attachments) {
	          const chip = document.createElement("span");
	          chip.className = "sandbox-attachment";
	          chip.innerHTML = `<span>${esc(item.name || item.filename || "attachment")}</span>`;
	          chips.append(chip);
	        }
	        bubble.append(chips);
	      }
	      if (options.pending) {
	        const progress = document.createElement("div");
	        progress.className = "sandbox-progress";
	        progress.innerHTML = `<span>${esc(options.pendingLabel || "Working...")}</span><div class="sandbox-progress-bar"></div>`;
	        bubble.append(progress);
	      }
	      if (options.artifactRef) {
	        renderSandboxArtifact(bubble, { runId: options.runId, ref: options.artifactRef, mode: options.mode, label: options.artifactLabel });
	      }
	      div.append(bubble);
	      target.append(div);
	      target.scrollTop = target.scrollHeight;
	      return { el: div, bubble, body, meta };
	    }
	    function finalizeSandboxMessage(message, { content = "", meta = "", artifactRef = null, runId = "", mode = "", artifactLabel = "", usage = null, elapsedMs = 0, speakable = false } = {}) {
	      if (!message || !message.bubble) return;
	      const progress = Array.from(message.bubble.children || []).find((child) => String(child.className || "").includes("sandbox-progress"));
	      if (progress) progress.className = "hidden";
	      if (message.body) {
	        const messageIsAssistant = String(message.el?.className || "").includes("assistant");
	        setSandboxMessageBody(message.body, content, { markdown: messageIsAssistant });
	      }
	      const metaLine = sandboxUsageLabel(usage, elapsedMs) || meta;
	      if (metaLine && message.meta?.children?.[1]) message.meta.children[1].textContent = metaLine;
	      if (speakable && content && sandboxVoiceDefaultRow()) {
	        const speak = document.createElement("button");
	        speak.type = "button";
	        speak.className = "secondary sandbox-speak";
	        speak.title = "Speak this message";
	        speak.setAttribute?.("aria-label", "Speak this message");
	        speak.innerHTML = `<span aria-hidden="true">&#128266;</span>`;
	        speak.onclick = () => speakSandboxText(String(content || ""), message.bubble, speak);
	        message.meta.append(speak);
	      }
	      if (artifactRef) renderSandboxArtifact(message.bubble, { runId, ref: artifactRef, mode, label: artifactLabel });
	      const target = $("sandbox-transcript");
	      target.scrollTop = target.scrollHeight;
	    }
	    function findSandboxChild(root, className) {
	      if (!root) return null;
	      if (String(root.className || "").split(/\\s+/).includes(className)) return root;
	      for (const child of Array.from(root.children || [])) {
	        const found = findSandboxChild(child, className);
	        if (found) return found;
	      }
	      return null;
	    }
	    function sandboxMessageFromElement(el) {
	      if (!el) return null;
	      const bubble = findSandboxChild(el, "sandbox-bubble");
	      if (!bubble) return null;
	      return {
	        el,
	        bubble,
	        body: findSandboxChild(bubble, "sandbox-message-body"),
	        meta: findSandboxChild(bubble, "sandbox-message-meta"),
	      };
	    }
	    function latestPendingSandboxMessage() {
	      const target = $("sandbox-transcript");
	      const messages = Array.from(target?.children || []);
	      for (let index = messages.length - 1; index >= 0; index -= 1) {
	        const message = sandboxMessageFromElement(messages[index]);
	        const progress = findSandboxChild(message?.bubble, "sandbox-progress");
	        if (progress && !String(progress.className || "").includes("hidden")) return message;
	      }
	      return null;
	    }
	    function hideAllSandboxProgress() {
	      const visit = (node) => {
	        if (!node) return;
	        if (String(node.className || "").includes("sandbox-progress")) node.className = "hidden";
	        for (const child of Array.from(node.children || [])) visit(child);
	      };
	      visit($("sandbox-transcript"));
	    }
	    function failSandboxMessage(message, errorText) {
	      const targetMessage = message?.bubble ? message : latestPendingSandboxMessage();
	      if (!targetMessage || !targetMessage.bubble) {
	        hideAllSandboxProgress();
	        return false;
	      }
	      const text = String(errorText || "Generation failed.");
	      const progress = findSandboxChild(targetMessage.bubble, "sandbox-progress");
	      if (progress) progress.className = "hidden";
	      hideAllSandboxProgress();
	      if (targetMessage.el) targetMessage.el.className = "sandbox-message error";
	      if (targetMessage.body) setSandboxMessageBody(targetMessage.body, text, { markdown: false });
	      if (targetMessage.meta?.children?.[0]) targetMessage.meta.children[0].textContent = "Error";
	      if (targetMessage.meta?.children?.[1]) targetMessage.meta.children[1].textContent = sandboxNow();
	      const target = $("sandbox-transcript");
	      target.scrollTop = target.scrollHeight;
	      return true;
	    }
	    function sandboxRunId() {
	      const p = state.principal || {};
	      const tenant = String(p.tenant_id || "default").toLowerCase().replace(/[^a-z0-9_:-]+/g, "_").replace(/^_+|_+$/g, "") || "default";
	      const user = String(p.user_id || p.runtime_id || "user").toLowerCase().replace(/[^a-z0-9_:-]+/g, "_").replace(/^_+|_+$/g, "") || "user";
	      return `session_memory_gateway_console_sandbox_${tenant}_${user}`;
	    }
	    function sandboxRequestId() {
	      return `sandbox_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
	    }
	    async function uploadSandboxFile(file) {
	      if (typeof FormData === "undefined") throw new Error("This browser does not support file uploads.");
	      const form = new FormData();
	      form.append("session_id", sandboxRunId());
	      form.append("file", file);
	      form.append("filename", file?.name || "upload.bin");
	      if (file?.type) form.append("content_type", file.type);
	      const headers = new Headers();
	      headers.set("Accept", "application/json");
	      const token = csrf();
	      if (token) headers.set("X-AbstractGateway-CSRF", decodeURIComponent(token));
	      const res = await fetch("/api/gateway/attachments/upload", { method: "POST", body: form, headers, credentials: "same-origin" });
	      const text = await res.text();
	      let data = {};
	      try { data = text ? JSON.parse(text) : {}; } catch { data = { detail: text }; }
	      if (!res.ok) throw new Error(data.detail || `Upload failed (${res.status})`);
	      const artifact = data.attachment || data.artifact;
	      return { name: file?.name || "upload.bin", size: file?.size || 0, content_type: file?.type || artifact?.content_type || "", artifact };
	    }
	    function renderSandboxAttachments() {
	      const target = $("sandbox-attachments");
	      if (!target) return;
	      target.textContent = "";
	      for (const item of state.sandboxAttachments || []) {
	        const chip = document.createElement("span");
	        chip.className = "sandbox-attachment";
	        chip.innerHTML = `<span>${esc(item.name || "attachment")}</span>`;
	        target.append(chip);
	      }
	    }
	    async function handleSandboxFiles(files) {
	      const list = Array.from(files || []).filter(Boolean);
	      if (!list.length) return;
	      $("sandbox-message").textContent = "Uploading attachments...";
	      $("sandbox-message").className = "message";
	      try {
	        for (const file of list) state.sandboxAttachments.push(await uploadSandboxFile(file));
	        renderSandboxAttachments();
	        $("sandbox-message").textContent = "";
	      } catch (err) {
	        $("sandbox-message").textContent = String(err.message || err);
	        $("sandbox-message").className = "message error";
	      }
	    }
	    function setSandboxSpeakButton(button, mode) {
	      if (!button) return;
	      if (mode === "pause") {
	        button.disabled = false;
	        button.classList.add("speaking");
	        button.title = "Pause speech";
	        button.setAttribute?.("aria-label", "Pause speech");
	        button.innerHTML = `<span aria-hidden="true">II</span>`;
	      } else if (mode === "loading") {
	        button.disabled = true;
	        button.title = "Generating speech";
	        button.setAttribute?.("aria-label", "Generating speech");
	        button.innerHTML = `<span aria-hidden="true">...</span>`;
	      } else {
	        button.disabled = false;
	        button.classList.remove("speaking");
	        button.title = "Speak this message";
	        button.setAttribute?.("aria-label", "Speak this message");
	        button.innerHTML = `<span aria-hidden="true">&#128266;</span>`;
	      }
	    }
	    async function playSandboxAudio(audio, button) {
	      if (!audio) return;
	      try {
	        if (typeof audio.play === "function") {
	          await audio.play();
	          setSandboxSpeakButton(button, "pause");
	        }
	      } catch (err) {
	        setSandboxSpeakButton(button, "speak");
	        throw err;
	      }
	    }
	    async function speakSandboxText(text, host, button = null) {
	      const row = sandboxVoiceDefaultRow();
	      if (!row) return;
	      if (host?._sandboxSpeechAudio) {
	        const audio = host._sandboxSpeechAudio;
	        if (!audio.paused) {
	          if (typeof audio.pause === "function") audio.pause();
	          setSandboxSpeakButton(button, "speak");
	        } else {
	          await playSandboxAudio(audio, button);
	        }
	        return;
	      }
	      if (host?._sandboxSpeechBusy) return;
	      if (host) host._sandboxSpeechBusy = true;
	      setSandboxSpeakButton(button, "loading");
	      const runId = sandboxRunId();
	      try {
	        const body = { text, provider: row.provider, model: row.model, request_id: sandboxRequestId() };
	        const voice = textValue(objectValue(row.options)?.voice || objectValue(row.options)?.profile);
	        if (voice) body.voice = voice;
	        const res = await api(`/api/gateway/runs/${encodeURIComponent(runId)}/voice/tts`, { method: "POST", body: JSON.stringify(body) });
	        const audio = document.createElement("audio");
	        audio.preload = "auto";
	        audio.className = "hidden";
	        audio.onended = () => setSandboxSpeakButton(button, "speak");
	        audio.onpause = () => setSandboxSpeakButton(button, "speak");
	        audio.onplay = () => setSandboxSpeakButton(button, "pause");
	        if (host) host.append(audio);
	        await setSandboxMediaSource(audio, host, runId, res.audio_artifact, "Speech");
	        if (host) host._sandboxSpeechAudio = audio;
	        await playSandboxAudio(audio, button);
	      } catch (err) {
	        addSandboxMediaError(host, String(err.message || err));
	        setSandboxSpeakButton(button, "speak");
	      } finally {
	        if (host) host._sandboxSpeechBusy = false;
	      }
	    }
	    async function runSandbox() {
	      $("sandbox-message").textContent = "";
	      $("sandbox-message").className = "message";
	      const prompt = $("sandbox-prompt").value.trim();
	      const attachments = (state.sandboxAttachments || []).slice();
	      if (!prompt && !attachments.length) {
	        $("sandbox-message").textContent = "Prompt is required.";
	        $("sandbox-message").className = "message error";
	        return;
	      }
	      const row = selectedSandboxRoute();
	      const key = defaultRowKey(row);
	      const mode = sandboxRouteMode(key);
	      $("sandbox-run").disabled = true;
	      let pendingMessage = null;
	      try {
	        const promptText = prompt || "Please analyze the attached file(s).";
	        appendSandboxMessage("You", promptText, { kind: "user", attachments });
	        $("sandbox-prompt").value = "";
	        state.sandboxAttachments = [];
	        renderSandboxAttachments();
	        if (!defaultRowConfigured(row)) throw new Error(`${sandboxRouteLabel(row)} is not configured.`);
	        const started = Date.now();
	        if (mode === "text") {
	          const provider = row.provider;
	          const model = row.model;
	          const payload = {
	            capability: key,
	            provider,
	            model,
	            prompt: promptText,
	            system_prompt: $("sandbox-system").value.trim() || null,
	            messages: state.sandboxMessages,
	            attachments: attachments.map((item) => item.artifact).filter(Boolean),
	            client_context: sandboxClientContext(),
	          };
	          pendingMessage = appendSandboxMessage(`${state.providerLabels.get(provider) || provider} / ${model}`, "Thinking...", { pending: true, pendingLabel: "Generating answer", kind: "assistant" });
	          const res = await api("/api/gateway/sandbox/generate", { method: "POST", body: JSON.stringify(payload) });
	          const text = res.response || "(empty response)";
	          state.sandboxMessages.push({ role: "user", content: promptText }, { role: "assistant", content: text });
	          finalizeSandboxMessage(pendingMessage, { content: text, usage: res.usage, elapsedMs: Date.now() - started, speakable: true });
	        } else {
	          const runId = sandboxRunId();
	          let endpoint = "";
	          let body = {};
	          if (mode === "image") {
	            endpoint = `/api/gateway/runs/${encodeURIComponent(runId)}/images/generate`;
	            body = { prompt: promptText, image_provider: row.provider, image_model: row.model, request_id: sandboxRequestId() };
	          } else if (mode === "voice") {
	            endpoint = `/api/gateway/runs/${encodeURIComponent(runId)}/voice/tts`;
	            body = { text: promptText, provider: row.provider, model: row.model, request_id: sandboxRequestId() };
	            const voice = textValue(objectValue(row.options)?.voice || objectValue(row.options)?.profile);
	            if (voice) body.voice = voice;
	          } else if (mode === "music" || mode === "sound") {
	            endpoint = `/api/gateway/runs/${encodeURIComponent(runId)}/music/generate`;
	            const soundTask = key === "output.sound" || mode === "sound";
	            body = { prompt: promptText, task: soundTask ? "text_to_audio" : "text_to_music", music_provider: row.provider, music_model: row.model, request_id: sandboxRequestId() };
	          } else if (mode === "video") {
	            endpoint = `/api/gateway/runs/${encodeURIComponent(runId)}/videos/generate`;
	            body = { prompt: promptText, video_provider: row.provider, video_model: row.model, request_id: sandboxRequestId() };
	          }
	          pendingMessage = appendSandboxMessage(sandboxRouteShortLabel(row), "Starting generation...", { pending: true, pendingLabel: mode === "image" || mode === "video" ? "Generating media" : "Generating artifact", kind: "assistant" });
	          const res = await api(endpoint, { method: "POST", body: JSON.stringify(body) });
	          if (res.ok === false) throw new Error(res.error || res.code || "Generation failed.");
	          const ref = res.image_artifact || res.audio_artifact || res.music_artifact || res.video_artifact || null;
	          finalizeSandboxMessage(pendingMessage, {
	            content: "Generation completed.",
	            elapsedMs: Date.now() - started,
	            artifactRef: ref,
	            runId,
	            mode,
	            artifactLabel: mode === "image" ? "Open image" : mode === "video" ? "Open video" : "Open audio",
	          });
	        }
	      } catch (err) {
	        const message = String(err.message || err);
	        $("sandbox-message").textContent = message;
	        $("sandbox-message").className = "message error";
	        if (!failSandboxMessage(pendingMessage, message)) appendSandboxMessage("Error", message, { kind: "error" });
	      } finally {
	        updateSandboxControls();
	      }
	    }
	    function clearSandbox() {
	      sandboxRevokeObjectUrls();
	      state.sandboxMessages = [];
	      state.sandboxAttachments = [];
	      $("sandbox-transcript").textContent = "";
	      $("sandbox-message").textContent = "";
	      $("sandbox-message").className = "message";
	      $("sandbox-prompt").value = "";
	      renderSandboxAttachments();
	    }
	    async function openDefaultModal(row) {
	      if (defaultRowReadOnly(row)) return;
	      state.activeDefaultRow = row;
	      const key = defaultRowKey(row);
	      const catalog = defaultCatalogForRow(row);
	      $("default-modal-title").textContent = defaultRowConfigured(row) ? "Edit multimodal capability" : "Configure multimodal capability";
	      $("default-modal-description").textContent = `Select a ${catalog.scope} provider, then choose one of its discovered compatible models.`;
	      $("default-modal-route").textContent = `${key} - ${defaultRowCapability(row)}`;
	      $("default-modal-message").textContent = "";
	      $("default-modal-message").className = "message";
	      $("default-modal-backdrop").classList.remove("hidden");
	      let discoveredProviders = [];
	      try {
	        discoveredProviders = await fetchDefaultProviders(row);
	      } catch (err) {
	        $("default-modal-message").textContent = String(err.message || err);
	        $("default-modal-message").className = "message error";
	      }
	      const providers = row.provider && !discoveredProviders.includes(row.provider)
	        ? [row.provider, ...discoveredProviders]
	        : discoveredProviders;
	      setSelectOptions($("modal-default-provider"), providers, {
	        emptyLabel: providers.length ? "Select provider..." : catalog.emptyProviders,
	        disabled: !providers.length,
	        selected: row.provider || "",
	      });
	      if (row.provider && !discoveredProviders.includes(row.provider)) {
	        $("default-modal-message").textContent = `Configured provider "${row.provider}" is not currently discovered in the ${catalog.scope} catalog.`;
	        $("default-modal-message").className = "message error";
	      }
	      try {
	        await loadDefaultModels($("modal-default-provider").value, row.model || "", row);
	        await loadDefaultVoices($("modal-default-provider").value, $("modal-default-model").value, defaultVoiceValue(row), row);
	      } catch (err) {
	        if (row.model) {
	          setSelectOptions($("modal-default-model"), [row.model], {
	            emptyLabel: "Discovery failed",
	            disabled: false,
	            selected: row.model,
	          });
	        } else {
	          setSelectOptions($("modal-default-model"), [], { emptyLabel: "Model discovery failed", disabled: true });
	        }
	        await loadDefaultVoices($("modal-default-provider").value, $("modal-default-model").value, defaultVoiceValue(row), row).catch(() => {});
	        $("default-modal-message").textContent = String(err.message || err);
	        $("default-modal-message").className = "message error";
	      }
	      $("clear-default").classList.toggle("hidden", !defaultRowConfigured(row));
	    }
	    function closeDefaultModal() {
	      $("default-modal-backdrop").classList.add("hidden");
	      state.activeDefaultRow = null;
	    }
    async function refresh() {
      let me;
      try {
        me = await api("/api/gateway/me");
      } catch (err) {
        renderAccount(null);
        $("login-section").classList.remove("hidden");
        return;
      }
      renderAccount(me);
      $("login-section").classList.add("hidden");
      try {
        await loadEndpointProfiles();
      } catch (err) {
        const tbody = $("endpoint-profiles-table");
        tbody.textContent = "";
        const tr = document.createElement("tr");
        tr.innerHTML = `<td colspan="6" class="empty">Provider connections could not be loaded: ${esc(String(err.message || err))}</td>`;
        tbody.append(tr);
      }
      try {
        await loadProviders();
      } catch (err) {
        $("defaults-message").textContent = String(err.message || err);
        $("defaults-message").className = "message error";
      }
      try {
        const defaults = await api("/api/gateway/config/capability-defaults");
        await renderDefaults(defaults);
      } catch (err) {
        $("defaults-message").textContent = String(err.message || err);
        $("defaults-message").className = "message error";
      }
      if (me.principal?.admin) {
        try {
          const users = await api("/api/gateway/admin/users");
          state.users = users.users || [];
          renderUsers(state.users);
        } catch (err) {
          $("users-message").textContent = String(err.message || err);
          $("users-message").className = "message error";
        }
        try {
          const reservations = await api("/api/gateway/admin/runtime-reservations");
          renderRuntimeReservations(reservations.runtime_reservations || []);
        } catch (err) {
          $("reservations-message").textContent = String(err.message || err);
          $("reservations-message").className = "message error";
        }
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
	      $("default-modal-message").textContent = "";
	      try {
	        const row = state.activeDefaultRow;
	        if (!row) throw new Error("No capability route selected.");
	        const provider = $("modal-default-provider").value;
	        const model = $("modal-default-model").value;
	        if (!provider || !model) throw new Error("Select a discovered provider and model before saving.");
	        const { kind, modality, task } = defaultRowKindModality(row);
	        const options = row.options && typeof row.options === "object" && !Array.isArray(row.options) ? { ...row.options } : {};
	        if (isVoiceOutputDefault(row)) {
	          delete options.voice;
	          delete options.profile;
	          const voice = $("modal-default-voice").value;
	          if (voice) options.voice = voice;
	        }
	        const taskPath = task ? `/${encodeURIComponent(task)}` : "";
	        await api(`/api/gateway/config/capability-defaults/${encodeURIComponent(kind)}/${encodeURIComponent(modality)}${taskPath}`, {
	          method: "PUT",
	          body: JSON.stringify({
	            provider,
	            model,
	            base_url: row.base_url || null,
	            options
	          })
	        });
	        $("defaults-message").textContent = "Saved.";
	        $("defaults-message").className = "message ok";
	        closeDefaultModal();
	        await renderDefaults(await api("/api/gateway/config/capability-defaults"));
	      } catch (err) {
	        $("default-modal-message").textContent = String(err.message || err);
	        $("default-modal-message").className = "message error";
	      }
	    }
	    async function clearDefault(row = null) {
	      const target = row || state.activeDefaultRow;
	      if (!target) return;
	      const { kind, modality, task } = defaultRowKindModality(target);
	      const taskPath = task ? `/${encodeURIComponent(task)}` : "";
	      await api(`/api/gateway/config/capability-defaults/${encodeURIComponent(kind)}/${encodeURIComponent(modality)}${taskPath}`, { method: "DELETE" });
	      if (!row) closeDefaultModal();
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
	    $("open-appearance").onclick = openAppearance;
	    $("appearance-close").onclick = closeAppearance;
	    $("appearance-backdrop").onclick = (event) => { if (event.target === $("appearance-backdrop")) closeAppearance(); };
	    $("appearance-theme").onchange = updateAppearanceFromForm;
	    $("appearance-font-size").onchange = updateAppearanceFromForm;
	    $("appearance-header-size").onchange = updateAppearanceFromForm;
	    $("tab-button-users").onclick = () => setActiveTab("users");
	    $("tab-button-providers").onclick = () => setActiveTab("providers");
	    $("tab-button-defaults").onclick = () => setActiveTab("defaults");
	    $("tab-button-sandbox").onclick = () => setActiveTab("sandbox");
	    $("create-user").onclick = createUser;
	    $("refresh-catalog").onclick = async () => {
	      state.providerModels.clear();
	      await loadProviders();
	      await renderDefaults(await api("/api/gateway/config/capability-defaults"));
	    };
	    $("close-default-modal").onclick = closeDefaultModal;
	    $("default-modal-backdrop").onclick = (event) => { if (event.target === $("default-modal-backdrop")) closeDefaultModal(); };
	    $("modal-default-provider").onchange = async () => {
	      const row = state.activeDefaultRow || null;
	      await loadDefaultModels($("modal-default-provider").value, "", row);
	      await loadDefaultVoices($("modal-default-provider").value, $("modal-default-model").value, "", row);
	    };
	    $("modal-default-model").onchange = () => loadDefaultVoices(
	      $("modal-default-provider").value,
	      $("modal-default-model").value,
	      "",
	      state.activeDefaultRow || null
	    );
	    $("save-default").onclick = saveDefault;
	    $("clear-default").onclick = () => clearDefault();
	    $("sandbox-capability").onchange = updateSandboxControls;
	    $("sandbox-provider").onchange = () => loadSandboxModels();
	    $("sandbox-run").onclick = runSandbox;
	    $("sandbox-prompt").onkeydown = (event) => {
	      if (event.key === "Enter" && !event.shiftKey) {
	        event.preventDefault();
	        if (!$("sandbox-run").disabled) runSandbox();
	      }
	    };
	    $("sandbox-clear").onclick = clearSandbox;
	    $("sandbox-attach").onclick = () => $("sandbox-file-input").click();
	    $("sandbox-file-input").onchange = (event) => handleSandboxFiles(event?.target?.files || []);
	    $("sandbox-dropzone").ondragover = (event) => { event.preventDefault(); $("sandbox-dropzone").classList.add("dragover"); };
	    $("sandbox-dropzone").ondragleave = () => $("sandbox-dropzone").classList.remove("dragover");
	    $("sandbox-dropzone").ondrop = (event) => {
	      event.preventDefault();
	      $("sandbox-dropzone").classList.remove("dragover");
	      handleSandboxFiles(event?.dataTransfer?.files || []);
	    };
    $("save-endpoint-profile").onclick = saveEndpointProfile;
    $("cancel-endpoint-profile").onclick = closeEndpointModal;
    $("provider-modal-backdrop").onclick = (event) => { if (event.target === $("provider-modal-backdrop")) closeEndpointModal(); };
	    $("endpoint-provider-family").onchange = handleEndpointFamilyChange;
	    $("discover-endpoint-models").onclick = discoverEndpointModels;
	    $("clear-endpoint-models").onclick = clearEndpointModelAllowlist;
	    $("endpoint-models").onchange = updateEndpointModelSummary;
	    state.appearance = loadAppearanceSettings();
	    initAppearanceControls();
	    applyAppearanceSettings();
	    state.activeTab = readStringSetting(ACTIVE_TAB_KEY, "providers");
	    setActiveTab(state.activeTab);
	    initEndpointProfileFormOptions();
	    setEndpointModelOptions([], []);
	    refresh();
  </script>
</body>
</html>"""
