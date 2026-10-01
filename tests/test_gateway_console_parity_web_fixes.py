"""Web-console defects found while porting the console to the terminal (2026-09-27).

Each test runs the SHIPPED function source (sliced out of the served page) in
node, so it cannot drift from what `/console` serves:

1. Host-aware recommendations: AbstractCore's apply plan reports a route whose
   recommended engine cannot run here as `action: "unavailable"` + `reason`
   (and an `unavailable` count); the grid row carries
   `recommendation_unavailable`. The console says so instead of "every
   recommended route already matched"; absent fields keep the old behaviour.
2. The Welcome "Computer" tile reads `host.host_name` (AbstractCore's host
   identity) and the OS from `gateway.service.platform`.
3. The Sandbox image/video lanes resolve a task row the way the server does
   (exact task row, else the `output.image` / `output.video` parent).
4. The Sandbox upload sends the SESSION id, so the gateway files it under the
   same owner run the media routes use (no `session_memory_session_memory_`).
5. Video (AbstractCore parity/video): the Models catalog has a Video category
   chip, MLX-Gen is labelled for images and video, the first-run guide names
   the video route.
6. `route_unavailable` (lead's contract, REVIEW-1): a CONFIGURED row whose
   provider cannot run on this host warns in the grid and in the first-run
   model step.
7. Session-memory ids use the server's run-id alphabet (`_SAFE_RUN_ID_PATTERN`).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
from node_requirement import require_node

from abstractgateway.console import gateway_console_html

pytestmark = pytest.mark.basic


def _console_script() -> str:
    scripts = re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S)
    assert scripts, "console served no inline <script>"
    return "\n".join(scripts)


def _slice_function(source: str, name: str) -> str:
    """`[async] function <name>(...) { ... }` verbatim, brace-matched."""
    match = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", source)
    assert match, f"{name}() not found in the console JavaScript"
    start = match.start()
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
    raise AssertionError(f"unbalanced braces in {name}()")


def _slice_const(source: str, name: str) -> str:
    match = re.search(r"const\s+" + re.escape(name) + r"\s*=\s*\{", source)
    assert match, f"const {name} not found"
    depth = 0
    for i in range(source.index("{", match.start()), len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : i + 1] + ";"
    raise AssertionError(f"unbalanced braces in {name}")


_PRELUDE = r"""
const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch] || ch);
const state = { defaults: [], providerLabels: new Map(), principal: {} };
"""


def _run(functions: list[str], body: str, extra: str = "") -> object:
    source = _console_script()
    parts = [_PRELUDE, extra]
    parts += [_slice_function(source, name) for name in functions]
    parts.append(f"const out = (async () => {{ {body} }})();\nout.then((v) => console.log(JSON.stringify(v)));")
    node = require_node()
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as f:
        f.write("\n".join(parts))
        path = Path(f.name)
    try:
        proc = subprocess.run([node, str(path)], capture_output=True, text=True, check=False, timeout=60)
    finally:
        path.unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# AbstractCore `plan_recommended_capability_defaults` on a Linux host (shape of
# abstractcore/config/capability_defaults.py, branch parity/defaults bd3c224).
_REASON = (
    "MLX-Gen image generation needs MLX, and MLX runs on Apple silicon only; "
    "set output.image to a provider that runs here (Diffusers or a cloud provider)"
)
_LINUX_REPORT = {
    "ok": True,
    "dry_run": False,
    "force": False,
    "changed": 1,
    "kept": 0,
    "already": 1,
    "unavailable": 1,
    "routes": [
        {"key": "input.text", "action": "apply", "changed": True, "before": {}, "after": {"provider": "lmstudio", "model": "qwen/qwen3.5-9b"}},
        {"key": "output.voice", "action": "already", "changed": False, "before": {"provider": "supertonic", "model": "supertonic-3"}, "after": {"provider": "supertonic", "model": "supertonic-3"}},
        {"key": "output.image", "selector": "image", "action": "unavailable", "changed": False, "recommended": {}, "before": {}, "after": {}, "download": {}, "reason": _REASON},
    ],
}


def test_apply_summary_names_unavailable_routes_with_reason() -> None:
    out = _run(_ENGINE_HELPERS + ["describeAppliedRecommended"], f"return describeAppliedRecommended({json.dumps(_LINUX_REPORT)});")
    assert "output.image" in out and _REASON in out, out
    assert "1 route has no recommendation this computer can run" in out, out
    # Nothing-but-unavailable must not read as "all matched" either.
    only = {"unavailable": 1, "routes": [_LINUX_REPORT["routes"][2], _LINUX_REPORT["routes"][1]]}
    out_only = _run(_ENGINE_HELPERS + ["describeAppliedRecommended"], f"return describeAppliedRecommended({json.dumps(only)});")
    assert "already matched" not in out_only, out_only
    assert _REASON in out_only


def test_apply_summary_without_unavailable_rows_is_unchanged() -> None:
    old = {"routes": [_LINUX_REPORT["routes"][1]]}
    out = _run(_ENGINE_HELPERS + ["describeAppliedRecommended"], f"return describeAppliedRecommended({json.dumps(old)});")
    assert out == "every recommended route already matched"


def test_apply_summary_counts_several_unavailable_routes() -> None:
    video = {"key": "output.video", "action": "unavailable", "changed": False, "before": {}, "after": {}, "reason": "no video engine here"}
    report = {"routes": [_LINUX_REPORT["routes"][2], video]}
    out = _run(_ENGINE_HELPERS + ["describeAppliedRecommended"], f"return describeAppliedRecommended({json.dumps(report)});")
    assert "2 routes have no recommendation this computer can run" in out, out
    assert "output.video \u2014 no video engine here" in out, out


# `plan_recommended_capability_defaults` on a Linux host (synthetic cuda24) for
# a store carried over from a Mac (input.text mlx, output.image mlx-gen):
# AbstractCore parity/video 6c64508 (the `route_unavailable` slice; generated
# 2026-09-27 from core-video; the fields the console reads, verbatim).
_MLX_TEXT = {"provider": "mlx", "model": "mlx-community/Qwen3.5-9B-MLX-4bit"}
_MLX_TEXT_BROKEN = dict(_MLX_TEXT, reason="MLX runs only on Apple Silicon Macs (macOS, arm64); this computer's recommended route is lmstudio/qwen/qwen3.5-9b")
_MLXGEN_IMAGE = {"provider": "mlx-gen", "model": "AbstractFramework/flux.2-klein-4b-8bit"}
_IMAGE_REASON = (
    "MLX-Gen image generation needs MLX, and MLX runs only on Apple Silicon Macs (macOS, arm64); set output.image to an "
    "image engine this host runs: diffusers (install profile gpu), sdcpp (stable-diffusion.cpp, optional extra) or a cloud image provider"
)
_VIDEO_REASON = (
    "MLX-Gen video generation needs MLX, and MLX runs only on Apple Silicon Macs (macOS, arm64); no other local engine in "
    "AbstractFramework generates video today (abstractvision's Diffusers video path is disabled, stable-diffusion.cpp has none); "
    "the remaining option is an OpenAI-compatible video endpoint (abstractvision openai-compatible backend)"
)
_VOICE_APPLY = {"key": "output.voice", "action": "apply", "changed": True, "before": {}, "after": {"provider": "supertonic", "model": "supertonic-3"}}
_VIDEO_UNAVAILABLE = {"key": "output.video", "action": "unavailable", "changed": False, "before": {}, "after": {}, "reason": _VIDEO_REASON}
_CARRIED_OVER_PLAN = {"routes": [
    {"key": "input.text", "action": "kept", "changed": False, "before": _MLX_TEXT, "after": _MLX_TEXT, "route_unavailable": _MLX_TEXT_BROKEN},
    _VOICE_APPLY,
    {"key": "output.image", "action": "unavailable", "changed": False, "before": _MLXGEN_IMAGE, "after": _MLXGEN_IMAGE,
     "reason": _IMAGE_REASON, "route_unavailable": dict(_MLXGEN_IMAGE, reason=_IMAGE_REASON)},
    _VIDEO_UNAVAILABLE,
]}
_CARRIED_OVER_FORCED = {"routes": [
    {"key": "input.text", "action": "overwrite", "changed": True, "before": _MLX_TEXT, "after": {"provider": "lmstudio", "model": "qwen/qwen3.5-9b"}, "route_unavailable": _MLX_TEXT_BROKEN},
    _VOICE_APPLY,
    {"key": "output.image", "action": "cleared", "changed": True, "before": _MLXGEN_IMAGE, "after": {},
     "reason": _IMAGE_REASON, "route_unavailable": dict(_MLXGEN_IMAGE, reason=_IMAGE_REASON)},
    _VIDEO_UNAVAILABLE,
]}


def test_apply_summary_never_reports_a_configured_route_that_cannot_run_as_fine() -> None:
    out = _run(
        _ENGINE_HELPERS + ["describeAppliedRecommended"],
        f"return [describeAppliedRecommended({json.dumps(_CARRIED_OVER_PLAN)}), describeAppliedRecommended({json.dumps(_CARRIED_OVER_FORCED)})];",
    )
    plain, forced = out
    # kept + broken: the reason rides on the kept entry.
    assert "kept yours on input.text (mlx/mlx-community/Qwen3.5-9B-MLX-4bit; cannot run on this computer: MLX runs only on Apple Silicon" in plain, plain
    # unavailable + broken: kept in place, and said so (never "left unset").
    assert "output.image \u2014 " + _IMAGE_REASON + " (configured mlx-gen/AbstractFramework/flux.2-klein-4b-8bit cannot run here either)" in plain, plain
    assert "left unset" not in plain
    assert "output.video \u2014 " + _VIDEO_REASON + ";" not in plain and plain.endswith(_VIDEO_REASON)
    # forced: overwrite reads as a change, cleared reads as cleared (not "-/-").
    assert "input.text: mlx/mlx-community/Qwen3.5-9B-MLX-4bit \u2192 lmstudio/qwen/qwen3.5-9b" in forced, forced
    assert "output.image: removed mlx-gen/AbstractFramework/flux.2-klein-4b-8bit \u2014 cannot run on this computer: " + _IMAGE_REASON in forced, forced
    assert "\u2192 -/-" not in forced


def _apply_message(report: dict) -> list:
    """Run the SHIPPED applyRecommendedDefaults against a stub page and api."""
    extra = r"""
