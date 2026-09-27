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
    out = _run(["describeAppliedRecommended"], f"return describeAppliedRecommended({json.dumps(_LINUX_REPORT)});")
    assert "output.image" in out and _REASON in out, out
    assert "1 route has no recommendation this computer can run" in out, out
    # Nothing-but-unavailable must not read as "all matched" either.
    only = {"unavailable": 1, "routes": [_LINUX_REPORT["routes"][2], _LINUX_REPORT["routes"][1]]}
    out_only = _run(["describeAppliedRecommended"], f"return describeAppliedRecommended({json.dumps(only)});")
    assert "already matched" not in out_only, out_only
    assert _REASON in out_only


def test_apply_summary_without_unavailable_rows_is_unchanged() -> None:
    old = {"routes": [_LINUX_REPORT["routes"][1]]}
    out = _run(["describeAppliedRecommended"], f"return describeAppliedRecommended({json.dumps(old)});")
    assert out == "every recommended route already matched"


def test_apply_message_is_not_ok_toned_when_a_route_is_unavailable() -> None:
    body = _slice_function(_console_script(), "applyRecommendedDefaults")
    assert 'r.action === "unavailable"' in body
    assert '? "message" : "message ok"' in body


_GRID_HELPERS = ["defaultRowConfigured", "defaultRowUnavailableReason", "defaultRowUnavailableMarkup"]


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
        _GRID_HELPERS + ["defaultRowKey", "routeKey", "firstRunUnavailableCards"],
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
        ["sandboxSessionId", "sandboxRunId", "uploadSandboxFile"],
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
