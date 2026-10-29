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
const INSTALLED = __INSTALLED__;
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
  ICONS: { trash: "<svg data-icon=\"trash\"></svg>" },
  downloadJobKey: (p, a) => `${p || ""}/${a || ""}`,
  servedModelId: (p, a) => a,
  trackDownloadJob: (job) => { tracked.push(job); },
  api: async (path, opts) => {
    calls.push({ path, method: (opts && opts.method) || "GET", body: (opts && opts.body) || "" });
    if (path === "/api/gateway/models/catalog") return JSON.parse(JSON.stringify(FIXTURE));
    if (path.startsWith("/api/gateway/models/catalog?")) {
      if (HUB === null) throw new Error("hub search failed (offline?): connection refused");
      if (HUB.__pages) {  // one answer per `hub_page` (page 1 when absent)
        const m = /[?&]hub_page=(\d+)/.exec(path);
        return JSON.parse(JSON.stringify(HUB.__pages[(m ? Number(m[1]) : 1) - 1]));
      }
      return JSON.parse(JSON.stringify(HUB));
    }
    if (path === "/api/gateway/models/installed") {
      if (INSTALLED === null) throw new Error("installed list failed: connection refused");
      return JSON.parse(JSON.stringify(INSTALLED));
    }
    if (path === "/api/gateway/models/delete-download") {
      const body = JSON.parse(opts.body);
      if (!body.dry_run) INSTALLED.rows = INSTALLED.rows.filter((r) => !(r.provider === body.provider && r.artifact === body.artifact));
      return body.dry_run ? { status: "planned", freed_bytes: 2000000000, also_used_by: [] } : { status: "deleted", freed_bytes: 2000000000, presence: "absent" };
    }
    if (path === "/api/gateway/models/download") return { ok: true, job: { job: "dl-1", provider: "mlx", artifact: "x", status: "running", state: "downloading" } };
    return {};
  },
});
vm.runInContext(source, ctx);
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setTimeout(r, 1)); };
const out = {};
const view = () => vm.runInContext("mcStore.views.get('tab')", ctx);
const chip = (group, value, disabled) => root.onclick({ target: { closest: (sel) => (sel === "[data-af-facet]" ? { disabled: !!disabled, dataset: { afFacet: group, afValue: value } } : null) } });
const scope = (mode) => root.onclick({ target: { closest: (sel) => (sel === "[data-af-scope]" ? { dataset: { afScope: mode } } : null) } });
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
    quantDisabled: /data-af-facet="quant"[^>]*disabled/.test(html) && !/data-af-facet="quant"[^>]*aria-pressed="true"/.test(html),
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
  scope("catalog");
  snap("backToCatalog");
  scope("hf");
  root.onkeydown({ key: "Enter", preventDefault() {}, target: { value: "qwen coder", dataset: { mcSearch: "1" } } });
  await settle();
  snap("second");
  out.hubCalls2 = calls.filter((c) => c.path.startsWith("/api/gateway/models/catalog?")).map((c) => c.path);
}
if (__SCRIPT__ === "host") {
  out.host = vm.runInContext("mcHostMarkup()", ctx);
}
if (__SCRIPT__ === "hfmore") {
  out.html1 = root.innerHTML;
  const more = () => root.onclick({ target: { closest: (sel) => (sel === "[data-mc-action]" ? { disabled: false, dataset: { mcAction: "hub-more" } } : null) } });
  more(); await settle(); snap("page2"); out.html2 = root.innerHTML;
  more(); await settle(); snap("page3"); out.html3 = root.innerHTML;
  out.hubCalls = calls.filter((c) => c.path.startsWith("/api/gateway/models/catalog?")).map((c) => c.path);
}
if (__SCRIPT__ === "beside") {
  out.html = root.innerHTML;
  out.hubCalls = calls.filter((c) => c.path.startsWith("/api/gateway/models/catalog?")).map((c) => c.path);
  // Switching to Hugging Face mode with the same words reuses the answer (no second request).
  scope("hf");
  await settle();
  snap("hf"); out.hfHtml = root.innerHTML;
  out.hubCalls2 = calls.filter((c) => c.path.startsWith("/api/gateway/models/catalog?")).map((c) => c.path);
}
if (__SCRIPT__ === "every") {
  out.calls1 = calls.filter((c) => c.path.startsWith("/api/gateway/models/catalog")).map((c) => c.path);
  root.onchange({ target: { dataset: { mcEvery: "1" }, checked: true } });
  await settle();
  snap("every"); out.html = root.innerHTML;
  out.calls2 = calls.filter((c) => c.path.startsWith("/api/gateway/models/catalog")).map((c) => c.path);
  root.onchange({ target: { dataset: { mcEvery: "1" }, checked: false } });
  await settle();
  snap("plain");
  out.calls3 = calls.filter((c) => c.path.startsWith("/api/gateway/models/catalog")).map((c) => c.path);
}
if (__SCRIPT__ === "hfevery") {
  const hubCalls = () => calls.filter((c) => c.path.startsWith("/api/gateway/models/catalog?q=")).map((c) => c.path);
  out.hubCalls1 = hubCalls();
  out.html = root.innerHTML;
  // The Catalog's switch never re-asks Hugging Face (its search is always every quant).
  root.onchange({ target: { dataset: { mcEvery: "1" }, checked: true } });
  await settle();
  out.hubCalls2 = hubCalls();
}
if (__SCRIPT__ === "gpulimit") {
  out.html = root.innerHTML;
  root.onchange({ target: { dataset: { mcFits: "1" }, checked: true } }); snap("fits");
  out.fitsHtml = root.innerHTML;
}
if (__SCRIPT__ === "merged") {
  out.html = root.innerHTML;
  chip("status", "downloaded"); snap("downloaded"); out.downloadedHtml = root.innerHTML;
  chip("status", "not_downloaded"); snap("notDownloaded"); out.notDownloadedHtml = root.innerHTML;
  chip("status", "downloaded");
  chip("provider", "ollama"); snap("ollama"); out.ollamaHtml = root.innerHTML; chip("provider", "all");
  chip("cap", "vision"); snap("vision"); out.visionHtml = root.innerHTML; chip("cap", "all");
  vm.runInContext(`mcSetFilter(mcStore.views.get("tab"), { q: "my-own" })`, ctx); snap("search"); out.searchHtml = root.innerHTML;
  vm.runInContext(`mcSetFilter(mcStore.views.get("tab"), { q: "" })`, ctx);
  if (INSTALLED) {
  const X = INSTALLED.rows[0];
  const click = (action) => root.onclick({ target: { closest: (sel) => (sel === "[data-mc-action]" ? { disabled: false, dataset: { mcAction: action, provider: X.provider, artifact: X.artifact } } : null) } });
  click("delete-ask"); await settle(); out.confirmHtml = root.innerHTML;
  vm.runInContext(`for (const v of mcStore.del.values()) v.at = 0`, ctx);
  click("delete-confirm"); await settle(); await settle(); snap("afterDelete"); out.afterDeleteHtml = root.innerHTML;
  out.deletes = calls.filter((c) => c.path === "/api/gateway/models/delete-download").map((c) => JSON.parse(c.body));
  }
}
if (__SCRIPT__ === "unloadable") {
  out.html = root.innerHTML;
}
if (__SCRIPT__ === "unreported") {
  chip("quant", "8bit", true); snap("clicked");
}
console.log(JSON.stringify(out));
"""


EMPTY_INSTALLED = {"schema": "models_installed_v1", "rows": [], "errors": {}}


def _run(fixture: dict, script: str, hash_: str = "", js: str | None = None, hub: dict | None = None, installed: dict | None = EMPTY_INSTALLED) -> dict:
    node = require_node()
    source = js if js is not None else CONSOLE_UI_JS + CATALOG_JS
    harness = (
        _HARNESS.replace("__SOURCE__", json.dumps(source))
        .replace("__FIXTURE__", json.dumps(fixture))
        .replace("__HUB__", json.dumps(hub))
        .replace("__INSTALLED__", json.dumps(installed))
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
    assert out["parsed"] == {"q": "qwen 3", "quant": "8bit", "provider": "mlx", "cap": "vision", "status": "downloaded", "fits": True, "every": False, "hf": None}
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
    panel = html[html.index('id="tab-catalog"') : html.index('id="tab-apps"')]
    # R5.2: one page. The cards are the whole panel; no second "On this computer" list.
    assert 'id="catalog-cards-root"' in panel
    assert 'catalog-core-root' not in panel and "On this computer" not in panel and "mc-installed" not in panel


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
    assert out["hubCalls"] == ["/api/gateway/models/catalog?q=smol%20lm&hub=true&extended=true"]
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
    assert out["hubCalls2"][-1] == "/api/gateway/models/catalog?q=qwen%20coder&hub=true&extended=true"
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


@pytest.mark.parametrize("profile,expected,absent", [
    ({"accelerator": "cuda", "gpu_name": "Quadro RTX 5000", "unified_memory": False, "ram_bytes": 28734308352,
      "vram_bytes": 16106127360, "ceiling_bytes": 16106127360}, ["Quadro RTX 5000", "16.1 GB VRAM", "28.7 GB RAM"], ["models up to", "memory"]),
    ({"accelerator": "metal", "unified_memory": True, "ram_bytes": 137438953472, "ceiling_bytes": 103079215104},
     ["137 GB unified memory", "models up to about"], ["VRAM"]),
])
def test_the_machine_line_shows_vram_and_ram_separately(profile, expected, absent) -> None:
    """QA 2026-10-28 (e): a 16 GB GPU + 26 GB RAM server read "28.7 GB memory · models up to 16.1 GB"."""
    cat = dict(_catalog(2), host_profile=profile)
    host = _run(cat, "host")["host"]
    for text in expected:
        assert text in host, host
    for text in absent:
        assert text not in host, host


def _hub_page(first: int, n: int, *, page: int, more: bool, handle: str | None = None, found: bool = True) -> dict:
    cat = _hub(first + n)
    cat["rows"] = cat["rows"][first:]
    cat["hub"]["search"] = {"query": "smol lm", "page": page, "page_size": n, "rows": n, "has_more": more,
                            "handle": handle, "handle_found": bool(handle) and found}
    return cat


def test_hugging_face_results_page_with_a_count_and_show_more() -> None:
    """Backlog 0864 (front item 4): the count says how many Hugging Face results are shown and
    whether more exist; "Show more results" appends the next page; the last page has no button."""
    hub = {"__pages": [_hub_page(0, 4, page=1, more=True), _hub_page(4, 4, page=2, more=True), _hub_page(8, 2, page=3, more=False)]}
    out = _run(_catalog(10), "hfmore", "#catalog?hf=smol%20lm", hub=hub)
    assert out["initial"]["cards"] == 4
    assert 'data-mc-hub-count="4">Showing 4 Hugging Face results; more are available.' in out["html1"]
    assert 'data-mc-action="hub-more"' in out["html1"]
    assert out["page2"]["cards"] == 8 and 'data-mc-hub-count="8"' in out["html2"]
    assert out["page3"]["cards"] == 10 and 'data-mc-hub-count="10">Showing 10 Hugging Face results.</span>' in out["html3"]
    assert 'data-mc-action="hub-more"' not in out["html3"]
    assert out["hubCalls"] == [
        "/api/gateway/models/catalog?q=smol%20lm&hub=true&extended=true",
        "/api/gateway/models/catalog?q=smol%20lm&hub=true&extended=true&hub_page=2",
        "/api/gateway/models/catalog?q=smol%20lm&hub=true&extended=true&hub_page=3",
    ]


def test_a_handle_hugging_face_does_not_have_is_said() -> None:
    hub = _hub_page(0, 2, page=1, more=False, handle="nobody/Smol-LM", found=False)
    out = _run(_catalog(10), "hash", "#catalog?hf=nobody%2FSmol-LM", hub=hub)
    assert out["fromhash"]["cards"] == 2
    html = _run(_catalog(10), "hfmore", "#catalog?hf=nobody%2FSmol-LM", hub=hub)["html1"]
    assert "Hugging Face has no repo named “nobody/Smol-LM”; these models match its name." in html


def test_catalog_search_shows_hugging_face_results_beside_the_catalog() -> None:
    """Batch item (a): searching the catalog also asks Hugging Face for the same words; its
    results come below the catalog's cards, and Hugging Face mode reuses that answer."""
    cat = _catalog(10)
    hub = _hub_page(0, 3, page=1, more=True)
    hub["rows"].insert(0, dict(cat["rows"][0], source="curated"))  # a curated row also in the answer: drawn once
    out = _run(cat, "beside", "#catalog?q=org-1", hub=hub)
    assert out["hubCalls"] == ["/api/gateway/models/catalog?q=org-1&hub=true&extended=true"]
    assert '<h4 class="mc-hub-beside__title">From Hugging Face</h4>' in out["html"]
    assert 'data-mc-hub-beside="3"' in out["html"]
    assert out["html"].count('data-mc-model="hf:') == 3
    assert 'data-mc-hub-count="3">Showing 3 Hugging Face results; more are available.' in out["html"]
    assert out["hubCalls2"] == out["hubCalls"]  # the mode switch asked nothing new
    assert out["hf"]["hash"] == "#catalog?hf=org-1" and 'data-mc-hub-beside' not in out["hfHtml"]