const els = {};
const $ = (id) => (els[id] = els[id] || { id, textContent: "", className: "", disabled: false, children: [], append(x) { this.children.push(x); } });
const document = { createElement: () => ({ className: "", innerHTML: "", onclick: null }), createTextNode: (t) => ({ text: t }) };
let REPORT = null;
const api = async (path) => (path.endsWith("/apply-recommended") ? { applied_recommended: REPORT } : { routes: [] });
const renderDefaults = async () => {};
const refreshAvailability = async () => {};
"""
    return _run(
        _ENGINE_HELPERS + ["appliedRecommendedBrokenRows", "appliedRecommendedFixableRows", "describeAppliedRecommended", "applyRecommendedDefaults"],
        f"REPORT = {json.dumps(report)}; await applyRecommendedDefaults(null, false);"
        "const m = $('defaults-message'); const btn = m.children.find((c) => c.innerHTML !== undefined);"
        "return [m.className, m.textContent, btn ? btn.innerHTML : null];",
        extra,
    )


def test_apply_message_is_not_ok_toned_when_a_route_is_unavailable() -> None:
    cls, text, button = _apply_message(_LINUX_REPORT)
    assert cls == "message", (cls, text)
    assert _REASON in text
    assert button is None, "nothing kept, nothing broken: no second pass to offer"
    ok_cls, _, ok_button = _apply_message({"routes": [_LINUX_REPORT["routes"][0], _LINUX_REPORT["routes"][1]]})
    assert ok_cls == "message ok" and ok_button is None


def test_apply_offers_the_forced_pass_for_a_route_that_cannot_run() -> None:
    # kept (broken) -> "Replace mine too"
    cls, _, button = _apply_message(_CARRIED_OVER_PLAN)
    assert cls == "message" and "Replace mine too" in button, button
    # only a broken route nothing can replace -> the forced pass clears it
    only_broken = {"routes": [_VOICE_APPLY, _CARRIED_OVER_PLAN["routes"][2]]}
    cls, _, button = _apply_message(only_broken)
    assert cls == "message" and "Clear what cannot run here" in button, button
    # a broken route the apply FIXED (overwrite) is not left to warn about
    # kept + broken alone (no unavailable row) still is not an `ok` result
    cls, _, button = _apply_message({"routes": [_CARRIED_OVER_PLAN["routes"][0], _VOICE_APPLY]})
    assert cls == "message" and "Replace mine too" in button, (cls, button)
    fixed = {"routes": [_CARRIED_OVER_FORCED["routes"][0], _VOICE_APPLY]}
    cls, _, button = _apply_message(fixed)
    assert cls == "message ok" and button is None, (cls, button)


# AbstractCore `engine_missing` / `needs_gpu_limit` helpers (wave 2): the grid
# status, the apply report and the model cards call them.
_ENGINE_HELPERS = ["engineMissingInfo", "engineMissingText", "engineMissingMarkup", "gpuLimitText"]
_GRID_HELPERS = ["defaultRowConfigured", "defaultRowUnavailableReason", "defaultRowUnavailableMarkup"] + _ENGINE_HELPERS


def test_grid_row_shows_the_unavailable_reason() -> None:
    row = {"key": "output.image", "configured": False, "recommendation_unavailable": {"provider": "mlx-gen", "model": "AbstractFramework/flux.2-klein-4b-8bit", "reason": _REASON}}
    configured = dict(row, provider="diffusers", model="x", configured=True)
    plain = {"key": "output.image", "configured": False}
    out = _run(
        _GRID_HELPERS,
        f"return [defaultRowUnavailableMarkup({json.dumps(row)}), defaultRowUnavailableMarkup({json.dumps(configured)}), defaultRowUnavailableMarkup({json.dumps(plain)})];",
    )
    assert "No recommended model runs on this computer" in out[0] and "MLX runs on Apple silicon only" in out[0]
    assert "mlx-gen / AbstractFramework/flux.2-klein-4b-8bit" in out[0]
    assert out[1] == "" and out[2] == ""
    # ...and the grid actually renders it in the status cell.
    assert "defaultRowUnavailableMarkup(row)" in _slice_function(_console_script(), "renderDefaultRows")


def test_first_run_model_step_lists_unavailable_routes_as_cards() -> None:
    source = _console_script()
    extra = _slice_const(source, "FIRST_RUN_ROUTE_COPY") + "\n" + _slice_function(source, "uiPill")
    defaults = [
        {"key": "input.text", "provider": "lmstudio", "model": "qwen/qwen3.5-9b", "configured": True},
        {"key": "output.image", "configured": False, "recommendation_unavailable": {"provider": "mlx-gen", "model": "flux", "reason": _REASON}},
    ]
    out = _run(
        _GRID_HELPERS + ["defaultRowRouteUnavailableReason", "defaultRowKey", "routeKey", "firstRunUnavailableCards"],
        f"state.defaults = {json.dumps(defaults)};"
        "return [firstRunUnavailableCards([{route: 'input.text'}]), firstRunUnavailableCards([{route: 'output.image'}])];",
        extra,
    )
    assert "Not available here" in out[0] and "Images" in out[0] and "MLX runs on Apple silicon only" in out[0]
    assert "first-run-download" not in out[0], "an unavailable route must offer no Download"
    assert out[1] == "", "a route the download plan already lists gets no second card"
    assert "firstRunUnavailableCards(rows)" in _slice_function(source, "renderFirstRunModel")


def test_welcome_computer_tile_reads_host_name_and_service_platform() -> None:
    source = _console_script()
    extra = r"""
