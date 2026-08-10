"""Offline resilience of the web console (2026-08-02 operator incident).

The operator could configure the gateway offline through the console-tui but
not through the web console: the capability-defaults model select sat on
"Loading models..." forever. Reproduced in the live page, three defects:

  1. `api()` used a bare `fetch()` — no timeout, so a blackholed upstream
     (packets dropped, connect never completes) left the promise pending
     forever and the "Loading models..." label became permanent.
  2. `fetchDefaultModels()` cached the in-flight PROMISE, so a REJECTED
     promise stayed cached and re-threw forever WITHOUT touching the network.
     Measured: the retry issued no request at all, and restoring the network
     did not heal it — only a full page reload did.
  3. The loaders that set "Loading..." had no terminal failure state, and
     `$("modal-default-provider").onchange` did not guard the call at all.

The console-tui never had this asymmetry: its ureq agents carry
timeout_connect(5s)/timeout_read(60s) plus a slow_agent at 300s
(abstractgateway/console-tui/src/api.rs). These tests pin the web console to
the same contract.

Kept in its own module deliberately: test_gateway_console.py is a large shared
harness, and these assertions are about one incident.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from abstractgateway.console import gateway_console_html


def _console_script() -> str:
    scripts = re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S)
    assert scripts, "console served no inline <script>"
    return "\n".join(scripts)


def _slice_function(source: str, name: str) -> str:
    """Return `[async] function <name>(...) { ... }` verbatim, brace-matched.

    Tests the SHIPPED source of the function rather than a copy of it, so the
    behavioural checks below cannot drift away from what the console serves.
    """
    match = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", source)
    assert match, f"{name}() not found in the console JavaScript"
    start = match.start()
    # Skip the parameter list before hunting the body brace — a default like
    # `options = {}` would otherwise be mistaken for the function body.
    paren = source.index("(", start)
    depth = 0
    for i in range(paren, len(source)):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                paren = i
                break
    depth = 0
    for i in range(source.index("{", paren), len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"unbalanced braces slicing {name}")


# --------------------------------------------------------------------------
# 1. api() timeout wiring
# --------------------------------------------------------------------------


def test_api_declares_tui_parity_timeout_budgets() -> None:
    source = _console_script()
    # Mirrors console-tui's two ureq agents: 60s normal, 300s slow.
    assert "const API_TIMEOUT_MS = 60000;" in source
    assert "const API_SLOW_TIMEOUT_MS = 300000;" in source
    api = _slice_function(source, "api")
    # A budget is always chosen, and `slow` selects the long tier.
    assert "slow ? API_SLOW_TIMEOUT_MS : API_TIMEOUT_MS" in api
    # The abort is wired into the actual request...
    assert "signal: controller.signal" in api
    # ...and the bound is a raced deadline, so it still fires where
    # AbortController is unavailable (the promise MUST settle either way).
    assert "Promise.race" in api
    assert "setTimeout(" in api and "clearTimeout(" in api
    # `slow`/`timeoutMs` are ours and must never reach fetch's request init.
    assert "const { slow = false, timeoutMs, ...init } = options;" in api


def test_api_names_the_failure_class_for_the_operator() -> None:
    api = _slice_function(_console_script(), "api")
    # A wedged gateway and a down one are different problems; the console-tui
    # types them (ApiErrorKind::Unreachable) and so must the console.
    assert "Request timed out after" in api
    assert "Gateway unreachable:" in api


def test_long_running_routes_opt_into_the_slow_budget() -> None:
    """Bounding every call must not strangle calls that legitimately run long."""
    source = _console_script()
    for route in (
        "/voice/tts`, { slow: true,",          # TTS synthesis
        '"/api/gateway/models/download", { slow: true,',  # download kickoff
        '"/api/gateway/sandbox/generate", { slow: true,',  # media generation
    ):
        assert route in source, route
    # The entity chat turn is a real LLM round trip.
    turn = source[source.index("/turn`, {") : source.index("/turn`, {") + 200]
    assert "slow: true" in turn


def test_api_bounds_a_blackholed_request() -> None:
    """A fetch that never settles must still reject — the core of the incident."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")

    api_src = _slice_function(_console_script(), "api")
    harness = f"""
{api_src}
const API_TIMEOUT_MS = 60000;
const API_SLOW_TIMEOUT_MS = 300000;
function csrf() {{ return ""; }}
const results = [];

// BLACKHOLE: never settles, never errors (TEST-NET-3 shaped).
globalThis.fetch = () => new Promise(() => {{}});
const t0 = Date.now();
api("/api/gateway/discovery/providers/lmstudio/models", {{ timeoutMs: 120 }}).then(
  () => results.push({{ step: "blackhole", outcome: "resolved" }}),
  (e) => results.push({{ step: "blackhole", outcome: "rejected", ms: Date.now() - t0, err: String(e.message) }}),
).then(async () => {{
  // REFUSED: instant network error (offline / nothing listening).
  globalThis.fetch = () => Promise.reject(new TypeError("Failed to fetch"));
  await api("/x", {{ timeoutMs: 5000 }}).then(
    () => results.push({{ step: "refused", outcome: "resolved" }}),
    (e) => results.push({{ step: "refused", outcome: "rejected", err: String(e.message) }}),
  );
  // A healthy call must still pass straight through.
  globalThis.fetch = async () => ({{ ok: true, status: 200, text: async () => JSON.stringify({{ models: ["m1"] }}) }});
  await api("/y").then(
    (d) => results.push({{ step: "healthy", outcome: "resolved", models: d.models }}),
    (e) => results.push({{ step: "healthy", outcome: "rejected", err: String(e.message) }}),
  );
  console.log(JSON.stringify(results));
}});
"""
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as f:
        f.write(harness)
        path = Path(f.name)
    try:
        proc = subprocess.run([node, str(path)], capture_output=True, text=True, check=False)
    finally:
        path.unlink(missing_ok=True)

    assert proc.returncode == 0, proc.stderr
    steps = {r["step"]: r for r in json.loads(proc.stdout.strip().splitlines()[-1])}

    # The blackhole is BOUNDED — this is the bug that stuck the select forever.
    assert steps["blackhole"]["outcome"] == "rejected"
    assert "timed out after" in steps["blackhole"]["err"]
    assert steps["blackhole"]["ms"] < 5000, steps["blackhole"]

    # A refused connection is named, not surfaced as the browser's opaque text.
    assert steps["refused"]["outcome"] == "rejected"
    assert steps["refused"]["err"].startswith("Gateway unreachable:")

    # Bounding must not break the happy path.
    assert steps["healthy"]["outcome"] == "resolved"
    assert steps["healthy"]["models"] == ["m1"]