def test_every_quant_asks_for_the_whole_quant_set_and_shows_it() -> None:
    """Batch item (b): "Every quant" asks the catalog with `hub=true&extended=true` (AbstractCore 0865)
    and draws the extra quants as rows of the same cards; off, the plain catalog again; in the link."""
    cat = _catalog(10)
    extended = json.loads(json.dumps(cat))
    for row in extended["rows"]:
        row["artifacts"].append(dict(row["artifacts"][0], artifact=row["artifacts"][0]["artifact"] + ":UD-Q3_K_XL", extended=True))
    out = _run(cat, "every", "", hub=extended)
    assert out["calls1"] == ["/api/gateway/models/catalog"]
    assert out["calls2"][-1] == "/api/gateway/models/catalog?hub=true&extended=true"
    assert out["every"]["arts"] == out["initial"]["arts"] + 10 and out["every"]["hash"] == "#catalog?every=1"
    assert 'data-mc-every="1" checked' in out["html"]
    assert out["calls3"][-1] == "/api/gateway/models/catalog" and out["plain"]["arts"] == out["initial"]["arts"]


def test_a_hugging_face_search_lists_every_quant_and_has_no_switch() -> None:
    """Operator ruling (2026-10-29): "this is a Hugging Face search, it should show what's available,
    period". OVH 8443 showed 2 of the 24 quants of unsloth/Qwen3.8-27B-GGUF. The Hugging Face tab now
    always asks `extended=true` and shows no "Every quant" switch (the Catalog tab keeps it)."""
    cat = _catalog(10)
    hub = _hub_page(0, 3, page=1, more=False)
    out = _run(cat, "hfevery", "#catalog?hf=unsloth%2FQwen3.8-27B-GGUF", hub=hub)
    assert out["hubCalls1"] == ["/api/gateway/models/catalog?q=unsloth%2FQwen3.8-27B-GGUF&hub=true&extended=true"]
    assert "data-mc-every-switch" not in out["html"] and "Every quant" not in out["html"], out["html"][:600]
    assert out["hubCalls2"] == out["hubCalls1"]
    catalog = _run(cat, "every", "", hub=hub)  # the Catalog tab keeps its switch, shown
    assert 'data-mc-every-switch="1" title=' in catalog["html"]