const CORE_CONSOLE = { hostName: "page-server-name" };
const FIRST_RUN_STEPS = ["welcome", "engines", "model", "apps", "done"];
const FIRST_RUN_STEP_COPY = {};
const FIRST_RUN_STEP_TITLES = { engines: "Engines", model: "Model", apps: "Apps" };
const firstRun = {};
const box = { innerHTML: "" };
const $ = () => box;
let snapshot = null;
const api = async () => snapshot;
"""
    out = _run(
        ["firstRunTiles", "firstRunOsLabel", "_fmtBytes", "loadFirstRunWelcome"],
        "const tile = () => (box.innerHTML.match(/<dt>Computer<\\/dt><dd>(.*?)<\\/dd>/) || [])[1];"
        "snapshot = { host: { host_id: 'abc', host_name: 'forge.local', kind: 'local' }, gateway: { service: { platform: 'linux' } } };"
        "await loadFirstRunWelcome(); const a = tile();"
        "snapshot = { host: { host_id: null, host_name: null, kind: 'local' }, gateway: { service: {} } };"
        "await loadFirstRunWelcome(); const b = tile();"
        "return [a, b];",
        extra,
    )
    assert out[0].startswith("forge.local"), out
    assert '<div class="ui-sub">Linux</div>' in out[0], out
    # No host name -> the page server's name; no platform -> no sub-line.
    assert out[1] == "page-server-name", out


def test_os_label_names_every_platform_the_service_reports() -> None:
    out = _run(
        ["firstRunOsLabel"],
        "return ['darwin', 'windows', 'linux', ' Linux ', 'freebsd', '', null].map(firstRunOsLabel);",
    )
    assert out == ["macOS", "Windows", "Linux", "Linux", "", "", ""], out


# Fresh install (AbstractCore seed): only the parent route is written, the task
# rows are empty and decorated `inherits_broad` + `broad_key`.
_FRESH_DEFAULTS = [
    {"key": "output.image", "provider": "mlx-gen", "model": "flux-klein", "configured": True, "task_keys": ["output.image.text_to_image"]},
    {"key": "output.image.text_to_image", "configured": False, "broad_key": "output.image", "inherits_broad": True},
    {"key": "output.video", "configured": False},
    {"key": "output.video.text_to_video", "configured": False, "broad_key": "output.video"},
    {"key": "output.image.image_to_image", "configured": True, "options": {"steps": 4}, "broad_key": "output.image"},
]


def test_sandbox_task_route_resolves_like_the_server() -> None:
    out = _run(
        ["defaultRowConfigured", "defaultRowKey", "routeKey", "defaultRowParentKey", "findDefaultRow", "sandboxEffectiveRow"],
        f"state.defaults = {json.dumps(_FRESH_DEFAULTS)};"
        "const t2i = sandboxEffectiveRow(state.defaults[1]);"
        "const t2v = sandboxEffectiveRow(state.defaults[3]);"
        "const partial = sandboxEffectiveRow(state.defaults[4]);"
        "const own = sandboxEffectiveRow({ key: 'output.image.text_to_image', provider: 'openai', model: 'gpt-image-1', broad_key: 'output.image' });"
        "return [t2i, defaultRowConfigured(t2v), defaultRowConfigured(partial), own];",
    )
    t2i, t2v_ready, partial_ready, own = out
    assert t2i["provider"] == "mlx-gen" and t2i["model"] == "flux-klein" and t2i["inherited_from"] == "output.image"
    assert t2i["key"] == "output.image.text_to_image"
    assert t2v_ready is False, "an unset parent must not make the task row ready"
    assert partial_ready is False, "Core stops at a task row that carries any field"
    assert own["provider"] == "openai" and "inherited_from" not in own
    t2v_row = _run(
        ["defaultRowConfigured", "defaultRowKey", "routeKey", "defaultRowParentKey", "findDefaultRow", "sandboxEffectiveRow"],
        f"state.defaults = {json.dumps(_FRESH_DEFAULTS)}; return sandboxEffectiveRow(state.defaults[3]);",
    )
    assert t2v_row == _FRESH_DEFAULTS[3], "an unset parent lends nothing (no inherited_from)"
    assert "sandboxEffectiveRow(row)" in _slice_function(_console_script(), "sandboxCandidateRows")


def test_sandbox_upload_session_maps_to_the_media_owner_run() -> None:
    from abstractgateway.routes.gateway import _session_memory_run_id

    extra = r"""
