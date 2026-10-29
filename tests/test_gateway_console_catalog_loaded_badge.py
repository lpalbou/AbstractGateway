"""Models catalog: a build that is in memory now shows a small "Loaded" / "Kept loaded" badge that
opens the Resources page (operator ruling 2026-10-29: residency controls live on Resources; the
Models page stays the catalog). Read from the host snapshot (`state.hostState`). MUTANT: no badge
(or the badge not opening Resources) -> RED."""
from __future__ import annotations

import json
import subprocess
import tempfile

from node_requirement import require_node

from abstractgateway.console_catalog import CATALOG_JS
from abstractgateway.console_islands import ISLANDS_JS

_SCRIPT = r"""
import vm from "node:vm";
const clicks = [];
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
// The REAL vendored kit bundle: the catalog's filter bar is its filterBar* (no console copy).
const KIT_WINDOW = (() => {
  const kit = vm.createContext({ console });
  kit.globalThis = kit;
  kit.window = kit;
  vm.runInContext(__ISLANDS__, kit);
  const lib = kit.AfConsoleIslands;
  return { AfConsoleIslands: { filterBarFacetsHtml: lib.filterBarFacetsHtml, filterBarScopeHtml: lib.filterBarScopeHtml, filterBarScopeButtonsHtml: lib.filterBarScopeButtonsHtml } };
})();
const ctx = vm.createContext({
  console, setTimeout, clearTimeout, encodeURIComponent, decodeURIComponent,
  location: { hash: "", pathname: "/console", search: "" }, history: { replaceState() {} }, document: { activeElement: null }, window: KIT_WINDOW,
  esc: (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]),
  ICONS: { trash: "<svg data-icon=\"trash\"></svg>", lock: "<svg data-icon=\"lock\"></svg>" },
  $: (id) => ({ click: () => clicks.push(id) }),
  state: { principal: { admin: true }, downloadJobs: new Map(), defaults: [], activeTab: "catalog", providerLabels: new Map(),
           hostState: { models: [
             { provider: "huggingface", model: "mradermacher/Qwen3.5-9B-Kimi-k3-Distilled-GGUF", resident: true, locked: false,
               held_by: ["the runtime of account default/admin (loaded)"], model_path: "/hub/x/Qwen3.5-9B-Kimi-k3-Distilled.Q4_K_M.gguf" },
             { provider: "mlx", model: "mlx-community/Qwen3-4B-4bit", resident: true, locked: true, held_by: [] },
             { provider: "huggingface", model: "unsloth/Qwen3-4B-GGUF", resident: false } ] } },
  api: async () => ({}),
  uiRestoreDownloads: () => {}, uiPill: (l, t) => `<span class="ui-pill tone-${t}">${l}</span>`, uiNum: (v) => typeof v === "number",
  uiBytes: (n) => `${n} B`, uiDetails: () => "", uiCopy: () => {}, dlActive: () => false, dlStatePill: () => "", dlJobId: (j) => j && j.job, trackDownloadJob: () => {}, downloadJobKey: (p, a) => `${p}/${a}`, servedModelId: (p, a) => a,
});
vm.runInContext(__SOURCE__, ctx);
const b = (a) => vm.runInContext(`mcLoadedBadge(${JSON.stringify(a)})`, ctx);
const out = {
  loaded: b({ provider: "huggingface", artifact: "mradermacher/Qwen3.5-9B-Kimi-k3-Distilled-GGUF:Q4_K_M" }),
  otherQuant: b({ provider: "huggingface", artifact: "mradermacher/Qwen3.5-9B-Kimi-k3-Distilled-GGUF:Q8_0" }),
  kept: b({ provider: "mlx", artifact: "mlx-community/Qwen3-4B-4bit" }),
  notResident: b({ provider: "huggingface", artifact: "unsloth/Qwen3-4B-GGUF" }),
  otherProvider: b({ provider: "lmstudio", artifact: "mlx-community/Qwen3-4B-4bit" }),
};
out.row = vm.runInContext(`mcArtMarkup({}, ${JSON.stringify({ provider: "mlx", artifact: "mlx-community/Qwen3-4B-4bit", presence: { status: "installed" }, fit: { verdict: "fits" } })}, true)`, ctx);
const root = { innerHTML: "", dataset: {} };
vm.runInContext(`mountModelCatalog("t", ROOT, {})`, Object.assign(ctx, { ROOT: root }));
root.onclick({ target: { closest: (sel) => (sel === "[data-mc-open-resources]" ? {} : null) } });
out.clicks = clicks;
console.log(JSON.stringify(out));
"""


def test_the_catalog_row_says_loaded_or_kept_and_opens_resources():
    node = require_node()
    src = _SCRIPT.replace("__SOURCE__", json.dumps(CATALOG_JS)).replace("__ISLANDS__", json.dumps(ISLANDS_JS))
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
        f.write(src)
    proc = subprocess.run([node, f.name], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert ">Loaded</button>" in out["loaded"] and 'data-mc-open-resources="1"' in out["loaded"]
    assert "Held by the runtime of account default/admin" in out["loaded"] and "Open Resources to keep it loaded" in out["loaded"]
    assert out["otherQuant"] == "", "another quant of the same repo is not the build in memory"
    assert "Kept loaded</button>" in out["kept"] and 'data-icon="lock"' in out["kept"]
    assert out["notResident"] == "" and out["otherProvider"] == ""
    assert "tab-button-models" in out["clicks"], "the badge opens the Resources page"
    assert "Kept loaded</button>" in out["row"], "the catalog row carries the badge"
