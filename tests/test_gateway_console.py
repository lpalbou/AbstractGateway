from __future__ import annotations

import re
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from abstractgateway.console import gateway_console_html


def test_gateway_console_routes_are_served(monkeypatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractgateway.app import app

    with TestClient(app) as client:
        root = client.get("/", follow_redirects=False)
        console = client.get("/console")

    assert root.status_code == 307
    assert root.headers["location"] == "/console"
    assert console.status_code == 200
    assert "AbstractGateway Console" in console.text
    assert "/api/gateway/session/login" in console.text
    assert "/api/gateway/admin/users" in console.text
    assert "function principalKind(u)" in console.text
    assert "u.principal_kind" in console.text
    assert 'principalKind(u) !== "entity"' in console.text
    assert "/api/gateway/admin/runtime-reservations" in console.text
    assert "/api/gateway/config/provider-endpoint-profiles" in console.text
    assert "/api/gateway/config/provider-endpoint-profiles/discover-models" in console.text
    assert "/api/gateway/discovery/providers/${encodeURIComponent(provider)}/models" in console.text
    assert "/api/gateway/vision/provider_models" in console.text
    assert "/api/gateway/audio/speech/models" in console.text
    assert "/api/gateway/audio/transcriptions/models" in console.text
    assert "/api/gateway/audio/music/models" in console.text
    assert "/api/gateway/embeddings/models" in console.text
    # Models tab (host residency) routes: one snapshot read, the residency
    # verbs, the estimate read, the per-session cache clear, and the
    # discovery contract that carries the canonical modality palette.
    assert "/api/gateway/host/state" in console.text
    assert "/api/gateway/models/load" in console.text
    assert "/api/gateway/models/unload" in console.text
    assert "/api/gateway/models/lock" in console.text
    assert "/api/gateway/models/unlock" in console.text
    assert "/api/gateway/models/context_estimate" in console.text
    assert "/api/gateway/sessions/${encodeURIComponent(sessionId)}/prompt_cache/clear_all" in console.text
    assert "/api/gateway/discovery/capabilities" in console.text
    assert 'capability_route: "output.text"' in console.text
    assert 'capability_route: "input.image,output.text"' in console.text
    assert 'capability_route: "input.video,output.text"' in console.text
    assert 'capability_route: "input.sound,output.text"' in console.text
    assert 'capability_route: "input.music,output.text"' in console.text
    assert 'visionCatalog("video generation", "text_to_video")' in console.text
    assert 'visionCatalog("image edit", "image_to_image")' in console.text
    assert 'visionCatalog("image restore / upscale", "image_upscale")' in console.text
    assert 'visionCatalog("image to video", "image_to_video")' in console.text
    assert '<select id="modal-default-provider">' in console.text
    assert '<select id="modal-default-model">' in console.text
    assert '<select id="modal-default-voice">' in console.text
    assert 'id="default-modal-backdrop"' in console.text
    assert 'class="modal flow-modal default-modal"' in console.text
    # Charter refactor (operator 2026-07-15): the kit tokens are now the
    # SOURCE (--bg-primary is the literal charter value; the console's
    # historical names alias onto it — the reverse of the old mapping).
    assert "--bg-primary: #1a1a2e" in console.text
    assert "--bg: var(--bg-primary)" in console.text
    assert "--accent: #e94560" in console.text
    assert 'class="shell_sidebar' in console.text  # the family shell
    # REVERSED (operator 2026-08-02): the base URL field was absent since
    # 0.2.26 and this line locked that in. It is a parity gap — the console-TUI
    # route editor has always carried base URL and options and sends both on
    # every save, and PUT /config/capability-defaults/{kind}/{modality} accepts
    # them. Offline it is load-bearing: base_url is how a single route is
    # pointed at a local inference server on a non-default port.
    assert 'id="modal-default-base-url"' in console.text
    assert 'id="modal-default-options"' in console.text
    assert 'id="refresh-default-models"' not in console.text
    assert "Available Providers" in console.text
    assert "Multimodal Capabilities" in console.text
    assert "Sandbox" in console.text
    assert "/api/gateway/sandbox/generate" in console.text
    assert "renderMarkdown" in console.text
    assert "sandbox-mode-copy" in console.text
    assert "Saved Connections" not in console.text
    assert 'id="provider-modal-backdrop"' in console.text
    assert 'id="cancel-endpoint-profile"' in console.text
    assert 'id="endpoint-description"' in console.text
    assert 'id="endpoint-api-key"' in console.text
    assert 'id="endpoint-clear-api-key"' in console.text
    assert 'id="discover-endpoint-models"' in console.text
    assert 'id="endpoint-models" class="model-picker__select" multiple' in console.text
    assert 'id="endpoint-capabilities"' not in console.text
    assert 'id="endpoint-profiles-table"' in console.text
    assert "virtual provider" in console.text
    assert "Pick a provider type to configure" in console.text
    assert "Use Test to preview discovery" in console.text
    assert "Confirm" in console.text
    assert "provider-modal" in console.text
    assert 'id="new-email"' in console.text
    assert 'id="runtime-reservations-section"' in console.text
    assert 'id="confirm-backdrop"' in console.text
    # Wave 3: the confirm modal's INPUT variant (steer guidance) — the last
    # window.prompt in the console is gone; steering uses the themed modal.
    assert 'id="confirm-input"' in console.text
    assert "window.prompt(" not in console.text
    assert 'id="login-form"' in console.text
    assert 'id="toggle-token"' in console.text
    assert "Connect to AbstractGateway" in console.text
    # Operator IA (2026-07-13): Users & Entities | Runtimes | Providers |
    # Multimodal Capabilities | Sandbox. Entities are door principals and
    # live WITH users; runtimes split into their own tab; sandbox is last.
    assert 'id="tab-button-users"' in console.text
    assert 'id="tab-button-runtimes"' in console.text
    assert 'id="tab-button-providers"' in console.text
    assert 'id="tab-button-defaults"' in console.text
    assert 'id="tab-button-sandbox"' in console.text
    assert 'id="tab-button-models"' in console.text
    assert 'id="tab-button-entities"' not in console.text, "entities merged into Users & Entities"
    # Family shell: navigation lives in the sidebar (ids unchanged).
    assert 'class="shell_nav"' in console.text
    assert 'id="tab-button-users"' in console.text
    # Summoned Entities: the full create + manage surface (operator directive
    # 2026-07-12) — template gallery with locked core values, substrate at
    # create, per-phase capability matrix, and lifecycle management for
    # existing entities (state, loop, substrate, capabilities, prompt,
    # reembed, verify) — every route the JS drives is pinned here.
    assert 'id="tab-users"' in console.text
    assert 'id="tab-runtimes"' in console.text
    assert 'id="entity-template"' in console.text
    assert 'id="entity-create"' in console.text
    assert 'id="entities-table"' in console.text
    assert 'id="entity-template-values"' in console.text
    assert 'id="entity-advanced"' in console.text
    assert 'id="entity-new-matrix"' in console.text
    assert 'id="entity-manage-section"' in console.text
    assert 'id="entity-subtab-overview"' in console.text
    assert 'id="entity-subtab-lifecycle"' in console.text
    assert 'id="entity-subtab-substrate"' in console.text
    assert 'id="entity-subtab-tools"' in console.text
    assert 'id="entity-subtab-prompt"' in console.text
    assert 'id="entity-manage-matrix"' in console.text
    assert 'id="entity-prompt-layers"' in console.text
    assert 'id="entity-reembed"' in console.text
    assert 'id="entity-verify"' in console.text
    assert 'id="entity-owntime-toggle"' in console.text  # laurent 12:32: ONE push button, server-truth rendered
    assert 'aria-pressed' in console.text
    assert 'id="entity-state-asleep"' in console.text
    assert "/api/gateway/entities/templates" in console.text
    assert "/api/gateway/entities/inventory/capability-matrix" in console.text
    assert "/api/gateway/entities/${enc}/validate" in console.text
    assert "/api/gateway/entities/${enc}/substrate" in console.text
    assert "/api/gateway/entities/${enc}/tool-policy" in console.text
    assert "/api/gateway/entities/${encodeURIComponent(name)}/state" in console.text
    assert "/api/gateway/entities/${encodeURIComponent(name)}/loop/start" in console.text
    assert "/api/gateway/entities/${encodeURIComponent(name)}/loop/stop" in console.text
    assert "/api/gateway/entities/${encodeURIComponent(name)}/substrate" in console.text
    assert "/api/gateway/entities/${encodeURIComponent(name)}/tool-policy" in console.text
    assert "/api/gateway/entities/${encodeURIComponent(name)}/prompt" in console.text
    assert "/api/gateway/entities/${encodeURIComponent(name)}/reembed" in console.text
    assert "/api/gateway/entities/${encodeURIComponent(name)}/verify" in console.text
    assert "/api/gateway/entities/${encodeURIComponent(name)}/card" in console.text
    # Runtime domain (operator dm#32 redesign, 2026-07-25): master table ->
    # ONE tabbed detail pane [Runs | Sessions | Caches]. The old global
    # Console-TUI mirror (laurent dm#35): TWO tabs only — Sessions | Data &
    # cache — behind a teaching line; nothing loads until a runtime is
    # chosen. The runs machinery (ids preserved: runs-table/runs-refresh/
    # run-inspect) lives INSIDE the Sessions tab; there is no Runs tab and
    # no session-fold table. Machine-wide Data & Caches stays a collapsed
    # disclosure whose walk loads on first expand.
    assert 'id="runs-section"' not in console.text  # the stacked section is gone
    assert 'id="runtime-detail-teach"' in console.text
    assert 'id="runtime-detail-tabs"' in console.text
    assert 'id="runtime-subtab-runs"' not in console.text  # no Runs tab (dm#35)
    assert 'id="runtime-subtab-sessions"' in console.text
    assert 'id="runtime-subtab-caches"' in console.text
    # Workspace access policy (operator redesign 2026-08-19): NO standing
    # card/form — ONE modal edits both the gateway defaults (default-row
    # button) and a single user's policy (per-row button, Users-table
    # Workspace action). Posture = radio cards; trust first.
    assert 'id="runtime-settings-section"' not in console.text
    assert 'id="workspace-policy-modal-backdrop"' in console.text
    assert 'id="wsp-mode-cards"' in console.text
    assert 'id="wsp-trust"' in console.text
    assert 'id="wsp-allowed"' in console.text
    assert 'id="wsp-blocked"' in console.text
    assert 'id="wsp-root-field"' in console.text
    assert 'id="runtime-workspace-root"' not in console.text  # inline form gone
    assert 'id="runtime-config-save"' not in console.text
    assert 'id="runtime-runs-default"' in console.text
    assert 'id="runtime-runs-readonly"' in console.text
    assert 'id="runtime-sessions-table"' not in console.text  # fold table gone (dm#35)
    assert 'id="runtime-caches-table"' in console.text
    # The Runtimes tab is the TABLE + the tabbed panel (Runs | Cache),
    # nothing else (operator 2026-08-19): no machine-wide section — the
    # default runtime's Cache tab lists machine-wide stores (TUI parity);
    # retained runtimes appears only when reservations exist.
    assert 'id="data-homes-section"' not in console.text
    assert '<section id="runtime-reservations-section"' in console.text
    # Tab order (operator): Runs | Artifacts | Cache | Logs. The Cache tab
    # lists ONLY disposable stores; deliverables live in the Artifacts tab;
    # logs are their own readable category.
    assert ">Runs</button>" in console.text
    assert ">Artifacts</button>" in console.text
    assert ">Cache</button>" in console.text
    assert ">Logs</button>" in console.text
    assert console.text.index(">Runs</button>") < console.text.index(">Artifacts</button>") < console.text.index(">Cache</button>") < console.text.index(">Logs</button>")
    assert 'id="runtime-artifacts-table"' in console.text
    assert 'id="runtime-logs-table"' in console.text
    assert 'id="log-modal-backdrop"' in console.text  # log tail opens in a MODAL
    # Artifacts preview + run inspect are MODALS too (operator 2026-08-19);
    # rows are the controls — no per-row Open buttons, no per-tab refresh.
    assert 'id="artifact-modal-backdrop"' in console.text
    assert 'id="run-modal-backdrop"' in console.text
    assert 'id="runtime-artifacts-search"' in console.text
    assert 'id="runtime-artifacts-refresh"' not in console.text
    # ONE toolbar shape on every list tab: compact select + search bar.
    # 5 = the four runtime-tab lists + the Workflows registry list.
    assert console.text.count('class="list-toolbar"') == 5
    assert 'id="runs-search"' in console.text
    assert 'id="runtime-caches-search"' in console.text
    assert 'id="runtime-logs-search"' in console.text
    assert 'class="artifacts-toolbar"' not in console.text
    assert 'id="runs-session-chip"' not in console.text  # dead chip removed
    assert 'id="run-inspect"' not in console.text
    assert 'class="table-scroll"' in console.text  # bounded master (load-bearing)
    assert 'id="runs-table"' in console.text
    assert 'id="runs-refresh"' not in console.text  # no per-tab refresh (pane ↻ owns it)
    assert "/api/gateway/runs?" in console.text
    assert '"/api/gateway/commands"' in console.text
    # Models tab (host residency: the agentic-OS resources view). One
    # snapshot call feeds three stacked sections; the 5s poll is scoped to
    # the ACTIVE tab; residency is TRI-STATE (null renders as a distinct
    # "unknown", never a blank); the warm-up form is admin-only; the meter
    # rides the SHARED determinate-bar recipe (one recipe, drive bars and
    # host meters together — wave-3 rule).
    assert 'id="tab-models"' in console.text
    assert 'id="models-message"' in console.text
    assert 'id="models-caches-message"' in console.text  # survives the finally-refresh clearing #models-message
    assert 'id="models-table"' in console.text
    assert 'id="models-caches-table"' in console.text
    assert 'id="models-meters"' in console.text
    assert 'id="models-degraded"' in console.text
    assert 'id="models-host-facts"' in console.text
    assert 'id="models-load-form"' in console.text
    assert 'id="models-load-provider"' in console.text
    assert 'id="models-load-model"' in console.text
    assert 'id="models-load-lock"' in console.text
    assert 'id="models-breakdown"' in console.text  # the itemized memory usage under the meters
    # The warm-up row rides the console's OWN select recipe (operator: "both
    # the provider and model should be the official dropdown components"), not
    # free-text inputs with datalist suggestions. The free-text lanes survive
    # only as the shared offline escape hatch.
    assert '<select id="models-load-provider"' in console.text
    assert '<select id="models-load-model"' in console.text
    assert 'id="models-load-provider-options"' not in console.text
    assert 'id="models-load-model-options"' not in console.text
    assert 'id="models-load-provider-custom"' in console.text
    assert 'id="models-load-model-custom"' in console.text
    assert 'id="models-refresh"' in console.text
    assert ".drive-track, .meter-track {" in console.text, "the meter must share the drive bar's recipe"
    assert 'state.activeTab !== "models"' in console.text, "the host poll must stop off-tab"
    assert '"configured — not in memory"' in console.text and '"resident"' in console.text, "residency must be tri-state"
    # Loaded-vs-default truth: the table defaults to RESIDENT rows only, the
    # rest sit behind an explicit configured/cached toggle with a count.
    assert 'id="models-show-cached"' in console.text
    assert 'id="models-show-cached-label"' in console.text
    assert 'id="models-loaded-title"' in console.text
    assert 'id="sandbox-capability"' in console.text
    assert 'id="sandbox-provider"' in console.text
    assert 'id="sandbox-run"' in console.text
    assert "sandbox-composer-toolbar" in console.text
    assert "sandbox-controls" not in console.text
    assert "position: sticky;" in console.text
    assert 'id="appearance-backdrop"' in console.text
    assert "abstractgateway_ui_settings_v1" in console.text
    assert "display: grid;" in console.text
    assert "body:not(.signed-in) .console-shell { align-content: start; padding-block: 18px; }" in console.text
    assert "radial-gradient(circle at 1px 1px" not in console.text
    assert "Gateway URL" not in console.text


def test_gateway_console_inline_javascript_parses() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript syntax checking")

    scripts = re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S)
    assert scripts
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as f:
        f.write("\n".join(scripts))
        f.flush()
        result = subprocess.run([node, "--check", f.name], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr
    assert "HTML_ESCAPES" in scripts[0]


def test_gateway_console_inline_javascript_submits_login_request() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript console smoke checks")

    scripts = re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S)
    assert scripts
    script_source = json.dumps("\n".join(scripts))
    harness = f"""
import vm from "node:vm";
const source = {script_source};

class Element {{
  constructor(id) {{
    this.id = id;
    this.value = "";
    this.checked = false;
    this.type = "";
    this.disabled = false;
    this._textContent = "";
    this.innerHTML = "";
	    this.className = "";
	    this.style = {{}};
	    this.children = [];
    this.classList = {{
      add: (...names) => {{
        const set = new Set(String(this.className || "").split(/\\s+/).filter(Boolean));
        for (const name of names) set.add(name);
        this.className = [...set].join(" ");
      }},
      remove: (...names) => {{
        const remove = new Set(names);
        this.className = String(this.className || "").split(/\\s+/).filter((name) => name && !remove.has(name)).join(" ");
      }},
      toggle: (name, force) => {{
        const set = new Set(String(this.className || "").split(/\\s+/).filter(Boolean));
        const shouldAdd = force === undefined ? !set.has(name) : Boolean(force);
        if (shouldAdd) set.add(name);
        else set.delete(name);
        this.className = [...set].join(" ");
      }},
      // The real DOM has `contains`, and page code reads it to decide whether a
      // conditionally-shown control is live — the offline free-text model lane
      // does exactly that. A stub missing it does not fail honestly; it throws
      // inside the handler under test and looks like a page bug.
      contains: (name) => new Set(
        String(this.className || "").split(/\\s+/).filter(Boolean)
      ).has(name),
    }};
  }}
  get textContent() {{ return this._textContent; }}
  set textContent(value) {{
    this._textContent = String(value || "");
    if (!this._textContent) this.children = [];
  }}
  get options() {{ return this.children; }}
  get selectedOptions() {{ return this.children.filter((child) => child.selected); }}
  append(...items) {{ this.children.push(...items); }}
}}

const elements = new Map();
function el(id) {{
  if (!elements.has(id)) elements.set(id, new Element(id));
  return elements.get(id);
}}
const body = el("body");
const document = {{
  body,
  documentElement: el("html"),
  cookie: "",
  getElementById: el,
  createElement: (tag) => new Element(tag),
}};
const localStorageData = new Map();
const localStorage = {{
  getItem(key) {{ return localStorageData.has(key) ? localStorageData.get(key) : null; }},
  setItem(key, value) {{ localStorageData.set(key, String(value)); }},
}};
let loggedIn = false;
const calls = [];
function response(status, payload) {{
  return {{
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
    blob: async () => new Blob([JSON.stringify(payload)], {{ type: "application/json" }}),
  }};
}}
function blobResponse(contentType, body = "ok") {{
  return {{
    ok: true,
    status: 200,
    text: async () => body,
    blob: async () => new Blob([body], {{ type: contentType }}),
  }};
}}
async function fetch(path, options = {{}}) {{
  calls.push({{ path, method: options.method || "GET", body: options.body || "" }});
  if (path === "/api/gateway/session/login") {{
    const body = JSON.parse(String(options.body || "{{}}"));
    if (body.user_id !== "admin" || body.token !== "test-token") return response(401, {{ detail: "bad login" }});
    loggedIn = true;
    document.cookie = "abstractgateway_csrf=agcsrf_test";
    return response(200, {{ ok: true }});
  }}
  if (path === "/api/gateway/me") {{
    if (!loggedIn) return response(401, {{ detail: "signed out" }});
    return response(200, {{
      ok: true,
      principal: {{ tenant_id: "default", user_id: "admin", runtime_id: "default", roles: ["admin", "user"], admin: true }}
    }});
  }}
  if (path === "/api/gateway/discovery/providers") return response(200, {{
    items: [
      {{ name: "openai", display_name: "OpenAI" }},
      {{ name: "lmstudio", display_name: "LM Studio" }},
      {{ name: "endpoint:openai", display_name: "OpenAI Production" }}
    ]
  }});
  if (path === "/api/gateway/discovery/providers/openai/models?capability_route=output.text") return response(200, {{ models: ["gpt-4.1"] }});
  if (path === "/api/gateway/discovery/providers/endpoint%3Aopenai/models?capability_route=output.text") return response(200, {{ models: ["gpt-4.1"] }});
  if (path === "/api/gateway/discovery/providers/lmstudio/models?capability_route=input.image%2Coutput.text") return response(200, {{ models: ["qwen/qwen3.6-35b-a3b"] }});
  if (path === "/api/gateway/discovery/providers/lmstudio/models?capability_route=input.video%2Coutput.text") return response(200, {{ models: ["qwen/qwen3.6-35b-a3b"] }});
  if (path === "/api/gateway/discovery/providers/lmstudio/models?capability_route=input.sound%2Coutput.text") return response(200, {{ models: ["qwen3-omni-30b-a3b-captioner"] }});
  if (path === "/api/gateway/discovery/providers/lmstudio/models?capability_route=input.music%2Coutput.text") return response(200, {{ models: ["qwen3-omni-30b-a3b-captioner"] }});
  if (path === "/api/gateway/vision/provider_models?task=text_to_image&providers_only=true") return response(200, {{ available_providers: ["mflux"] }});
  if (path === "/api/gateway/vision/provider_models?task=text_to_image&provider=mflux") return response(200, {{ provider_models: [{{ provider: "mflux", model: "flux-dev" }}] }});
  if (path === "/api/gateway/vision/provider_models?task=image_to_image&providers_only=true") return response(200, {{ available_providers: ["mlx-gen"] }});
  if (path === "/api/gateway/vision/provider_models?task=image_to_image&provider=mlx-gen") return response(200, {{ provider_models: [{{ provider: "mlx-gen", model: "AbstractFramework/qwen-image-edit-2511-4bit" }}] }});
  if (path === "/api/gateway/vision/provider_models?task=image_upscale&providers_only=true") return response(200, {{ available_providers: ["mlx-gen"] }});
  if (path === "/api/gateway/vision/provider_models?task=image_upscale&provider=mlx-gen") return response(200, {{ provider_models: [{{ provider: "mlx-gen", model: "AbstractFramework/seedvr2-3b-8bit" }}] }});
  if (path === "/api/gateway/vision/provider_models?task=text_to_video&providers_only=true") return response(200, {{ available_providers: ["mlx-gen"] }});
  if (path === "/api/gateway/vision/provider_models?task=text_to_video&provider=mlx-gen") return response(200, {{ provider_models: [{{ provider: "mlx-gen", model: "Wan-AI/Wan2.2-TI2V-5B-Diffusers" }}] }});
  if (path === "/api/gateway/vision/provider_models?task=image_to_video&providers_only=true") return response(200, {{ available_providers: ["mlx-gen"] }});
  if (path === "/api/gateway/vision/provider_models?task=image_to_video&provider=mlx-gen") return response(200, {{ provider_models: [{{ provider: "mlx-gen", model: "AbstractFramework/wan2.2-i2v-a14b-diffusers-8bit" }}] }});
  if (path === "/api/gateway/voice/voices?providers_only=true&compact=true") return response(200, {{ tts_providers: ["openai"] }});
  if (path === "/api/gateway/audio/speech/models?provider=openai") return response(200, {{ tts_models_by_provider: {{ openai: ["tts-1"] }} }});
  if (path === "/api/gateway/voice/voices?provider=openai&model=tts-1&compact=true") return response(200, {{
    items: [
      {{ provider: "openai", model: "tts-1", voice_id: "alloy", label: "Alloy" }},
      {{ provider: "openai", model: "tts-1", voice_id: "coral", label: "Coral" }}
    ],
    tts_voices_by_provider: {{ openai: ["alloy", "coral"] }}
  }});
  if (path === "/api/gateway/audio/transcriptions/models?providers_only=true") return response(200, {{ stt_providers: ["whisper"] }});
  if (path === "/api/gateway/audio/transcriptions/models?provider=whisper") return response(200, {{ stt_models_by_provider: {{ whisper: ["whisper-large-v3"] }} }});
  if (path === "/api/gateway/audio/music/providers?task=text_to_audio") return response(200, {{ music_providers: ["stable-audio"] }});
  if (path === "/api/gateway/audio/music/models?task=text_to_audio&provider=stable-audio") return response(200, {{ music_models_by_provider: {{ "stable-audio": ["stabilityai/stable-audio-open-small"] }} }});
  if (path === "/api/gateway/audio/music/providers?task=text_to_music") return response(200, {{ music_providers: ["acemusic"] }});
  if (path === "/api/gateway/audio/music/models?task=text_to_music&provider=acemusic") return response(200, {{ music_models_by_provider: {{ acemusic: ["ace-step"] }} }});
  if (path === "/api/gateway/embeddings/models?providers_only=true") return response(200, {{ embedding_providers: ["lmstudio"] }});
  if (path === "/api/gateway/embeddings/models?provider=lmstudio") return response(200, {{ embedding_models_by_provider: {{ lmstudio: ["bge-small-en-v1.5"] }} }});
  if (path === "/api/gateway/config/capability-defaults") return response(200, {{
    authority: "abstractcore.local",
    writable: true,
    config_file: "/home/u/.abstractcore/config/abstractcore.json",
    routes: [{{ key: "input.text", kind: "input", modality: "text", label: "Text Input", provider: "openai", model: "gpt-4.1", configured: true }}]
  }});
  if (path === "/api/gateway/config/capability-defaults/input/text" && options.method === "PUT") return response(200, {{ routes: [] }});
  if (path === "/api/gateway/config/capability-defaults/output/image/image_to_image" && options.method === "PUT") return response(200, {{ routes: [] }});
  if (path === "/api/gateway/config/capability-defaults/output/voice" && options.method === "PUT") return response(200, {{ routes: [] }});
  if (path === "/api/gateway/attachments/upload" && options.method === "POST") {{
    return response(200, {{ ok: true, artifact: {{ "$artifact": "upload-1", artifact_id: "upload-1", content_type: "image/png", filename: "content.png", modality: "image" }} }});
  }}
  if (path === "/api/gateway/config/provider-endpoint-profiles") return response(200, {{
    authority: "abstractcore.local",
    writable: true,
    config_file: "/home/u/.abstractcore/config/abstractcore.json",
    profiles: [
      {{ id: "openai", display_name: "OpenAI Production", virtual_provider: "endpoint:openai", provider_family: "openai", enabled: true, scope: "gateway" }},
      {{ id: "anthropic", provider_id: "anthropic", display_name: "Anthropic", provider_family: "anthropic", enabled: true, scope: "environment", managed: false, api_key_set: true }}
    ]
  }});
  if (path === "/api/gateway/config/provider-endpoint-profiles/discover-models") {{
    const body = JSON.parse(String(options.body || "{{}}"));
    if (body.base_url !== "https://preview.example.test/v1" || body.api_key !== "preview-key") {{
      return response(400, {{ detail: "bad endpoint discovery payload" }});
    }}
    return response(200, {{ models: ["remote-model"] }});
  }}
  if (path === "/api/gateway/admin/users") return response(200, {{ users: [] }});
  if (path === "/api/gateway/admin/runtime-reservations") return response(200, {{ runtime_reservations: [] }});
  if (path === "/api/gateway/sandbox/generate" && options.method === "POST") {{
    const body = JSON.parse(String(options.body || "{{}}"));
    if (el("sandbox-prompt").value !== "") {{
      return response(400, {{ detail: "sandbox prompt should clear before the request is sent" }});
    }}
    if (body.prompt === "force sandbox failure") {{
      return response(502, {{ detail: "Audio input is not supported by model, and input.voice is not configured." }});
    }}
    if (body.provider !== "lmstudio" || body.model !== "qwen/qwen3.6-35b-a3b" || body.capability !== "input.text") {{
      return response(400, {{ detail: "sandbox text payload did not use the configured default" }});
    }}
    if (!body.client_context || body.client_context.timezone !== "Europe/Paris" || body.client_context.locale !== "fr-FR" || body.client_context.locale_country !== "FR") {{
      return response(400, {{ detail: "sandbox text payload did not include browser grounding context" }});
    }}
    if (!String(body.client_context.local_datetime || "").includes("T")) {{
      return response(400, {{ detail: "sandbox browser grounding context did not include local datetime" }});
    }}
    return response(200, {{ ok: true, response: "**sandbox** ok\\n- markdown", usage: {{ completion_tokens: 4 }} }});
  }}
  if (path === "/api/gateway/runs/session_memory_gateway_console_sandbox_default_admin/images/generate" && options.method === "POST") {{
    const body = JSON.parse(String(options.body || "{{}}"));
    if (body.image_provider !== "mflux" || body.image_model !== "flux-dev") {{
      return response(400, {{ detail: "sandbox image payload did not use the configured default" }});
    }}
    return response(200, {{ ok: true, image_artifact: {{ "$artifact": "img-1", content_type: "image/png" }} }});
  }}
  if (path === "/api/gateway/runs/session_memory_gateway_console_sandbox_default_admin/voice/tts" && options.method === "POST") {{
    const body = JSON.parse(String(options.body || "{{}}"));
    if (body.provider !== "openai" || body.model !== "tts-1") {{
      return response(400, {{ detail: "sandbox voice payload did not use the configured default" }});
    }}
    return response(200, {{ ok: true, audio_artifact: {{ "$artifact": "voice-1", content_type: "audio/wav" }} }});
  }}
  if (path === "/api/gateway/runs/session_memory_gateway_console_sandbox_default_admin/music/generate" && options.method === "POST") {{
    const body = JSON.parse(String(options.body || "{{}}"));
    if (body.task === "text_to_audio") {{
      if (body.music_provider !== "stable-audio" || body.music_model !== "stabilityai/stable-audio-open-small") {{
        return response(400, {{ detail: "sandbox sound payload did not use the configured SFX default" }});
      }}
      return response(200, {{ ok: true, music_artifact: {{ "$artifact": "sound-1", content_type: "audio/wav" }} }});
    }}
    if (body.music_provider !== "acemusic" || body.music_model !== "ace-step" || body.task !== "text_to_music") {{
      return response(400, {{ detail: "sandbox music payload did not use the configured default" }});
    }}
    return response(200, {{ ok: true, music_artifact: {{ "$artifact": "music-1", content_type: "audio/wav" }} }});
  }}
  if (path === "/api/gateway/runs/session_memory_gateway_console_sandbox_default_admin/artifacts/img-1/content") return blobResponse("image/png", "png");
  if (path === "/api/gateway/runs/session_memory_gateway_console_sandbox_default_admin/artifacts/voice-1/content") return blobResponse("audio/wav", "wav");
  if (path === "/api/gateway/runs/session_memory_gateway_console_sandbox_default_admin/artifacts/music-1/content") return blobResponse("audio/wav", "wav");
  if (path === "/api/gateway/runs/session_memory_gateway_console_sandbox_default_admin/artifacts/sound-1/content") return blobResponse("audio/wav", "wav");
  return response(404, {{ detail: path }});
}}
let blobCounter = 0;
const URL = {{
  createObjectURL(blob) {{ blobCounter += 1; return `blob:${{blob.type}}:${{blobCounter}}`; }},
  revokeObjectURL() {{}},
}};
const browserIntl = {{
  DateTimeFormat() {{
    return {{ resolvedOptions() {{ return {{ timeZone: "Europe/Paris" }}; }} }};
  }},
  Locale: class {{
    constructor(value) {{
      this.region = String(value || "").split("-")[1] || "";
    }}
  }},
}};

const context = vm.createContext({{
  document,
  fetch,
	  Headers,
	  FormData: globalThis.FormData,
	  File: globalThis.File,
	  localStorage,
	  URL,
	  Blob,
  Intl: browserIntl,
  navigator: {{ languages: ["fr-FR"], language: "fr-FR" }},
  console,
  // Browser lifetime polls must not keep this completed Node harness alive.
  setTimeout: (fn, ms, ...args) => {{ const timer = setTimeout(fn, ms, ...args); timer.unref(); return timer; }},
  clearTimeout,
  encodeURIComponent,
  decodeURIComponent,
  location: {{ reload() {{}} }},
}});
vm.runInContext(source, context);
await new Promise((resolve) => setTimeout(resolve, 0));

el("login-user").value = "admin";
el("login-token").value = "test-token";
el("login-form").onsubmit({{ preventDefault() {{}} }});
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));

if (!calls.some((call) => call.path === "/api/gateway/session/login" && call.method === "POST")) {{
  throw new Error("login form did not POST to /api/gateway/session/login");
}}
if (!String(body.className || "").includes("signed-in")) {{
  throw new Error("login flow did not render a signed-in console state");
}}
if (String(el("login-message").textContent || "").trim()) {{
  throw new Error("unexpected login error: " + el("login-message").textContent);
}}

// ONE STORE, SAID OUT LOUD. Both core-shared panels must name the file the
// API says they edit, and say that the edit lands in AbstractCore -- an
// operator changing a default here is changing `abstractcore`'s own store.
for (const id of ["defaults-authority", "endpoint-profiles-authority"]) {{
  const note = el(id);
  if (String(note.className || "").split(/\\s+/).includes("hidden")) {{
    throw new Error(id + " stayed hidden even though the payload named a store");
  }}
  if (!String(note.innerHTML || "").includes("/home/u/.abstractcore/config/abstractcore.json")) {{
    throw new Error(id + " did not name the store path: " + note.innerHTML);
  }}
  if (!String(note.innerHTML || "").includes("edits here apply to AbstractCore directly")) {{
    throw new Error(id + " did not say the edit lands in AbstractCore: " + note.innerHTML);
  }}
}}

// HONESTY, BOTH WAYS. A read-only store must not promise a write, and a
// store the Gateway owns must not borrow AbstractCore's sentence at all.
context.renderStoreAuthority("defaults-authority", {{
  authority: "abstractcore.local", writable: false, config_file: "/ro/abstractcore.json"
}});
if (!String(el("defaults-authority").innerHTML || "").includes("read-only from this Gateway")) {{
  throw new Error("a non-writable store did not say read-only: " + el("defaults-authority").innerHTML);
}}
if (!String(el("defaults-authority").className || "").split(/\\s+/).includes("authority-readonly")) {{
  throw new Error("a non-writable store was not styled read-only");
}}
context.renderStoreAuthority("defaults-authority", {{
  authority: "abstractgateway.local", writable: true, config_file: "/gw/provider_endpoint_profiles.json"
}});
if (String(el("defaults-authority").innerHTML || "").trim()) {{
  throw new Error("a Gateway-owned store still claimed AbstractCore authority");
}}
if (!String(el("defaults-authority").className || "").split(/\\s+/).includes("hidden")) {{
  throw new Error("a Gateway-owned store left the authority line visible");
}}
context.renderStoreAuthority("defaults-authority", {{
  authority: "abstractcore.runtime", writable: true, config_file: "/rt/abstractcore.json"
}});
if (String(el("defaults-authority").innerHTML || "").includes("edits here apply to AbstractCore directly")) {{
  throw new Error("a per-runtime overlay was passed off as the shared AbstractCore store");
}}
if (!String(el("defaults-authority").innerHTML || "").includes("/rt/abstractcore.json")) {{
  throw new Error("a per-runtime overlay did not name its own file");
}}

el("endpoint-profile-id").value = "preview";
el("endpoint-provider-family").value = "openai-compatible";
el("endpoint-base-url").value = "https://preview.example.test/v1";
el("endpoint-api-key").value = "preview-key";
el("discover-endpoint-models").onclick();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));

if (!calls.some((call) => call.path === "/api/gateway/config/provider-endpoint-profiles/discover-models" && call.method === "POST")) {{
  throw new Error("discover models did not POST to the endpoint preview route");
}}
if (!el("endpoint-models").children.some((child) => child.value === "remote-model")) {{
  throw new Error("endpoint model picker was not populated from discovery");
}}

el("modal-default-provider").value = "endpoint:openai";
el("modal-default-provider").onchange();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));

if (!calls.some((call) => call.path === "/api/gateway/discovery/providers/endpoint%3Aopenai/models?capability_route=output.text")) {{
  throw new Error("default model discovery did not request models for the selected provider");
}}
if (!el("modal-default-model").children.some((child) => child.value === "gpt-4.1")) {{
  throw new Error("default model picker was not populated from provider discovery");
}}

el("endpoint-id").value = "";
el("endpoint-profile-id").value = "openai";
el("endpoint-name").value = "OpenAI";
el("endpoint-description").value = "OpenAI account connection for GPT and embedding models.";
el("endpoint-provider-family").value = "anthropic";
el("endpoint-provider-family").onchange();
if (el("endpoint-profile-id").value !== "anthropic" || el("endpoint-name").value !== "Anthropic") {{
  throw new Error("provider family change did not reset create-mode provider identity");
}}

async function assertDefaultModal(row, provider, model, providerPath, modelPath, voicePath = "", voice = "") {{
  await context.openDefaultModal(row);
  if (!calls.some((call) => call.path === providerPath)) {{
    throw new Error(`default provider catalog not requested: ${{providerPath}}`);
  }}
  if (el("modal-default-provider").value !== provider) {{
    el("modal-default-provider").value = provider;
    el("modal-default-provider").onchange();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
  }}
  if (!calls.some((call) => call.path === modelPath)) {{
    throw new Error(`default model catalog not requested: ${{modelPath}}`);
  }}
  if (!el("modal-default-model").children.some((child) => child.value === model)) {{
    throw new Error(`default model picker missing ${{model}} for ${{row.key}}`);
  }}
  if (voicePath) {{
    el("modal-default-model").value = model;
    el("modal-default-model").onchange();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
    if (!calls.some((call) => call.path === voicePath)) {{
      throw new Error(`default voice catalog not requested: ${{voicePath}}`);
    }}
    if (!el("modal-default-voice").children.some((child) => child.value === voice)) {{
      throw new Error(`default voice picker missing ${{voice}} for ${{row.key}}`);
    }}
  }}
  context.closeDefaultModal();
}}

await assertDefaultModal(
  {{ key: "input.image", kind: "input", modality: "image", label: "Image Input" }},
  "lmstudio",
  "qwen/qwen3.6-35b-a3b",
  "/api/gateway/discovery/providers",
  "/api/gateway/discovery/providers/lmstudio/models?capability_route=input.image%2Coutput.text"
);
await assertDefaultModal(
  {{ key: "input.video", kind: "input", modality: "video", label: "Video Input" }},
  "lmstudio",
  "qwen/qwen3.6-35b-a3b",
  "/api/gateway/discovery/providers",
  "/api/gateway/discovery/providers/lmstudio/models?capability_route=input.video%2Coutput.text"
);
await assertDefaultModal(
  {{ key: "input.sound", kind: "input", modality: "sound", label: "Sound Input" }},
  "lmstudio",
  "qwen3-omni-30b-a3b-captioner",
  "/api/gateway/discovery/providers",
  "/api/gateway/discovery/providers/lmstudio/models?capability_route=input.sound%2Coutput.text"
);
await assertDefaultModal(
  {{ key: "input.music", kind: "input", modality: "music", label: "Music Input" }},
  "lmstudio",
  "qwen3-omni-30b-a3b-captioner",
  "/api/gateway/discovery/providers",
  "/api/gateway/discovery/providers/lmstudio/models?capability_route=input.music%2Coutput.text"
);
await assertDefaultModal(
  {{ key: "output.image.text_to_image", kind: "output", modality: "image", task: "text_to_image", label: "Image Generation" }},
  "mflux",
  "flux-dev",
  "/api/gateway/vision/provider_models?task=text_to_image&providers_only=true",
  "/api/gateway/vision/provider_models?task=text_to_image&provider=mflux"
);
await assertDefaultModal(
  {{ key: "output.image.image_to_image", kind: "output", modality: "image", task: "image_to_image", label: "Image Edit" }},
  "mlx-gen",
  "AbstractFramework/qwen-image-edit-2511-4bit",
  "/api/gateway/vision/provider_models?task=image_to_image&providers_only=true",
  "/api/gateway/vision/provider_models?task=image_to_image&provider=mlx-gen"
);
await assertDefaultModal(
  {{ key: "output.image.image_upscale", kind: "output", modality: "image", task: "image_upscale", label: "Image Restore / Upscale" }},
  "mlx-gen",
  "AbstractFramework/seedvr2-3b-8bit",
  "/api/gateway/vision/provider_models?task=image_upscale&providers_only=true",
  "/api/gateway/vision/provider_models?task=image_upscale&provider=mlx-gen"
);
await assertDefaultModal(
  {{ key: "output.video.text_to_video", kind: "output", modality: "video", task: "text_to_video", label: "Video Generation" }},
  "mlx-gen",
  "Wan-AI/Wan2.2-TI2V-5B-Diffusers",
  "/api/gateway/vision/provider_models?task=text_to_video&providers_only=true",
  "/api/gateway/vision/provider_models?task=text_to_video&provider=mlx-gen"
);
await assertDefaultModal(
  {{ key: "output.video.image_to_video", kind: "output", modality: "video", task: "image_to_video", label: "Image To Video" }},
  "mlx-gen",
  "AbstractFramework/wan2.2-i2v-a14b-diffusers-8bit",
  "/api/gateway/vision/provider_models?task=image_to_video&providers_only=true",
  "/api/gateway/vision/provider_models?task=image_to_video&provider=mlx-gen"
);
await assertDefaultModal(
  {{ key: "output.voice", kind: "output", modality: "voice", label: "Voice Output" }},
  "openai",
  "tts-1",
  "/api/gateway/voice/voices?providers_only=true&compact=true",
  "/api/gateway/audio/speech/models?provider=openai",
  "/api/gateway/voice/voices?provider=openai&model=tts-1&compact=true",
  "coral"
);
await assertDefaultModal(
  {{ key: "input.voice", kind: "input", modality: "voice", label: "Voice Input" }},
  "whisper",
  "whisper-large-v3",
  "/api/gateway/audio/transcriptions/models?providers_only=true",
  "/api/gateway/audio/transcriptions/models?provider=whisper"
);
await assertDefaultModal(
  {{ key: "output.sound", kind: "output", modality: "sound", label: "Sound Output" }},
  "stable-audio",
  "stabilityai/stable-audio-open-small",
  "/api/gateway/audio/music/providers?task=text_to_audio",
  "/api/gateway/audio/music/models?task=text_to_audio&provider=stable-audio"
);
await assertDefaultModal(
  {{ key: "output.music", kind: "output", modality: "music", label: "Music Output" }},
  "acemusic",
  "ace-step",
  "/api/gateway/audio/music/providers?task=text_to_music",
  "/api/gateway/audio/music/models?task=text_to_music&provider=acemusic"
);
await assertDefaultModal(
  {{ key: "embedding.text", kind: "embedding", modality: "text", label: "Text Embeddings" }},
  "lmstudio",
  "bge-small-en-v1.5",
  "/api/gateway/embeddings/models?providers_only=true",
  "/api/gateway/embeddings/models?provider=lmstudio"
);

await context.openDefaultModal({{
  key: "input.text",
  kind: "input",
  modality: "text",
  label: "Text Input",
  provider: "endpoint:openai",
  model: "gpt-4.1",
  base_url: "https://models.example.test/v1",
  options: {{ temperature: 0.3 }},
}});
el("save-default").onclick();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
const saveCall = calls.find((call) => call.path === "/api/gateway/config/capability-defaults/input/text" && call.method === "PUT");
if (!saveCall) throw new Error("save default did not call the capability default route");
const saveBody = JSON.parse(saveCall.body);
// The save sends what this modal controls, and of that only what the operator
// EDITED. base_url and options gained controls on 2026-08-02, but they are
// prefilled from the row the grid last rendered and the modal never re-reads
// it -- so naming an untouched one lets a minutes-old render overwrite a value
// set through `abstractcore config` in between. Nothing was touched here.
if ("base_url" in saveBody) {{
  throw new Error("save default echoed back a base_url the operator never edited");
}}
if ("options" in saveBody) {{
  throw new Error("save default echoed back options the operator never edited");
}}
if (!saveBody.provider || !saveBody.model || typeof saveBody.reasoning !== "string") {{
  throw new Error("save default must send provider, model and the text route's reasoning");
}}

// ...and the other half of the rule: an EDIT travels, including an emptying
// edit, which is the only way an operator can clear an override. (A save
// closes the modal, so the row is opened again first -- which is also what
// re-arms the prefill the edit is measured against.)
await context.openDefaultModal({{
  key: "input.text",
  kind: "input",
  modality: "text",
  label: "Text Input",
  provider: "endpoint:openai",
  model: "gpt-4.1",
  base_url: "https://models.example.test/v1",
  options: {{ temperature: 0.3 }},
}});
el("modal-default-base-url").value = "http://localhost:1234/v1";
el("modal-default-options").value = "";
el("save-default").onclick();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
const editedSave = JSON.parse(calls.filter((call) => call.path === "/api/gateway/config/capability-defaults/input/text" && call.method === "PUT").pop().body);
if (editedSave.base_url !== "http://localhost:1234/v1") {{
  throw new Error("an edited base_url must travel: " + JSON.stringify(editedSave));
}}
if (JSON.stringify(editedSave.options) !== "{{}}") {{
  throw new Error("emptying the options box must clear the stored dict: " + JSON.stringify(editedSave));
}}

await context.openDefaultModal({{
  key: "output.image.image_to_image",
  kind: "output",
  modality: "image",
  task: "image_to_image",
  label: "Image Edit",
  provider: "mlx-gen",
  model: "AbstractFramework/qwen-image-edit-2511-4bit",
}});
el("save-default").onclick();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
const imageEditSaveCall = calls.find((call) => call.path === "/api/gateway/config/capability-defaults/output/image/image_to_image" && call.method === "PUT");
if (!imageEditSaveCall) throw new Error("save image edit default did not call the task-specific capability default route");
const imageEditSaveBody = JSON.parse(imageEditSaveCall.body);
if (imageEditSaveBody.provider !== "mlx-gen" || imageEditSaveBody.model !== "AbstractFramework/qwen-image-edit-2511-4bit") {{
  throw new Error("save image edit default did not preserve provider/model");
}}

await context.openDefaultModal({{
  key: "output.voice",
  kind: "output",
  modality: "voice",
  label: "Voice Output",
  provider: "openai",
  model: "tts-1",
  options: {{ voice: "alloy", quality_preset: "standard" }},
}});
if (el("modal-default-voice").value !== "alloy") {{
  throw new Error("voice default modal did not restore the configured voice");
}}
el("modal-default-voice").value = "coral";
el("save-default").onclick();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
const voiceSaveCall = calls.find((call) => call.path === "/api/gateway/config/capability-defaults/output/voice" && call.method === "PUT");
if (!voiceSaveCall) throw new Error("save voice default did not call the capability default route");
const voiceSaveBody = JSON.parse(voiceSaveCall.body);
if (voiceSaveBody.options.voice !== "coral" || voiceSaveBody.options.quality_preset !== "standard") {{
  throw new Error("save voice default did not persist the selected voice in options");
}}

await context.renderDefaults({{
  routes: [
	    {{ key: "input.text", kind: "input", modality: "text", label: "Text Input", provider: "lmstudio", model: "qwen/qwen3.6-35b-a3b", configured: true }},
	    {{ key: "input.image", kind: "input", modality: "image", label: "Image Input", provider: "openai", model: "gpt-4o", configured: true }},
	    {{ key: "input.video", kind: "input", modality: "video", label: "Video Input", configured: false }},
	    {{ key: "input.sound", kind: "input", modality: "sound", label: "Sound Input", configured: false }},
	    {{ key: "input.music", kind: "input", modality: "music", label: "Music Input", configured: false }},
    {{ key: "output.text", kind: "output", modality: "text", label: "Text Output", configured: false, derived_from: "input.text", read_only: true }},
    {{ key: "output.image", kind: "output", modality: "image", label: "Image Output", provider: "mflux", model: "flux-dev", configured: true, task_keys: ["output.image.text_to_image", "output.image.image_to_image", "output.image.image_upscale"] }},
    {{ key: "output.image.text_to_image", kind: "output", modality: "image", task: "text_to_image", label: "Image Generation", provider: "mflux", model: "flux-dev", configured: true, broad_key: "output.image" }},
    {{ key: "output.image.image_to_image", kind: "output", modality: "image", task: "image_to_image", label: "Image Edit", provider: "mlx-gen", model: "AbstractFramework/qwen-image-edit-2511-4bit", configured: true, broad_key: "output.image" }},
    {{ key: "output.image.image_upscale", kind: "output", modality: "image", task: "image_upscale", label: "Image Restore / Upscale", configured: false, source: "not_configured", broad_key: "output.image", inherits_broad: true }},
    {{ key: "output.video", kind: "output", modality: "video", label: "Video Output", configured: false, source: "not_configured", covered_by_tasks: true, task_keys: ["output.video.text_to_video", "output.video.image_to_video"] }},
    {{ key: "output.video.text_to_video", kind: "output", modality: "video", task: "text_to_video", label: "Video Generation", provider: "mlx-gen", model: "Wan-AI/Wan2.2-TI2V-5B-Diffusers", configured: true, broad_key: "output.video" }},
    {{ key: "output.video.image_to_video", kind: "output", modality: "video", task: "image_to_video", label: "Image To Video", provider: "mlx-gen", model: "AbstractFramework/wan2.2-i2v-a14b-diffusers-8bit", configured: true, broad_key: "output.video" }},
    {{ key: "output.voice", kind: "output", modality: "voice", label: "Voice Output", provider: "openai", model: "tts-1", configured: true, options: {{ voice: "coral" }} }},
    {{ key: "output.music", kind: "output", modality: "music", label: "Music Output", provider: "acemusic", model: "ace-step", configured: true }},
    {{ key: "output.sound", kind: "output", modality: "sound", label: "Sound Output", provider: "stable-audio", model: "stabilityai/stable-audio-open-small", configured: true }},
    {{ key: "input.scene3d", kind: "input", modality: "scene3d", label: "3D Scene Input", provider: "abstract3d", model: "scene", configured: true }},
    {{ key: "output.scene3d", kind: "output", modality: "scene3d", label: "3D Scene Output", provider: "abstract3d", model: "scene", configured: true }},
  ]
}});
// REVERSED (operator 2026-08-02): scene3d was hidden "for now" because the
// modal could not configure it -- there is no scene3d discovery endpoint. With
// the free-text provider and model lanes, "nothing to discover" is a supported
// state rather than a dead end, so the row is shown and savable. Hiding it let
// the store hold a scene3d route (the TUI writes all 24) that this grid could
// neither show nor clear.
if (!el("defaults-table").children.some((child) => String(child.innerHTML || "").includes("scene3d"))) {{
  throw new Error("scene3d defaults must be visible in the Gateway Console");
}}
// THE ROUTE HIERARCHY, NOT HIDDEN ROWS (operator question 2026-08-01:
// "why do we have output.image and output.video? are those remnants?").
// This grid used to HIDE both parents, which meant the one surface that
// can WRITE the store could not set the row Gateway and Core both READ
// (`_resolved_vision_backend`, `_vision_route_defaults`) — and it is the
// row a fresh install seeds. They are shown as parents now: grouped,
// labeled for what they serve, and benign when the task rows cover them.
const defaultRowsHtml = el("defaults-table").children.map((child) => String(child.innerHTML || ""));
const imageParentIdx = defaultRowsHtml.findIndex((html) => html.includes("<code>output.image</code>"));
if (imageParentIdx < 0) {{
  throw new Error("the output.image parent row must be shown — it is what a fresh install seeds and what advertising reads");
}}
if (!defaultRowsHtml[imageParentIdx].includes("any image task (fallback)")) {{
  throw new Error("the parent row must say what it is for, or it reads as a duplicate key");
}}
for (const [offset, task] of [".text_to_image", ".image_to_image", ".image_upscale"].entries()) {{
  const html = defaultRowsHtml[imageParentIdx + 1 + offset] || "";
  if (!html.includes("capability-route-task") || !html.includes("<code>" + task + "</code>")) {{
    throw new Error("task row " + task + " must render indented immediately under its output.image parent");
  }}
}}
// THE MIRROR, and the shape a fresh install has: the seed writes
// `output.image` alone, so a task row with no value of its own is
// ANSWERED by the parent and must not be painted as a gap.
const upscaleHtml = defaultRowsHtml[imageParentIdx + 3] || "";
if (!upscaleHtml.includes("inherited ← output.image")) {{
  throw new Error("a task row answered by its configured parent must say so, not claim to be unconfigured");
}}
if (upscaleHtml.includes(">not configured<")) {{
  throw new Error("an inherited task row must not also claim to be unconfigured");
}}
const videoParentIdx = defaultRowsHtml.findIndex((html) => html.includes("<code>output.video</code>"));
if (videoParentIdx < 0) {{
  throw new Error("the output.video parent row must be shown");
}}
// An UNSET parent whose task rows are all set is benign, never a red
// "not configured" — nothing can reach it in that state.
if (!defaultRowsHtml[videoParentIdx].includes("not needed")) {{
  throw new Error("an unset parent covered by its task rows must read 'not needed', not 'not configured'");
}}
if (defaultRowsHtml[videoParentIdx].includes(">not configured<")) {{
  throw new Error("a covered parent must not also claim to be unconfigured");
}}
// ...and it stays SETTABLE: one value for every video task is the simple
// path, so the row must not be dressed read-only the way a derived or
// covered-by-input.text row is.
const videoParentNode = el("defaults-table").children[videoParentIdx];
if (String(videoParentNode.className || "").includes("capability-derived")) {{
  throw new Error("a covered parent must stay editable — setting it is the one-value-for-every-task path");
}}
if (!el("defaults-table").children.some((child) => String(child.innerHTML || "").includes("derived"))) {{
  throw new Error("output.text should render as derived from input.text (one vocabulary across every console: 'derived <- input.text' / 'covered by input.text', matching both TUIs)");
}}
if (!el("defaults-table").children.some((child) => String(child.innerHTML || "").includes("covered"))) {{
  throw new Error("input.image should render as covered by a vision-capable input.text model");
}}
el("sandbox-capability").value = "output.text";
el("sandbox-prompt").value = "hello";
await context.runSandbox();
await new Promise((resolve) => setTimeout(resolve, 0));
if (!calls.some((call) => call.path === "/api/gateway/sandbox/generate" && call.method === "POST")) {{
  throw new Error("sandbox text test did not call the configured-default smoke route");
}}
function treeHas(node, predicate) {{
  if (predicate(node)) return true;
  for (const child of node.children || []) {{
    if (treeHas(child, predicate)) return true;
  }}
  return false;
}}
function treeFind(node, predicate) {{
  if (predicate(node)) return node;
  for (const child of node.children || []) {{
    const found = treeFind(child, predicate);
    if (found) return found;
  }}
  return null;
}}
if (!treeHas(el("sandbox-transcript"), (node) => String(node.className || "").includes("sandbox-speak"))) {{
  throw new Error("sandbox assistant text should expose a speaker action when voice is configured");
}}
if (!treeHas(el("sandbox-transcript"), (node) => String(node.innerHTML || "").includes("<strong>sandbox</strong>") && String(node.innerHTML || "").includes("<li>markdown</li>"))) {{
  throw new Error("sandbox assistant text should render markdown");
}}
const speaker = treeFind(el("sandbox-transcript"), (node) => String(node.className || "").includes("sandbox-speak"));
const beforeSpeakMessages = el("sandbox-transcript").children.length;
speaker.onclick();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
if (el("sandbox-transcript").children.length !== beforeSpeakMessages) {{
  throw new Error("speaker action should play speech in-place instead of appending a new chat message");
}}
if (!treeHas(el("sandbox-transcript"), (node) => node.id === "audio" && String(node.src || "").startsWith("blob:audio/wav"))) {{
  throw new Error("speaker action should attach a blob-backed audio element in-place");
}}
if (el("sandbox-prompt").value !== "") {{
  throw new Error("sandbox prompt should be empty after submit");
}}
calls.length = 0;
el("sandbox-capability").value = "output.text";
el("sandbox-prompt").value = "force sandbox failure";
await context.runSandbox();
await new Promise((resolve) => setTimeout(resolve, 0));
const failedBubble = treeFind(el("sandbox-transcript"), (node) => String(node.className || "").includes("sandbox-message error"));
if (!failedBubble || !treeHas(failedBubble, (node) => String(node.textContent || node.innerHTML || "").includes("input.voice is not configured"))) {{
  throw new Error("failed sandbox request should turn the pending response into an error bubble");
}}
if (treeHas(el("sandbox-transcript"), (node) => String(node.className || "").includes("sandbox-progress") && !String(node.className || "").includes("hidden"))) {{
  throw new Error("failed sandbox request should not leave an active progress bar visible");
}}
if (typeof File !== "undefined" && typeof FormData !== "undefined") {{
  calls.length = 0;
  await context.handleSandboxFiles([new File(["png"], "content.png", {{ type: "image/png" }})]);
  if (!el("sandbox-attachments").children.length) {{
    throw new Error("uploaded sandbox attachment was not rendered as a chip");
  }}
  el("sandbox-capability").value = "output.text";
  el("sandbox-prompt").value = "describe this picture";
  await context.runSandbox();
  await new Promise((resolve) => setTimeout(resolve, 0));
  const mediaSandboxCall = calls.find((call) => call.path === "/api/gateway/sandbox/generate" && call.method === "POST");
  if (!mediaSandboxCall) throw new Error("sandbox text media test did not call the smoke route");
  const mediaSandboxBody = JSON.parse(mediaSandboxCall.body);
  if (!Array.isArray(mediaSandboxBody.attachments) || mediaSandboxBody.attachments.length !== 1) {{
    throw new Error("sandbox text media test did not include uploaded attachments");
  }}
  if (mediaSandboxBody.attachments[0]["$artifact"] !== "upload-1" || mediaSandboxBody.attachments[0].content_type !== "image/png") {{
    throw new Error("sandbox text media attachment payload was not the uploaded image artifact");
  }}
  if (!mediaSandboxBody.client_context || mediaSandboxBody.client_context.timezone !== "Europe/Paris" || mediaSandboxBody.client_context.locale !== "fr-FR" || mediaSandboxBody.client_context.locale_country !== "FR") {{
    throw new Error("sandbox text media payload did not include browser grounding context");
  }}
  if (el("sandbox-prompt").value !== "" || el("sandbox-attachments").children.length !== 0) {{
    throw new Error("sandbox composer and attachment chips should clear immediately after media submit");
  }}
}}
calls.length = 0;
el("sandbox-capability").value = "output.text";
el("sandbox-prompt").value = "enter send";
let preventedEnter = false;
el("sandbox-prompt").onkeydown({{ key: "Enter", shiftKey: false, preventDefault() {{ preventedEnter = true; }} }});
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
if (!preventedEnter || !calls.some((call) => call.path === "/api/gateway/sandbox/generate" && call.method === "POST")) {{
  throw new Error("Enter should send the sandbox message");
}}
calls.length = 0;
el("sandbox-prompt").value = "line one";
el("sandbox-prompt").onkeydown({{ key: "Enter", shiftKey: true, preventDefault() {{ throw new Error("Shift+Enter should not be prevented"); }} }});
await new Promise((resolve) => setTimeout(resolve, 0));
if (calls.some((call) => call.path === "/api/gateway/sandbox/generate")) {{
  throw new Error("Shift+Enter should leave message editing in place");
}}
el("sandbox-capability").value = "output.image.text_to_image";
el("sandbox-prompt").value = "a tiny joyful monkey";
await context.runSandbox();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
if (!calls.some((call) => call.path === "/api/gateway/runs/session_memory_gateway_console_sandbox_default_admin/images/generate" && call.method === "POST")) {{
  throw new Error("sandbox image test did not use a session-memory run id");
}}
if (!treeHas(el("sandbox-transcript"), (node) => node.id === "img" && String(node.src || "").startsWith("blob:image/png"))) {{
  throw new Error("sandbox image artifact should render inline from a blob URL");
}}
el("sandbox-capability").value = "output.voice";
el("sandbox-prompt").value = "hello voice";
await context.runSandbox();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
if (!treeHas(el("sandbox-transcript"), (node) => node.id === "audio" && String(node.src || "").startsWith("blob:audio/wav"))) {{
  throw new Error("sandbox voice artifact should render as a blob-backed inline audio player");
}}
el("sandbox-capability").value = "output.music";
el("sandbox-prompt").value = "calm jazz";
await context.runSandbox();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
if (!treeHas(el("sandbox-transcript"), (node) => node.id === "audio" && String(node.src || "").startsWith("blob:audio/wav"))) {{
  throw new Error("sandbox music artifact should render as a blob-backed inline audio player");
}}
el("sandbox-capability").value = "output.sound";
el("sandbox-prompt").value = "scifi laser";
await context.runSandbox();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));
const soundCall = calls.find((call) => call.path === "/api/gateway/runs/session_memory_gateway_console_sandbox_default_admin/music/generate" && String(call.body || "").includes("scifi laser"));
if (!soundCall) {{
  throw new Error("sandbox SFX test did not call the generated-audio route");
}}
const soundBody = JSON.parse(soundCall.body);
if (soundBody.task !== "text_to_audio") {{
  throw new Error("sandbox SFX test must request text_to_audio, got " + soundBody.task);
}}
if (!treeHas(el("sandbox-transcript"), (node) => node.id === "audio" && String(node.src || "").startsWith("blob:audio/wav"))) {{
  throw new Error("sandbox SFX artifact should render as a blob-backed inline audio player");
}}
"""
    with tempfile.TemporaryDirectory() as tmpdir:
        harness_path = Path(tmpdir) / "gateway-console-inline-test.mjs"
        harness_path.write_text(harness, encoding="utf-8")
        result = subprocess.run(["node", str(harness_path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_console_surfaces_core_store_authority_on_shared_panels() -> None:
    """The web console must obey the same passthrough principle as both TUIs:
    a panel that edits AbstractCore-owned data has to NAME the store it writes
    to. Without this the operator edits AbstractCore's file believing it is a
    Gateway-local setting — and has no way to know which file `abstractcore
    config` disagrees with."""
    html = gateway_console_html()

    # Both core-shared panels carry a persistent authority line...
    for anchor in ("defaults-authority", "endpoint-profiles-authority"):
        assert anchor in html, f"missing authority line anchor: {anchor}"
        assert f'renderStoreAuthority("{anchor}"' in html, f"{anchor} is never rendered"

    # ...and the claim is driven by the API payload, never guessed by the page.
    assert "payload?.config_file" in html
    assert 'authority.startsWith("abstractcore")' in html
    assert "edits here apply to AbstractCore directly" in html
    # A non-writable store must not claim edits apply.
    assert "read-only from this Gateway" in html


def test_console_authority_line_requires_evidence() -> None:
    """No claim without evidence: a payload that does not name an AbstractCore
    store must leave the line empty, so gateway-owned panels can never inherit
    a sentence about a file they do not touch."""
    html = gateway_console_html()
    start = html.index("function renderStoreAuthority")
    body = html[start:start + 2200]
    assert 'el.innerHTML = "";' in body
    assert "classList.add(\"hidden\")" in body
    # A per-runtime overlay is an AbstractCore file, but NOT the shared one.
    assert "abstractcore.runtime" in body


def test_models_meter_and_residency_primitives_offline() -> None:
    """The Models tab's render primitives, on the SHIPPED source (the offline
    module's slicing idiom): the shared determinate-bar recipe steps
    ok -> warn (>=75%) -> crit (>=90%) and renders a null fraction as an
    EMPTY track with honest text; residency is TRI-STATE — null is a
    rendered 'unknown', visually distinct from both yes and no."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    harness = f"""
{_slice_function(source, "meterRow")}
{_slice_function(source, "residencyPill")}
{_slice_function(source, "_fmtCtx")}
class Element {{
  constructor() {{ this.children = []; this.className = ""; this.textContent = ""; this.style = {{}}; this.title = ""; }}
  append(...items) {{ this.children.push(...items); }}
  get classList() {{
    const self = this;
    return {{ add(name) {{ self.className = (self.className + " " + name).trim(); }} }};
  }}
}}
const document = {{ createElement: () => new Element(), createTextNode: (t) => ({{ text: t }}) }};
const find = (el, cls) => {{
  if (String(el.className || "").split(" ").includes(cls)) return el;
  for (const child of el.children || []) {{ const hit = find(child, cls); if (hit) return hit; }}
  return null;
}};
const results = [];
for (const [frac, text] of [[0.5, "half"], [0.8, "warm"], [0.97, "hot"], [null, "unknown"]]) {{
  const row = meterRow("RAM", frac, text, "t");
  const fill = find(row, "meter-fill");
  results.push({{ kind: "meter", frac, width: fill.style.width, cls: fill.className, value: find(row, "meter-value").textContent }});
}}
for (const resident of [true, false, null]) {{
  const pill = residencyPill({{ resident }});
  results.push({{ kind: "pill", resident, cls: pill.className, text: pill.textContent }});
}}
results.push({{ kind: "ctx", short: _fmtCtx(900), k: _fmtCtx(32768), none: _fmtCtx(null) }});
console.log(JSON.stringify(results));
"""
    rows = _node(harness)
    meters = {r["frac"]: r for r in rows if r["kind"] == "meter"}
    assert meters[0.5]["width"] == "50%" and "warn" not in meters[0.5]["cls"] and "crit" not in meters[0.5]["cls"]
    assert "warn" in meters[0.8]["cls"] and "crit" not in meters[0.8]["cls"]
    assert "crit" in meters[0.97]["cls"]
    assert meters[None]["width"] == "0%", "an unknown fraction must render an empty track"
    assert meters[None]["value"] == "unknown"
    pills = {r["resident"]: r for r in rows if r["kind"] == "pill"}
    assert pills[True]["text"] == "resident" and "ok" in pills[True]["cls"]
    # `false` names the truth in words: configured/cached, NOT in memory.
    assert pills[False]["text"] == "configured — not in memory" and "ok" not in pills[False]["cls"]
    assert pills[None]["text"] == "unknown" and "covered" in pills[None]["cls"], (
        "null residency must render as its own third state"
    )
    assert len({p["text"] for p in pills.values()}) == 3, "the three residency states must be distinct"
    ctx = next(r for r in rows if r["kind"] == "ctx")
    assert ctx == {"kind": "ctx", "short": "900", "k": "32K", "none": ""}


def test_models_table_defaults_to_resident_rows_with_configured_cached_toggle() -> None:
    """Loaded-vs-default truth on the SHIPPED renderModelsTable: the table
    defaults to provider-verified RESIDENT rows only; configured/cached rows
    (resident false or unknown) appear only behind the 'Show configured /
    cached (N)' toggle, carry no Unload/Lock buttons (Estimate stays), and
    the section title counts RESIDENT rows — a capability-default model with
    nothing in memory must never present as loaded/unloadable."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    harness = f"""
{_slice_function(source, "renderModelsTable")}
{_slice_function(source, "renderModelsResidentCount")}
{_slice_function(source, "renderModelsShowCachedToggle")}
{_slice_function(source, "residencyPill")}
{_slice_function(source, "modelsEmptyRow")}
{_slice_function(source, "modelRowKey")}
{_slice_function(source, "modelDisplaySize")}
{_slice_function(source, "modelCacheBytes")}
class El {{
  constructor(tag) {{ this.tag = tag || ""; this.children = []; this.className = ""; this._tc = ""; this.style = {{}}; this.title = ""; this.innerHTML = ""; }}
  get textContent() {{ return this._tc; }}
  set textContent(v) {{ this._tc = String(v || ""); if (!this._tc) this.children = []; }}
  append(...items) {{ this.children.push(...items); }}
  get classList() {{
    const self = this;
    return {{
      add(name) {{ self.className = (self.className + " " + name).trim(); }},
      toggle(name, force) {{
        const parts = String(self.className || "").split(" ").filter(Boolean).filter((p) => p !== name);
        if (force) parts.push(name);
        self.className = parts.join(" ");
      }},
    }};
  }}
}}
const document = {{ createElement: (tag) => new El(tag), createTextNode: (t) => ({{ tag: "#text", text: t, children: [] }}) }};
const els = {{}};
for (const id of ["models-table", "models-loaded-title", "models-show-cached-label", "models-show-cached-text", "models-show-cached"]) els[id] = new El(id);
function $(id) {{ return els[id] || new El(id); }}
const ICONS = {{ lock: "<svg/>" }};
const state = {{ principal: {{ admin: true }}, modelEstimates: new Map(), modelsShowCached: false, modalityUi: null }};
function modalityChipEl() {{ return new El("chip"); }}
function _fmtBytes(v) {{ return String(v); }}
function _fmtCtx(v) {{ return v == null ? "" : String(v); }}
function estimateDetailRow() {{ return new El("est"); }}
function estimateModelContext() {{}}
function toggleModelLock() {{}}
function unloadModel() {{}}
const buttons = (el, out = []) => {{
  if (el.tag === "button") out.push(el.textContent);
  for (const child of el.children || []) buttons(child, out);
  return out;
}};
const pillText = (el) => {{
  if (String(el.className || "").includes("state-pill") && el.tag === "span" && ["resident", "configured — not in memory", "unknown"].includes(el.textContent)) return el.textContent;
  for (const child of el.children || []) {{ const hit = pillText(child); if (hit) return hit; }}
  return null;
}};
const snap = () => ({{
  title: els["models-loaded-title"].textContent,
  toggleHidden: els["models-show-cached-label"].className.includes("hidden"),
  toggleText: els["models-show-cached-text"].textContent,
  rows: els["models-table"].children.map((tr) => ({{ pill: pillText(tr), buttons: buttons(tr), empty: tr.children.length === 1 && tr.children[0].className === "empty" ? tr.children[0].textContent : null }})),
}});
const data = {{ models: [
  {{ runtime_id: "r1", provider: "lmstudio", model: "really-loaded", resident: true, locked: true, lockable: true, default: false, pinned: true, size_bytes: 1000, task: "text_generation" }},
  {{ runtime_id: "r2", provider: "lmstudio", model: "qwen/qwen3.6-35b-a3b", resident: false, default: true, pinned: false, lockable: true, task: "text_generation" }},
  {{ runtime_id: "r3", provider: "ollama", model: "mystery", resident: null, task: "text_generation" }},
] }};
const results = [];
renderModelsTable(data);
results.push({{ step: "default-view", ...snap() }});
state.modelsShowCached = true;
renderModelsTable(data);
results.push({{ step: "toggled", ...snap() }});
state.modelsShowCached = false;
renderModelsTable({{ models: [ {{ provider: "p", model: "a", resident: false }}, {{ provider: "p", model: "b", resident: null }} ] }});
results.push({{ step: "none-resident", ...snap() }});
state.modelsShowCached = true;
renderModelsTable({{ models: [ {{ provider: "lmstudio", model: "locked-evicted", resident: false, locked: true, lockable: true }} ] }});
results.push({{ step: "locked-evicted", ...snap() }});
console.log(JSON.stringify(results));
"""
    steps = {r["step"]: r for r in _node(harness)}
    default_view = steps["default-view"]
    assert default_view["title"] == "Models (1 resident)", "the header counts RESIDENT rows only"
    assert default_view["toggleHidden"] is False
    assert default_view["toggleText"] == "Show configured / cached (2)"
    assert len(default_view["rows"]) == 1, "default view is resident rows ONLY"
    assert default_view["rows"][0]["pill"] == "resident"
    assert "Unload" in default_view["rows"][0]["buttons"] and "Unlock" in default_view["rows"][0]["buttons"]

    toggled = steps["toggled"]
    assert toggled["title"] == "Models (1 resident)", "the toggle must not inflate the resident count"
    assert len(toggled["rows"]) == 3
    by_pill = {row["pill"]: row for row in toggled["rows"]}
    # The operator-defect row: a capability DEFAULT with nothing in memory —
    # named in words, Estimate only, no Unload/Lock.
    assert by_pill["configured — not in memory"]["buttons"] == ["Estimate"]
    assert by_pill["unknown"]["buttons"] == ["Estimate"]
    assert "Unload" in by_pill["resident"]["buttons"]

    none_resident = steps["none-resident"]
    assert none_resident["title"] == "Models (0 resident)"
    assert len(none_resident["rows"]) == 1 and none_resident["rows"][0]["empty"]
    assert "No models resident in memory right now" in none_resident["rows"][0]["empty"]
    assert "2 configured / cached rows behind the toggle" in none_resident["rows"][0]["empty"]

    # A LOCKED-but-evicted pair must never be stranded: the lock still blocks
    # facade unloads, so Unlock renders despite resident:false — while Lock
    # and Unload stay absent (nothing is in memory to lock or unload).
    locked_evicted = steps["locked-evicted"]
    assert len(locked_evicted["rows"]) == 1
    assert locked_evicted["rows"][0]["buttons"] == ["Estimate", "Unlock"]


def test_models_unload_409_offers_force_and_sends_force_true() -> None:
    """The locked-unload contract, end to end on the SHIPPED unloadModel():
    a 409 whose BODY carries model_locked opens a SECOND deliberate confirm
    and only then re-sends with force:true; a 409 WITHOUT the code gets a
    generic conflict message and never offers a force it cannot mean;
    declining the first confirm sends nothing."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    harness = f"""
{_slice_function(source, "unloadModel")}
{_slice_function(source, "modelUnloadTarget")}
{_slice_function(source, "_modelsMutationResult")}
{_slice_function(source, "_modelsLockedRefusal")}
const els = new Map();
function $(id) {{
  if (!els.has(id)) els.set(id, {{ textContent: "", className: "", disabled: false }});
  return els.get(id);
}}
const confirms = [];
let confirmAnswers = [];
async function confirmAction(opts) {{ confirms.push(opts.title); return confirmAnswers.shift(); }}
const calls = [];
// mode: which refusal the first (non-force) unload answers with. The
// message is a bare "HTTP 409" so the BODY code is what the gate reads.
let mode = "locked";
async function api(path, options = {{}}) {{
  const body = JSON.parse(options.body || "{{}}");
  calls.push({{ path, body }});
  if (path === "/api/gateway/models/unload" && !body.force) {{
    const err = new Error("HTTP 409");
    err.status = 409;
    err.data = mode === "locked"
      ? {{ ok: false, error: "model_locked", locked: true }}
      : {{ ok: false, error: "runtime_busy" }};
    throw err;
  }}
  return {{ ok: true }};
}}
async function loadHostState() {{}}
const results = [];
confirmAnswers = [true, true];
await unloadModel({{ runtime_id: "rt1", provider: "mlx", model: "qwen" }}, null);
results.push({{ step: "locked", confirms: [...confirms], calls: [...calls], message: $("models-loaded-message").textContent }});
confirms.length = 0;
calls.length = 0;
confirmAnswers = [false];
await unloadModel({{ provider: "mlx", model: "other" }}, null);
results.push({{ step: "declined", confirms: [...confirms], calls: [...calls] }});
confirms.length = 0;
calls.length = 0;
mode = "busy";
confirmAnswers = [true, true];
await unloadModel({{ runtime_id: "rt2", provider: "mlx", model: "busy" }}, null);
results.push({{ step: "other409", confirms: [...confirms], calls: [...calls], message: $("models-loaded-message").textContent }});
console.log(JSON.stringify(results));
"""
    steps = {r["step"]: r for r in _node(harness)}
    locked = steps["locked"]
    assert locked["confirms"] == ["Unload model", "Model locked"], "the locked 409 must open a second, explicit confirm"
    assert [c["body"] for c in locked["calls"]] == [{"runtime_id": "rt1"}, {"runtime_id": "rt1", "force": True}], (
        "force:true must ride only the second call, addressed by runtime_id"
    )
    assert locked["message"].startswith("Force-unloaded")
    declined = steps["declined"]
    assert declined["confirms"] == ["Unload model"]
    assert declined["calls"] == [], "a declined confirm must send nothing"
    other = steps["other409"]
    assert other["confirms"] == ["Unload model"], "a non-locked 409 must not open the force confirm"
    assert len(other["calls"]) == 1 and "force" not in other["calls"][0]["body"], (
        "a non-locked 409 must never re-send with force"
    )
    assert other["message"].startswith("Unload conflicted (HTTP 409)")


def test_models_cache_clear_failure_message_survives_the_refresh() -> None:
    """A refused cache clear lands in the caches section's OWN message line
    and the finally-refresh (real loadHostState, success path) must not wipe
    it — the exact zero-feedback defect: the clear's error went to
    #models-message, which the refresh then unconditionally cleared."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    harness = f"""
{_slice_function(source, "clearSessionCache")}
{_slice_function(source, "_modelsMutationResult")}
{_slice_function(source, "loadHostState")}
const els = new Map();
function $(id) {{
  if (!els.has(id)) els.set(id, {{
    textContent: "", className: "", disabled: false,
    classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
  }});
  return els.get(id);
}}
const state = {{ principal: {{ admin: true }}, hostStateSeq: 0 }};
function tableLoadingRow() {{}}
function modelsEmptyRow() {{}}
async function ensureModalityUi() {{}}
const rendered = [];
async function loadGatewayHost() {{}}
function renderHostState(data) {{ rendered.push(data); }}
async function confirmAction() {{ return true; }}
async function api(path, options = {{}}) {{
  if (path.includes("/prompt_cache/clear_all")) throw new Error("cache clear refused");
  return {{ ok: true, models: [], session_caches: [], totals: {{}} }};
}}
await clearSessionCache("sess1", null);
console.log(JSON.stringify([{{
  cachesMsg: $("models-caches-message").textContent,
  cachesCls: $("models-caches-message").className,
  mainMsg: $("models-message").textContent,
  refreshed: rendered.length,
}}]));
"""
    row = _node(harness)[0]
    assert row["refreshed"] == 1, "the finally-refresh must still run"
    assert "cache clear refused" in row["cachesMsg"], "the clear's error must survive the refresh"
    assert "error" in row["cachesCls"]
    assert row["mainMsg"] == "", "the snapshot message line is the refresh's to clear"


def test_models_host_facts_never_fabricate_zeros() -> None:
    """Degraded sections must not read '0 models'/'0 caches' beside a table
    that says unavailable: the fact line renders only when the section
    enumerated (array non-null). The models line counts RESIDENT rows —
    totals.models_resident when the server sent it, else counted from the
    enumerated rows themselves (row-derived, never fabricated) — and names
    the non-resident remainder 'configured / cached', never 'loaded'."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    harness = f"""
{_slice_function(source, "renderHostFacts")}
{_slice_function(source, "_fmtBytes")}
class El {{
  constructor() {{ this.children = []; this.className = ""; this._tc = ""; }}
  get textContent() {{ return this._tc; }}
  set textContent(v) {{ this._tc = String(v || ""); if (!this._tc) this.children = []; }}
  append(...items) {{ this.children.push(...items); }}
}}
const facts = new El();
function $(id) {{ return id === "models-host-facts" ? facts : new El(); }}
const document = {{ createElement: () => new El() }};
const lines = () => Object.fromEntries(facts.children.map((line) => [line.children[0].textContent, line.children[1].textContent]));
const results = [];
renderHostFacts({{ models: null, session_caches: [{{}}], totals: {{ models: 3, models_resident: 3, model_bytes: 5, session_caches: 1, session_cache_bytes: 4096 }}, memory: {{ process: {{ rss_bytes: 100 }} }} }});
results.push({{ step: "models-degraded", lines: lines() }});
renderHostFacts({{ models: [{{ resident: false }}], session_caches: null, totals: {{}} }});
results.push({{ step: "totals-absent", lines: lines() }});
renderHostFacts({{ models: [{{ resident: true, size_bytes: 1024 }}, {{ resident: false, size_bytes: 4096 }}], session_caches: [], totals: {{ models: 2, models_resident: 1, model_bytes: 5120, session_caches: 0, session_cache_bytes: null }} }});
results.push({{ step: "healthy", lines: lines() }});
console.log(JSON.stringify(results));
"""
    steps = {r["step"]: r["lines"] for r in _node(harness)}
    degraded = steps["models-degraded"]
    assert "Models" not in degraded, "null models must not render a fabricated model count"
    assert degraded["Session caches"] == "1 cache · 4.0 KiB", "the healthy section still renders"
    # Process RSS is stated EXACTLY ONCE, and this is not the place: it is the
    # breakdown's `process_rss` item, where it carries the label that makes it
    # readable ("includes memory-mapped GGUF weights"). Rendering it here too
    # put the same 76 GB in one panel twice under two different framings.
    assert "Process RSS" not in degraded
    absent = steps["totals-absent"]
    # The rows ARE enumerated: the resident count derives from them (a
    # truthful zero, not a fabrication) and the non-resident row is named
    # configured/cached — never presented as loaded.
    assert absent["Models"] == "0 resident · 1 configured / cached"
    assert "Session caches" not in absent, "an absent totals block must not render cache zero counts"
    healthy = steps["healthy"]
    # RESIDENT count + resident-only bytes; the false row is configured/cached.
    assert healthy["Models"] == "1 resident · 1.0 KiB · 1 configured / cached"
    assert healthy["Session caches"] == "0 caches", "a REAL zero still renders"


def test_models_stale_host_snapshot_never_overwrites_a_fresh_one() -> None:
    """Overlapping snapshots land in REQUEST order, not resolution order
    (the shipped loadHostState's sequence guard): a hung slow-lane fetch
    resolving after a newer snapshot must paint NOTHING — not resurrect an
    unloaded model on screen."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    harness = f"""
{_slice_function(source, "loadHostState")}
const state = {{ principal: {{ admin: true }}, hostStateSeq: 0 }};
function $() {{ return {{ textContent: "", className: "", classList: {{ add() {{}}, remove() {{}} }} }}; }}
function tableLoadingRow() {{}}
function modelsEmptyRow() {{}}
async function ensureModalityUi() {{}}
const painted = [];
async function loadGatewayHost() {{}}
function renderHostState(data) {{ painted.push(data.marker); }}
const pending = [];
async function api() {{ return new Promise((resolve) => pending.push(resolve)); }}
const p1 = loadHostState({{ quiet: true }});
const p2 = loadHostState({{ quiet: true }});
await new Promise((resolve) => setTimeout(resolve, 0));
if (pending.length !== 2) throw new Error("expected two in-flight snapshots, got " + pending.length);
pending[1]({{ marker: "fresh" }});
await p2;
pending[0]({{ marker: "stale" }});
await p1;
console.log(JSON.stringify([{{ painted, kept: state.hostState && state.hostState.marker }}]));
"""
    row = _node(harness)[0]
    assert row["painted"] == ["fresh"], "the stale resolution must paint nothing"
    assert row["kept"] == "fresh", "state.hostState must keep the fresh snapshot"


def test_models_load_ok_but_lock_fail_reports_the_mixed_outcome() -> None:
    """Warm-up with 'lock in memory': a successful load followed by a failed
    lock is a MIXED outcome — the message must say the model IS resident
    (unlocked) AND why locking failed, never read as total failure."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    harness = f"""
{_slice_function(source, "loadModelResidency")}
{_slice_function(source, "_modelsMutationResult")}
{_slice_function(source, "activeModelsLoadProvider")}
{_slice_function(source, "activeModelsLoadModel")}
{_slice_function(source, "customLaneValue")}
const els = new Map();
function $(id) {{
  if (!els.has(id)) els.set(id, {{ textContent: "", className: "hidden", disabled: false, checked: false, value: "", classList: {{ contains: (c) => c === "hidden" }} }});
  return els.get(id);
}}
$("models-load-provider").value = "mlx";
$("models-load-model").value = "qwen";
$("models-load-lock").checked = true;
async function loadHostState() {{}}
const calls = [];
async function api(path, options = {{}}) {{
  calls.push(path);
  if (path === "/api/gateway/models/lock") throw new Error("lock not supported by this runtime");
  return {{ ok: true }};
}}
await loadModelResidency();
console.log(JSON.stringify([{{ msg: $("models-loaded-message").textContent, cls: $("models-loaded-message").className, calls }}]));
"""
    row = _node(harness)[0]
    assert "/api/gateway/models/load" in row["calls"] and "/api/gateway/models/lock" in row["calls"]
    assert row["msg"].startswith("Loaded mlx/qwen"), "the message must lead with the load that SUCCEEDED"
    assert "UNLOCKED" in row["msg"] and "locking failed" in row["msg"]
    assert "lock not supported by this runtime" in row["msg"], "the lock failure's reason must survive"
    assert "error" in row["cls"], "a mixed outcome still needs the operator's attention"


def test_models_accelerator_meter_scopes_itself_and_carries_the_gguf_note() -> None:
    """PART A of the canonical accelerator spec. `device.allocated_bytes` is
    PROCESS-LOCAL: on this Mac it reads 0 while a 93 GB GGUF is resident in
    another process, which is how the meter came to say "Device · metal 0 B"
    beside an accelerator with 105 GB in use. `device.host_in_use_bytes` is a
    genuine ACCELERATOR counter (driver-allocated Metal buffers across every
    process) and `device.wired_limit_bytes` the real ceiling — both win
    whenever known.

    But that counter is NOT the machine's memory use: it is blind to
    memory-mapped GGUF weights (llama.cpp mmaps the file and wraps the pages
    with newBufferWithBytesNoCopy, so they never become driver-allocated).
    Measured live here: an 89,986,353,824 B fully-offloaded GGUF with
    host_in_use_bytes at 1,042,120,704. So the line is labelled as an
    ACCELERATOR HEAP line, its scope is named in exactly two phrasings — "all
    processes" / "this process only" — and the GGUF caveat rides the title on
    BOTH variants. RAM stays the primary system meter, rendered first."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    harness = f"""
{_slice_function(source, "deviceMeterView")}
{_slice_function(source, "_fmtBytes")}
const results = [];
// The LIVE payload measured on the operator's Mac (2026-08-27).
results.push({{ step: "live", ...deviceMeterView({{ backend: "metal", allocated_bytes: 0, host_in_use_bytes: 105743990784, wired_limit_bytes: 115343360000, total_bytes: 137438953472 }}) }});
// No cross-process figure: the process-local number renders, LABELED as such.
results.push({{ step: "process-only", ...deviceMeterView({{ backend: "cuda", allocated_bytes: 500, total_bytes: 1000 }}) }});
// Cross-process figure, no wired limit: the device total is the ceiling.
results.push({{ step: "no-wired", ...deviceMeterView({{ backend: "metal", allocated_bytes: 0, host_in_use_bytes: 600, total_bytes: 1000 }}) }});
// An unknown/empty backend still gets a name: the literal `device`.
results.push({{ step: "no-backend", ...deviceMeterView({{ allocated_bytes: 0, host_in_use_bytes: 600, total_bytes: 1000 }}) }});
// Nothing known: an EMPTY track and honest text, never a guessed bar.
results.push({{ step: "unknown", ...deviceMeterView({{ backend: "metal" }}) }});
results.push({{ step: "junk", ...deviceMeterView(null) }});
console.log(JSON.stringify(results));
"""
    steps = {r["step"]: r for r in _node(harness)}

    live = steps["live"]
    assert live["scope"] == "all processes"
    assert live["used"] == 105743990784, "the cross-process figure must win over the process-local 0"
    assert live["ceiling"] == 115343360000, "the wired limit is the ceiling, not the 137 GB device total"
    assert live["label"] == "Accelerator heap · metal (all processes)", "the EXACT PART A2 label"
    assert live["fraction"] is not None and 0.9 < live["fraction"] < 0.93
    assert live["value"].startswith("98.5 GiB"), live["value"]
    assert live["value"] != "0 B", "a 0 B bar beside a full accelerator is the bug this fixes"
    assert "host_in_use_bytes" in live["title"] and "wired limit" in live["title"]

    proc = steps["process-only"]
    assert proc["scope"] == "this process only"
    assert proc["label"] == "Accelerator heap · cuda (this process only)", (
        "a process-local figure must SAY it is process-local"
    )
    assert proc["fraction"] == 0.5
    assert "THIS PROCESS ONLY" in proc["title"]

    no_wired = steps["no-wired"]
    assert no_wired["scope"] == "all processes" and no_wired["ceiling"] == 1000
    assert "device total" in no_wired["title"]

    assert steps["no-backend"]["label"] == "Accelerator heap · device (all processes)", (
        "an unknown backend falls back to the literal `device`, never to a bare label"
    )

    for key in ("unknown", "junk"):
        assert steps[key]["fraction"] is None, f"{key} must render an EMPTY track"
        assert steps[key]["value"] == "unknown", f"{key} must never fabricate a number"

    # The note is EXACT and rides EVERY variant — it is what stops the figure
    # being read as "how full is this machine".
    note = "memory-mapped GGUF weights are not counted here"
    for key, view in steps.items():
        assert view["note"] == note, key
        assert view["title"].startswith(note), f"{key}: the caveat must lead the tooltip"
        assert view["label"].startswith("Accelerator heap · "), key
        # PART A3: "host" is GONE as a scope name for this figure.
        assert "(host)" not in view["label"] and "host-wide" not in view["label"], key
        assert "host-wide" not in view["title"], key
        assert view["scope"] in ("all processes", "this process only"), key


def test_models_display_size_coalesces_and_names_its_source() -> None:
    """The shared display-size rule: first KNOWN of size_bytes ->
    size_vram_bytes -> est_weights_bytes. An MLX/HF row carrying only
    est_weights_bytes used to render a BLANK size cell; it now renders the
    estimate — and the tooltip says it is an ESTIMATE, so it can never be read
    as a measurement. cache_bytes rides along as a secondary figure."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    harness = f"""
{_slice_function(source, "modelDisplaySize")}
{_slice_function(source, "modelCacheBytes")}
const results = [];
for (const [step, row] of [
  ["reported", {{ size_bytes: 3, size_vram_bytes: 2, est_weights_bytes: 1 }}],
  ["vram", {{ size_vram_bytes: 2, est_weights_bytes: 1 }}],
  ["estimated", {{ est_weights_bytes: 1 }}],
  ["nothing", {{}}],
  ["zero", {{ size_bytes: 0 }}],
]) results.push({{ step, ...modelDisplaySize(row) }});
results.push({{ step: "cache", bytes: modelCacheBytes({{ cache_bytes: 42 }}), none: modelCacheBytes({{}}), junk: modelCacheBytes({{ cache_bytes: "big" }}) }});
console.log(JSON.stringify(results));
"""
    steps = {r["step"]: r for r in _node(harness)}
    assert steps["reported"]["bytes"] == 3 and steps["reported"]["source"] == "size_bytes"
    assert steps["vram"]["bytes"] == 2 and steps["vram"]["source"] == "size_vram_bytes"
    est = steps["estimated"]
    assert est["bytes"] == 1 and est["source"] == "est_weights_bytes"
    assert "ESTIMATED" in est["label"], "an estimate must announce itself as one"
    assert steps["nothing"]["bytes"] is None and steps["nothing"]["label"] == "size unknown"
    assert steps["zero"]["bytes"] == 0, "a real reported zero is a fact, not an absence"
    cache = steps["cache"]
    assert cache["bytes"] == 42 and cache["none"] is None and cache["junk"] is None


# PART C SHARED FIXTURE — the live payload measured on this machine
# (2026-08-27), verbatim. Every residency surface parses THIS one and must
# agree on the keys, the order and the byte values.
_HOST_STATE_FIXTURE = {
    "ok": True,
    "memory": {
        "ram": {
            "total_bytes": 137438953472,
            "available_bytes": 96368312320,
            "used_bytes": 33741111296,
            "percent": 29.9,
        },
        "process": {"rss_bytes": 76762775552},
        "device": {
            "backend": "metal",
            "allocated_bytes": 0,
            "total_bytes": 137438953472,
            "free_bytes": None,
            "host_in_use_bytes": 1042120704,
            "wired_limit_bytes": 115343360000,
        },
    },
    "models": [
        {
            "runtime_id": "local:text_generation:huggingface:unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q3_K_XL",
            "task": "text_generation",
            "provider": "huggingface",
            "model": "unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q3_K_XL",
            "source": "provider_server",
            "resident": True,
            "state": "provider_loaded",
            "locked": False,
            "lockable": True,
            "est_weights_bytes": 89986353824,
            "cache_bytes": 2147483648,
            "details": {"est_weights_bytes": 89986353824, "cache_bytes": 2147483648},
        }
    ],
    "totals": {
        "models": 1,
        "models_resident": 1,
        "model_bytes": 89986353824,
        "session_caches": 3,
        "session_cache_bytes": 4352519172,
    },
}

_GGUF_NOTE = (
    "Σ model weights exceeds the accelerator heap. That is the normal case for "
    "memory-mapped GGUF weights: llama.cpp maps them from disk, so they are resident "
    "as process RSS and are not counted in the accelerator heap."
)


def test_models_memory_breakdown_pins_the_shared_fixture() -> None:
    """PART B/C of the canonical spec, on the SHIPPED memoryBreakdown().

    The old `unattributed` remainder subtracted RAM-dimensioned quantities
    (model weights, process RSS) from an ACCELERATOR counter
    (host_in_use_bytes). On this fixture it computes 1,042,120,704 −
    170,748,732,196 = −169.7 GB, clamped to "0 B" and blamed "overlap" for
    what was a category error. It is GONE, with no replacement remainder.

    What replaces it is three kinds of line, in one order, on every residency
    surface: ITEMS (facts the framework knows, each labelled with what it
    measures), then REFERENCE counters (Σ weights / RAM / accelerator heap —
    separate measurements, NOT summable with the items), then the GGUF NOTE
    when Σ weights exceeds the heap, which is the normal mmapped-GGUF case and
    not an inconsistency."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    harness = f"""
{_slice_function(source, "memoryBreakdown")}
{_slice_function(source, "deviceMeterView")}
{_slice_function(source, "modelDisplaySize")}
{_slice_function(source, "modelCacheBytes")}
{_slice_function(source, "_fmtBytes")}
const results = [];
results.push({{ step: "fixture", ...memoryBreakdown({json.dumps(_HOST_STATE_FIXTURE)}) }});
// EMISSION RULE: a KNOWN value is emitted even when it is 0; an UNKNOWN one is
// omitted. A resident row with no known size is SKIPPED — never an invented 0.
results.push({{ step: "known-zeros", ...memoryBreakdown({{
  memory: {{ process: {{ rss_bytes: 0 }}, device: {{ backend: "cuda", allocated_bytes: 0, total_bytes: 1000 }} }},
  models: [{{ runtime_id: "a", model: "sized", resident: true, size_bytes: 0, cache_bytes: 0 }},
           {{ runtime_id: "b", model: "sizeless", resident: true }}],
  totals: {{ session_cache_bytes: 0 }},
}}) }});
// Nothing known at all: no items, no references, no note — never a zero line.
results.push({{ step: "degraded", ...memoryBreakdown({{ memory: {{}}, models: null }}) }});
results.push({{ step: "junk", ...memoryBreakdown(null) }});
// The note fires on the COMPARISON, not on the file format: weights under the
// heap means no note.
results.push({{ step: "weights-under-heap", ...memoryBreakdown({{
  memory: {{ device: {{ backend: "metal", host_in_use_bytes: 900, total_bytes: 1000 }} }},
  models: [{{ runtime_id: "a", model: "m", resident: true, size_bytes: 100 }}],
}}) }});
// Source phrases ride the field that actually supplied the number.
results.push({{ step: "sources", ...memoryBreakdown({{
  memory: {{}},
  models: [{{ runtime_id: "a", model: "reported", resident: true, size_bytes: 3 }},
           {{ runtime_id: "b", model: "vram", resident: true, size_vram_bytes: 2 }},
           {{ runtime_id: "c", model: "guess", resident: true, est_weights_bytes: 1 }}],
}}) }});
console.log(JSON.stringify(results));
"""
    steps = {r["step"]: r for r in _node(harness)}
    fixture = steps["fixture"]

    # 1. Item keys, in order.
    assert [i["key"] for i in fixture["items"]] == [
        "model:local:text_generation:huggingface:unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q3_K_XL",
        "model_caches",
        "session_caches",
        "process_rss",
    ]
    # 2. Their byte values, in order.
    assert [i["bytes"] for i in fixture["items"]] == [89986353824, 2147483648, 4352519172, 76762775552]
    # 3. Reference keys, in order.
    assert [r["key"] for r in fixture["references"]] == ["sum_model_weights", "ram", "accelerator"]
    refs = {r["key"]: r for r in fixture["references"]}
    # 4. Σ model weights is the sum of the model items.
    assert refs["sum_model_weights"]["bytes"] == 89986353824
    # 5. The accelerator reference IS the PART A2 line, caveat and all.
    assert "Accelerator heap · metal (all processes)" in refs["accelerator"]["name"]
    assert refs["accelerator"]["detail"] == "memory-mapped GGUF weights are not counted here"
    assert refs["accelerator"]["bytes"] == 1042120704
    # 6. The note is present (89986353824 > 1042120704) and EXACT.
    assert fixture["note"] is not None, "Σ weights exceeds the heap here — the explanation must render"
    assert fixture["note"]["text"] == _GGUF_NOTE

    every_line = fixture["items"] + fixture["references"] + [fixture["note"]]
    blob = json.dumps(every_line, ensure_ascii=False)
    # 7. The remainder is GONE — key, name, note and all.
    assert "nattributed" not in blob
    # 8. And "host-wide" is gone as a scope name.
    assert "host-wide" not in blob
    for line in every_line:
        for field in ("name", "detail", "text"):
            assert "host-wide" not in str(line.get(field) or "")

    # The names and details are the spec's, not paraphrases of them.
    names = {i["key"]: i["name"] for i in fixture["items"]}
    assert names["model:local:text_generation:huggingface:unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q3_K_XL"] == (
        "unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q3_K_XL"
    )
    assert names["model_caches"] == "model KV caches"
    assert names["session_caches"] == "session caches"
    assert names["process_rss"] == "gateway process RSS"
    details = {i["key"]: i["detail"] for i in fixture["items"]}
    assert details["model:local:text_generation:huggingface:unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q3_K_XL"] == (
        "resident model weights · estimated on-disk weight size (est_weights_bytes)"
    )
    assert details["model_caches"] == "prompt-cache bytes held for resident models"
    assert details["session_caches"] == "prompt-cache bytes held by gateway sessions"
    assert details["process_rss"] == (
        "resident set size of the gateway process — includes memory-mapped GGUF weights"
    )
    assert refs["sum_model_weights"]["name"] == "Σ model weights"
    assert refs["sum_model_weights"]["detail"] == "sum of the resident model weights above"
    assert refs["ram"]["name"] == "RAM used"
    assert refs["ram"]["detail"] == "system memory in use / installed"
    assert " / " in refs["ram"]["value"], "RAM renders used / installed"

    # THE EMISSION RULE, one rule everywhere: known 0 IS a line; unknown is not.
    zeros = steps["known-zeros"]
    assert [i["key"] for i in zeros["items"]] == ["model:a", "model_caches", "session_caches", "process_rss"]
    assert [i["bytes"] for i in zeros["items"]] == [0, 0, 0, 0], "a known 0 is a fact, not an absence"
    assert "model:b" not in [i["key"] for i in zeros["items"]], (
        "a resident row with no known size is SKIPPED, never rendered as 0"
    )
    # The process-local accelerator figure still earns its reference line.
    assert [r["key"] for r in zeros["references"]] == ["sum_model_weights", "accelerator"], (
        "RAM is absent from this payload, so its reference line is too"
    )
    assert zeros["note"] is None, "0 does not exceed 0"

    for key in ("degraded", "junk"):
        assert steps[key]["items"] == [], f"{key} must not invent 0-byte lines"
        assert steps[key]["references"] == [], key
        assert steps[key]["note"] is None, key

    assert steps["weights-under-heap"]["note"] is None, (
        "the note explains an EXCESS; without one it must not appear"
    )

    sources = [i["detail"] for i in steps["sources"]["items"] if i["key"].startswith("model:")]
    assert sources == [
        "resident model weights · reported by the model server (size_bytes)",
        "resident model weights · reported by the model server (size_vram_bytes)",
        "resident model weights · estimated on-disk weight size (est_weights_bytes)",
    ]
    estimated = [i["estimated"] for i in steps["sources"]["items"] if i["key"].startswith("model:")]
    assert estimated == [False, False, True], "only an est_weights_bytes figure is marked an estimate"


def test_console_source_carries_the_canonical_wording_and_none_of_the_old() -> None:
    """Belt and braces on the SERVED page, not just the sliced functions: the
    spec's exact strings ship, and the two banned words ship nowhere — not in a
    label, not in a tooltip, not in a comment that could be copied back into
    one."""
    html = gateway_console_html()
    for exact in (
        "Accelerator heap · ${backend || \"device\"} (${scope})",
        "memory-mapped GGUF weights are not counted here",
        "all processes",
        "this process only",
        "resident model weights · ",
        "reported by the model server (size_bytes)",
        "reported by the model server (size_vram_bytes)",
        "estimated on-disk weight size (est_weights_bytes)",
        "prompt-cache bytes held for resident models",
        "prompt-cache bytes held by gateway sessions",
        "resident set size of the gateway process — includes memory-mapped GGUF weights",
        "Σ model weights",
        "sum of the resident model weights above",
        "RAM used",
        "system memory in use / installed",
        _GGUF_NOTE,
    ):
        assert exact in html, exact
    # The remainder and the misleading scope name are gone from the whole page.
    assert "nattributed" not in html
    assert "host-wide" not in html and "Host-wide" not in html


def test_models_process_rss_is_stated_exactly_once() -> None:
    """Cross-surface rule (abstractflow found the same duplication in its own
    panel): the gateway's process RSS is stated ONCE, as the breakdown's
    `process_rss` item — the one rendering that says what the number means
    ("includes memory-mapped GGUF weights"). The host-facts line used to print
    it as well, so a single memory panel carried the same 76 GB twice under two
    different framings, which is exactly the double-counting this wave removes.
    Host id / host name stay: identity is not a duplicate figure."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    harness = f"""
{_slice_function(source, "renderHostFacts")}
{_slice_function(source, "renderHostBreakdown")}
{_slice_function(source, "renderHostMeters")}
{_slice_function(source, "meterRow")}
{_slice_function(source, "memoryBreakdown")}
{_slice_function(source, "deviceMeterView")}
{_slice_function(source, "modelDisplaySize")}
{_slice_function(source, "modelCacheBytes")}
{_slice_function(source, "_fmtBytes")}
{_slice_function(source, "_fmtPct")}
class El {{
  constructor() {{ this.children = []; this.className = ""; this._tc = ""; this.title = ""; this.style = {{}}; }}
  get textContent() {{ return this._tc; }}
  set textContent(v) {{ this._tc = String(v || ""); if (!this._tc) this.children = []; }}
  append(...items) {{ this.children.push(...items); }}
  get classList() {{
    const self = this;
    return {{
      add(name) {{ self.className = (self.className + " " + name).trim(); }},
      remove(name) {{ self.className = String(self.className).split(" ").filter((p) => p && p !== name).join(" "); }},
      toggle(name, force) {{
        const parts = String(self.className || "").split(" ").filter(Boolean).filter((p) => p !== name);
        if (force) parts.push(name);
        self.className = parts.join(" ");
      }},
    }};
  }}
}}
const document = {{ createElement: () => new El() }};
const boxes = {{ "models-host-facts": new El(), "models-breakdown": new El(), "models-meters": new El() }};
function $(id) {{ return boxes[id] || new El(); }}
const text = (el) => el.textContent + (el.children || []).map(text).join("");
const data = {json.dumps(_HOST_STATE_FIXTURE)};
renderHostFacts(data);
renderHostMeters(data);
renderHostBreakdown(data);
console.log(JSON.stringify([{{
  facts: boxes["models-host-facts"].children.map(text),
  meters: boxes["models-meters"].children.map((r) => text(r) + " | " + (r.children[1] || {{}}).title),
  breakdown: boxes["models-breakdown"].children.map(text),
}}]));
"""
    (view,) = _node(harness)
    rss = "71.5 GiB"  # 76762775552 bytes, as the console's _fmtBytes renders it
    stated = sorted({where for where, lines in view.items() for line in lines if rss in line})
    assert stated == ["breakdown"], f"process RSS must be stated exactly once, found in: {stated}"
    rss_lines = [line for line in view["breakdown"] if rss in line]
    assert len(rss_lines) == 1
    assert rss_lines[0].startswith("gateway process RSS")
    assert "includes memory-mapped GGUF weights" in rss_lines[0]
    # Identity survives — it was never the duplicate.
    assert not any("Process RSS" in line for line in view["facts"])


def test_models_breakdown_item_key_falls_back_to_provider_and_model() -> None:
    """Real sweep rows arrive with `runtime_id: null`. The shared item-key rule
    for all four surfaces: `model:<runtime_id>` when the host sent one, else
    `model:<provider>:<model>` — no index suffix, no task segment, no leading
    empty segment. Colliding keys are KEPT; a genuine duplicate provider+model
    row is itself worth seeing."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    harness = f"""
{_slice_function(source, "memoryBreakdown")}
{_slice_function(source, "deviceMeterView")}
{_slice_function(source, "modelDisplaySize")}
{_slice_function(source, "modelCacheBytes")}
{_slice_function(source, "_fmtBytes")}
const view = memoryBreakdown({{
  memory: {{}},
  models: [
    {{ runtime_id: null, provider: "lmstudio", model: "qwen/qwen3-vl-4b", resident: true, size_bytes: 10 }},
    {{ runtime_id: "", provider: "lmstudio", model: "qwen/qwen3-vl-4b", resident: true, size_bytes: 10 }},
    {{ runtime_id: "rt-1", provider: "mlx", model: "named", resident: true, size_bytes: 10 }},
  ],
}});
console.log(JSON.stringify([view.items.map((i) => i.key)]));
"""
    (keys,) = _node(harness)
    assert keys == [
        "model:lmstudio:qwen/qwen3-vl-4b",
        # An EMPTY runtime_id is not a runtime_id — and the collision is kept.
        "model:lmstudio:qwen/qwen3-vl-4b",
        "model:rt-1",
    ]


def test_models_breakdown_separates_references_from_items() -> None:
    """A reader must never add the reference counters onto the items, so the
    renderer puts a RULE between the two groups and dims the references. The
    GGUF note closes the block on its own line."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    harness = f"""
{_slice_function(source, "renderHostBreakdown")}
{_slice_function(source, "memoryBreakdown")}
{_slice_function(source, "deviceMeterView")}
{_slice_function(source, "modelDisplaySize")}
{_slice_function(source, "modelCacheBytes")}
{_slice_function(source, "_fmtBytes")}
class El {{
  constructor() {{ this.children = []; this.className = ""; this._tc = ""; this.title = ""; }}
  get textContent() {{ return this._tc; }}
  set textContent(v) {{ this._tc = String(v || ""); if (!this._tc) this.children = []; }}
  append(...items) {{ this.children.push(...items); }}
  get classList() {{
    const self = this;
    return {{
      add(name) {{ self.className = (self.className + " " + name).trim(); }},
      remove(name) {{ self.className = String(self.className).split(" ").filter((p) => p && p !== name).join(" "); }},
    }};
  }}
}}
const document = {{ createElement: () => new El() }};
const box = new El();
function $(id) {{ return id === "models-breakdown" ? box : null; }}
const text = (el) => el.textContent + (el.children || []).map(text).join("");
renderHostBreakdown({json.dumps(_HOST_STATE_FIXTURE)});
console.log(JSON.stringify([{{
  hidden: box.className.includes("hidden"),
  rows: box.children.map((row) => ({{ cls: row.className, text: text(row) }})),
}}]));
"""
    (view,) = _node(harness)
    assert view["hidden"] is False
    classes = [r["cls"] for r in view["rows"]]
    assert classes == [
        "mem-breakdown-head",
        "mem-breakdown-row",  # the model
        "mem-breakdown-row",  # model KV caches
        "mem-breakdown-row",  # session caches
        "mem-breakdown-row",  # gateway process RSS
        "mem-breakdown-rule",  # THE SEPARATOR — items above, references below
        "mem-breakdown-row reference",
        "mem-breakdown-row reference",
        "mem-breakdown-row reference",
        "mem-breakdown-note-line",
    ], classes
    rows = view["rows"]
    assert rows[1]["text"].startswith("unsloth/Qwen3.8-Flash-Next-GGUF:UD-Q3_K_XL")
    # PART D2's marker rides the breakdown too: this weight is an ESTIMATE.
    assert "~" in rows[1]["text"], "an estimated weight must be marked as one on screen"
    assert "Accelerator heap · metal (all processes)" in rows[8]["text"]
    assert rows[9]["text"] == _GGUF_NOTE, "the note is quoted whole, never truncated or reworded"
    assert "nattributed" not in json.dumps(view, ensure_ascii=False)


def test_models_every_resident_row_offers_a_lock_including_swept_ones() -> None:
    """Operator: 'i should have a lock on each line to lock/unlock a model.'
    Lock now ADOPTS an externally loaded (LM Studio / ollama) resident model,
    so a sweep-resident row whose `lockable` the host never reported (null)
    offers Lock too — only an EXPLICIT lockable:false withholds it. A locked
    row always offers Unlock, resident or not (a locked-but-evicted lock still
    blocks facade unloads); a non-resident configured row keeps Estimate
    only.

    PART D1: the ADOPT WORDING is keyed on `source === "provider_server"`, not
    on `lockable`. The residency sweep stamps every row it finds
    `lockable: true`, so the old `row.lockable === true` test could never
    separate a gateway-loaded model from an adopted one and the adopt sentence
    never fired. The GATE is untouched.

    PART D2: an `est_weights_bytes` figure renders with the SAME `~` prefix the
    TUIs use, ON SCREEN — a marker that lives only in a tooltip is a marker
    nobody sees. The tooltip still names the source field."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    harness = f"""
{_slice_function(source, "renderModelsTable")}
{_slice_function(source, "renderModelsResidentCount")}
{_slice_function(source, "renderModelsShowCachedToggle")}
{_slice_function(source, "residencyPill")}
{_slice_function(source, "modelsEmptyRow")}
{_slice_function(source, "modelRowKey")}
{_slice_function(source, "modelDisplaySize")}
{_slice_function(source, "modelCacheBytes")}
class El {{
  constructor(tag) {{ this.tag = tag || ""; this.children = []; this.className = ""; this._tc = ""; this.style = {{}}; this.title = ""; this.innerHTML = ""; }}
  get textContent() {{ return this._tc; }}
  set textContent(v) {{ this._tc = String(v || ""); if (!this._tc) this.children = []; }}
  append(...items) {{ this.children.push(...items); }}
  get classList() {{
    const self = this;
    return {{
      add(name) {{ self.className = (self.className + " " + name).trim(); }},
      toggle(name, force) {{
        const parts = String(self.className || "").split(" ").filter(Boolean).filter((p) => p !== name);
        if (force) parts.push(name);
        self.className = parts.join(" ");
      }},
    }};
  }}
}}
const document = {{ createElement: (tag) => new El(tag), createTextNode: (t) => ({{ tag: "#text", text: t, children: [] }}) }};
const els = {{}};
for (const id of ["models-table", "models-loaded-title", "models-show-cached-label", "models-show-cached-text", "models-show-cached"]) els[id] = new El(id);
function $(id) {{ return els[id] || new El(id); }}
const ICONS = {{ lock: "<svg/>" }};
const state = {{ principal: {{ admin: true }}, modelEstimates: new Map(), modelsShowCached: true, modalityUi: null }};
function modalityChipEl() {{ return new El("chip"); }}
function _fmtBytes(v) {{ return String(v); }}
function _fmtCtx(v) {{ return v == null ? "" : String(v); }}
function estimateDetailRow() {{ return new El("est"); }}
function estimateModelContext() {{}}
function toggleModelLock() {{}}
function unloadModel() {{}}
const walk = (el, out = []) => {{
  if (el.tag === "button") out.push({{ text: el.textContent, title: el.title }});
  for (const child of el.children || []) walk(child, out);
  return out;
}};
const sizeCell = (tr) => {{ const td = tr.children[4]; return {{ text: td.textContent, title: td.title }}; }};
const models = [
  {{ provider: "lmstudio", model: "swept", resident: true, size_bytes: 1000 }},
  {{ provider: "mlx", model: "managed", resident: true, lockable: true, est_weights_bytes: 700, cache_bytes: 40 }},
  {{ provider: "ollama", model: "refused", resident: true, lockable: false }},
  {{ provider: "mlx", model: "locked-swept", resident: true, locked: true }},
  {{ provider: "mlx", model: "locked-evicted", resident: false, locked: true }},
  {{ provider: "mlx", model: "configured", resident: false, lockable: true }},
  // D1: the sweep stamps BOTH of these lockable:true. Only `source` separates
  // the adopted model from the one this gateway loaded itself.
  {{ provider: "lmstudio", model: "adopt-me", resident: true, source: "provider_server", lockable: true, size_bytes: 5 }},
  {{ provider: "mlx", model: "gateway-loaded", resident: true, source: "gateway", lockable: true, size_bytes: 5 }},
  // The live wire really does serve lockable:false on a provider_server row
  // (an LM Studio one) beside lockable:true on another: the GATE still wins.
  {{ provider: "lmstudio", model: "refused-adopt", resident: true, source: "provider_server", lockable: false, size_bytes: 5 }},
  // D2: an estimate with no cache beside it.
  {{ provider: "mlx", model: "est-only", resident: true, lockable: true, est_weights_bytes: 900 }},
];
// The table renders RESIDENT rows first, then the configured / cached ones.
const visible = models.filter((r) => r.resident === true).concat(models.filter((r) => r.resident !== true));
const rows = {{}};
renderModelsTable({{ models }});
els["models-table"].children.forEach((tr, i) => {{
  const found = walk(tr);
  rows[visible[i].model] = {{
    buttons: found.map((b) => b.text),
    lockTitle: (found.find((b) => b.text === "Lock" || b.text === "Unlock") || {{}}).title || "",
    size: sizeCell(tr),
  }};
}});
console.log(JSON.stringify([rows]));
"""
    (rows,) = _node(harness)
    # A sweep-resident row (lockable UNREPORTED) now offers Lock: locking
    # adopts the externally loaded model.
    assert rows["swept"]["buttons"] == ["Estimate", "Lock", "Unload"]
    assert rows["managed"]["buttons"] == ["Estimate", "Lock", "Unload"]
    # Only an explicit refusal withholds the control.
    assert rows["refused"]["buttons"] == ["Estimate", "Unload"]
    assert rows["locked-swept"]["buttons"] == ["Estimate", "Unlock", "Unload"]
    # Locked-but-evicted keeps Unlock and nothing else: no lock is stranded.
    assert rows["locked-evicted"]["buttons"] == ["Estimate", "Unlock"]
    # A configured row that is NOT in memory has nothing to lock or unload.
    assert rows["configured"]["buttons"] == ["Estimate"]

    # D1 — the GATE is unchanged (both rows still offer Lock); the WORDING is
    # what `source` now selects.
    adopt = "Lock this model in memory — this host loaded it outside the Gateway, so locking adopts it first"
    plain = "Lock this model in memory so nothing can evict it"
    assert rows["adopt-me"]["buttons"] == ["Estimate", "Lock", "Unload"]
    assert rows["adopt-me"]["lockTitle"] == adopt
    assert rows["gateway-loaded"]["lockTitle"] == plain, (
        "a gateway-loaded row must not be told the lock adopts anything"
    )
    # The GATE is untouched by D1: an EXPLICIT lockable:false still withholds
    # the button, provider_server or not — there is no adopt wording to show
    # because there is no control to show it on.
    assert rows["refused-adopt"]["buttons"] == ["Estimate", "Unload"]
    assert not rows["refused-adopt"]["lockTitle"]
    # `lockable: true` alone must NEVER select the adopt sentence — that was the
    # defect: the sweep stamps it on every row it finds.
    assert rows["managed"]["lockTitle"] == plain
    assert rows["swept"]["lockTitle"] == plain, "no source reported = no adoption claim"

    # SIZE renders for every resident row, and names its source.
    assert rows["swept"]["size"]["text"] == "1000"
    assert "reported size" in rows["swept"]["size"]["title"]
    # D2 — the `~` prefix rides the CELL, not only the tooltip.
    assert rows["managed"]["size"]["text"] == "~700 + 40 cache", "the estimate and its cache both render"
    assert rows["est-only"]["size"]["text"] == "~900"
    assert "ESTIMATED" in rows["managed"]["size"]["title"], "an estimate must never pass for a measurement"
    assert "est_weights_bytes" in rows["est-only"]["size"]["title"], "the tooltip still names the source field"
    # A MEASURED size never gets the marker.
    assert not rows["swept"]["size"]["text"].startswith("~")
    assert not rows["adopt-me"]["size"]["text"].startswith("~")
    assert rows["refused"]["size"]["text"] == ""
    assert "size unknown" in rows["refused"]["size"]["title"]


def test_models_load_form_selects_ride_the_shared_discovery_cache() -> None:
    """Operator: 'both the provider and model should be the official dropdown
    components ... select the provider, which then auto refresh the list of
    available models for that provider.' The warm-up row uses the console's own
    setSelectOptions recipe over the SAME cache the capability-defaults tab
    fills (fetchDefaultProviders / fetchDefaultModels) — no second fetch path —
    and the free-text lane opens ONLY when discovery has nothing to offer."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    # No second fetch path: both loaders go through the defaults-tab helpers.
    provider_sync = _slice_function(source, "syncModelsLoadProviderOptions")
    model_sync = _slice_function(source, "syncModelsLoadModelOptions")
    assert "fetchDefaultProviders(" in provider_sync
    assert "fetchDefaultModels(" in model_sync
    assert 'setCustomLane("models-load-provider-custom"' in provider_sync
    assert 'setCustomLane("models-load-model-custom"' in model_sync

    harness = f"""
{provider_sync}
{model_sync}
{_slice_function(source, "activeModelsLoadProvider")}
{_slice_function(source, "activeModelsLoadModel")}
{_slice_function(source, "customLaneValue")}
{_slice_function(source, "setCustomLane")}
{_slice_function(source, "setSelectOptions")}
class El {{
  constructor(id) {{ this.id = id; this.children = []; this.className = ""; this._tc = ""; this.value = ""; this.disabled = false; this.options = []; }}
  get textContent() {{ return this._tc; }}
  set textContent(v) {{ this._tc = String(v || ""); if (!this._tc) this.children = []; }}
  append(...items) {{ this.children.push(...items); this.options = this.children; }}
  get classList() {{
    const self = this;
    return {{
      contains: (name) => String(self.className).split(" ").includes(name),
      add(name) {{ self.className = (self.className + " " + name).trim(); }},
      toggle(name, force) {{
        const parts = String(self.className || "").split(" ").filter(Boolean).filter((p) => p !== name);
        if (force) parts.push(name);
        self.className = parts.join(" ");
      }},
    }};
  }}
}}
const els = new Map();
function $(id) {{ if (!els.has(id)) els.set(id, new El(id)); return els.get(id); }}
const document = {{ createElement: () => new El("option") }};
const state = {{ providerLabels: new Map() }};
let _modelsCatalogSeq = 0;
$("models-load-provider-custom").className = "hidden";
$("models-load-model-custom").className = "hidden";
let providers = ["lmstudio", "mlx"];
let models = {{ lmstudio: ["qwen3-a3b"], mlx: [] }};
const asked = [];
async function fetchDefaultProviders() {{ asked.push("providers"); return providers; }}
async function fetchDefaultModels(provider) {{ asked.push("models:" + provider); return models[provider] || []; }}
const snap = (step) => ({{
  step,
  providerOptions: $("models-load-provider").options.map((o) => o.value),
  providerDisabled: $("models-load-provider").disabled,
  providerLaneHidden: $("models-load-provider-custom").className.includes("hidden"),
  modelOptions: $("models-load-model").options.map((o) => o.value),
  modelEmptyLabel: $("models-load-model").options[0] ? $("models-load-model").options[0].textContent : "",
  modelLaneHidden: $("models-load-model-custom").className.includes("hidden"),
  active: [activeModelsLoadProvider(), activeModelsLoadModel()],
  asked: [...asked],
}});
const results = [];
await syncModelsLoadProviderOptions();
results.push(snap("initial"));
// The operator picks a provider: the model list auto-refreshes for it.
$("models-load-provider").value = "lmstudio";
await syncModelsLoadModelOptions();
results.push(snap("picked-lmstudio"));
// A provider whose catalog is EMPTY opens the typing lane for the model.
$("models-load-provider").value = "mlx";
await syncModelsLoadModelOptions();
results.push(snap("picked-mlx"));
// Discovery with nothing at all: the provider lane opens too, and the typed
// value is what the load reads.
providers = [];
await syncModelsLoadProviderOptions();
$("models-load-provider-custom").value = " typed-provider ";
$("models-load-model-custom").value = "typed-model";
results.push(snap("offline"));
console.log(JSON.stringify(results));
"""
    steps = {r["step"]: r for r in _node(harness)}

    initial = steps["initial"]
    assert initial["providerOptions"] == ["", "lmstudio", "mlx"], "the provider select is populated from discovery"
    assert initial["providerDisabled"] is False
    assert initial["providerLaneHidden"] is True, "a healthy catalog keeps the free-text lane SHUT"
    assert initial["asked"] == ["providers"], "no provider picked yet = no model catalog to fetch"
    assert initial["modelEmptyLabel"] == "Select provider first"

    picked = steps["picked-lmstudio"]
    assert "models:lmstudio" in picked["asked"], "picking a provider refreshes that provider's models"
    assert picked["modelOptions"] == ["", "qwen3-a3b"]
    assert picked["modelLaneHidden"] is True

    mlx = steps["picked-mlx"]
    assert mlx["modelOptions"] == [""], "an empty catalog offers nothing to pick"
    assert mlx["modelLaneHidden"] is False, "an empty catalog must open the typing lane, not dead-end"

    offline = steps["offline"]
    assert offline["providerLaneHidden"] is False
    assert offline["active"] == ["typed-provider", "typed-model"], "an open lane outranks the empty select"


def test_models_load_custom_model_lane_never_inherits_the_previous_providers_model() -> None:
    """PART D3. `setCustomLane("models-load-model-custom", !models.length, keep)`
    seeded the free-text lane with `keep` — the model chosen under the PREVIOUS
    provider. Switching provider to one whose catalog is empty therefore armed
    the warm-up row with `newProvider/oldModel` and a click would load a pair
    that never existed. The lane opens EMPTY; console-tui already does this."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    from test_gateway_console_offline import _node, _slice_function

    source = "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))
    # The literal, so a future edit cannot quietly re-arm the lane.
    assert 'setCustomLane("models-load-model-custom", !models.length, "");' in source
    assert 'setCustomLane("models-load-model-custom", !models.length, models.length ? "" : keep)' not in source

    harness = f"""
{_slice_function(source, "syncModelsLoadModelOptions")}
{_slice_function(source, "activeModelsLoadProvider")}
{_slice_function(source, "activeModelsLoadModel")}
{_slice_function(source, "customLaneValue")}
{_slice_function(source, "setCustomLane")}
{_slice_function(source, "setSelectOptions")}
class El {{
  constructor(id) {{ this.id = id; this.children = []; this.className = ""; this._tc = ""; this.value = ""; this.disabled = false; this.options = []; }}
  get textContent() {{ return this._tc; }}
  set textContent(v) {{ this._tc = String(v || ""); if (!this._tc) this.children = []; }}
  append(...items) {{ this.children.push(...items); this.options = this.children; }}
  get classList() {{
    const self = this;
    return {{
      contains: (name) => String(self.className).split(" ").includes(name),
      add(name) {{ self.className = (self.className + " " + name).trim(); }},
      toggle(name, force) {{
        const parts = String(self.className || "").split(" ").filter(Boolean).filter((p) => p !== name);
        if (force) parts.push(name);
        self.className = parts.join(" ");
      }},
    }};
  }}
}}
const els = new Map();
function $(id) {{ if (!els.has(id)) els.set(id, new El(id)); return els.get(id); }}
const document = {{ createElement: () => new El("option") }};
const state = {{ providerLabels: new Map() }};
let _modelsCatalogSeq = 0;
$("models-load-provider-custom").className = "hidden";
$("models-load-model-custom").className = "hidden";
const catalogs = {{ lmstudio: ["qwen3-a3b"], mlx: [] }};
async function fetchDefaultModels(provider) {{ return catalogs[provider] || []; }}
const results = [];
// The operator picks lmstudio, then its one model.
$("models-load-provider").value = "lmstudio";
await syncModelsLoadModelOptions();
$("models-load-model").value = "qwen3-a3b";
results.push({{ step: "picked", model: activeModelsLoadModel() }});
// ...then switches PROVIDER to one whose catalog is empty.
$("models-load-provider").value = "mlx";
await syncModelsLoadModelOptions();
results.push({{
  step: "switched",
  laneHidden: $("models-load-model-custom").className.includes("hidden"),
  laneValue: $("models-load-model-custom").value,
  active: [activeModelsLoadProvider(), activeModelsLoadModel()],
}});
console.log(JSON.stringify(results));
"""
    steps = {r["step"]: r for r in _node(harness)}
    assert steps["picked"]["model"] == "qwen3-a3b"
    switched = steps["switched"]
    assert switched["laneHidden"] is False, "an empty catalog must still open the typing lane"
    assert switched["laneValue"] == "", "the lane must NEVER carry the previous provider's model"
    assert switched["active"] == ["mlx", ""], (
        "the warm-up row must read as mlx with nothing chosen, not mlx/qwen3-a3b"
    )
