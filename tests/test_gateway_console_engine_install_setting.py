"""Root backlog 0858: the console's "Allow engine installs" switch on the Engines tab.

It shows the effective value and where it comes from (stored, or the default for this bind / a
caller on the gateway's own computer), an admin switches it (POST /admin/runtime-config), a stored
choice can go back to the default, a non-admin sees it read-only, and the CLI twin is shown.
Driven on the SHIPPED functions (sliced from the served console) under node.
"""

from __future__ import annotations

import pytest

from test_gateway_console_offline import _console_script, _node, _slice_function

pytestmark = pytest.mark.basic

_FUNCS = ("engineInstallSourceText", "engineInstallSettingMarkup", "engineInstallSettingSave")


def _run(scenario: str) -> dict:
    source = _console_script()
    funcs = "\n".join(_slice_function(source, name) for name in _FUNCS)
    script = f"""
const ESC = {{ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }};
function esc(v) {{ return String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]); }}
const engineSetting = {{ data: null, error: "", busy: false, note: null }};
const engineStore = {{ data: {{ install_allowed: true }} }};
const posts = [];
let rendered = 0, refreshed = 0;
function engineRender() {{ rendered += 1; }}
async function engineRefresh() {{ refreshed += 1; }}
async function api(path, opts = {{}}) {{
  const body = opts.body ? JSON.parse(opts.body) : null;
  posts.push({{ path, method: opts.method || "GET", body }});
  const v = body.allow_engine_install;
  return {{ writable: true, allow_engine_install: v === null ? {{ value: true, source: "default", loopback_bind: true }} : {{ value: v, source: "stored" }} }};
}}
const results = [];
{funcs}
{scenario}
console.log(JSON.stringify(results));
"""
    return _node(script)[0]


def test_the_switch_shows_the_value_its_source_and_the_cli() -> None:
    out = _run("""
engineSetting.data = { writable: true, allow_engine_install: { value: false, source: "default", loopback_bind: false } };
results.push({ html: engineInstallSettingMarkup() });
""")
    html = out["html"]
    assert 'role="switch"' in html and 'aria-checked="false"' in html and 'data-engine-action="allow-install"' in html
    assert "Off: the default for a gateway other computers reach." in html
    assert "abstractgateway-config set allow_engine_install on" in html
    assert "aria-disabled" not in html and "Use the default" not in html


def test_an_admin_switches_it_and_can_go_back_to_the_default() -> None:
    out = _run("""
engineSetting.data = { writable: true, allow_engine_install: { value: false, source: "default", loopback_bind: false } };
await engineInstallSettingSave(true);
const on = engineInstallSettingMarkup();
await engineInstallSettingSave(null);
results.push({ posts, on, after: engineInstallSettingMarkup(), refreshed, note: engineSetting.note });
""")
    assert out["posts"] == [
        {"path": "/api/gateway/admin/runtime-config", "method": "POST", "body": {"allow_engine_install": True}},
        {"path": "/api/gateway/admin/runtime-config", "method": "POST", "body": {"allow_engine_install": None}},
    ]
    assert 'aria-checked="true"' in out["on"] and "On: your setting." in out["on"] and "Use the default" in out["on"]
    assert "On: the default for a gateway that only this computer reaches." in out["after"]
    assert out["refreshed"] == 2 and out["note"] == {"tone": "ok", "text": "Saved"}


def test_a_non_admin_sees_it_read_only() -> None:
    out = _run("""
engineSetting.data = { writable: false, allow_engine_install: { value: true, source: "default_same_machine" } };
results.push({ html: engineInstallSettingMarkup() });
""")
    assert 'aria-disabled="true"' in out["html"] and "Only an admin can change this" in out["html"]
    assert "On: the default: you are on the gateway&#39;s own computer." in out["html"]
