"""Console capability editor, output.voice: the kit's voice picker (backlog 0947).

The "Edit multimodal capability" dialog mounts the shared voice picker
(abstractuic islands `mountVoiceSettings`, ui-kit >= 0.9.1) in place of its own
provider / model / voice selects: voices by name, grouped Your / Engine /
Shared voices, the language filter and search, and choosing a voice plays it
with one play/stop toggle (the kit's behaviour, checked in abstractuic's
`check_voice_library.mjs`). These tests pin the console's half: the sample
call it injects (`previewVoice`), its cancel, the mount lifecycle, the save,
and that no native voice list or replay panel is left.

Driven on the SHIPPED functions (sliced from the served console) under node.
"""

from __future__ import annotations

import re

from abstractgateway.console import gateway_console_html
from abstractgateway.console_islands import ISLANDS_PROVENANCE
from test_gateway_console_offline import _console_script, _node, _slice_function

_FUNCS = (
    "voiceCatalog",
    "defaultVoicePickActive",
    "defaultVoiceSample",
    "defaultVoicePickerProps",
    "renderDefaultVoicePicker",
    "unmountDefaultVoicePicker",
    "setDefaultVoicePickMode",
    "activeDefaultProvider",
    "activeDefaultModel",
    "closeDefaultModal",
)


def _harness(scenario: str) -> str:
    source = _console_script()
    funcs = "\n".join(_slice_function(source, name) for name in _FUNCS)
    sample_text = re.search(r"const DEFAULT_VOICE_SAMPLE_TEXT = [^\n]*;", source)
    assert sample_text, "DEFAULT_VOICE_SAMPLE_TEXT missing"
    return f"""
{sample_text.group(0)}
const els = new Map();
function $(id) {{
  if (!els.has(id)) {{
    const cls = new Set();
    els.set(id, {{ id, value: "", textContent: "", className: "",
      classList: {{ toggle: (n, on) => (on ? cls.add(n) : cls.delete(n)), add: (n) => cls.add(n), remove: (n) => cls.delete(n), contains: (n) => cls.has(n) }} }});
  }}
  return els.get(id);
}}
const state = {{ activeDefaultRow: null, defaultVoicePick: null, defaultVoiceIsland: null, defaultModalPrefill: null }};
function isVoiceOutputDefault(row) {{ return row.key === "output.voice"; }}
function customLaneValue() {{ return ""; }}
function setCustomLane() {{}}
function withQuery(path, params) {{ return path + "?" + Object.entries(params).map(([k, v]) => `${{k}}=${{encodeURIComponent(v)}}`).join("&"); }}
function sandboxRequestId() {{ return "req1"; }}
function voiceTestRunId() {{ return "voicetest_run"; }}
function sandboxArtifactUrl(runId, ref) {{ return ref ? `/art/${{runId}}/${{ref.artifact_id}}` : ""; }}
const calls = [];
let ttsAnswer = null;  // null = park until the signal aborts
async function api(path, options = {{}}) {{
  calls.push({{ path, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null, signal: options.signal || null }});
  if (path.startsWith("/api/gateway/voice/voices")) return {{ items: [] }};
  if (ttsAnswer) return ttsAnswer;
  return await new Promise((_, reject) => {{
    options.signal?.addEventListener("abort", () => {{ const e = new Error("Request cancelled."); e.name = "AbortError"; reject(e); }}, {{ once: true }});
  }});
}}
const fetches = [];
globalThis.fetch = async (url, init = {{}}) => {{
  fetches.push({{ url, signal: init.signal || null }});
  return {{ ok: true, status: 200, arrayBuffer: async () => new Uint8Array([1, 2, 3, 4]).buffer }};
}};
// A stand-in islands bundle: records every mount, update and unmount.
const islands = [];
let lib = {{
  mountVoiceSettings(el, props) {{
    const handle = {{ el, props, updates: 0, unmounted: false,
      update(next) {{ this.props = next; this.updates += 1; }},
      unmount() {{ this.unmounted = true; }} }};
    islands.push(handle);
    return handle;
  }},
}};
function islandsLib() {{ return lib; }}
const results = [];
{funcs}
{scenario}
console.log(JSON.stringify(results));
"""


def test_the_sample_rides_the_tts_lane_and_hands_the_kit_bytes() -> None:
    scenario = """
ttsAnswer = { ok: true, audio_artifact: { artifact_id: "s1", content_type: "audio/wav" } };
const ctl = new AbortController();
const bytes = await defaultVoiceSample({ provider: "qwen3-tts", model: "Qwen/Qwen3-TTS-12Hz-0.6B-Base", voice: "", profile: "e0d57043" }, undefined, { signal: ctl.signal });
results.push({ calls: calls.map((c) => ({ path: c.path, body: c.body, signalled: c.signal === ctl.signal })),
  fetches: fetches.map((f) => ({ url: f.url, signalled: f.signal === ctl.signal })), bytes: bytes.byteLength });
"""
    out = _node(_harness(scenario))[0]
    call = out["calls"][0]
    assert call["path"] == "/api/gateway/runs/voicetest_run/voice/tts"
    assert call["body"]["voice"] == "e0d57043", "a profile id is the sample's voice (one id)"
    assert call["body"]["timeout_s"] == 25 and call["body"]["model"] == "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
    assert call["signalled"], "the kit's cancel signal must reach the synthesis request"
    assert out["fetches"] == [{"url": "/art/voicetest_run/s1", "signalled": True}], "and the audio download"
    assert out["bytes"] == 4


