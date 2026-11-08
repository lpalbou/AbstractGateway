"""Console model controls follow the selected model (operator rule 2026-11-07).

"MTP should only ever be selected for MTP-capable models. When changing a model from the dropdown,
always go back to default for reasoning and MTP, in every app." The console's three hand-written
controls (Sandbox, the route-defaults window, the entity create form) are driven here through their
SHIPPED functions under node, against a small fake DOM and a fake capability endpoint.
"""
from __future__ import annotations

import json

from test_gateway_console_offline import _console_script, _node, _save_default_harness, _slice_function

_SHARED = ("mtpCard", "mtpCardNote", "gateMtpSelect", "showModelOptionNote", "modelReasoningLevels")

_FAKE_DOM = r"""
const MTP_UNKNOWN = "MTP is offered when the gateway confirms this model supports it.";
const MTP_UNSUPPORTED = "This model does not support MTP.";
const MTP_CHECKING = "Checking whether this model supports MTP…";
const REASONING_UNAVAILABLE = "Reasoning choices appear when the gateway can describe the selected model.";
function el(tag) {
  const node = {
    tag, value: "", disabled: false, hidden: false, children: [], _text: "",
    get textContent() { return this._text + this.children.map((c) => typeof c === "string" ? c : c.textContent).join(""); },
    set textContent(v) { this._text = String(v); this.children = []; },
    append(...items) { for (const i of items) this.children.push(i); },
    classList: { _s: new Set(), add(n) { this._s.add(n); }, remove(n) { this._s.delete(n); }, contains(n) { return this._s.has(n); },
                 toggle(n, on) { on ? this._s.add(n) : this._s.delete(n); } },
  };
  return node;
}
function selectWith(values) {
  const s = el("select");
  s.append(...values.map((v) => { const o = el("option"); o.value = v; return o; }));
  Object.defineProperty(s, "options", { get() { return this.children.filter((c) => c.tag === "option"); } });
  let current = "";
  Object.defineProperty(s, "value", {
    get() { return current; },
    set(v) { current = this.options.some((o) => o.value === v) ? v : ""; },
  });
  s.querySelector = (sel) => s.options.find((o) => sel.includes(`"${o.value}"`)) || null;
  return s;
}
const document = { createElement: (tag) => el(tag) };
const els = new Map();
function $(id) { if (!els.has(id)) els.set(id, el("div")); return els.get(id); }
let cards = {};
const asked = [];
async function api(path) {
  const q = new URLSearchParams(path.split("?")[1] || "");
  const key = `${q.get("provider")}/${q.get("model_name")}`;
  asked.push(key);
  const card = cards[key];
  if (card instanceof Error) throw card;
  return card || {};
}
const state = {};
const results = [];
const opts = (s) => s.options.map((o) => ({ value: o.value, disabled: o.disabled }));
"""

MTP_OPTIONS = ["", "off", "2", "3", "4", "5"]
SUPPORTED = {"execution": {"speculation": {"supported": True, "ready": True, "supported_depths": [2, 3]}}}
UNSUPPORTED = {"execution": {"speculation": {"supported": False, "ready": False, "reason": "native_mtp_backend_unavailable",
                                             "message": None, "supported_depths": []}}}


def _script(*functions: str, body: str) -> str:
    source = _console_script()
    sliced = "\n".join(_slice_function(source, name) for name in (*_SHARED, *functions))
    return f"{_FAKE_DOM}\n{sliced}\n{body}\nconsole.log(JSON.stringify(results));\n"


def test_the_shared_words_are_the_kits() -> None:
    source = _console_script()
    for words in ('const MTP_UNKNOWN = "MTP is offered when the gateway confirms this model supports it.";',
                  'const MTP_UNSUPPORTED = "This model does not support MTP.";',
                  'const MTP_CHECKING = "Checking whether this model supports MTP…";',
                  'const REASONING_UNAVAILABLE = "Reasoning choices appear when the gateway can describe the selected model.";'):
        assert words in source, words


def test_sandbox_mtp_is_disabled_including_off_for_a_model_without_mtp_and_says_why() -> None:
    body = f"""
els.set("sandbox-speculation", selectWith({json.dumps(MTP_OPTIONS)}));
els.set("sandbox-reasoning", selectWith(["", "low", "high"]));
cards = {{"openai/gpt": {json.dumps(UNSUPPORTED)}, "mlx/qwen": {json.dumps(SUPPORTED)}}};
await refreshSandboxSpeculationSupport({{provider: "openai", model: "gpt"}});
const s = $("sandbox-speculation");
results.push({{disabled: s.disabled, options: opts(s), note: $("sandbox-speculation-note").textContent}});
await refreshSandboxSpeculationSupport({{provider: "mlx", model: "qwen"}});
results.push({{disabled: s.disabled, options: opts(s), note: $("sandbox-speculation-note").textContent}});
"""
    unsupported, supported = _node(_script("refreshSandboxSpeculationSupport", body=body))
    assert unsupported["disabled"] is True
    assert [o["value"] for o in unsupported["options"] if not o["disabled"]] == [""], "Off must not be offered either"
    assert unsupported["note"].startswith("This model does not support MTP.")
    assert "Technical details" in unsupported["note"] and "native_mtp_backend_unavailable" in unsupported["note"]
    assert supported["disabled"] is False
    assert [o["value"] for o in supported["options"] if not o["disabled"]] == ["", "off", "2", "3"]
    assert supported["note"] == ""


