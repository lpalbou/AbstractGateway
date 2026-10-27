"""Console capability editor: choosing a voice plays it (backlog 0947).

The output.voice dialog has no Test button and no replay panel any more:
changing the Voice select synthesizes a short sample through the production
TTS lane and plays it; a play/stop toggle at the right of the select replays
from 0 or stops back to 0. A newer choice cancels the older sample, so fast
scrolling never queues playback.

Driven on the SHIPPED functions (sliced from the served console) under node,
with the browser pieces (api, fetch, <audio>) stubbed.
"""

from __future__ import annotations

import re

from abstractgateway.console import gateway_console_html
from test_gateway_console_offline import _console_script, _node, _slice_function

_FUNCS = (
    "voicePreviewSelection",
    "voicePreviewKey",
    "setVoicePlayState",
    "voicePreviewNote",
    "voicePreviewAbort",
    "voicePreviewRewind",
    "voicePreviewReset",
    "voicePreviewPlay",
    "voicePreviewEnded",
    "voicePreviewSynthesize",
    "voicePreviewOnSelect",
    "voicePreviewToggle",
)


def _const(source: str, name: str) -> str:
    match = re.search(r"const " + re.escape(name) + r" = [^\n]*;", source)
    assert match, f"const {name} not found in the console JavaScript"
    return match.group(0)


def _harness(scenario: str) -> str:
    source = _console_script()
    funcs = "\n".join(_slice_function(source, name) for name in _FUNCS)
    return f"""
{_const(source, "VOICE_PREVIEW_TEXT")}
const VOICE_PREVIEW_DEBOUNCE_MS = 15;
{_const(source, "voicePreview")}
const els = new Map();
function el(id, extra = {{}}) {{
  if (!els.has(id)) els.set(id, {{ id, textContent: "", className: "", value: "", dataset: {{}}, attrs: {{}},
    setAttribute(k, v) {{ this.attrs[k] = v; }}, ...extra }});
  return els.get(id);
}}
const glyph = {{ textContent: "▶" }};
el("modal-default-voice-play", {{ querySelector: () => glyph }});
const plays = [];
const audio = el("modal-default-voice-audio", {{
  src: "", currentTime: 0, paused: true,
  play() {{ plays.push({{ src: this.src, from: this.currentTime }}); this.paused = false; return Promise.resolve(); }},
  pause() {{ this.paused = true; }},
}});
function $(id) {{ return el(id); }}
let provider = "openai", model = "gpt-4o-mini-tts";
function activeDefaultProvider() {{ return provider; }}
function activeDefaultModel() {{ return model; }}
let reqSeq = 0;
function sandboxRequestId() {{ reqSeq += 1; return `req${{reqSeq}}`; }}
function voiceTestRunId() {{ return "voicetest_run"; }}
function sandboxArtifactUrl(runId, ref) {{ return ref ? `/art/${{runId}}/${{ref.artifact_id}}` : ""; }}
// api(): each call parks until the scenario settles it; the caller's signal
// rejects it as AbortError, like the real api().
const calls = [];
async function api(path, options = {{}}) {{
  const body = JSON.parse(options.body || "{{}}");
  return await new Promise((resolve, reject) => {{
    const call = {{ path, body, signal: options.signal, resolve, reject, aborted: false }};
    if (options.signal) options.signal.addEventListener("abort", () => {{
      call.aborted = true;
      const e = new Error("Request cancelled."); e.name = "AbortError"; reject(e);
    }}, {{ once: true }});
    calls.push(call);
  }});
}}
function answer(i) {{ calls[i].resolve({{ ok: true, audio_artifact: {{ artifact_id: `a_${{calls[i].body.voice || "default"}}`, content_type: "audio/wav" }} }}); }}
let lastFetched = "";
globalThis.fetch = async (url, init) => {{
  lastFetched = url;
  if (init && init.signal && init.signal.aborted) {{ const e = new Error("aborted"); e.name = "AbortError"; throw e; }}
  return {{ ok: true, status: 200, blob: async () => new Blob([url], {{ type: "" }}) }};
}};
let objectUrls = 0;
URL.createObjectURL = (blob) => {{ objectUrls += 1; return `blob:${{objectUrls}}:${{lastFetched}}:${{blob.type}}`; }};
URL.revokeObjectURL = () => {{}};
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
const settle = async () => {{ for (let i = 0; i < 6; i++) await tick(); }};
const button = el("modal-default-voice-play");
const results = [];
function snap(step, extra = {{}}) {{
  results.push({{ step, state: button.dataset.state, busy: button.attrs["aria-busy"], glyph: glyph.textContent,
    calls: calls.map((c) => ({{ voice: c.body.voice || "", aborted: c.aborted, timeout_s: c.body.timeout_s, text: c.body.text, path: c.path }})),
    plays: plays.map((p) => ({{ ...p }})), currentTime: audio.currentTime, paused: audio.paused,
    note: el("default-modal-message").textContent, ...extra }});
}}
{funcs}
{scenario}
console.log(JSON.stringify(results));
"""