# --------------------------------------------------------------------------
# 2. no rejection caching
# --------------------------------------------------------------------------


def test_fetch_default_models_evicts_a_failed_lookup() -> None:
    """A rejection must never be cached: the retry has to hit the network.

    Live-page measurement before the fix: call 1 failed, and the retry plus a
    fully restored network both re-threw INSTANTLY with the fetch count frozen
    at 1. The select stayed "Loading models..." until a page reload.
    """
    fn = _slice_function(_console_script(), "fetchDefaultModels")
    # The failure branch drops the key it just wrote...
    assert "state.providerModels.delete(cacheKey)" in fn
    assert "promise.catch(" in fn
    # ...guarded so a slow failure cannot evict a newer in-flight entry.
    assert "state.providerModels.get(cacheKey) === promise" in fn
    # ...and the await goes through the LOCAL handle: re-reading the cache
    # after eviction yields undefined and would downgrade the failure to
    # "no models discovered" instead of surfacing it.
    assert "const cached = await entry;" in fn
    assert "await state.providerModels.get(cacheKey)" not in fn


def test_provider_and_voice_caches_never_store_a_promise() -> None:
    """The sibling caches await BEFORE writing — keep it that way."""
    source = _console_script()
    for name in ("fetchDefaultProviders", "fetchDefaultVoices"):
        fn = _slice_function(source, name)
        assert "const payload = await api(" in fn, name
        assert ", promise)" not in fn, name


# --------------------------------------------------------------------------
# 3. every "Loading..." has a terminal failure state
# --------------------------------------------------------------------------


