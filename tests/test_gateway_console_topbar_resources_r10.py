"""R10.3 (operator 2026-10-04): the console's top bar carries the tray's
discreet realtime memory/compute widget — memory used/total (%) · GPU busy %
· models loaded — instead of the gateway address + copy control.

It reads the Resources snapshot (GET /host/state, `state.hostState`, no
second store), refreshes every few seconds while signed in, carries a kit
tooltip (`data-af-tip`) listing the real values, and a click opens the
Resources page. The address stays on the Network page.

The behavioural checks run the SHIPPED console functions (sliced from the
served page) under node.
"""

from __future__ import annotations

import json
import re

import pytest

from abstractgateway.console import gateway_console_html

pytestmark = pytest.mark.basic

GIB = 1024**3

SNAPSHOT = {
    "memory": {
        "ram": {"used_bytes": int(41.2 * GIB), "total_bytes": 64 * GIB, "percent": 64.4},
        "device": {"backend": "metal", "host_in_use_bytes": 18 * GIB, "wired_limit_bytes": 48 * GIB, "total_bytes": 64 * GIB},
        "process": {"rss_bytes": 3 * GIB},
    },
    "gpu": {"supported": True, "utilization_gpu_pct": 12.0, "source": "ioreg"},
    "models": [{"provider": "mlx", "model": "qwen3.6-27b-4bit", "resident": True, "size_bytes": 15 * GIB, "cache_bytes": 1 * GIB}],
    "session_caches": [{"bytes": 512 * 1024**2}],
    "totals": {"models_resident": 1, "model_bytes": 15 * GIB, "cache_bytes_models": 1 * GIB, "session_cache_bytes": 512 * 1024**2},
}


def _source() -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", gateway_console_html(), flags=re.S))


def _fns(src: str, *names: str) -> str:
    from test_gateway_console_offline import _slice_function

    return "\n".join(_slice_function(src, n) for n in names)


_VIEW_DEPS = ("_fmtBytes", "_fmtPct", "processHeldBasisWords", "deviceMeterView", "modelDisplaySize", "modelCacheBytes", "memoryBreakdown", "topbarResourcesView")

_DOM = """
const els = new Map();
function $(id) {
  if (!els.has(id)) els.set(id, { id, textContent: "", style: {}, dataset: {}, ariaLabel: "", className: "hidden",
    classList: { toggle(n, f) { const s = new Set(this._o.className.split(/\\s+/).filter(Boolean)); (f ? s.add(n) : s.delete(n)); this._o.className = [...s].join(" "); }, add(n) { this.toggle(n, true); }, contains(n) { return this._o.className.split(/\\s+/).includes(n); } } });
  const e = els.get(id); e.classList._o = e; return e;
}
const widget = () => ({ mem: $("topbar-res-mem").textContent, memShort: $("topbar-res-mem-short").textContent, gpu: $("topbar-res-gpu").textContent,
  models: $("topbar-res-models").textContent, memBar: $("topbar-res-mem-bar").style.height, gpuBar: $("topbar-res-gpu-bar").style.height,
  tip: $("topbar-resources").dataset.afTip, label: $("topbar-resources").ariaLabel, cls: $("topbar-resources").className });
"""


def test_header_carries_the_widget_and_no_address_control() -> None:
    html = gateway_console_html()
    header = html[html.index('<header class="shell_header">'): html.index("</header>")]
    # The widget sits left of the kit cluster, as a button (a click opens Resources).
    assert header.index('id="topbar-resources"') < header.index('id="af-topbar-root"')
    assert re.search(r'<button id="topbar-resources"[^>]*type="button"[^>]*data-af-tip=', header)
    for part in ("topbar-res-mem", "topbar-res-mem-short", "topbar-res-gpu", "topbar-res-models", "topbar-res-mem-bar", "topbar-res-gpu-bar"):
        assert f'id="{part}"' in header
    # No address + copy in the top bar any more.
    src = _source()
    props = _fns(src, "topBarIslandProps")
    assert "island-address" not in props and "netPrimaryUrl" not in props
    assert "island-address" not in html
    # The address keeps its home: the Network page's address rows, with Copy.
    assert "data-net-copy=" in html
    # The click is the sidebar's Resources door.
    assert '$("topbar-resources").onclick = openResourcesFromTopbar;' in src
    assert '$("tab-button-models").click();' in _fns(src, "openResourcesFromTopbar")