def _gpu_limit_catalog() -> dict:
    """The 128 GiB Mac case: the recommendation fits once the GPU wired limit is raised."""
    cat = _catalog(3)
    art = cat["rows"][0]["artifacts"][1]
    art["fit"] = {
        "verdict": "needs_gpu_limit",
        "notes": ["fits once the GPU may use more memory: sudo sysctl iogpu.wired_limit_mb=118000"],
        "gpu_limit": {"sysctl": "iogpu.wired_limit_mb", "required_mb": 118000, "command": "sudo sysctl iogpu.wired_limit_mb=118000"},
    }
    for row in cat["rows"][1:]:
        for a in row["artifacts"]:
            a["fit"] = {"verdict": "too_large", "notes": []}
    for a in cat["rows"][0]["artifacts"]:
        if a is not art:
            a["fit"] = {"verdict": "too_large", "notes": []}
    return cat


def test_needs_gpu_limit_shows_its_badge_and_command_and_counts_as_fitting() -> None:
    """AbstractCore 2.18's needs_gpu_limit verdict: never "Fit unknown", the
    sysctl command is on the card, and the fits filter keeps it (core's
    FITS_FILTER_VERDICTS), so the 128 GiB recommendation is not hidden."""
    cat = _gpu_limit_catalog()
    out = _run(cat, "gpulimit")
    html = out["html"]
    assert "Needs GPU limit" in html
    assert html.count('data-mc-gpu-limit="1"') == 1
    assert "sudo sysctl iogpu.wired_limit_mb=118000" in html and "115 GiB" in html
    assert out["fits"]["count"] == [1, 3, 1]
    assert 'data-mc-gpu-limit="1"' in out["fitsHtml"]


