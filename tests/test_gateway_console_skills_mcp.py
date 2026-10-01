"""The console's Skills & MCP page (DESIGN-v3 §6): sidebar entry, kit tabs, the shelf setting
moved off Apps, row markup (actions rendered only when available), the MCP truth sentence and the
handshake result rendering. Runs the SHIPPED JavaScript under node."""

from __future__ import annotations

import re

import pytest

from abstractgateway.console import gateway_console_html
from abstractgateway.mcp_registry import AGENTS_NOTE
from test_gateway_console_offline import _console_script, _node, _slice_function

pytestmark = pytest.mark.basic

HELPERS = """
const HTML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch] || ch);
const SKILL_TRUST_TEXT = { first_party: "First party", audited: "Audited", adopted: "Adopted", community: "Community", unverified: "Unverified", blocked: "Blocked" };
const SKILL_TRUST_TONE = { first_party: "is-ok", audited: "is-ok", adopted: "is-ok", community: "is-muted", unverified: "is-warn", blocked: "is-err" };
"""


def _fns(source: str, *names: str) -> str:
    return "\n".join(_slice_function(source, n) for n in names)


def test_sidebar_work_group_and_page_structure() -> None:
    html = gateway_console_html()
    nav = html[html.index('<nav class="shell_nav"') : html.index("</nav>")]
    work = nav[nav.index('id="nav-group-work"') : nav.index('id="nav-group-models"')]
    assert re.findall(r'<span class="shell_nav_label">([^<]+)</span>', work) == ["Workflows", "Skills &amp; MCP", "Runtimes", "Apps"]
    panel = html[html.index('id="tab-skills"') : html.index('id="tab-runtimes"')]
    assert 'role="tablist"' in panel and ">Skills</button>" in panel and ">MCP servers</button>" in panel
    # The shelf setting lives on the Skills tab, and no longer on Apps.
    assert 'id="skills-settings-root"' in panel
    apps = html[html.index('<div id="tab-apps"') : html.index('<div id="tab-network"')]
    assert "skills-settings-root" not in apps
    # Both modals are the kit's wide modal.
    for mid in ("skill-modal-backdrop", "mcp-modal-backdrop"):
        block = html[html.index(f'id="{mid}"') : html.index(f'id="{mid}"') + 400]
        assert 'class="af-modal af-modal--wide"' in block
    assert ">Test connection</button>" in html


def test_mcp_truth_sentence_is_the_gateways_and_matches_the_design() -> None:
    assert AGENTS_NOTE == (
        "Agents can't call MCP tools yet: registering a server records it and checks the connection; "
        "using its tools in runs comes in a later version."
    )
    source = _console_script()
    render = _slice_function(source, "renderMcpList")
    assert 'skmcp.mcp.agents_note' in render
    # Nothing on the page claims agents can use MCP tools.
    html = gateway_console_html()
    page = html[html.index('id="skmcp-pane-mcp"') : html.index('id="tab-runtimes"')]
    assert "agents can use" not in page.lower() and "available to agents" not in page.lower()


def test_skill_rows_render_only_available_actions() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "skillTrustCell", "skillsRowsMarkup") + """
const rows = [
  { name: "coredoc", description: "Docs", origin: "curated", source_label: "Curated registry", version: "2026.09.25", trust_level: "first_party", reasons: ["validated"] },
  { name: "field-notes", description: "Notes <b>", origin: "imported", source_label: "Imported", version: "1.4.0", trust_level: "unverified", requires_review: true, reasons: ["no validation record", "has scripts"] },
  { name: "old-notes", description: "Old", origin: "archived", source_label: "Imported (archived)", archived: true },
];
const out = [];
out.push({ k: "admin", html: skillsRowsMarkup(rows, { admin: true }) });
out.push({ k: "user", html: skillsRowsMarkup(rows, { admin: false }) });
out.push({ k: "search", html: skillsRowsMarkup(rows, { admin: true, search: "zzz" }) });
console.log(JSON.stringify(out));
"""
    r = {x["k"]: x["html"] for x in _node(harness)}
    admin, user = r["admin"], r["user"]
    curated = admin[admin.index('data-skill-row="coredoc"') : admin.index('data-skill-row="field-notes"')]
    assert 'data-skill-view="coredoc"' in curated and 'data-skill-export="coredoc"' in curated
    assert "data-skill-archive" not in curated  # curated: never archivable
    assert ">First party<" in curated and "2026.09.25" in curated
    imported = admin[admin.index('data-skill-row="field-notes"') : admin.index('data-skill-row="old-notes"')]
    assert 'data-skill-archive="field-notes"' in imported
    assert ">Unverified<" in imported and 'title="no validation record\nhas scripts"' in imported
    assert "Notes &lt;b&gt;" in imported
    archived = admin[admin.index('data-skill-row="old-notes"') :]
    assert 'data-skill-unarchive="old-notes"' in archived and "data-skill-export" not in archived
    assert "data-skill-archive" not in user and "data-skill-unarchive" not in user
    assert "No skill matches this search." in r["search"]


