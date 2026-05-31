from __future__ import annotations

import re
import json
import shutil
import subprocess
import tempfile

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
    assert "/api/gateway/discovery/providers?include_models=false" in console.text
    assert "/api/gateway/discovery/providers/${encodeURIComponent(provider)}/models${query}" in console.text
    assert '<select id="default-provider">' in console.text
    assert '<select id="default-model">' in console.text
    assert 'id="endpoint-description"' in console.text
    assert 'id="endpoint-api-key"' in console.text
    assert 'id="endpoint-clear-api-key"' in console.text
    assert 'id="discover-endpoint-models"' in console.text
    assert 'id="endpoint-models" class="model-picker__select" multiple' in console.text
    assert 'id="endpoint-capabilities"' not in console.text
    assert 'id="endpoint-profiles-table"' in console.text
    assert "virtual provider" in console.text
    assert 'id="new-email"' in console.text
    assert 'id="runtime-reservations-section"' in console.text
    assert 'id="confirm-backdrop"' in console.text
    assert 'id="login-form"' in console.text
    assert 'id="toggle-token"' in console.text
    assert "Connect to AbstractGateway" in console.text
    assert "min-height: calc(100vh - 61px)" in console.text
    assert "align-content: center" in console.text
    assert "body:not(.signed-in) main.layout { align-content: start; padding-block: 18px; }" in console.text
    assert "Gateway URL" not in console.text
    assert "localStorage" not in console.text


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
  cookie: "",
  getElementById: el,
  createElement: (tag) => new Element(tag),
}};
let loggedIn = false;
const calls = [];
function response(status, payload) {{
  return {{
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
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
  if (path === "/api/gateway/discovery/providers?include_models=false") return response(200, {{ providers: ["openai"] }});
  if (path === "/api/gateway/discovery/providers/openai/models") return response(200, {{ models: ["gpt-4.1"] }});
  if (path === "/api/gateway/discovery/providers/openai/models?base_url=https%3A%2F%2Fmodels.example.test%2Fv1") return response(200, {{ models: ["gpt-4.1-base-url"] }});
  if (path === "/api/gateway/config/capability-defaults") return response(200, {{
    routes: [{{ key: "input.text", kind: "input", modality: "text", label: "Text Input", provider: "openai", model: "gpt-4.1", configured: true }}]
  }});
  if (path === "/api/gateway/config/provider-endpoint-profiles") return response(200, {{ profiles: [] }});
  if (path === "/api/gateway/config/provider-endpoint-profiles/discover-models") {{
    const body = JSON.parse(String(options.body || "{{}}"));
    if (body.base_url !== "https://preview.example.test/v1" || body.api_key !== "preview-key") {{
      return response(400, {{ detail: "bad endpoint discovery payload" }});
    }}
    return response(200, {{ models: ["remote-model"] }});
  }}
  if (path === "/api/gateway/admin/users") return response(200, {{ users: [] }});
  if (path === "/api/gateway/admin/runtime-reservations") return response(200, {{ runtime_reservations: [] }});
  return response(404, {{ detail: path }});
}}

const context = vm.createContext({{
  document,
  fetch,
  Headers,
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

el("default-base-url").value = "https://models.example.test/v1";
el("default-provider").value = "openai";
el("default-base-url").onchange();
await new Promise((resolve) => setTimeout(resolve, 0));
await new Promise((resolve) => setTimeout(resolve, 0));

if (!calls.some((call) => call.path === "/api/gateway/discovery/providers/openai/models?base_url=https%3A%2F%2Fmodels.example.test%2Fv1")) {{
  throw new Error("default model discovery did not forward the configured base URL");
}}
if (!el("default-model").children.some((child) => child.value === "gpt-4.1-base-url")) {{
  throw new Error("default model picker was not populated from base-url discovery");
}}
"""
    result = subprocess.run(["node", "--input-type=module", "-e", harness], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