def test_choosing_a_voice_synthesizes_once_and_plays_it_without_a_panel() -> None:
    scenario = """
$("modal-default-voice").value = "coral";
voicePreviewOnSelect();
snap("pending");
await tick(40);
snap("in_flight");
answer(0);
await settle();
snap("playing");
"""
    steps = {r["step"]: r for r in _node(_harness(scenario))}
    assert steps["pending"]["state"] == "loading" and steps["pending"]["busy"] == "true", (
        "the toggle must show a busy state while the sample is on its way"
    )
    flight = steps["in_flight"]
    assert len(flight["calls"]) == 1, "one choice = one synthesis"
    call = flight["calls"][0]
    assert call["voice"] == "coral" and call["timeout_s"] == 25
    assert call["path"] == "/api/gateway/runs/voicetest_run/voice/tts", "the production TTS lane, as Test used"
    playing = steps["playing"]
    assert len(playing["plays"]) == 1, "the sample plays itself — no replay panel, no extra click"
    assert playing["plays"][0]["from"] == 0
    assert playing["plays"][0]["src"].endswith(":audio/wav"), "the artifact's content type types the blob"
    assert playing["state"] == "playing" and playing["glyph"] == "■" and playing["busy"] == "false"


def test_a_second_choice_cancels_the_first_and_only_the_latest_plays() -> None:
    scenario = """
$("modal-default-voice").value = "aria";
voicePreviewOnSelect();
await tick(40);
$("modal-default-voice").value = "coral";
voicePreviewOnSelect();
snap("after_second_choice");
await tick(40);
answer(1);
await settle();
snap("done");
// Fast scrolling inside the debounce starts no synthesis per row.
calls.length = 0; plays.length = 0;
for (const v of ["ash", "ballad", "echo", "sage"]) { $("modal-default-voice").value = v; voicePreviewOnSelect(); await tick(1); }
await tick(40);
snap("scrolled");
"""
    steps = {r["step"]: r for r in _node(_harness(scenario))}
    second = steps["after_second_choice"]
    assert [c["voice"] for c in second["calls"]] == ["aria"]
    assert second["calls"][0]["aborted"], "a newer choice must abort the older in-flight synthesis"
    done = steps["done"]
    assert [c["voice"] for c in done["calls"]] == ["aria", "coral"]
    assert len(done["plays"]) == 1, "fast choices must never queue playback"
    assert "/a_coral:" in done["plays"][0]["src"], "the playing sample must be the LATEST choice's audio"
    assert done["note"] == "", "a superseded sample's cancel is not an error"
    scrolled = steps["scrolled"]
    assert [c["voice"] for c in scrolled["calls"]] == ["sage"], "only the row the operator stopped on is synthesized"


def test_stop_resets_to_zero_and_play_replays_from_zero_without_resynthesizing() -> None:
    scenario = """
$("modal-default-voice").value = "coral";
voicePreviewOnSelect();
await tick(40);
answer(0);
await settle();
audio.currentTime = 1.7;
voicePreviewToggle();
snap("stopped");
audio.currentTime = 0.9;
voicePreviewToggle();
await settle();
snap("replayed");
audio.currentTime = 2.4;
voicePreviewEnded();
snap("ended");
"""
    steps = {r["step"]: r for r in _node(_harness(scenario))}
    stopped = steps["stopped"]
    assert stopped["paused"] and stopped["currentTime"] == 0, "stop must stop AND rewind to 0"
    assert stopped["state"] == "idle" and stopped["glyph"] == "▶"
    replayed = steps["replayed"]
    assert len(replayed["calls"]) == 1, "replaying the same voice reuses the sample"
    assert [p["from"] for p in replayed["plays"]] == [0, 0], "play must (re)start from 0"
    assert replayed["state"] == "playing"
    ended = steps["ended"]
    assert ended["currentTime"] == 0 and ended["state"] == "idle", "a finished sample is ready to replay from 0"


def test_failures_are_one_line_notes_and_closing_drops_the_sample() -> None:
    scenario = """
$("modal-default-voice").value = "coral";
voicePreviewOnSelect();
await tick(40);
calls[0].reject(new Error("voice/tts watchdog fired"));
await settle();
snap("failed");
$("modal-default-voice").value = "aria";
voicePreviewOnSelect();
await tick(40);
voicePreviewReset();
answer(1);
await settle();
snap("closed");
"""
    steps = {r["step"]: r for r in _node(_harness(scenario))}
    failed = steps["failed"]
    assert failed["note"] == "Voice sample failed: voice/tts watchdog fired"
    assert failed["state"] == "idle" and failed["plays"] == []
    closed = steps["closed"]
    assert closed["calls"][1]["aborted"], "closing the dialog cancels the in-flight sample"
    assert closed["plays"] == [] and closed["state"] == "idle"
    assert closed["note"] == "", "the next sample (or a close) clears the previous sample's error note"


def test_voice_route_has_no_test_button_or_replay_panel_and_the_toggle_is_a_real_button() -> None:
    html = gateway_console_html()
    source = _console_script()
    # The toggle sits in the Voice row, is a native button (Space toggles it).
    row = re.search(r'<label id="modal-default-voice-label".*?</label>', html, flags=re.S)
    assert row, "voice row missing"
    assert '<select id="modal-default-voice">' in row.group(0)
    assert '<button id="modal-default-voice-play" type="button"' in row.group(0)
    assert '$("modal-default-voice").onchange = () => voicePreviewOnSelect();' in source
    assert '$("modal-default-voice-play").onclick = () => voicePreviewToggle();' in source
    # Test is a text-route probe only now; the voice lane no longer renders a panel.
    testable = _slice_function(source, "defaultRowTestable")
    assert '"output.voice"' not in testable
    assert "voice/tts" not in _slice_function(source, "testDefault")
    # Every route change drops the old sample.
    for name in ("closeDefaultModal", "openDefaultModal", "reloadDefaultModalCatalogs"):
        assert "voicePreviewReset();" in _slice_function(source, name), name