def test_mcp_rows_status_and_test_result() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "skmcpAgo", "mcpTransportText", "mcpStatusText", "mcpToolsCell", "mcpRowsMarkup", "mcpTestResultMarkup") + """
const now = Date.parse("2026-10-01T12:00:00Z");
const ok = { ok: true, at: "2026-10-01T11:58:00Z", message: "Connected", tools: [{ name: "a", description: "A" }, { name: "b" }, { name: "c" }] };
const rows = [
  { name: "docs", transport: "http", url: "https://example.com/mcp", headers: { Authorization: { fingerprint: "ab12cd34ef56" } }, last_test: ok },
  { name: "calc", transport: "stdio", command: "npx", args: ["-y", "calc-mcp"], last_test: { ok: false, at: "2026-10-01T11:00:00Z", message: "Couldn't start `npx -y calc-mcp`: command not found." } },
  { name: "new", transport: "http", url: "http://x", last_test: null },
  { name: "gone", transport: "http", url: "http://y", archived: true, last_test: null },
];
const out = [];
out.push({ k: "status", v: [mcpStatusText(ok, now).text, mcpStatusText(rows[1].last_test, now).text, mcpStatusText(null, now).text] });
out.push({ k: "admin", html: mcpRowsMarkup(rows, { admin: true, now }) });
out.push({ k: "archived", html: mcpRowsMarkup(rows, { admin: true, archived: true, now }) });
out.push({ k: "user", html: mcpRowsMarkup(rows, { admin: false, now }) });
out.push({ k: "result", html: mcpTestResultMarkup({ ok: true, message: "Connected to fake-docs 2.1.0 · 3 tools.", tools: ok.tools }) });
out.push({ k: "failed", html: mcpTestResultMarkup({ ok: false, message: "The server answered but refused initialize: nope" }) });
console.log(JSON.stringify(out));
"""
    r = {x["k"]: x.get("html", x.get("v")) for x in _node(harness)}
    assert r["status"] == ["OK · 3 tools · 2 min ago", "Failed: Couldn't start `npx -y calc-mcp`: command not found.", "Not tested"]
    admin = r["admin"]
    assert 'data-mcp-row="gone"' not in admin and 'data-mcp-row="gone"' in r["archived"]
    assert 'data-mcp-unarchive="gone"' in r["archived"]
    for action in ("edit", "test", "archive"):
        assert f'data-mcp-{action}="docs"' in admin
        assert f"data-mcp-{action}=" not in r["user"]
    assert "npx -y calc-mcp" in admin and "https://example.com/mcp" in admin
    assert "ab12cd34ef56" not in admin  # fingerprints are for the edit modal, not the list
    assert "<summary>3 tools</summary>" in admin
    assert "Connected to fake-docs 2.1.0 · 3 tools." in r["result"] and "<code>a</code> — A" in r["result"]
    assert "Connection failed" in r["failed"] and "refused initialize: nope" in r["failed"]


def test_mcp_modal_masks_saved_header_values() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "mcpTestResultMarkup", "mcpModalMarkup") + """
const m = { existing: { name: "docs", transport: "http", url: "https://e/mcp", headers: { Authorization: { fingerprint: "ab12cd34ef56" } } },
  transport: "http", url: "https://e/mcp", headers: [{ name: "Authorization", value: "", saved: true }], args: [], result: null };
console.log(JSON.stringify([{ html: mcpModalMarkup(m) }]));
"""
    html = _node(harness)[0]["html"]
    assert 'type="password"' in html and 'value=""' in html
    assert "Saved · fingerprint ab12cd34ef56; type to replace" in html
    assert 'aria-selected="true"' in html and ">URL</button>" in html and ">Command</button>" in html


def test_descriptions_and_transports_wrap_never_truncate() -> None:
    from abstractgateway.console_skills_mcp import SKILLS_MCP_CSS

    source = _console_script()
    for fn in ("skillsRowsMarkup", "mcpRowsMarkup"):
        body = _slice_function(source, fn)
        assert "skmcp-clamp" not in body and "title=" not in body.split("skillTrustCell")[0], fn
    mono = re.search(r"\.skmcp-mono \{([^}]*)\}", SKILLS_MCP_CSS).group(1)
    assert "ellipsis" not in mono and "nowrap" not in mono


def test_shelf_folder_is_a_folded_disclosure_at_the_bottom_of_the_skills_tab() -> None:
    html = gateway_console_html()
    pane = html[html.index('id="skmcp-pane-skills"') : html.index('id="skmcp-pane-mcp"')]
    assert pane.index('id="skills-table"') < pane.index("<summary>Shelf folder</summary>") < pane.index('id="skills-settings-root"')
    assert '<details class="skmcp-shelf">' in pane  # folded (no `open`)