def test_load_default_models_has_a_terminal_failure_state() -> None:
    fn = _slice_function(_console_script(), "loadDefaultModels")
    assert 'emptyLabel: "Loading models..."' in fn
    # The label's owner paints the end state...
    assert "} catch (e) {" in fn
    assert '"Model discovery failed"' in fn
    assert "model discovery failed:" in fn
    # ...and rethrows, so openDefaultModal's configured-model fallback still runs.
    assert "throw e;" in fn


def test_load_default_voices_has_a_terminal_failure_state() -> None:
    fn = _slice_function(_console_script(), "loadDefaultVoices")
    assert 'emptyLabel: "Loading voices..."' in fn
    assert '"Voice discovery failed"' in fn
    assert "throw e;" in fn


def test_provider_change_handler_guards_both_loaders() -> None:
    """The genuinely unguarded path: switching provider while offline.

    openDefaultModal already wrapped its call; this handler did not, so a
    failed model lookup propagated and the voice select was never degraded.
    """
    handler = _slice_function(_console_script(), "reloadDefaultModalCatalogs")
    assert "try {" in handler
    assert "await loadDefaultModels(" in handler
    assert ".catch(" in handler


def test_the_free_text_provider_lane_is_a_live_control() -> None:
    """A lane only the SAVE reads is a field that accepts typing and does nothing.

    Every loader read the <select>, so on an unconfigured row a typed provider
    triggered no model discovery: the model lane never opened, and the save then
    refused for want of a model with no field to type one into. That is the
    fresh-install-offline row and the scene3d row — precisely the cases the lane
    was added for.
    """
    source = _console_script()
    # Both provider controls drive the same reload...
    assert '$("modal-default-provider").onchange = reloadDefaultModalCatalogs;' in source
    assert '$("modal-default-provider-custom").onchange = reloadDefaultModalCatalogs;' in source
    # ...and that reload asks for the ACTIVE provider, not the select's value.
    reload_fn = _slice_function(source, "reloadDefaultModalCatalogs")
    assert "activeDefaultProvider()" in reload_fn
    assert '$("modal-default-provider").value' not in reload_fn, (
        "the reload still reads the select directly and so ignores the typed provider"
    )
    # The typed MODEL is a model choice too, so the voice catalog follows it.
    assert '$("modal-default-model-custom").onchange' in source
    # openDefaultModal and saveDefault ask the same question as the loaders.
    for name in ("openDefaultModal", "saveDefault"):
        body = _slice_function(source, name)
        assert "activeDefaultProvider()" in body, f"{name} does not use the shared accessor"


def test_a_modality_with_no_discovery_says_so_instead_of_borrowing_text() -> None:
    """scene3d has no discovery endpoint, and the text catalog is not a stand-in.

    Falling through to `textCatalog` offered TEXT providers and TEXT models for
    a 3D route, and — because the lanes open only on an empty catalog — held
    both lanes SHUT exactly when some text provider happened to be reachable.
    The row this console just stopped hiding would have been misconfigurable
    rather than configurable.
    """
    source = _console_script()
    catalog = _slice_function(source, "defaultCatalogForRow")
    assert "scene3d" in catalog, "scene3d still falls through to the text catalog"
    assert "discovery: false" in catalog
    # ...and the fetchers honour the declaration instead of requesting "".
    for name in ("fetchDefaultProviders", "fetchDefaultModels"):
        assert "catalog.discovery === false" in _slice_function(source, name), name


# --------------------------------------------------------------------------
# 4. The free-text lanes: one contract, and the save actually obeys it
# --------------------------------------------------------------------------