def test_widget_renders_the_real_values_and_follows_new_data() -> None:
    from node_requirement import require_node

    require_node()
    from test_gateway_console_offline import _node

    src = _source()
    second = json.loads(json.dumps(SNAPSHOT))
    second["memory"]["ram"].update({"used_bytes": int(50.0 * GIB), "percent": 78.1})
    second["gpu"]["utilization_gpu_pct"] = 87.0
    second["totals"]["models_resident"] = 2
    script = f"""
const state = {{ principal: {{ admin: true }} }};
{_DOM}
{_fns(src, *_VIEW_DEPS, "renderTopbarResources")}
const out = [];
renderTopbarResources(null, null); out.push(widget());
renderTopbarResources({json.dumps(SNAPSHOT)}, null); out.push(widget());
renderTopbarResources({json.dumps(second)}, null); out.push(widget());
renderTopbarResources(null, "HTTP 503"); out.push(widget());
state.principal = null; renderTopbarResources({json.dumps(SNAPSHOT)}, null); out.push(widget());
console.log(JSON.stringify(out));
"""
    before, first, updated, failed, signed_out = _node(script)

    assert before["mem"] == "—" and before["gpu"] == "—" and "Reading host resources" in before["tip"]

    assert first["mem"] == "41.2 GiB / 64.0 GiB (64%)"
    assert first["memShort"] == "64%" and first["gpu"] == "12%" and first["models"] == "1 model"
    assert first["memBar"] == "64%" and first["gpuBar"] == "12%"
    assert "hidden" not in first["cls"] and "is-stale" not in first["cls"]
    tip = first["tip"].split("\n")
    assert tip[0] == "RAM: 41.2 GiB of 64.0 GiB (64%)"
    assert tip[1] == "Accelerator heap: 18.0 GiB / 48.0 GiB (all processes)"
    assert tip[2] == "Model weights: 15.0 GiB · 1 model loaded"
    assert tip[3] == "KV caches: 1.0 GiB for models · 512.0 MiB in sessions"
    assert tip[4] == "GPU load: 12% (via ioreg)"
    assert tip[-1] == "Click to open Resources."
    assert first["label"].startswith("Memory 41.2 GiB / 64.0 GiB (64%), GPU 12% busy, 1 model loaded")

    # New data → new figures (the poll's whole point).
    assert updated["mem"] == "50.0 GiB / 64.0 GiB (78%)" and updated["gpu"] == "87%" and updated["models"] == "2 models"
    assert updated["gpuBar"] == "87%" and "2 models loaded" in updated["tip"]

    # A failure is labelled, never stale numbers.
    assert failed["mem"] == "—" and "is-stale" in failed["cls"] and "Host resources unavailable: HTTP 503" in failed["tip"]

    # Signed out: hidden.
    assert "hidden" in signed_out["cls"]


def test_unknown_figures_are_dashes_never_zero() -> None:
    from node_requirement import require_node

    require_node()
    from test_gateway_console_offline import _node

    src = _source()
    script = f"""
const state = {{ principal: {{ admin: true }} }};
{_DOM}
{_fns(src, *_VIEW_DEPS, "renderTopbarResources")}
renderTopbarResources({{ memory: {{}}, gpu: {{ supported: false }}, models: [], totals: {{}} }}, null);
console.log(JSON.stringify([widget()]));
"""
    w = _node(script)[0]
    assert w["mem"] == "—" and w["gpu"] == "—" and w["models"] == "— models"
    assert "GPU load: not measured on this host" in w["tip"] and "RAM: unknown" in w["tip"]


