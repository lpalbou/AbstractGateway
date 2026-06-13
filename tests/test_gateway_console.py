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
    assert "/api/gateway/admin/runtime-reservations" in console.text
    assert "/api/gateway/config/provider-endpoint-profiles" in console.text
    assert "/api/gateway/config/provider-endpoint-profiles/discover-models" in console.text
    assert "/api/gateway/discovery/providers/${encodeURIComponent(provider)}/models" in console.text
    assert "/api/gateway/vision/provider_models" in console.text
    assert "/api/gateway/audio/speech/models" in console.text
    assert "/api/gateway/audio/transcriptions/models" in console.text
    assert "/api/gateway/audio/music/models" in console.text
    assert "/api/gateway/embeddings/models" in console.text
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
    assert "--bg-primary: var(--bg)" in console.text
    assert 'id="modal-default-base-url"' not in console.text
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
    assert 'id="login-form"' in console.text
    assert 'id="toggle-token"' in console.text
    assert "Connect to AbstractGateway" in console.text
    assert 'id="tab-button-users"' in console.text
    assert 'id="tab-button-providers"' in console.text
    assert 'id="tab-button-defaults"' in console.text
    assert 'id="tab-button-sandbox"' in console.text
    assert 'id="console-tabs-bar"' in console.text
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
    routes: [{{ key: "input.text", kind: "input", modality: "text", label: "Text Input", provider: "openai", model: "gpt-4.1", configured: true }}]
  }});
  if (path === "/api/gateway/config/capability-defaults/input/text" && options.method === "PUT") return response(200, {{ routes: [] }});
  if (path === "/api/gateway/config/capability-defaults/output/image/image_to_image" && options.method === "PUT") return response(200, {{ routes: [] }});
  if (path === "/api/gateway/config/capability-defaults/output/voice" && options.method === "PUT") return response(200, {{ routes: [] }});
  if (path === "/api/gateway/attachments/upload" && options.method === "POST") {{
    return response(200, {{ ok: true, artifact: {{ "$artifact": "upload-1", artifact_id: "upload-1", content_type: "image/png", filename: "content.png", modality: "image" }} }});
  }}
  if (path === "/api/gateway/config/provider-endpoint-profiles") return response(200, {{
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
  setTimeout,
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
if (saveBody.base_url !== "https://models.example.test/v1" || saveBody.options.temperature !== 0.3) {{
  throw new Error("save default did not preserve hidden base_url/options");
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
    {{ key: "output.image", kind: "output", modality: "image", label: "Image Output", provider: "mflux", model: "flux-dev", configured: true }},
    {{ key: "output.image.text_to_image", kind: "output", modality: "image", task: "text_to_image", label: "Image Generation", provider: "mflux", model: "flux-dev", configured: true }},
    {{ key: "output.image.image_to_image", kind: "output", modality: "image", task: "image_to_image", label: "Image Edit", provider: "mlx-gen", model: "AbstractFramework/qwen-image-edit-2511-4bit", configured: true }},
    {{ key: "output.image.image_upscale", kind: "output", modality: "image", task: "image_upscale", label: "Image Restore / Upscale", provider: "mlx-gen", model: "AbstractFramework/seedvr2-3b-8bit", configured: true }},
    {{ key: "output.video", kind: "output", modality: "video", label: "Video Output", provider: "mlx-gen", model: "Wan-AI/Wan2.2-TI2V-5B-Diffusers", configured: true }},
    {{ key: "output.video.text_to_video", kind: "output", modality: "video", task: "text_to_video", label: "Video Generation", provider: "mlx-gen", model: "Wan-AI/Wan2.2-TI2V-5B-Diffusers", configured: true }},
    {{ key: "output.video.image_to_video", kind: "output", modality: "video", task: "image_to_video", label: "Image To Video", provider: "mlx-gen", model: "AbstractFramework/wan2.2-i2v-a14b-diffusers-8bit", configured: true }},
    {{ key: "output.voice", kind: "output", modality: "voice", label: "Voice Output", provider: "openai", model: "tts-1", configured: true, options: {{ voice: "coral" }} }},
    {{ key: "output.music", kind: "output", modality: "music", label: "Music Output", provider: "acemusic", model: "ace-step", configured: true }},
    {{ key: "output.sound", kind: "output", modality: "sound", label: "Sound Output", provider: "stable-audio", model: "stabilityai/stable-audio-open-small", configured: true }},
    {{ key: "input.scene3d", kind: "input", modality: "scene3d", label: "3D Scene Input", provider: "abstract3d", model: "scene", configured: true }},
    {{ key: "output.scene3d", kind: "output", modality: "scene3d", label: "3D Scene Output", provider: "abstract3d", model: "scene", configured: true }},
  ]
}});
if (el("defaults-table").children.some((child) => String(child.innerHTML || "").includes("scene3d"))) {{
  throw new Error("scene3d defaults should be hidden in the Gateway Console for now");
}}
if (el("defaults-table").children.some((child) => String(child.innerHTML || "").includes("output.image</code>"))) {{
  throw new Error("broad output.image should be hidden when task-specific image defaults are present");
}}
if (el("defaults-table").children.some((child) => String(child.innerHTML || "").includes("output.video</code>"))) {{
  throw new Error("broad output.video should be hidden when task-specific video defaults are present");
}}
if (!el("defaults-table").children.some((child) => String(child.innerHTML || "").includes("linked"))) {{
  throw new Error("output.text should render as linked to input.text");
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