def _node(script: str) -> list:
    """Run `script` under node and return the JSON array it printed last."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for JavaScript behaviour checking")
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as f:
        f.write(script)
        path = Path(f.name)
    try:
        proc = subprocess.run([node, str(path)], capture_output=True, text=True, check=False)
    finally:
        path.unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_both_free_text_lanes_go_through_one_contract() -> None:
    """Provider and model degrade through the SAME helper, not two copies.

    As two copies they had already drifted: the model lane prefilled only when
    the value was truthy, the provider lane always; and the visibility test that
    decides which control the save reads was written out twice, in a third
    place. One helper for the write, one for the read, no other spelling.
    """
    source = _console_script()
    for lane in ("modal-default-provider-custom", "modal-default-model-custom"):
        assert f'setCustomLane("{lane}"' in source, f"{lane} does not use the shared setter"
        assert f'customLaneValue("{lane}")' in source, f"{lane} is never read back"
    # Exactly one place decides "is this lane live", and it is the reader that
    # pairs with the writer above. A second hand-rolled visibility test is how
    # the two lanes drifted the first time.
    assert source.count('classList.contains("hidden")') == 1, (
        "the lane-visibility test is duplicated again — centralise it in customLaneValue"
    )


def _save_default_harness(scenario_js: str) -> str:
    """The SHIPPED saveDefault() driven against a fake DOM.

    Only saveDefault's collaborators are stubbed; the value-selection helpers it
    is being tested WITH (`customLaneValue`, `defaultVoiceValue`) are the real
    shipped source, because they are half of the behaviour under test.
    """
    source = _console_script()
    return f"""
{_slice_function(source, "saveDefault")}
{_slice_function(source, "customLaneValue")}
{_slice_function(source, "activeDefaultProvider")}
{_slice_function(source, "activeDefaultModel")}
{_slice_function(source, "defaultVoiceValue")}
{_slice_function(source, "textValue")}

const els = new Map();
function $(id) {{
  if (!els.has(id)) els.set(id, {{
    value: "", disabled: false, className: "", textContent: "",
    classList: {{ contains: (n) => String(els.get(id).className).split(" ").includes(n) }},
  }});
  return els.get(id);
}}
const puts = [];
async function api(path, options = {{}}) {{
  if (options.method === "PUT") puts.push({{ path, body: JSON.parse(options.body) }});
  return {{ routes: [] }};
}}
const state = {{ activeDefaultRow: null, defaultModalPrefill: null }};
// openDefaultModal's prefill, as the save sees it: what was on screen when the
// modal opened, so an untouched field can be told from an edited one.
function prefillFrom(row) {{
  state.defaultModalPrefill = {{
    base_url: $("modal-default-base-url").value,
    options: $("modal-default-options").value,
    voice: defaultVoiceValue(row),
  }};
}}
function defaultRowKindModality(row) {{
  return {{ kind: row.kind, modality: row.modality, task: row.task || "" }};
}}
function isTextGenerationDefault(row) {{ return row.key === "output.text" || row.key === "input.text"; }}
function isVoiceOutputDefault(row) {{ return row.modality === "voice" && row.kind === "output"; }}
function closeDefaultModal() {{}}
async function renderDefaults() {{}}

const results = [];
{scenario_js}
console.log(JSON.stringify(results));
"""


def test_save_refuses_options_that_parse_but_are_not_an_object() -> None:
    """Valid JSON is not the bar — the store holds a dict.

    `[1,2]`, `"text"` and `null` all parse. Sent as-is they would either be
    rejected downstream with a stack-shaped error or, worse, replace a route's
    options with a non-dict. The refusal has to happen where the operator can
    still see what they typed.
    """
    scenario = """
state.activeDefaultRow = { key: "input.text", kind: "input", modality: "text" };
$("modal-default-provider").value = "lmstudio";
$("modal-default-model").value = "qwen3";
$("modal-default-provider-custom").className = "hidden";
$("modal-default-model-custom").className = "hidden";
for (const text of ["[1, 2]", '"just text"', "null", "17", "{not json"]) {
  $("modal-default-options").value = text;
  await saveDefault();
  results.push({ text, message: $("default-modal-message").textContent, puts: puts.length });
}
"""
    for row in _node(_save_default_harness(scenario)):
        assert row["puts"] == 0, f"{row['text']} reached the store: {row}"
        assert "JSON" in row["message"] or "object" in row["message"], row


def test_an_open_lane_outranks_the_select_and_a_shut_one_is_ignored() -> None:
    """The visibility contract, end to end, on the field that carries it.

    Offline the operator types a provider the catalog never offered; that value
    must win. When discovery later succeeds the lane is shut, and the value left
    inside it must NOT outrank the pick the operator then made from the select —
    which is exactly what reading the input unconditionally would do.
    """
    scenario = """
