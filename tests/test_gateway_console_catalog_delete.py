"""Models page: Delete (trash-bin icon, tooltip "Delete") on every downloaded row (round 4 §2).

Drives the REAL view code (``console_ui.CONSOLE_UI_JS`` +
``console_catalog.CATALOG_JS``) in a node VM with a recording `api`:

- every downloaded row, and only those, carries the trash-bin icon button
  (tooltip and accessible name "Delete", no text label: the row names the model);
- the first click asks the gateway (dry run) and shows the inline confirmation
  with the size it answered and "Files only — nothing in your runs is touched.";
- a double click is not an answer; Keep closes the question, nothing sent;
- Delete posts the real request, the row turns "Not downloaded" with a Download
  button and says what was freed;
- a refusal ("Unload it first", LM Studio) is shown with its reason and fix,
  and no confirmation is offered.
"""

from __future__ import annotations

import json
import subprocess
import tempfile

import pytest
from node_requirement import require_node

from abstractgateway.console_catalog import CATALOG_JS
from abstractgateway.console_ui import CONSOLE_UI_JS
from test_gateway_console_catalog import _catalog

pytestmark = pytest.mark.basic

_HARNESS = r"""
import vm from "node:vm";
const source = __SOURCE__;
const FIXTURE = __FIXTURE__;
const calls = [];
const location = { hash: "", pathname: "/console", search: "" };
const history = { replaceState() {} };
class El { constructor(id) { this.id = id; this.innerHTML = ""; this.dataset = {}; } }
const root = new El("catalog-cards-root");
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const ctx = vm.createContext({
  console, setTimeout: (fn, ms) => { const t = setTimeout(fn, Math.min(ms || 0, 5)); t.unref(); return t; }, clearTimeout,
  encodeURIComponent, decodeURIComponent, location, history, document: { activeElement: null }, window: {},
  state: { principal: { admin: __ADMIN__ }, downloadJobs: new Map(), defaults: [], activeTab: "catalog", providerLabels: new Map() },
  esc: (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ESC[c]),
  $: () => null,
  ICONS: { trash: '<svg data-icon="trash"></svg>' },
  downloadJobKey: (p, a) => `${p || ""}/${a || ""}`,
  servedModelId: (p, a) => a,
  trackDownloadJob: () => {},
  api: async (path, opts) => {
    const body = (opts && opts.body) ? JSON.parse(opts.body) : null;
    calls.push({ path, body });
    if (path === "/api/gateway/models/catalog") return JSON.parse(JSON.stringify(FIXTURE));
    if (path === "/api/gateway/models/installed") return { schema: "models_installed_v1", rows: [], errors: {} };
    if (path === "/api/gateway/models/delete-download") {
      if (body.provider === "lmstudio") {
        const err = new Error("LM Studio keeps its own model library, so this model is deleted in LM Studio.");
        err.status = 409;
        err.data = { status: "refused", reason: "managed_elsewhere", message: "LM Studio keeps its own model library, so this model is deleted in LM Studio.", fix: "Open LM Studio › My Models and delete it there." };
        throw err;
      }
      if (!body.dry_run && !__STALE__) {
        // The engines' answer after the delete: the catalog reload says absent.
        for (const r of FIXTURE.rows) for (const a of r.artifacts) if (a.provider === body.provider && a.artifact === body.artifact) a.presence = { status: "absent" };
      }
      return body.dry_run
        ? { schema: "model_download_delete_v1", ok: true, status: "planned", freed_bytes: 351000000, also_used_by: [] }
        : { schema: "model_download_delete_v1", ok: true, status: "deleted", freed_bytes: 351000000, presence: "absent" };
    }
    return {};
  },
});
vm.runInContext(source, ctx);
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setTimeout(r, 1)); };
const click = (action, a) => root.onclick({ target: { closest: (sel) => (sel === "[data-mc-action]" ? { disabled: false, dataset: { mcAction: action, provider: a.provider, artifact: a.artifact } } : null) } });
const row = (a) => {
  const key = `${a.provider}/${a.artifact}`;
  const chunk = root.innerHTML.split('<li class="mc-art').find((c) => c.includes(`data-mc-art="${key}"`)) || "";
  return chunk.split("</li>")[0];
};
const deletes = () => calls.filter((c) => c.path === "/api/gateway/models/delete-download").map((c) => c.body);
const out = {};
vm.runInContext(`mountModelCatalog("tab", ROOT, { syncHash: false })`, Object.assign(ctx, { ROOT: root }));
await settle();
const html = root.innerHTML;
out.buttons = (html.match(/data-mc-action="delete-ask"/g) || []).length;
out.trash = (html.match(/data-mc-action="delete-ask"[^>]*aria-label="Delete" data-af-tip="Delete [^"]+ from this computer \(files only\)">(<span class="button-icon" aria-hidden="true"><svg data-icon="trash"><\/svg><\/span>)<\/button>/g) || []).length;
out.disabledButtons = (html.match(/data-mc-action="delete-ask"[^>]*disabled/g) || []).length;
const A = __TARGET__;
const L = __LMS__;
out.installedBefore = row(A).includes(">Downloaded<");
// Ask, then Keep: nothing deleted.
click("delete-ask", A); await settle();
out.askConfirm = row(A);
click("delete-keep", A); await settle();
out.afterKeep = row(A);
// Ask, then a double click on Delete is ignored; a real answer deletes.
click("delete-ask", A); await settle();
click("delete-confirm", A); await settle();
out.afterFastClick = deletes();
vm.runInContext(`for (const v of mcStore.del.values()) v.at = 0`, ctx);
click("delete-confirm", A); await settle();
out.deletes = deletes();
out.afterDelete = row(A);
// A refusal: reason + fix, no confirmation.
click("delete-ask", L); await settle();
out.refused = row(L);
console.log(JSON.stringify(out));
"""