def test_fits_filter_matches_abstractcore_fits_filter_verdicts() -> None:
    from abstractcore.config.model_catalog import FITS_FILTER_VERDICTS

    m = re.search(r"const MC_FITS_FILTER_VERDICTS = (\[[^\]]*\]);", CATALOG_JS)
    assert m, "the console names its fits-filter verdicts"
    assert tuple(json.loads(m.group(1))) == tuple(FITS_FILTER_VERDICTS)


def test_a_gguf_this_llamacpp_cannot_load_greys_download_and_use_as_default() -> None:
    """AbstractCore 0867/0868 (OVH: XHToken/Spark-X2.6-4B-GGUF, architecture spark2_5): a build the gateway's
    llama.cpp cannot load is never offered for download or as the default; the button is greyed with the
    sentence as its tooltip. Downloaded: the installed row's `loadable: false` (0867); not yet: the catalog
    artifact's, from the Hub's architecture (0868)."""
    reason = "This computer's llama.cpp (llama-cpp-python 0.3.16) cannot load the spark2_5 architecture. Update llama-cpp-python, or choose another model."
    cat = _catalog(2)
    row = cat["rows"][1]
    assert row["capabilities"]["text"] is True and not row["capabilities"]["embedding"]
    a0, a1, a2 = row["artifacts"]
    # Not downloaded, the Hub says spark2_5: core marks it not downloadable with the reason.
    a1.update(loadable=False, unloadable_reason=reason, downloadable=False, gguf_architecture="spark2_5")
    # Downloaded: the installed row (GET /models/installed) carries the reason.
    a0["presence"] = {"status": "installed", "location": None, "evidence": None}
    installed = {"schema": "models_installed_v1", "errors": {}, "rows": [
        {"provider": a0["provider"], "artifact": a0["artifact"], "loadable": False, "unloadable_reason": reason},
        # A downloaded model outside the catalog (#45's "Use as default"): greyed the same way.
        {"provider": "huggingface", "artifact": "XHToken/Spark-X2.6-4B-GGUF", "kind": "model", "tasks": ["text_generation"],
         "loadable": False, "unloadable_reason": reason},
    ]}
    html = _run(cat, "unloadable", installed=installed)["html"]
    card = html[html.index('data-mc-model="model-1"'):]
    greyed = f'<button type="button" class="ui-btn is-ghost" data-mc-unloadable="1" disabled title="{reason.replace(chr(39), "&#39;")}">'
    assert greyed + "Download</button>" in card
    assert greyed + "Use as default</button>" in card
    # The other build of the same model is offered as usual.
    assert f'data-mc-action="download" data-provider="{a2["provider"]}" data-artifact="{a2["artifact"]}"' in card
    extra = html[html.index("XHToken/Spark-X2.6-4B-GGUF"):]
    assert greyed + "Use as default</button>" in extra and 'data-mc-action="default" data-provider="huggingface" data-artifact="XHToken/Spark-X2.6-4B-GGUF"' not in extra
    # Model 0's downloaded build (no flag) keeps its working buttons.
    assert 'data-mc-unloadable' not in html[:html.index('data-mc-model="model-1"')]



def test_an_unloadable_gguf_a_newer_llamacpp_loads_offers_the_update() -> None:
    """AbstractCore `llamacpp_update`: "Update llama.cpp" instead of only greying (operator
    2026-10-29). MUTANT: greyed Download only -> RED."""
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    assert 'data-mc-action="llama-update"' in html and "Update llama.cpp" in html
    assert '/api/gateway/engines/llamacpp/install' in html
    assert "mcLlamaUpdateMarkup(a, unloadable, admin) ||" in html, "the greyed button stays only without an update"