state.activeDefaultRow = { key: "output.scene3d", kind: "output", modality: "scene3d" };
$("modal-default-options").value = "";
// Lane OPEN (offline): the typed value is the only real one.
$("modal-default-provider-custom").className = "";
$("modal-default-provider-custom").value = "  mlx-gen  ";
$("modal-default-model-custom").className = "";
$("modal-default-model-custom").value = "my-local-scene";
$("modal-default-provider").value = "";
$("modal-default-model").value = "";
await saveDefault();
results.push({ step: "open", put: puts[puts.length - 1] });
// Lane SHUT (discovery healthy again) with the same text still inside it.
$("modal-default-provider-custom").className = "hidden";
$("modal-default-model-custom").className = "hidden";
$("modal-default-provider").value = "ollama";
$("modal-default-model").value = "granite";
await saveDefault();
results.push({ step: "shut", put: puts[puts.length - 1] });
"""
    steps = {r["step"]: r["put"] for r in _node(_save_default_harness(scenario))}
    assert steps["open"]["path"] == "/api/gateway/config/capability-defaults/output/scene3d"
    assert steps["open"]["body"]["provider"] == "mlx-gen", "the typed provider was not trimmed/used"
    assert steps["open"]["body"]["model"] == "my-local-scene"
    assert steps["shut"]["body"]["provider"] == "ollama", "a hidden lane outranked the select"
    assert steps["shut"]["body"]["model"] == "granite"


def test_an_offline_save_cannot_unset_a_voice_the_picker_could_not_show() -> None:
    """A disabled control expresses no intent, so it must not be read as one.

    Offline, voice discovery fails and the picker degrades to a disabled, empty
    select. The stored `voice` is hidden from the JSON box (the picker owns that
    key), so reading "" off the dead select deleted it from both places at once:
    an operator editing the base URL of a working route silently unset its
    voice, with nothing on screen to show it happened.
    """
    scenario = """
const row = {
  key: "output.voice", kind: "output", modality: "voice",
  options: { voice: "aria", language: "fr" },
};
state.activeDefaultRow = row;
$("modal-default-provider").value = "abstractvoice";
$("modal-default-model").value = "supertonic";
$("modal-default-provider-custom").className = "hidden";
$("modal-default-model-custom").className = "hidden";
// The JSON box as openDefaultModal prefills it: voice/profile stripped out,
// because the picker owns them.
$("modal-default-options").value = JSON.stringify({ language: "fr" });
$("modal-default-voice").value = "aria";
prefillFrom(row);

// Offline: discovery failed, so the select is empty and DISABLED. The JSON box
// is edited, so options travel — and must carry the voice the picker hid.
$("modal-default-voice").disabled = true;
$("modal-default-voice").value = "";
$("modal-default-options").value = JSON.stringify({ language: "de" });
await saveDefault();
results.push({ step: "offline", body: puts[puts.length - 1].body });

// Online: the picker is a real control again and owns the key.
$("modal-default-voice").disabled = false;
$("modal-default-voice").value = "coral";
await saveDefault();
results.push({ step: "online", body: puts[puts.length - 1].body });

// ...including clearing it, which is a deliberate act on a live control.
$("modal-default-voice").value = "";
await saveDefault();
results.push({ step: "cleared", body: puts[puts.length - 1].body });
"""
    steps = {r["step"]: r["body"] for r in _node(_save_default_harness(scenario))}
    assert steps["offline"]["options"] == {"language": "de", "voice": "aria"}, (
        "an offline save wiped a voice the operator never touched"
    )
    assert steps["online"]["options"] == {"language": "de", "voice": "coral"}
    assert steps["cleared"]["options"] == {"language": "de"}, (
        "a live picker set to 'no voice' must still be able to clear it"
    )


def test_an_untouched_base_url_or_options_is_never_echoed_back() -> None:
    """Prefilled from the last GRID RENDER, so echoing them rolls values back.

    The modal never re-GETs the row it opened. Scenario: the tab is open with
    base_url ``:1234``; someone runs ``abstractcore config --base-url :1235``;
    the operator changes only the model and saves. Naming the stale ``:1234``
    wins the server-side merge and silently undoes ``:1235``. Same shape for a
    COVERED input.video row, whose prefill is input.text's inherited values --
    AbstractCore refuses to persist those server-side, and an unconditional
    client send did it anyway. Editing must still be able to CLEAR: "" is a
    value the operator can express, because "" differs from what they were shown.
    """
    scenario = """