const appended = [];
globalThis.FormData = class { append(k, v) { appended.push([k, typeof v === "string" ? v : "<file>"]); } };
globalThis.Headers = class { constructor() { this.h = {}; } set(k, v) { this.h[k] = v; } };
const csrf = () => "";
globalThis.fetch = async () => ({ ok: true, status: 200, text: async () => JSON.stringify({ attachment: { "$artifact": "a1" } }) });
"""
    out = _run(
        ["sessionMemoryIdPart", "sandboxSessionId", "sandboxRunId", "uploadSandboxFile"],
        "state.principal = { tenant_id: 'default', user_id: 'admin' };"
        "await uploadSandboxFile({ name: 'x.png', size: 3, type: 'image/png' });"
        "return [Object.fromEntries(appended).session_id, sandboxRunId()];",
        extra,
    )
    session_id, run_id = out
    assert not session_id.startswith("session_memory_"), session_id
    assert run_id == "session_memory_gateway_console_sandbox_default_admin"
    # The server files the upload under exactly the run the media routes use.
    assert _session_memory_run_id(session_id) == run_id


def test_session_memory_ids_use_the_server_run_id_alphabet() -> None:
    """A principal id with `:` (or any character outside `_SAFE_RUN_ID_PATTERN`)
    used to pass straight into the owner run id: the media and voice-test
    routes then refused to create the run (404) and the upload's session id
    was hashed to a different owner run."""
    from abstractgateway.routes.gateway import _SAFE_RUN_ID_PATTERN, _session_memory_run_id

    out = _run(
        ["sessionMemoryIdPart", "sandboxSessionId", "sandboxRunId", "voiceTestRunId"],
        "state.principal = { tenant_id: 'Org:Acme', user_id: 'oidc:alice@example.com' };"
        "const a = [sandboxSessionId(), sandboxRunId(), voiceTestRunId()];"
        "state.principal = { tenant_id: ':::', runtime_id: 'rt-7' };"
        "const b = [sandboxSessionId(), sandboxRunId(), voiceTestRunId()];"
        "return [a, b];",
    )
    (session_a, run_a, voice_a), (session_b, run_b, voice_b) = out
    assert session_a == "gateway_console_sandbox_org_acme_oidc_alice_example_com", session_a
    for run_id in (run_a, voice_a, run_b, voice_b):
        assert _SAFE_RUN_ID_PATTERN.match(run_id), run_id
    # The upload (session id) and the media routes (run id) meet on one owner run.
    assert _session_memory_run_id(session_a) == run_a
    assert _session_memory_run_id(session_b) == run_b
    # Nothing left of an all-punctuation tenant: the fallback, not an empty part.
    assert session_b == "gateway_console_sandbox_default_rt-7", session_b
    assert voice_b == "session_memory_gateway_console_voicetest_default_rt-7", voice_b


def test_sandbox_context_names_the_route_a_task_row_inherits_from() -> None:
    source = _console_script()
    extra = r"""
