"""The Workflows page's "Broken workflows" group renders only when the gateway refused at
least one bundle file (adversary F2, round 3): no empty heading on a fresh install, and it
disappears again once the last broken version is archived. Runs the served
`renderWorkflowsSkipped()` in node against a minimal element stub."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

NODE = shutil.which("node")


def _render_source() -> str:
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    m = re.search(r"\n(\s*)function renderWorkflowsSkipped\(\) \{.*?\n\1\}\n", html, re.S)
    assert m, "renderWorkflowsSkipped() was not found in the served console"
    return m.group(0)


@pytest.mark.skipif(NODE is None, reason="node is required to run the console's renderer")
def test_broken_group_hidden_when_empty_and_shown_with_rows():
    stub = r"""
class El { constructor(id) { this.id = id; this.children = []; this.hidden = new Set(id === "workflows-skipped-section" ? ["hidden"] : []); this.textContent = ""; this.title = ""; this.className = "";
  this.classList = { toggle: (c, on) => { if (on) this.hidden.add(c); else this.hidden.delete(c); }, add: (c) => this.hidden.add(c) }; }
  appendChild(c) { this.children.push(c); return c; } set textContentSetter(v) {} }
const els = {}; const $ = (id) => (els[id] ||= new El(id));
globalThis.document = { createElement: (t) => new El(t) };
const state = { workflowsSkipped: [], principal: { admin: true } };
const archiveBrokenGroup = () => {};
"""
    script = stub + _render_source() + r"""
const out = {};
renderWorkflowsSkipped();
out.empty = $("workflows-skipped-section").hidden.has("hidden");
state.workflowsSkipped = [{ bundle_id: "too-new", bundle_version: "9.0.0", reason: "needs a newer runtime", path: "/x", can_archive: true }];
renderWorkflowsSkipped();
out.withRows = $("workflows-skipped-section").hidden.has("hidden");
state.workflowsSkipped = [];
renderWorkflowsSkipped();
out.afterArchive = $("workflows-skipped-section").hidden.has("hidden");
console.log(JSON.stringify(out));
"""
    res = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out == {"empty": True, "withRows": False, "afterArchive": True}, out