const row = {
  key: "input.text", kind: "input", modality: "text",
  base_url: "http://localhost:1234/v1", options: { temperature: 0.2 },
};
state.activeDefaultRow = row;
$("modal-default-provider").value = "lmstudio";
$("modal-default-provider-custom").className = "hidden";
$("modal-default-model-custom").className = "hidden";
$("modal-default-base-url").value = "http://localhost:1234/v1";
$("modal-default-options").value = JSON.stringify({ temperature: 0.2 }, null, 2);
prefillFrom(row);

// The operator edits ONLY the model.
$("modal-default-model").value = "qwen3-next";
await saveDefault();
results.push({ step: "model-only", body: puts[puts.length - 1].body });

// Now they deliberately empty both fields — that IS an edit, and must clear.
$("modal-default-base-url").value = "";
$("modal-default-options").value = "";
await saveDefault();
results.push({ step: "cleared", body: puts[puts.length - 1].body });

// And a real new value travels.
$("modal-default-base-url").value = "http://localhost:9999/v1";
await saveDefault();
results.push({ step: "retargeted", body: puts[puts.length - 1].body });
"""
    steps = {r["step"]: r["body"] for r in _node(_save_default_harness(scenario))}
    assert steps["model-only"]["model"] == "qwen3-next"
    assert "base_url" not in steps["model-only"], (
        "an untouched base_url was echoed back and can roll a newer value back"
    )
    assert "options" not in steps["model-only"], "untouched options were echoed back"
    assert steps["cleared"]["base_url"] == "", "an emptied base_url must clear the override"
    assert steps["cleared"]["options"] == {}, "emptied options must clear the stored dict"
    assert steps["retargeted"]["base_url"] == "http://localhost:9999/v1"


# --------------------------------------------------------------------------
# 5. A late resolve must not paint a modal that moved on
# --------------------------------------------------------------------------


def test_a_late_model_lookup_cannot_paint_another_route() -> None:
    """The 60s budget outlives a close-and-reopen; the paint must not.

    Offline the operator opens a slow route, closes it, and opens another. When
    the first lookup finally settles it would repaint the open modal with the
    FIRST route's catalog — and `state.activeDefaultRow` is what saveDefault
    writes through, so the next Save would put one route's model on another
    route's key. Row identity is the guard.
    """
    source = _console_script()
    harness = f"""
{_slice_function(source, "loadDefaultModels")}
{_slice_function(source, "setCustomLane")}
{_slice_function(source, "defaultModalMoved")}

const els = new Map();
function $(id) {{
  if (!els.has(id)) els.set(id, {{ value: "", disabled: false, className: "", textContent: "" }});
  return els.get(id);
}}
$("modal-default-model").classList = {{ toggle: () => {{}} }};
$("modal-default-model-custom").classList = {{ toggle: () => {{}} }};
const painted = [];
function setSelectOptions(select, values, opts = {{}}) {{
  painted.push({{ values, emptyLabel: opts.emptyLabel }});
}}
function defaultCatalogForRow() {{ return {{ scope: "text generation", emptyModels: "none" }}; }}
const rowA = {{ key: "output.video", kind: "output", modality: "video" }};
const rowB = {{ key: "input.text", kind: "input", modality: "text" }};
const state = {{ activeDefaultRow: rowA }};

let release;
const slow = new Promise((resolve) => {{ release = resolve; }});
async function fetchDefaultModels() {{ return slow; }}

const results = [];
const inFlight = loadDefaultModels("mlx-gen", "wan2.2", rowA);
// The operator closes the modal and opens a different route...
state.activeDefaultRow = rowB;
release(["wan2.2", "flux"]);
await inFlight;
results.push({{ paintsAfterMove: painted.filter((p) => p.values && p.values.includes("flux")).length }});

// ...and the same call on the STILL-OPEN row must paint normally.
state.activeDefaultRow = rowB;
await loadDefaultModels("ollama", "granite", rowB);
results.push({{ paintsWhenCurrent: painted.filter((p) => p.values && p.values.includes("flux")).length }});
console.log(JSON.stringify(results));
"""
    moved, current = _node(harness)
    assert moved["paintsAfterMove"] == 0, "a stale lookup repainted the reopened modal"
    assert current["paintsWhenCurrent"] == 1, "the guard also blocked the legitimate paint"
