"""The console's Runs tab shows EVERY session on the gateway (operator ruling, 0.21.0).

0.21.0 gave sessions a purpose (`session_kinds.py`): a turn listing
(`GET /runs?root_only=true`) serves conversations unless it says `kind=all`, so
AbstractNote's sessions vanished from the console's Runs tab. The Runs tab is a
technical listing: it must list notes and docs sessions too. User-facing apps
keep the conversation default (pinned in test_gateway_notes_kind.py).

The test runs the console's own `loadRuns` (extracted from the served page) in
node with a stub DOM, captures the request it makes, and sends that request to
a real gateway holding a conversation and a note session.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from node_requirement import require_node

from automations_fixtures import HEADERS, _run, chat_run, gateway_env, save_runs

NOTE_CAPTURE = "abstractnote@0.3.0:note-capture"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c


def _load_runs_request(*, root_only: bool) -> str:
    """The URL the console's Runs tab asks for, from its own `loadRuns`."""
    node = require_node()
    from abstractgateway.console import gateway_console_html

    page = gateway_console_html()
    start = page.index("async function loadRuns()")
    end = page.index("async function inspectRun(", start)
    fn = page[start:end]
    harness = """
const requests = [];
const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, value: "", checked: false, textContent: "", className: "", append() {} };
  return els[id];
}
const $ = (id) => el(id);
el("runs-root-only").checked = %s;
const state = {};
const document = { createElement: () => ({ append() {}, set onclick(v) {}, textContent: "", className: "" }) };
const _RUN_TERMINAL = new Set();
function tableLoadingRow() {}
function renderPager() {}
async function api(url) { requests.push(url); return { items: [], has_more: false }; }
%s
loadRuns().then(() => console.log(JSON.stringify(requests)));
""" % ("true" if root_only else "false", fn)
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as f:
        f.write(harness)
        f.flush()
        result = subprocess.run([node, f.name], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    [url] = json.loads(result.stdout.strip().splitlines()[-1])
    assert url.startswith("/api/gateway/runs?"), url
    return url


def test_the_runs_tab_lists_note_sessions_beside_conversations(client: TestClient) -> None:
    note = _run(run_id=str(uuid.uuid4()), workflow_id=NOTE_CAPTURE, vars={}, session_id="note-watch-1",
                parent_run_id=None, created_at="2026-11-06T08:00:00+00:00")
    save_runs(chat_run(session_id="s-conv", created_at="2026-11-06T07:00:00+00:00"), note)

    for root_only in (True, False):
        url = _load_runs_request(root_only=root_only)
        r = client.get(url, headers=HEADERS)
        assert r.status_code == 200, r.text
        sessions = {row.get("session_id") for row in r.json()["items"]}
        assert {"s-conv", "note-watch-1"} <= sessions, (root_only, url, r.json()["items"])

    # User-facing apps keep the conversation default: the same listing
    # without kind=all leaves the note session out.
    default = client.get("/api/gateway/runs?root_only=true&include_ledger_len=false", headers=HEADERS).json()
    assert {row.get("session_id") for row in default["items"]} == {"s-conv"}