const els = {};
const node = (id) => ({ id, textContent: "", placeholder: "", disabled: false, children: [], classList: { add() {}, toggle() {} } });
const $ = (id) => (els[id] = els[id] || node(id));
let ROW = null;
const selectedSandboxRoute = () => ROW;
const refreshSandboxSpeculationSupport = () => {};
const sandboxRouteShortLabel = () => "";
"""
    out = _run(
        ["defaultRowConfigured", "defaultRowKey", "routeKey", "defaultRowParentKey", "findDefaultRow", "sandboxEffectiveRow",
         "sandboxRouteMode", "defaultRowCapability", "sandboxRouteLabel", "updateSandboxControls"],
        f"state.defaults = {json.dumps(_FRESH_DEFAULTS)};"
        "ROW = sandboxEffectiveRow(state.defaults[1]); updateSandboxControls(); const a = $('sandbox-context').textContent;"
        "ROW = { key: 'output.image.text_to_image', provider: 'openai', model: 'gpt-image-1' }; updateSandboxControls(); const b = $('sandbox-context').textContent;"
        "return [a, b];",
        extra,
    )
    assert out[0].endswith("mlx-gen / flux-klein (inherited from output.image)."), out
    assert out[1].endswith("openai / gpt-image-1."), out


# --- Video (AbstractCore parity/video d8a057d) --------------------------------
# `abstractcore.config.model_catalog.catalog(host=metal128, tags=["video"])`,
# row `wan2.2-ti2v-5b`, verbatim `capabilities` (generated from core-video).
_VIDEO_ROW = {
    "id": "wan2.2-ti2v-5b",
    "capabilities": {
        "text": False, "vision": False, "audio": None, "tools": None, "thinking": None, "max_tokens": None,
        "embedding": False, "source": None, "video_generation": True, "text_to_video": True, "image_to_video": True,
    },
    "artifacts": [{"provider": "mlx-gen", "artifact": "AbstractFramework/wan2.2-ti2v-5b-diffusers-8bit"}],
}


def _slice_line_const(source: str, name: str) -> str:
    match = re.search(r"const\s+" + re.escape(name) + r"\s*=.*?;\n", source)
    assert match, f"const {name} not found"
    return match.group(0)


def test_catalog_has_a_video_category_and_labels_mlx_gen_for_video() -> None:
    source = _console_script()
    extra = "\n".join(_slice_line_const(source, n) for n in ("MC_CAPS", "MC_PROVIDER_LABEL"))
    out = _run(
        ["mcRowCaps", "mcDefaultFilters", "mcParseHash", "mcProviderLabel"],
        f"return [mcRowCaps({json.dumps(_VIDEO_ROW)}), mcParseHash('#catalog?cap=video').cap, mcProviderLabel('mlx-gen'),"
        " MC_CAPS.find((c) => c[0] === 'video'), mcRowCaps({ capabilities: { image_generation: true } })];",
        extra,
    )
    caps, parsed, label, chip, image_caps = out
    assert caps == ["video"], caps
    assert parsed == "video", "a #catalog?cap=video link must select the Video chip"
    assert label == "MLX images & video"
    assert chip == ["video", "Video"]
    assert image_caps == ["image"], "an image model is not a video model"
    no_video = _run(
        ["mcRowCaps"], "return mcRowCaps({ capabilities: { image_generation: true, video_generation: false } });"
    )
    assert no_video == ["image"], "video_generation: false is not a video model"


# `recommended_unavailable_routes(synthetic_host("metal64"))["output.video"]`
# (core-video): the video model does not fit a 64 GiB Mac.
_MAC64_VIDEO_UNAVAILABLE = {
    "provider": "mlx-gen",
    "model": "AbstractFramework/wan2.2-ti2v-5b-diffusers-8bit",
    "reason": (
        "Wan2.2 TI2V 5B (text/image to video) needs about 61.4 GiB of memory while it generates (measured), "
        "and this computer can give a model about 45.6 GiB; use an Apple silicon Mac with more unified memory, "
        "or an OpenAI-compatible video endpoint (abstractvision openai-compatible backend)"
    ),
}

_ROUTE_HELPERS = _GRID_HELPERS + ["defaultRowRouteUnavailableReason", "defaultRowRouteUnavailableMarkup", "defaultRowKey", "routeKey", "findDefaultRow"]


def _first_run_extra() -> str:
    source = _console_script()
    return _slice_const(source, "FIRST_RUN_ROUTE_COPY") + "\n" + _slice_function(source, "uiPill")


def test_first_run_names_the_video_route() -> None:
    defaults = [{"key": "output.video", "configured": False, "recommendation_unavailable": _MAC64_VIDEO_UNAVAILABLE}]
    out = _run(
        _ROUTE_HELPERS + ["firstRunUnavailableCards"],
        f"state.defaults = {json.dumps(defaults)}; return [firstRunUnavailableCards([]), FIRST_RUN_ROUTE_COPY['output.video']];",
        _first_run_extra(),
    )
    card, copy = out
    assert copy["title"] == "Video" and copy["what"], copy
    assert '<div class="ui-card__title">Video</div>' in card, card
    assert "61.4 GiB" in card and "first-run-download" not in card


def test_first_run_transcription_card_is_keyed_by_the_speech_input_route() -> None:
    """AbstractCore's speech-input route is `input.voice` (recommended-v2 seeds it): the
    Transcription card must be found under that key, or the card falls back to the raw key."""
    out = _run([], "return [FIRST_RUN_ROUTE_COPY['input.voice'] || null, FIRST_RUN_ROUTE_COPY['input.audio'] || null];", _first_run_extra())
    voice, audio = out
    assert voice is not None and voice["title"] == "Transcription", voice
    assert audio is None, audio


# --- route_unavailable (lead's contract, REVIEW-1 "Shared contract") ----------
# A CONFIGURED row whose provider cannot run here. Shape = recommendation_unavailable;
# the flag is AbstractCore's `configured_routes_unavailable(routes, cuda24)`
# for a carried-over `output.image: mlx-gen/...` (parity/video 6c64508).
_LINUX_MLXGEN = _IMAGE_REASON
_BROKEN_IMAGE_ROW = {
    "key": "output.image", "kind": "output", "modality": "image", "configured": True,
    "provider": "mlx-gen", "model": "AbstractFramework/flux.2-klein-4b-8bit",
    "route_unavailable": {"provider": "mlx-gen", "model": "AbstractFramework/flux.2-klein-4b-8bit", "reason": _LINUX_MLXGEN},
}


def test_grid_warns_on_a_configured_route_that_cannot_run_here() -> None:
    ok_row = {k: v for k, v in _BROKEN_IMAGE_ROW.items() if k != "route_unavailable"}
    unset = {"key": "output.image", "configured": False, "route_unavailable": _BROKEN_IMAGE_ROW["route_unavailable"]}
    out = _run(
        _ROUTE_HELPERS + ["rowHasProviderModel", "defaultRowStatus"],
        f"const rows = [{json.dumps(_BROKEN_IMAGE_ROW)}, {json.dumps(ok_row)}, {json.dumps(unset)}];"
        "return rows.map((r) => [defaultRowStatus(r), defaultRowRouteUnavailableMarkup(r)]);",
    )
    (broken_status, broken_markup), (ok_status, ok_markup), (unset_status, unset_markup) = out
    assert broken_status == {"label": "cannot run here", "cls": "off"}, broken_status
    assert "Configured but cannot run on this computer: " + _LINUX_MLXGEN in broken_markup, broken_markup
    assert "tone-warn" in broken_markup and "mlx-gen / AbstractFramework/flux.2-klein-4b-8bit" in broken_markup
    # Absent field: the old behaviour exactly.
    assert ok_status == {"label": "configured", "cls": "ok"} and ok_markup == ""
    # The contract is about configured rows; an unset row keeps its own state.
    assert unset_status["label"] == "not configured" and unset_markup == ""
    assert "defaultRowRouteUnavailableMarkup(row)" in _slice_function(_console_script(), "renderDefaultRows")


def test_first_run_model_step_warns_on_a_configured_route_that_cannot_run_here() -> None:
    # output.text mirrors input.text's flag (Core 6c64508); the source row
    # alone gets the card (seen live: two "Chat and text" cards otherwise).
    text = dict(_MLX_TEXT, key="input.text", configured=True, route_unavailable=_MLX_TEXT_BROKEN)
    derived = dict(_MLX_TEXT, key="output.text", configured=True, derived_from="input.text", route_unavailable=_MLX_TEXT_BROKEN)
    defaults = [text, derived, _BROKEN_IMAGE_ROW]
    chat = _run(
        _ROUTE_HELPERS + ["firstRunUnavailableCards"],
        f"state.defaults = {json.dumps(defaults)}; return [firstRunUnavailableCards([]), firstRunUnavailableCards([{{route: 'input.text'}}])];",
        _first_run_extra(),
    )
    assert chat[0].count("Chat and text") == 1, chat[0]
    assert "Chat and text" not in chat[1], "listed source route: its own card warns, no derived duplicate"
    defaults = [
        {"key": "input.text", "provider": "lmstudio", "model": "qwen/qwen3.5-9b", "configured": True},
        _BROKEN_IMAGE_ROW,
    ]
    out = _run(
        _ROUTE_HELPERS + ["firstRunUnavailableCards", "firstRunRouteUnavailableAlert"],
        f"state.defaults = {json.dumps(defaults)};"
        "return [firstRunUnavailableCards([{route: 'input.text'}]), firstRunUnavailableCards([{route: 'output.image'}]),"
        " firstRunRouteUnavailableAlert('output.image'), firstRunRouteUnavailableAlert('input.text')];",
        _first_run_extra(),
    )
    card, card_listed, alert, no_alert = out
    assert "Cannot run here" in card and "Images" in card, card
    assert "Configured but cannot run on this computer: " + _LINUX_MLXGEN in card
    assert "Configured: <b>mlx-gen</b>" in card
    assert "first-run-download" not in card, "a route that cannot run offers no Download"
    # A route the download plan lists warns on its own card, never twice.
    assert card_listed == ""
    assert "Configured now (mlx-gen / AbstractFramework/flux.2-klein-4b-8bit) but cannot run on this computer" in alert
    assert no_alert == ""
    assert "firstRunRouteUnavailableAlert(r.route)" in _slice_function(_console_script(), "renderFirstRunModel")


def test_apply_recommended_selector_help_names_video() -> None:
    from abstractcore.config.capability_defaults import RECOMMENDED_SELECTORS

    from abstractgateway.routes.gateway import _GatewayApplyRecommendedDefaultsRequest

    description = _GatewayApplyRecommendedDefaultsRequest.model_fields["only"].description
    for word in list(RECOMMENDED_SELECTORS) + ["video"]:
        assert word in description, (word, description)
    assert "all three" not in description


def _render_first_run_model(defaults: list, recommended: list) -> str:
    """Run the SHIPPED renderFirstRunModel against a stub page; return its markup."""
    source = _console_script()
    extra = _first_run_extra() + "\n" + _slice_const(source, "WEIGHT_LABELS") + r"""