def test_sandbox_model_change_puts_reasoning_and_mtp_back_on_the_default() -> None:
    body = f"""
els.set("sandbox-speculation", selectWith({json.dumps(MTP_OPTIONS)}));
els.set("sandbox-reasoning", selectWith(["", "low", "high"]));
cards = {{"mlx/a": {json.dumps(SUPPORTED)}, "mlx/b": {json.dumps(SUPPORTED)}}};
await refreshSandboxSpeculationSupport({{provider: "mlx", model: "a"}});
$("sandbox-speculation").value = "2"; $("sandbox-reasoning").value = "high";
await refreshSandboxSpeculationSupport({{provider: "mlx", model: "a"}});
results.push({{same: [$("sandbox-reasoning").value, $("sandbox-speculation").value]}});
await refreshSandboxSpeculationSupport({{provider: "mlx", model: "b"}});
results.push({{other: [$("sandbox-reasoning").value, $("sandbox-speculation").value]}});
"""
    same, other = _node(_script("refreshSandboxSpeculationSupport", body=body))
    assert same["same"] == ["high", "2"], "the same model keeps what was chosen"
    assert other["other"] == ["", ""], "another model resets reasoning and MTP"


def test_route_window_model_change_resets_both_and_the_save_drops_the_old_models_mtp() -> None:
    scenario = """
const row = {key: "input.text", kind: "input", modality: "text", provider: "mlx", model: "qwen-mtp",
             reasoning: "high", options: {temperature: 0.2, speculation: {mode: "native_mtp", num_draft_tokens: 2, require_acceleration: true}}};
state.activeDefaultRow = row;
$("modal-default-provider").value = "mlx";
$("modal-default-provider-custom").className = "hidden";
$("modal-default-model-custom").className = "hidden";
$("modal-default-options").value = JSON.stringify({temperature: 0.2}, null, 2);
prefillFrom(row);
state.defaultModalPrefill.speculation = "2";
$("modal-default-reasoning").value = "high";
$("modal-default-speculation").value = "2";
// Same model, untouched: the stored MTP is not named (no rollback of newer Core settings).
$("modal-default-model").value = "qwen-mtp";
await saveDefault(); results.push(puts.at(-1).body);
// The operator picks another model: the window resets reasoning and MTP...
$("modal-default-model").value = "gpt-oss";
resetDefaultModelOptions();
results.push({reasoning: $("modal-default-reasoning").value, speculation: $("modal-default-speculation").value});
// ...and the save clears the stored MTP instead of carrying it onto the new model.
await saveDefault(); results.push(puts.at(-1).body);
// Even when the selects were not reset (an older tab), a route change never keeps the stored MTP.
$("modal-default-speculation").value = "";
$("modal-default-model").value = "another";
await saveDefault(); results.push(puts.at(-1).body);
"""
    source = _console_script()
    script = (_slice_function(source, "speculationFromChoice") + _slice_function(source, "resetDefaultModelOptions")
              + _save_default_harness(scenario))
    unchanged, reset, moved, moved_again = _node(script)
    assert "options" not in unchanged
    assert reset == {"reasoning": "", "speculation": ""}
    assert moved["model"] == "gpt-oss" and moved["reasoning"] == ""
    assert moved["options"] == {"temperature": 0.2}, "the old model's MTP must not be saved for the new model"
    assert "speculation" not in moved_again["options"]


def test_route_window_gates_depths_on_the_card_and_keeps_a_stored_choice_while_unknown() -> None:
    body = f"""
els.set("modal-default-speculation", selectWith({json.dumps(MTP_OPTIONS)}));
state.activeDefaultRow = {{key: "input.text"}};
function isTextGenerationDefault() {{ return true; }}
let route = ["mlx", "qwen"];
function activeDefaultProvider() {{ return route[0]; }}
function activeDefaultModel() {{ return route[1]; }}
cards = {{"mlx/qwen": {json.dumps(SUPPORTED)}, "openai/gpt": {json.dumps(UNSUPPORTED)}, "remote/x": new Error("Core unreachable")}};
const s = $("modal-default-speculation");
s.value = "3";
await refreshDefaultSpeculationSupport();
results.push({{value: s.value, disabled: s.disabled, enabled: opts(s).filter((o) => !o.disabled).map((o) => o.value)}});
route = ["remote", "x"]; s.value = "3";
await refreshDefaultSpeculationSupport();
results.push({{value: s.value, disabled: s.disabled, note: $("modal-default-speculation-status").textContent}});
route = ["openai", "gpt"]; s.value = "3";
await refreshDefaultSpeculationSupport();
results.push({{value: s.value, disabled: s.disabled, note: $("modal-default-speculation-status").textContent}});
"""
    supported, unknown, unsupported = _node(_script("refreshDefaultSpeculationSupport", body=body))
    assert supported == {"value": "3", "disabled": False, "enabled": ["", "off", "2", "3"]}
    assert unknown["value"] == "3" and unknown["disabled"] is True, "an unreadable card must not erase the stored policy"
    assert unknown["note"].startswith("MTP is offered when the gateway confirms this model supports it.")
    assert unsupported["value"] == "" and unsupported["disabled"] is True
    assert unsupported["note"].startswith("This model does not support MTP.")


