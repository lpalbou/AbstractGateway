"""Console model catalog as cards (mission X2, 2026-09-24).

The operator on the old Models table: "seeing only the Q4 is a bit
problematic", "a top level filter for 4bit and 8bit quant", "1 card <-> 1
model". These tests drive the REAL view code (``console_ui.CONSOLE_UI_JS`` +
``console_catalog.CATALOG_JS``) in a node VM against model_catalog_v1
fixtures and pin:

- one card per model, every model drawn (filters hide, never cap: ADR-0026);
- the quant chips filter on ``artifact.quant_class`` (AbstractCore, mission
  W1) and the live count follows every filter;
- the filters round-trip through the ``#catalog?...`` link;
- a catalog WITHOUT ``quant_class`` shows the notice and a disabled quant
  filter, and nothing on the page guesses the class from the quant string;
- Download posts N's request and hands the job to the one download feed;
  a running job renders with the shared progress component and Cancel.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import tempfile

import pytest
from node_requirement import require_node

from abstractgateway.console import gateway_console_html
from abstractgateway.console_catalog import CATALOG_CSS, CATALOG_JS
from abstractgateway.console_ui import CONSOLE_UI_JS

pytestmark = pytest.mark.basic

PROVIDERS = ["mlx", "ollama", "lmstudio"]
CLASSES = ["4bit", "8bit", "16bit"]


def _art(i: int, j: int) -> dict:
    provider = PROVIDERS[j % 3]
    qc = CLASSES[(i + j) % 3]
    raw = {"4bit": "q4_k_m", "8bit": "q8_0", "16bit": "f16"}[qc]
    return {
        "provider": provider,
        "artifact": f"org-{i}/Model-{i}-{raw}-{provider}",
        "engine": provider,
        "quant": raw,
        "quant_class": qc,
        "bits": {"4bit": 4.85, "8bit": 8.5, "16bit": 16}[qc],
        "download_bytes": 1_000_000_000 * (j + 1),
        "size_source": "catalog",
        "presence": {"status": "installed" if (i % 10 == 0 and j == 0) else "absent", "location": None, "evidence": None},
        "fit": {"verdict": "too_large" if qc == "16bit" and i % 2 else "fits", "notes": ["a note"]},
        "supported_on_host": True,
        "downloadable": True,
        "recommended": j == 1,
    }


def _catalog(n_rows: int = 80) -> dict:
    rows = []
    for i in range(n_rows):
        rows.append({
            "id": f"model-{i}",
            "display_name": f"Model {i}",
            "vendor": "Vision Org" if i % 4 == 0 else "Org",
            "params_total": 4_000_000_000,
            "params_active": None,
            "license": "apache-2.0",
            "starter": i == 3,
            "capabilities": {"text": i % 4 != 0, "vision": i % 4 == 0, "embedding": i % 4 == 0, "tools": "native" if i % 2 else None},
            "artifacts": [_art(i, j) for j in range(3)],
        })
    return {"schema": "model_catalog_v1", "host_profile": {"accelerator": "metal", "gpu_name": "Apple M5 Max", "ram_bytes": 128e9, "unified_memory": True, "ceiling_bytes": 108e9}, "rows": rows}


def _expected(cat: dict, keep) -> tuple[int, int]:
    models = arts = 0
    for row in cat["rows"]:
        hit = [a for a in row["artifacts"] if keep(row, a)]
        if hit:
            models += 1
            arts += len(hit)
    return models, arts


_HARNESS = r"""
import vm from "node:vm";
const source = __SOURCE__;
const FIXTURE = __FIXTURE__;
const HUB = __HUB__;
const calls = [];
const tracked = [];
const location = { hash: __HASH__, pathname: "/console", search: "" };
const history = { calls: [], replaceState(_s, _t, url) { this.calls.push(String(url)); location.hash = (String(url).match(/#.*$/) || [""])[0]; } };
class El { constructor(id) { this.id = id; this.innerHTML = ""; this.dataset = {}; } }
const root = new El("catalog-cards-root");
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const ctx = vm.createContext({
  console, setTimeout: (fn, ms) => { const t = setTimeout(fn, Math.min(ms || 0, 5)); t.unref(); return t; }, clearTimeout,
  encodeURIComponent, decodeURIComponent, location, history, document: { activeElement: null }, window: {},
  state: { principal: { admin: true }, downloadJobs: new Map(), defaults: [], activeTab: "catalog", providerLabels: new Map() },
  esc: (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]),
  $: () => null,
  downloadJobKey: (p, a) => `${p || ""}/${a || ""}`,
  servedModelId: (p, a) => a,
  trackDownloadJob: (job) => { tracked.push(job); },
  api: async (path, opts) => {
    calls.push({ path, method: (opts && opts.method) || "GET", body: (opts && opts.body) || "" });
    if (path === "/api/gateway/models/catalog") return JSON.parse(JSON.stringify(FIXTURE));
    if (path.startsWith("/api/gateway/models/catalog?")) {
      if (HUB === null) throw new Error("hub search failed (offline?): connection refused");
      return JSON.parse(JSON.stringify(HUB));
    }
    if (path === "/api/gateway/models/download") return { ok: true, job: { job: "dl-1", provider: "mlx", artifact: "x", status: "running", state: "downloading" } };
    return {};
  },
});
vm.runInContext(source, ctx);
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setTimeout(r, 1)); };
const out = {};
const view = () => vm.runInContext("mcStore.views.get('tab')", ctx);
const chip = (group, value, disabled) => root.onclick({ target: { closest: (sel) => (sel === "[data-mc-filter]" ? { disabled: !!disabled, dataset: { mcFilter: group, mcValue: value } } : null) } });
const snap = (name) => {
  const html = root.innerHTML;
  const m = /<b>(\d+)<\/b> of (\d+) models? · <b>(\d+)<\/b>/.exec(html);
  out[name] = {
    count: m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null,
    cards: (html.match(/data-mc-model="/g) || []).length,
    arts: (html.match(/<li class="mc-art/g) || []).length,
    classes: [...html.matchAll(/data-quant-class="([^"]*)"/g)].map((x) => x[1]),
    providers: [...html.matchAll(/<li class="mc-art[^"]*" data-mc-art="[^"]*" data-provider="([^"]*)"/g)].map((x) => x[1]),
    hash: String(location.hash),
    notice: html.includes('data-mc-notice="quant_class"') && html.includes("This gateway's catalog does not report quant_class yet"),
    quantDisabled: /data-mc-filter="quant"[^>]*disabled/.test(html) && !/data-mc-filter="quant"[^>]*aria-pressed="true"/.test(html),
    boldQuant: [...html.matchAll(/<div class="mc-art__quant"[^>]*><b>([^<]*)<\/b>/g)].map((x) => x[1]),
    empty: html.includes('data-mc-empty="1"') && html.includes('data-mc-action="clear"'),
    hfEmpty: html.includes('data-mc-empty="hf"') && html.includes("Hugging Face has no model matching"),
    hfError: html.includes('data-mc-hf="error"') && html.includes("connection refused") && html.includes('data-mc-action="hf-search"'),
    hubNotice: html.includes('data-mc-notice="hub"') && html.includes("rate limited"),
    progress: /class="ui-progress"[^>]*data-state="downloading"/.test(html) && html.includes('aria-valuenow="37"') && html.includes('data-mc-action="cancel" data-job="dl-live"'),
  };
};
vm.runInContext(`mountModelCatalog("tab", ROOT, { syncHash: true, filters: mcParseHash(location.hash) })`, Object.assign(ctx, { ROOT: root }));
await settle();
snap("initial");
if (__SCRIPT__ === "filters") {
  chip("quant", "8bit"); snap("quant8");
  chip("provider", "mlx"); snap("quant8_mlx");
  chip("quant", "all"); chip("provider", "all");
  root.onchange({ target: { dataset: { mcFits: "1" }, checked: true } }); snap("fits");
  root.onchange({ target: { dataset: { mcFits: "1" }, checked: false } });
  chip("status", "downloaded"); snap("downloaded"); chip("status", "all");
  chip("cap", "vision"); snap("vision"); chip("cap", "all");
  vm.runInContext(`mcSetFilter(mcStore.views.get("tab"), { q: "org-7/ lmstudio" })`, ctx); snap("search");
  vm.runInContext(`mcSetFilter(mcStore.views.get("tab"), { q: "no-such-model" })`, ctx); snap("nomatch");
  root.onclick({ target: { closest: (sel) => (sel === "[data-mc-action]" ? { disabled: false, dataset: { mcAction: "clear" } } : null) } }); snap("cleared");
  out.parsed = vm.runInContext(`mcParseHash("#catalog?q=qwen%203&quant=8bit&provider=mlx&cap=vision&status=downloaded&fits=1")`, ctx);
  out.roundtrip = vm.runInContext(`mcHashFor(mcParseHash("#catalog?q=qwen%203&quant=8bit&provider=mlx&cap=vision&status=downloaded&fits=1"))`, ctx);
  // Download -> N's POST, the job goes to the ONE feed; a live job renders.
  const first = FIXTURE.rows[1].artifacts[0];
  root.onclick({ target: { closest: (sel) => (sel === "[data-mc-action]" ? { disabled: false, dataset: { mcAction: "download", provider: first.provider, artifact: first.artifact } } : null) } });
  await settle();
  out.post = calls.filter((c) => c.path === "/api/gateway/models/download").map((c) => JSON.parse(c.body));
  out.tracked = tracked.length;
  ctx.state.downloadJobs.set(`${first.provider}/${first.artifact}`, { job: "dl-live", provider: first.provider, artifact: first.artifact, status: "running", state: "downloading", bytes_done: 370, bytes_total: 1000, bytes_per_second: 100, eta_s: 6 });
  vm.runInContext("mcOnDownloads()", ctx); snap("live");
}
if (__SCRIPT__ === "hash") {
  snap("fromhash");
}
if (__SCRIPT__ === "hf") {
  out.hubCalls = calls.filter((c) => c.path.startsWith("/api/gateway/models/catalog?")).map((c) => c.path);
  out.html = root.innerHTML;
  // Back to the catalog, then Hugging Face again with a new query (Enter).
  root.onclick({ target: { closest: (sel) => (sel === "[data-mc-action]" ? { disabled: false, dataset: { mcAction: "mode", mcMode: "catalog" } } : null) } });
  snap("backToCatalog");
  root.onclick({ target: { closest: (sel) => (sel === "[data-mc-action]" ? { disabled: false, dataset: { mcAction: "mode", mcMode: "hf" } } : null) } });
  root.onkeydown({ key: "Enter", preventDefault() {}, target: { value: "qwen coder", dataset: { mcSearch: "1" } } });
  await settle();
  snap("second");
  out.hubCalls2 = calls.filter((c) => c.path.startsWith("/api/gateway/models/catalog?")).map((c) => c.path);
}
if (__SCRIPT__ === "unreported") {
  chip("quant", "8bit", true); snap("clicked");
}
console.log(JSON.stringify(out));
"""


def _run(fixture: dict, script: str, hash_: str = "", js: str | None = None, hub: dict | None = None) -> dict:
    node = require_node()
    source = js if js is not None else CONSOLE_UI_JS + CATALOG_JS
    harness = (
        _HARNESS.replace("__SOURCE__", json.dumps(source))
        .replace("__FIXTURE__", json.dumps(fixture))
        .replace("__HUB__", json.dumps(hub))
        .replace("__HASH__", json.dumps(hash_))
        .replace("__SCRIPT__", json.dumps(script))
    )
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as f:
        f.write(harness)
        path = f.name
    r = subprocess.run([node, path], capture_output=True, text=True, check=False, timeout=60)
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(r.stdout.strip().splitlines()[-1])


def check_filters(out: dict, cat: dict) -> None:
    """Every assertion of the filters scenario (shared with the mutation run)."""
    n = len(cat["rows"])
    total_arts = sum(len(r["artifacts"]) for r in cat["rows"])
    # One card per model, EVERY model (no cap).
    assert out["initial"]["cards"] == n
    assert out["initial"]["arts"] == total_arts
    assert out["initial"]["count"] == [n, n, total_arts]
    assert out["initial"]["hash"] == "#catalog"
    assert out["initial"]["notice"] is False
    assert set(out["initial"]["boldQuant"]) == {"4-bit", "8-bit", "16-bit"}
    # 8-bit: only quant_class 8bit rows, count and link follow.
    m, a = _expected(cat, lambda r, x: x["quant_class"] == "8bit")
    assert set(out["quant8"]["classes"]) == {"8bit"}
    assert out["quant8"]["count"] == [m, n, a] and out["quant8"]["cards"] == m and out["quant8"]["arts"] == a
    assert out["quant8"]["hash"] == "#catalog?quant=8bit"
    m, a = _expected(cat, lambda r, x: x["quant_class"] == "8bit" and x["provider"] == "mlx")
    assert set(out["quant8_mlx"]["classes"]) == {"8bit"} and set(out["quant8_mlx"]["providers"]) == {"mlx"}
    assert out["quant8_mlx"]["count"] == [m, n, a]
    assert out["quant8_mlx"]["hash"] == "#catalog?quant=8bit&provider=mlx"
    m, a = _expected(cat, lambda r, x: x["fit"]["verdict"] in ("fits", "tight"))
    assert out["fits"]["count"] == [m, n, a] and out["fits"]["hash"] == "#catalog?fits=1"
    m, a = _expected(cat, lambda r, x: x["presence"]["status"] == "installed")
    assert out["downloaded"]["count"] == [m, n, a] and m > 0
    m, a = _expected(cat, lambda r, x: bool(r["capabilities"].get("vision")))
    assert out["vision"]["count"] == [m, n, a] and out["vision"]["hash"] == "#catalog?cap=vision"
    assert out["search"]["count"] == [1, n, 1] and out["search"]["providers"] == ["lmstudio"]
    assert out["search"]["hash"].startswith("#catalog?q=org-7%2F%20lmstudio")
    assert out["nomatch"]["empty"] is True and out["nomatch"]["count"] == [0, n, 0]
    assert out["cleared"]["count"] == [n, n, total_arts] and out["cleared"]["hash"] == "#catalog"
    assert out["parsed"] == {"q": "qwen 3", "quant": "8bit", "provider": "mlx", "cap": "vision", "status": "downloaded", "fits": True, "hf": None}
    assert out["roundtrip"] == "#catalog?q=qwen%203&quant=8bit&provider=mlx&cap=vision&status=downloaded&fits=1"
    first = cat["rows"][1]["artifacts"][0]
    assert out["post"] == [{"provider": first["provider"], "artifact": first["artifact"], "expected_bytes": first["download_bytes"]}]
    assert out["tracked"] == 1
    assert out["live"]["progress"] is True


def check_unreported(out: dict, cat: dict) -> None:
    n = len(cat["rows"])
    total_arts = sum(len(r["artifacts"]) for r in cat["rows"])
    for snap in ("initial", "clicked"):
        assert out[snap]["notice"] is True, snap
        assert out[snap]["quantDisabled"] is True, snap
        # Every artifact is still listed; the class is never guessed.
        assert out[snap]["count"] == [n, n, total_arts], snap
        assert out[snap]["classes"] == [""] * total_arts, snap
        assert not {"4-bit", "8-bit", "16-bit"} & set(out[snap]["boldQuant"]), snap
        assert set(out[snap]["boldQuant"]) == {"q4_k_m", "q8_0", "f16"}, snap


def _unreported() -> dict:
    cat = _catalog(12)
    for row in cat["rows"]:
        for a in row["artifacts"]:
            del a["quant_class"]
    return cat


def test_catalog_is_spliced_into_the_page_once() -> None:
    html = gateway_console_html()
    assert html.count("function mountModelCatalog(") == 1
    assert html.count(".mc-card {") == 1
    assert html.count('id="catalog-cards-root"') == 1
    panel = html[html.index('id="tab-catalog"') : html.index('id="tab-engines"')]
    assert panel.index('id="catalog-cards-root"') < panel.index('id="catalog-core-root"')


def test_filters_counts_hash_and_downloads() -> None:
    cat = _catalog(80)
    check_filters(_run(cat, "filters"), cat)


def test_a_catalog_link_reproduces_the_view() -> None:
    cat = _catalog(80)
    out = _run(cat, "hash", "#catalog?quant=8bit&provider=mlx&fits=1")
    m, a = _expected(cat, lambda r, x: x["quant_class"] == "8bit" and x["provider"] == "mlx" and x["fit"]["verdict"] in ("fits", "tight"))
    assert out["fromhash"]["count"] == [m, 80, a]
    assert out["fromhash"]["hash"] == "#catalog?quant=8bit&provider=mlx&fits=1"


def test_missing_quant_class_is_said_out_loud_and_never_guessed() -> None:
    cat = _unreported()
    check_unreported(_run(cat, "unreported", "#catalog?quant=8bit"), cat)


def test_no_quant_class_derivation_in_the_view() -> None:
    # The class comes from AbstractCore (mission W1). The view reads
    # `quant_class` and the raw `quant` string only for display.
    js = CATALOG_JS
    assert "a.quant_class" in js
    for pattern in (r"\.quant\b[^;\n]*\.(match|test|replace|startsWith|includes)\(", r"/\(?\\d\+?\)?bit/", r"q4_k_m|q8_0", r"a\.bits\s*[<>=]"):
        assert not re.search(pattern, js), pattern


def test_catalog_css_rides_the_kit_tokens() -> None:
    for m in re.finditer(r"border-radius:\s*([^;]+);", CATALOG_CSS):
        value = m.group(1).strip()
        assert value.startswith("var(--radius-") or value == "999px", value
    # Nothing in the view character-wraps an id (ADR-0026).
    assert "overflow-wrap: anywhere" not in CATALOG_CSS and "word-break: break-all" not in CATALOG_CSS


def test_fixture_is_not_mutated_between_runs() -> None:
    cat = _catalog(5)
    before = copy.deepcopy(cat)
    _run(cat, "hash")
    assert cat == before


def _hub(rows: int) -> dict:
    cat = _catalog(rows)
    for i, row in enumerate(cat["rows"]):
        row["id"] = f"hf:org-{i}/hub-model-{i}"
        row["source"] = "hf_search"
        row["capabilities"] = {"text": None}
        for a in row["artifacts"]:
            a["provider"] = "mlx"
            a["quant_class"] = "unknown" if i % 2 else "8bit"
            a["quant"] = None if i % 2 else "8bit"
    cat["hub"] = {"enabled": True, "ok": True, "errors": []}
    return cat


def check_hf(out: dict, hub: dict) -> None:
    n = len(hub["rows"])
    arts = sum(len(r["artifacts"]) for r in hub["rows"])
    assert out["hubCalls"] == ["/api/gateway/models/catalog?q=smol%20lm&hub=true"]
    # The Hub answer is drawn as the SAME cards and rows, every one of them.
    assert out["initial"]["cards"] == n and out["initial"]["arts"] == arts
    assert out["initial"]["count"] == [n, n, arts]
    assert out["initial"]["hash"] == "#catalog?hf=smol%20lm"
    assert 'aria-pressed="true">Hugging Face</button>' in out["html"]
    assert "Hugging Face</span>" in out["html"]  # the card badge
    # quant_class "unknown" reads "Not stated", never a guessed class.
    assert set(out["initial"]["boldQuant"]) == {"8-bit", "Not stated"}
    assert 'data-mc-action="download"' in out["html"]
    assert out["backToCatalog"]["hash"] == "#catalog" and out["backToCatalog"]["cards"] == 80
    assert out["hubCalls2"][-1] == "/api/gateway/models/catalog?q=qwen%20coder&hub=true"
    assert out["second"]["hash"] == "#catalog?hf=qwen%20coder"


def test_hugging_face_search_renders_cards_and_keeps_the_query_in_the_link() -> None:
    hub = _hub(6)
    check_hf(_run(_catalog(80), "hf", "#catalog?hf=smol%20lm", hub=hub), hub)


def test_hugging_face_empty_and_unreachable_are_plain() -> None:
    empty = {"schema": "model_catalog_v1", "rows": [], "hub": {"enabled": True, "ok": True, "errors": []}}
    out = _run(_catalog(10), "hash", "#catalog?hf=zzz", hub=empty)
    assert out["fromhash"]["cards"] == 0 and out["fromhash"]["hfEmpty"] and not out["fromhash"]["hfError"]
    down = _run(_catalog(10), "hash", "#catalog?hf=zzz", hub=None)
    assert down["fromhash"]["cards"] == 0 and down["fromhash"]["hfError"] and not down["fromhash"]["hfEmpty"]
    partial = dict(_hub(2), hub={"enabled": True, "ok": False, "errors": ["rate limited by the Hugging Face API (429)"]})
    part = _run(_catalog(10), "hash", "#catalog?hf=zzz", hub=partial)
    assert part["fromhash"]["cards"] == 2 and part["fromhash"]["hubNotice"]


def test_hugging_face_texts() -> None:
    js = CATALOG_JS
    for text in ("Hugging Face has no model matching", "Hugging Face could not be searched right now.", "Hugging Face could not be reached, so these results may be incomplete."):
        assert text in js
    assert "export " not in js and "HF_TOKEN" not in js and "environment variable" not in js