const firstRun = { open: true, step: "model" };
const els = {};
const $ = (id) => (id === "first-run-download-all" ? null : (els[id] = els[id] || { id, innerHTML: "", onclick: null }));
const dlFeed = { group: null };
const dlActive = () => false;
const dlGroupMarkup = () => "";
const downloadJobKey = (p, a) => `${p}:${a}`;
"""
    return _run(
        _ROUTE_HELPERS + ["weightView", "weightReason", "firstRunUnavailableCards", "firstRunRouteUnavailableAlert", "firstRunCap", "renderFirstRunModel"],
        f"state.defaults = {json.dumps(defaults)}; state.downloadJobs = new Map();"
        f"state.availabilityPlan = {{ recommended: {json.dumps(recommended)} }};"
        "renderFirstRunModel(); return $('first-run-model-recommended').innerHTML;",
        extra,
    )


def test_first_run_model_step_renders_unavailable_cards_without_any_download() -> None:
    """Linux: nothing in the download plan, yet the step must show the routes
    it cannot run instead of "no recommended downloads"."""
    html = _render_first_run_model([_BROKEN_IMAGE_ROW], [])
    assert '<div class="ui-card-grid is-fit is-aligned">' in html, html
    assert "Cannot run here" in html
    assert "This gateway reported no recommended downloads." not in html


def test_first_run_transcription_card_on_a_fresh_install_never_says_unknown() -> None:
    """Adversary pass 2 (F2): on a fresh install AbstractCore's probe answers `unknown` ("no Hugging
    Face cache directory exists on this machine yet"). The card says "Download needed", shows that
    reason, and offers Download (core marks the row downloadable)."""
    rec = [{"route": "input.voice", "provider": "huggingface", "artifact": "Systran/faster-whisper-base", "status": "unknown",
            "downloadable": True, "evidence": "hf cache scan", "detail": "no Hugging Face cache directory exists on this machine yet",
            "route_provider": "faster-whisper", "route_model": "base"}]
    html = _render_first_run_model([], rec)
    assert "Unknown" not in html and ">unknown<" not in html, html
    assert "Download needed" in html, html
    assert "No Hugging Face cache directory exists on this machine yet." in html
    assert 'class="ui-btn is-primary first-run-download" data-provider="huggingface" data-artifact="Systran/faster-whisper-base"' in html
    # Without a download verb it is "Not checked", with the reason, and no button.
    rec[0]["downloadable"] = False
    html = _render_first_run_model([], rec)
    assert "Not checked" in html and "first-run-download" not in html and "Unknown" not in html


def test_first_run_recommended_card_carries_the_route_warning() -> None:
    rec = [{"route": "output.image", "provider": "mlx-gen", "artifact": "AbstractFramework/flux.2-klein-4b-8bit", "status": "absent"}]
    html = _render_first_run_model([_BROKEN_IMAGE_ROW], rec)
    assert html.count("cannot run on this computer") == 1, html
    assert "Configured now (mlx-gen / AbstractFramework/flux.2-klein-4b-8bit)" in html
    assert "Sets the recommended models for text, voice, transcription, images and video" in html


def test_catalog_use_as_default_sets_exactly_the_chosen_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog's "Use as default" PUT, captured from the SHIPPED function,
    replayed against the real route: a previous route's base_url, reasoning
    and options must not survive (the gateway merges unnamed fields)."""
    from fastapi.testclient import TestClient

    source = _console_script()
    extra = r"""
const calls = [];
const api = async (path, opts) => { calls.push({ path, method: opts && opts.method, body: opts && opts.body ? JSON.parse(opts.body) : null }); return { routes: [] }; };
const servedModelId = (provider, artifact) => artifact;
const mcRender = () => {};
const renderDefaults = async () => {};
const $ = () => ({});
"""
    calls = _run(
        ["mcProviderLabel", "mcUseDefault"],
        "const view = {}; await mcUseDefault(view, 'ollama', 'qwen3.5:9b', null); return calls;",
        extra + _slice_line_const(source, "MC_PROVIDER_LABEL"),
    )
    put = [c for c in calls if c.get("method") == "PUT"]
    assert len(put) == 1 and put[0]["path"] == "/api/gateway/config/capability-defaults/output/text", calls
    body = put[0]["body"]
    assert body == {"provider": "ollama", "model": "qwen3.5:9b", "base_url": "", "reasoning": "", "options": {}}, body

    core_file = tmp_path / "coreconfig" / "abstractcore.json"
    core_file.parent.mkdir(parents=True, exist_ok=True)
    core_file.write_text(json.dumps({"capability_defaults": {"routes": {}}}), encoding="utf-8")
    (tmp_path / "bundles").mkdir()
    monkeypatch.delenv("ABSTRACTCORE_SERVER_BASE_URL", raising=False)
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(core_file))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "bundles"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    from abstractgateway.app import app

    headers = {"Authorization": "Bearer t"}
    route = "/api/gateway/config/capability-defaults/output/text"
    with TestClient(app) as client:
        seeded = client.put(route, headers=headers, json={
            "provider": "lmstudio", "model": "qwen/qwen3.5-9b", "base_url": "http://127.0.0.1:1234/v1",
            "reasoning": "high", "options": {"speculation": "mtp"},
        })
        assert seeded.status_code == 200, seeded.text
        assert client.put(route, headers=headers, json=body).status_code == 200
        rows = client.get("/api/gateway/config/capability-defaults", headers=headers).json()["routes"]
    row = next(r for r in rows if r.get("key") == "input.text")
    assert (row.get("provider"), row.get("model")) == ("ollama", "qwen3.5:9b"), row
    assert not row.get("base_url"), "the LM Studio port must not follow the new provider"
    assert not row.get("reasoning") and not row.get("options"), row