def test_entity_create_offers_only_the_models_levels_and_resets_on_model_change() -> None:
    body = """
els.set("entity-new-thinking", selectWith([""]));
els.set("entity-new-provider", selectWith(["", "mlx", "openai"]));
els.set("entity-new-model", selectWith(["", "qwen", "plain", "remote"]));
function _fillSelect(sel, options) {
  const prev = sel.value;
  sel.children = [];
  sel.append(...options.map((o) => { const n = el("option"); n.value = o.value; n.textContent = o.label; return n; }));
  sel.value = prev;
}
cards = {
  "mlx/qwen": {capabilities: {reasoning_levels: ["low", "medium", "high"]}},
  "mlx/plain": {capabilities: {thinking_support: false}},
  "mlx/remote": new Error("discovery unavailable"),
};
const label = $("entity-new-thinking-label"), sel = $("entity-new-thinking");
label.classList.add("hidden");
const snap = () => ({hidden: label.classList.contains("hidden"), disabled: sel.disabled, value: sel.value,
                     options: sel.options.map((o) => o.value), note: $("entity-new-thinking-note").textContent});
await refreshEntityReasoning(); results.push(snap());
$("entity-new-provider").value = "mlx"; $("entity-new-model").value = "qwen";
await refreshEntityReasoning(); results.push(snap());
sel.value = "high";
$("entity-new-model").value = "plain";
await refreshEntityReasoning(); results.push(snap());
$("entity-new-model").value = "qwen";
await refreshEntityReasoning(); results.push(snap());
$("entity-new-model").value = "remote";
await refreshEntityReasoning(); results.push(snap());
"""
    default, qwen, plain, back, remote = _node(_script("refreshEntityReasoning", body=body))
    assert default["hidden"] is True and default["options"] == [""], "no model chosen: nothing to choose"
    assert qwen["hidden"] is False and qwen["options"] == ["", "low", "medium", "high"] and qwen["value"] == ""
    assert plain["hidden"] is True and plain["value"] == "", "a model without levels offers none"
    assert back["value"] == "", "a model change never carries the previous level"
    assert remote["hidden"] is False and remote["disabled"] is True
    assert remote["note"].startswith("Reasoning choices appear when the gateway can describe the selected model.")


def test_entity_create_wires_both_dropdowns_to_the_reset() -> None:
    source = _console_script()
    assert '$("entity-new-provider").onchange = () => loadModelsForProvider($("entity-new-provider").value).then(refreshEntityReasoning);' in source
    assert '$("entity-new-model").onchange = () => refreshEntityReasoning();' in source
    model_change = source[source.index('$("modal-default-model").onchange = () => {'):][:200]
    assert "resetDefaultModelOptions();" in model_change
    assert "resetDefaultModelOptions();" in _slice_function(source, "reloadDefaultModalCatalogs")


def test_route_window_save_never_carries_a_stored_custom_mtp_onto_another_model() -> None:
    """The select still shows the old model's choice (here a custom policy no select option
    spells): a route change alone must not save it for the new model."""
    scenario = """
const row = {key: "input.text", kind: "input", modality: "text", provider: "mlx", model: "qwen-mtp",
             options: {temperature: 0.2, speculation: {mode: "native_mtp", num_draft_tokens: 7, drafter: "own/head"}}};
state.activeDefaultRow = row;
$("modal-default-provider").value = "mlx";
$("modal-default-provider-custom").className = "hidden";
$("modal-default-model-custom").className = "hidden";
$("modal-default-options").value = JSON.stringify({temperature: 0.2}, null, 2);
prefillFrom(row);
state.defaultModalPrefill.speculation = "custom";
$("modal-default-speculation").value = "custom";
$("modal-default-model").value = "qwen-mtp";
await saveDefault(); results.push(puts.at(-1).body);
$("modal-default-model").value = "gpt-oss";
await saveDefault(); results.push(puts.at(-1).body);
"""
    source = _console_script()
    same, moved = _node(_slice_function(source, "speculationFromChoice") + _save_default_harness(scenario))
    assert "options" not in same, "the same model keeps its stored custom policy untouched"
    assert moved["options"] == {"temperature": 0.2}, "a route change must not keep the old model's MTP"


def test_sandbox_never_sends_a_disabled_mtp_control() -> None:
    source = _console_script()
    assert 'const mtpChoice = $("sandbox-speculation").disabled ? "" : $("sandbox-speculation").value;' in source
