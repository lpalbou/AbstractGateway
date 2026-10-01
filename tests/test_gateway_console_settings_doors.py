"""Console settings doors (G2): the "Streamed replies" switch
(`agents.streaming_default`) beside the default agent workflow block, and the
Apps-tab Skills block's facts (shelf path, source word, bundled version, count,
the "Refresh the curated shelf" action) — on the SHIPPED functions."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.basic

_PRELUDE = """
const HTML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch] || ch);
"""


def test_streaming_default_switch_reads_writes_and_never_hides() -> None:
    from test_gateway_console_offline import _console_script, _node, _slice_function

    source = _console_script()
    # Rendered inside the default agent workflow block, wired to the change handler.
    assert "streamingDefaultMarkup(agentDefStore.data, agentDefStore)" in _slice_function(source, "agentDefaultsRender")
    mount = _slice_function(source, "mountAgentDefaults")
    # A kit af-switch (role=switch, aria-checked) toggled by a click, never a checkbox change.
    assert '[data-streaming-default]' in mount and 'streamingDefaultSave(sw.getAttribute("aria-checked") !== "true")' in mount
    harness = _PRELUDE + f"""
{_slice_function(source, "uiPill")}
const STREAMING_DEFAULT_LABEL = "Streamed replies";
const STREAMING_DEFAULT_DESC = "New interactive runs show the model's reply as it is written.";
{_slice_function(source, "streamingDefaultMarkup")}
{_slice_function(source, "streamingDefaultBody")}
{_slice_function(source, "streamingDefaultSave")}
const calls = [];
let answer = null;
async function api(path, opts) {{ calls.push({{ path, body: JSON.parse(opts.body) }}); if (answer instanceof Error) throw answer; return answer; }}
const renders = [];
function agentDefaultsRender() {{ renders.push(1); }}
const agentDefStore = {{ data: null, streamSaving: false, streamSaved: null }};
const row = (value, source) => ({{ key: "agents.streaming_default", value, source, default: false, label: "Stream replies by default", help: "Interactive runs only." }});
const out = {{}};
out.on = streamingDefaultMarkup({{ writable: true, agents: {{ streaming_default: row(true, "stored") }} }}, {{}});
out.off = streamingDefaultMarkup({{ writable: true, agents: {{ streaming_default: row(false, "default") }} }}, {{}});
out.viewer = streamingDefaultMarkup({{ writable: false, agents: {{ streaming_default: row(false, "default") }} }}, {{}});
out.missing = streamingDefaultMarkup({{ writable: true, agents: {{ default_workflow: {{}} }} }}, {{}});
out.junk = streamingDefaultMarkup({{ writable: true, agents: {{ streaming_default: {{ value: "yes", source: "stored" }} }} }}, {{}});
out.bodyOn = streamingDefaultBody(1);
out.bodyOff = streamingDefaultBody(false);
agentDefStore.data = {{ writable: true, agents: {{ default_workflow: {{ "abstractcode.agent.v1": {{ value: "x" }} }}, streaming_default: row(false, "default") }} }};
answer = {{ writable: true, agents: {{ streaming_default: row(true, "stored") }} }};
await streamingDefaultSave(true);
out.afterSave = {{ calls: calls.slice(), agents: agentDefStore.data.agents, saved: agentDefStore.streamSaved, saving: agentDefStore.streamSaving }};
answer = Object.assign(new Error("400"), {{ data: {{ detail: "agents.streaming_default must be true or false" }} }});
await streamingDefaultSave(false);
out.afterFail = agentDefStore.streamSaved;
console.log(JSON.stringify([out]));
"""
    out = _node(harness)[0]
    # Round-2 polish: one feature-labelled kit switch row under "Settings"; the state is the switch
    # (aria-checked), no "On — …/Off — …" text and no Default / Saved setting chip.
    for key in ("on", "off", "viewer"):
        assert 'role="switch"' in out[key] and "af-switch af-switch--row" in out[key] and ">Settings<" in out[key], out[key]
        assert "Streamed replies" in out[key] and 'type="checkbox"' not in out[key], out[key]
        assert "replies arrive whole" not in out[key] and ">Default<" not in out[key] and "Saved setting" not in out[key], out[key]
    assert 'aria-checked="true"' in out["on"] and 'aria-checked="false"' in out["off"]
    assert "abstractgateway config set agents.streaming_default true|false" in out["on"]
    assert 'aria-disabled="true"' in out["viewer"] and "Only an admin can change this." in out["viewer"]
    assert 'aria-disabled' not in out["on"]
    for key in ("missing", "junk"):
        assert "not available on this gateway" in out[key] and "data-streaming-default-missing" in out[key], out[key]
        assert "data-streaming-default " not in out[key] and "type=\"checkbox\"" not in out[key]
    assert out["bodyOn"] == {"agents": {"streaming_default": True}}
    assert out["bodyOff"] == {"agents": {"streaming_default": False}}
    after = out["afterSave"]
    assert after["calls"] == [{"path": "/api/gateway/admin/runtime-config", "body": {"agents": {"streaming_default": True}}}]
    assert after["agents"]["streaming_default"]["value"] is True
    assert "default_workflow" in after["agents"]  # the workflow block survives a write that did not return it
    assert after["saved"]["tone"] == "ok" and after["saving"] is False
    assert out["afterFail"] == {"tone": "err", "head": "Not saved", "text": "agents.streaming_default must be true or false"}


def test_skills_block_shows_path_source_bundled_version_and_refresh() -> None:
    from test_gateway_console_offline import _console_script, _node, _slice_function

    source = _console_script()
    assert "await skillsShelfCount();" in _slice_function(source, "skillsShelfRefresh")
    assert 'api("/api/gateway/skills")' in _slice_function(source, "skillsShelfCount")
    harness = _PRELUDE + f"""
{_slice_function(source, "uiPill")}
{_slice_function(source, "skillsShelfSourcePill")}
{_slice_function(source, "skillsShelfMarkup")}
{_slice_function(source, "skillsShelfCountText")}
const skillsShelfStore = {{ data: null, error: "", saving: false, saved: null, draft: null, views: new Map(), inventory: null }};
const shelf = {{ label: "Skills shelf", help: "h", source: "seeded", value: null, resolved: "/d/skills/registry", available: true, bundled_version: "2026.09.25", default_path: "/d/skills/registry" }};
skillsShelfStore.data = {{ writable: true, skills: {{ shelf }} }};
const out = {{}};
out.counting = skillsShelfMarkup();
skillsShelfStore.inventory = {{ skills: [{{ name: "a" }}, {{ name: "b" }}, {{ name: "c" }}] }};
out.three = skillsShelfMarkup();
skillsShelfStore.inventory = {{ skills: [{{ name: "a" }}] }};
out.one = skillsShelfMarkup();
skillsShelfStore.inventory = {{ error: "HTTP 500" }};
out.failed = skillsShelfMarkup();
skillsShelfStore.inventory = {{ warnings: [] }};
out.unlisted = skillsShelfMarkup();
skillsShelfStore.data = {{ writable: true, skills: {{ shelf: {{ ...shelf, source: "stored", value: "/x", bundled_version: null }} }} }};
out.noVersion = skillsShelfMarkup();
console.log(JSON.stringify([out]));
"""
    out = _node(harness)[0]
    three = out["three"]
    # DESIGN-v3 §6.1 + C3F review: the block is the folded "Shelf folder" disclosure under the
    # Skills table — the folder (placeholder = the gateway's own copy), one helper line with the
    # bundled version, "Refresh curated shelf" the only button. The count lives in the table
    # itself (one row per skill), so the block no longer repeats it.
    assert 'placeholder="/d/skills/registry"' in three  # path
    assert "Empty: the gateway's own copy of the curated shelf (version 2026.09.25)" in three  # source + bundled version
    assert ">Refresh curated shelf<" in three and "data-skills-shelf-reseed" in three
    assert "skills on this shelf" not in three and "Reads <code>" not in three
    assert "Saved setting" in out["noVersion"] and 'value="/x"' in out["noVersion"]
    assert "(version" not in out["noVersion"]