def _run(fixture: dict, target: dict, lms: dict, admin: bool = True, js: str | None = None, stale: bool = False) -> dict:
    node = require_node()
    harness = (
        _HARNESS.replace("__SOURCE__", json.dumps(js if js is not None else CONSOLE_UI_JS + CATALOG_JS))
        .replace("__FIXTURE__", json.dumps(fixture))
        .replace("__TARGET__", json.dumps(target))
        .replace("__LMS__", json.dumps(lms))
        .replace("__ADMIN__", "true" if admin else "false")
        .replace("__STALE__", "true" if stale else "false")
    )
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", encoding="utf-8", delete=False) as f:
        f.write(harness)
        path = f.name
    r = subprocess.run([node, path], capture_output=True, text=True, check=False, timeout=60)
    assert r.returncode == 0, r.stderr + r.stdout
    return json.loads(r.stdout.strip().splitlines()[-1])


def _fixture() -> tuple[dict, dict, dict]:
    cat = _catalog(12)
    lms = cat["rows"][0]["artifacts"][2]
    assert lms["provider"] == "lmstudio"
    lms["presence"] = {"status": "installed", "location": None, "evidence": None}
    target = cat["rows"][0]["artifacts"][0]
    assert target["presence"]["status"] == "installed"
    return cat, {"provider": target["provider"], "artifact": target["artifact"]}, {"provider": lms["provider"], "artifact": lms["artifact"]}


def test_every_downloaded_row_has_the_trash_icon_button_and_only_those() -> None:
    cat, target, lms = _fixture()
    installed = sum(1 for r in cat["rows"] for a in r["artifacts"] if a["presence"]["status"] == "installed")
    out = _run(cat, target, lms)
    assert installed == 3
    assert out["buttons"] == installed and out["trash"] == installed
    assert out["disabledButtons"] == 0
    assert out["installedBefore"] is True


def test_non_admins_see_the_control_disabled() -> None:
    cat, target, lms = _fixture()
    out = _run(cat, target, lms, admin=False)
    assert out["buttons"] == 3 and out["disabledButtons"] == 3


def test_confirm_states_the_size_then_deletes_and_the_row_turns_not_downloaded() -> None:
    cat, target, lms = _fixture()
    out = _run(cat, target, lms)
    assert "Deletes 351 MB from this computer. Files only — nothing in your runs is touched." in out["askConfirm"]
    assert 'data-mc-action="delete-confirm"' in out["askConfirm"] and ">Keep<" in out["askConfirm"]
    assert "Delete this download?" in out["askConfirm"]
    assert "data-mc-del-confirm" not in out["afterKeep"] and 'data-mc-action="delete-ask"' in out["afterKeep"]
    # Two dry runs (ask, ask again) and no real delete before a deliberate answer.
    assert out["afterFastClick"] == [dict(target, dry_run=True), dict(target, dry_run=True)]
    assert out["deletes"][-1] == dict(target, dry_run=False)
    assert ">Not downloaded<" in out["afterDelete"]
    assert 'data-mc-action="download"' in out["afterDelete"]
    assert 'data-mc-action="delete-ask"' not in out["afterDelete"]
    assert "Download deleted. 351 MB freed." in out["afterDelete"]


def test_a_refusal_shows_its_reason_and_fix_and_offers_no_confirmation() -> None:
    cat, target, lms = _fixture()
    out = _run(cat, target, lms)
    assert "LM Studio keeps its own model library" in out["refused"]
    assert "Open LM Studio › My Models and delete it there." in out["refused"]
    assert "data-mc-del-confirm" not in out["refused"]
    assert 'data-mc-action="delete-ask"' in out["refused"]


def test_the_engines_answer_wins_when_the_reload_still_reports_the_files() -> None:
    cat, target, lms = _fixture()
    out = _run(cat, target, lms, stale=True)
    # The catalog reload after the delete still says installed: drawn installed.
    assert ">Downloaded<" in out["afterDelete"] and 'data-mc-action="delete-ask"' in out["afterDelete"]