def test_a_newer_choice_cancels_the_sample_request() -> None:
    scenario = """
const ctl = new AbortController();
const pending = defaultVoiceSample({ provider: "openai", model: "tts-1", voice: "alloy" }, undefined, { signal: ctl.signal });
await new Promise((r) => setTimeout(r, 0));
ctl.abort();
let error = null;
try { await pending; } catch (e) { error = e.name; }
results.push({ error, fetches: fetches.length });
"""
    out = _node(_harness(scenario))[0]
    assert out["error"] == "AbortError", "an aborted sample must reject as a cancel (the kit drops it silently)"
    assert out["fetches"] == 0, "a cancelled sample downloads nothing"


def test_the_dialog_mounts_the_kit_picker_and_unmounts_it_on_close() -> None:
    scenario = """
const row = { key: "output.voice", provider: "qwen3-tts", model: "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", options: { voice: "aiden" } };
state.activeDefaultRow = row;
setDefaultVoicePickMode(true);
state.defaultVoicePick = { provider: row.provider, model: row.model, voice: "aiden", profile: "" };
const mounted = renderDefaultVoicePicker();
const island = islands[0];
results.push({ step: "open", mounted, count: islands.length, el: island.el.id, value: island.props.value,
  picker: !$("modal-default-voice-picker").classList.contains("hidden"),
  nativeHidden: $("modal-default-provider-label").classList.contains("hidden") && $("modal-default-model-label").classList.contains("hidden"),
  preview: typeof island.props.previewVoice, delivery: island.props.delivery, reset: island.props.showReset });
// The operator picks a clone: the pick (and the save's accessors) follow.
island.props.onChange({ provider: "qwen3-tts", model: "Qwen/Qwen3-TTS-12Hz-0.6B-Base", voice: "e0d57043", profile: "" });
results.push({ step: "picked", provider: activeDefaultProvider(), model: activeDefaultModel(), updates: island.updates, value: island.props.value, count: islands.length });
await island.props.fetchCatalog("qwen3-tts");
results.push({ step: "catalog", path: calls.at(-1).path });
closeDefaultModal();
results.push({ step: "closed", unmounted: island.unmounted, island: state.defaultVoiceIsland, row: state.activeDefaultRow });
// A missing bundle is said in the dialog; nothing native stands in.
lib = null;
const again = renderDefaultVoicePicker();
results.push({ step: "nobundle", again, message: $("default-modal-message").textContent, cls: $("default-modal-message").className, count: islands.length });
"""
    steps = {r["step"]: r for r in _node(_harness(scenario))}
    opened = steps["open"]
    assert opened["mounted"] and opened["count"] == 1 and opened["el"] == "modal-default-voice-picker"
    assert opened["value"] == {"provider": "qwen3-tts", "model": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", "voice": "aiden", "profile": ""}
    assert opened["picker"] and opened["nativeHidden"], "the kit picker replaces the native provider/model selects"
    assert opened["preview"] == "function", "previewVoice injected: choosing a voice plays it"
    assert opened["delivery"] is False and opened["reset"] is False
    picked = steps["picked"]
    assert picked["provider"] == "qwen3-tts" and picked["model"] == "Qwen/Qwen3-TTS-12Hz-0.6B-Base", (
        "a clone's pick carries its cloning checkpoint; the save reads it"
    )
    assert picked["updates"] == 1 and picked["count"] == 1, "a change updates the mounted island, never mounts a second"
    assert picked["value"]["voice"] == "e0d57043"
    assert steps["catalog"]["path"] == "/api/gateway/voice/voices?compact=true&provider=qwen3-tts"
    assert steps["closed"]["unmounted"] and steps["closed"]["island"] is None, "close unmounts (the kit stops its sample)"
    assert steps["closed"]["row"] is None
    nobundle = steps["nobundle"]
    assert nobundle["again"] is False and nobundle["count"] == 1
    assert nobundle["message"] == "The voice picker is unavailable: the abstractuic islands bundle did not load."
    assert nobundle["cls"] == "message error"


def test_no_native_voice_list_no_replay_panel_and_the_vendored_kit_has_the_toggle() -> None:
    html = gateway_console_html()
    source = _console_script()
    assert '<div id="modal-default-voice-picker"' in html
    for gone in ('id="modal-default-voice"', 'id="modal-default-voice-play"', "function loadDefaultVoices", "function voicePreviewOnSelect"):
        assert gone not in html, gone
    testable = _slice_function(source, "defaultRowTestable")
    assert '"output.voice"' not in testable, "output.voice has no Test button: choosing a voice plays it"
    assert "voice/tts" not in _slice_function(source, "testDefault"), "Test renders no voice replay panel"
    # The vendored kit is the one that plays a voice when chosen (0.9.1+).
    major, minor, patch = (int(x) for x in ISLANDS_PROVENANCE["kit_version"].split("."))
    assert (major, minor, patch) >= (0, 9, 1), ISLANDS_PROVENANCE["kit_version"]
    from abstractgateway.console_islands import ISLANDS_JS

    assert "af-voice-settings__voice-row" in ISLANDS_JS and "mountVoiceSettings" in ISLANDS_JS