# --- per-user apply: a broken route the user INHERITS (review round 2) --------
# AbstractCore 6c64508 plans against the user's overlay only, so an inherited
# route is `before: {}` with no flag; `force` never clears it (no `cleared`
# for a key outside the overlay: verified with core-video's plan, cuda24 host).
def _user_plan() -> dict:
    return {
        "ok": True, "dry_run": False, "force": True, "changed": 1, "kept": 0, "already": 1, "unavailable": 2, "cleared": 0,
        "routes": [
            {"key": "input.text", "action": "apply", "changed": True, "before": {}, "after": {"provider": "lmstudio", "model": "qwen/qwen3.5-9b"}},
            {"key": "output.voice", "action": "already", "changed": False, "before": {"provider": "supertonic", "model": "supertonic-3"}, "after": {"provider": "supertonic", "model": "supertonic-3"}},
            {"key": "output.image", "action": "unavailable", "changed": False, "before": {}, "after": {}, "reason": _IMAGE_REASON},
            dict(_VIDEO_UNAVAILABLE),
        ],
    }


def _user_grid() -> list:
    flag = dict(_MLXGEN_IMAGE, reason=_IMAGE_REASON)
    return [
        # inherited from the gateway store, flagged by Core in the merged grid
        dict(_MLXGEN_IMAGE, key="output.image", configured=True, route_unavailable=flag),
        dict(_MLX_TEXT, key="input.text", configured=True, route_unavailable=_MLX_TEXT_BROKEN),
        # the user's OWN route (flag included to prove own keys are never stamped)
        {"key": "output.voice", "provider": "supertonic", "model": "supertonic-3", "configured": True,
         "route_unavailable": {"provider": "supertonic", "model": "supertonic-3", "reason": "x"}},
        {"key": "output.video", "configured": False},
    ]