def test_the_poll_refreshes_the_widget_off_the_resources_page_only() -> None:
    """Every few seconds while signed in; on the Resources page its own chain
    refreshes the same snapshot; a hidden browser tab and sign-out stop asking."""
    from node_requirement import require_node

    require_node()
    from test_gateway_console_offline import _node

    src = _source()
    m = re.search(r"const TOPBAR_RES_POLL_MS = (\d+);", src)
    assert m and 2000 <= int(m.group(1)) <= 10000, "a few seconds"
    script = f"""
{m.group(0)}
const state = {{ principal: {{ admin: true }}, activeTab: "users", hostState: null }};
const document = {{ visibilityState: "visible" }};
const calls = []; const timers = [];
function setTimeout(fn, ms) {{ timers.push({{ fn, ms }}); }}
async function loadHostState(opts) {{ calls.push(opts); }}
function renderTopbarResources() {{}}
function $() {{ return {{ classList: {{ add() {{}} }} }}; }}
{_fns(src, "topbarResourcesTick", "startTopbarResourcesPoll", "stopTopbarResourcesPoll")}
const run = () => {{ const t = timers.shift(); t.fn(); return t.ms; }};
startTopbarResourcesPoll();
const a = calls.length;
const ms = timers[0].ms;
run(); const b = calls.length;                  // next tick, still off Resources
state.activeTab = "models"; run(); const c = calls.length;   // on Resources: its own chain
state.activeTab = "users"; document.visibilityState = "hidden"; run(); const d = calls.length;
document.visibilityState = "visible"; stopTopbarResourcesPoll(); run(); const e = calls.length; const left = timers.length;
console.log(JSON.stringify([{{ a, b, c, d, e, left, ms, opts: calls[0] }}]));
"""
    r = _node(script)[0]
    assert r["a"] == 1 and r["opts"] == {"quiet": True, "widgetOnly": True}
    assert r["ms"] == int(m.group(1))
    assert r["b"] == 2, "each tick refreshes the snapshot"
    assert r["c"] == 2, "on the Resources page the page's chain owns the refresh"
    assert r["d"] == 2, "a hidden browser tab asks nothing"
    assert r["e"] == 2 and r["left"] == 0, "sign-out ends the chain"


def test_widget_only_refresh_leaves_the_resources_page_alone() -> None:
    from node_requirement import require_node

    require_node()
    from test_gateway_console_offline import _node

    src = _source()
    script = f"""
{_fns(src, "loadHostState")}
const state = {{ principal: {{ admin: true }}, hostStateSeq: 0 }};
function $() {{ return {{ textContent: "", className: "", classList: {{ add() {{}}, remove() {{}} }} }}; }}
const log = [];
function tableLoadingRow() {{ log.push("loading-row"); }}
function modelsEmptyRow() {{}}
async function ensureModalityUi() {{ log.push("modality"); }}
async function loadGatewayHost() {{ log.push("gateway-card"); }}
function renderHostState() {{ log.push("resources-page"); }}
function renderTopbarResources(data, err) {{ log.push("widget:" + (data ? data.marker : "null") + (err ? ":" + err : "")); }}
let answer = {{ marker: "a" }};
async function api(path) {{ log.push(path); if (answer instanceof Error) throw answer; return answer; }}
await loadHostState({{ quiet: true, widgetOnly: true }});
answer = {{ marker: "b" }};
await loadHostState({{ quiet: true }});
answer = new Error("HTTP 503");
await loadHostState({{ quiet: true, widgetOnly: true }});
console.log(JSON.stringify([{{ log, inflight: state.hostStateInflight }}]));
"""
    r = _node(script)[0]
    assert r["log"] == [
        "/api/gateway/host/state", "widget:a",
        "modality", "/api/gateway/host/state", "widget:b", "resources-page", "gateway-card",
        "/api/gateway/host/state", "widget:null:HTTP 503",
    ], r["log"]
    assert r["inflight"] == 0


def test_signing_in_starts_and_signing_out_stops_the_widget() -> None:
    src = _source()
    render_account = _fns(src, "renderAccount")
    assert "startTopbarResourcesPoll();" in render_account
    assert "stopTopbarResourcesPoll();" in render_account