def _apply_as_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, scoped: bool) -> tuple:
    from abstractgateway import core_config

    user_file = tmp_path / "user" / "config" / "abstractcore.json"
    user_file.parent.mkdir(parents=True)
    user_file.write_text(json.dumps({"capability_defaults": {"routes": {}}}), encoding="utf-8")
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(tmp_path / "store.json"))
    monkeypatch.setattr(core_config, "core_server_base_url", lambda: "")
    core_config._save_core_config_route(user_file, "output", "voice", provider="supertonic", model="supertonic-3", base_url=None, options={})
    monkeypatch.setattr(core_config, "_writable_scoped_core_config_path", lambda _b: user_file if scoped else None)
    seen = {}
    monkeypatch.setattr(core_config.config_facade, "apply_recommended_capability_defaults", lambda **kw: seen.update(kw) or _user_plan())
    monkeypatch.setattr(core_config, "gateway_capability_defaults_payload", lambda **_kw: {"ok": True, "routes": _user_grid()})
    payload = core_config.apply_recommended_gateway_capability_defaults(force=True, base_dir=tmp_path / "user")
    return payload["applied_recommended"], seen, user_file


def test_per_user_apply_flags_an_inherited_route_that_cannot_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.core_config import INHERITED_ROUTE_NOTE

    report, seen, user_file = _apply_as_user(tmp_path, monkeypatch, scoped=True)
    assert seen["config_file"] == user_file
    rows = {r["key"]: r for r in report["routes"]}
    image = rows["output.image"]
    assert image["route_unavailable"] == dict(_MLXGEN_IMAGE, reason=_IMAGE_REASON, inherited=True, note=INHERITED_ROUTE_NOTE)
    assert INHERITED_ROUTE_NOTE == "inherited from the gateway store (admin)"
    # force never claims to have cleared a route it did not touch
    assert image["action"] == "unavailable" and image["changed"] is False and report["cleared"] == 0
    # written for this user now -> no longer inherited, not stamped
    assert "route_unavailable" not in rows["input.text"]
    # the user's own route: Core's business, never re-stamped as inherited
    assert "route_unavailable" not in rows["output.voice"]
    assert "route_unavailable" not in rows["output.video"]


def test_install_store_apply_is_not_stamped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report, seen, _ = _apply_as_user(tmp_path, monkeypatch, scoped=False)
    assert seen["config_file"] is None
    assert report == _user_plan(), "no overlay, nothing inherited: Core's report verbatim"


def test_apply_summary_for_an_inherited_broken_route_offers_no_clear() -> None:
    inherited = dict(_MLXGEN_IMAGE, reason=_IMAGE_REASON, inherited=True, note="inherited from the gateway store (admin)")
    report = {"routes": [_VOICE_APPLY, {"key": "output.image", "action": "unavailable", "changed": False, "before": {}, "after": {},
                                        "reason": _IMAGE_REASON, "route_unavailable": inherited}]}
    cls, text, button = _apply_message(report)
    assert "(configured mlx-gen/AbstractFramework/flux.2-klein-4b-8bit cannot run here either; inherited from the gateway store (admin))" in text, text
    assert cls == "message", "still not a success"
    assert button is None, "only an admin can change an inherited route: no forced pass to offer"
