"""The console's Triggers page (root backlog 1041, console_triggers.py): sidebar entry, page and
dialog markup, row markup (Source · Name · Status · Last event · Events/Test/Delete; the Account
column in the admin's all-accounts view), the recent-events list, the webhook address, the sources
a connection can be made for (loud when the gateway does not say `needs_connection`), and the form
engine for a source's `connection_schema`. Runs the SHIPPED JavaScript under node; the routes are
the W2 contract (GET/POST /triggers/connections, /events, /test-fire), never called here."""

from __future__ import annotations

import json
import re

import pytest

from abstractgateway.console import gateway_console_html
from test_gateway_console_offline import _console_script, _node, _slice_function

pytestmark = pytest.mark.basic

HELPERS = """
const HTML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => HTML_ESCAPES[ch] || ch);
const TRG_EVENTS_LIMIT = 20;
const TRG_STATUS = { active: ["Active", "is-ok"], paused: ["Paused", "is-muted"], error: ["Error", "is-err"], failed: ["Error", "is-err"], disabled: ["Off", "is-muted"] };
const TRG_FORM_KEYWORDS = new Set(["type", "title", "description", "enum", "default", "pattern", "format", "minimum", "maximum", "minLength", "maxLength", "examples", "writeOnly"]);
"""

ROW_FNS = ("trgAgo", "trgSourceLabel", "trgPrincipalText", "trgStatusChip", "trgFireLine", "trgCopyRow",
           "triggerEventsMarkup", "triggerDetailMarkup", "triggersRowsMarkup")

SOURCES = [
    {"id": "feed.item", "version": 1, "label": "Feed item", "available": True, "needs_connection": True,
     "connection_schema": {"type": "object", "required": ["url"], "properties": {
         "url": {"type": "string", "title": "Feed address", "description": "RSS or Atom URL"},
         "every_minutes": {"type": "integer", "title": "Check every (minutes)", "default": 15, "minimum": 5}}}},
    {"id": "webhook.received", "version": 1, "label": "Webhook received", "available": True, "needs_connection": True,
     "connection_schema": {"type": "object", "properties": {}}},
    {"id": "schedule", "version": 2, "label": "Schedule", "available": True, "needs_connection": False, "connection_schema": {}},
    {"id": "broken.thing", "label": "Broken", "available": False, "unavailable_reason": "import error"},
]


def _fns(source: str, *names: str) -> str:
    return "\n".join(_slice_function(source, n) for n in names)


def test_sidebar_page_and_dialog_structure() -> None:
    html = gateway_console_html()
    nav = html[html.index('<nav class="shell_nav"') : html.index("</nav>")]
    work = nav[nav.index('id="nav-group-work"') : nav.index('id="nav-group-models"')]
    assert re.findall(r'<span class="shell_nav_label">([^<]+)</span>', work)[:2] == ["Workflows", "Triggers"]
    assert 'const TABS = ["users", "runtimes", "workflows", "triggers", "skills",' in html
    assert 'triggers: ["Triggers", "Connections that can start your automations: mailboxes, feeds, webhooks"]' in html
    panel = html[html.index('id="tab-triggers"') : html.index('id="tab-skills"')]
    for el in ('id="triggers-table"', 'id="triggers-new"', 'id="triggers-refresh"', 'id="triggers-all-slot"', 'id="triggers-account-head"', ">New connection</button>"):
        assert el in panel, el
    block = html[html.index('id="trigger-modal-backdrop"') : html.index('id="trigger-modal-backdrop"') + 500]
    assert 'class="af-modal af-modal--wide"' in block
    source = _console_script()
    # The page opens with its tab and the deep link `#triggers?new=<source_id>` reaches the dialog.
    assert 'if (next === "triggers") openTriggersPage();' in source
    assert 'if (wantedTab === "triggers") trg.pendingNew = triggersNewFromHash();' in source
    assert "bindTriggersPage();" in source


def test_rows_render_source_status_last_event_and_actions() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, *ROW_FNS) + f"""
const SOURCES = {json.dumps(SOURCES)};
const now = Date.parse("2026-10-25T12:00:00Z");
const rows = [
  {{ connection_id: "c1", source_id: "feed.item", source_version: 1, label: "Release <feed>", status: "active", created_at: "2026-10-25T10:00:00Z", last_event_at: "2026-10-25T11:57:00Z", config_public: {{ url: "https://x" }} }},
  {{ connection_id: "c2", source_id: "webhook.received", source_version: 1, label: "CI hook", status: "paused", created_at: "2026-10-25T10:00:00Z", last_event_at: null, config_public: {{}}, ingress_url: "http://127.0.0.1:8080/api/gateway/triggers/webhooks/c2", principal: "default:bob" }},
  {{ connection_id: "c3", source_id: "gone.source", source_version: 3, label: "Old", status: "weird", created_at: "x", last_event_at: null }},
];
const out = [];
out.push({{ k: "mine", html: triggersRowsMarkup(rows, {{ sources: SOURCES, now, expanded: {{}}, fire: {{ c1: {{ ok: true, sequence: 7, event_id: "e7" }}, c3: {{ ok: false, message: "connection is paused" }} }} }}) }});
out.push({{ k: "all", html: triggersRowsMarkup(rows, {{ all: true, sources: SOURCES, now, expanded: {{}}, fire: {{}} }}) }});
out.push({{ k: "empty", html: triggersRowsMarkup([], {{ sources: SOURCES }}) }});
out.push({{ k: "emptyAll", html: triggersRowsMarkup([], {{ all: true, sources: SOURCES }}) }});
out.push({{ k: "open", html: triggersRowsMarkup(rows.slice(1, 2), {{ sources: SOURCES, now, expanded: {{ c2: {{ events: [] }} }} }}) }});
console.log(JSON.stringify(out));
"""
    r = {x["k"]: x["html"] for x in _node(harness)}
    mine = r["mine"]
    c1 = mine[mine.index('data-trg-row="c1"') : mine.index('data-trg-row="c2"')]
    assert "Feed item" in c1 and "feed.item v1" in c1
    assert "Release &lt;feed&gt;" in c1 and "<feed>" not in c1
    assert '<span class="trg-chip is-ok">Active</span>' in c1
    assert "3 min ago" in c1
    assert "Test event #7 sent." in c1
    for action in ('data-trg-events="c1"', 'data-trg-test="c1"', 'data-trg-delete="c1"'):
        assert action in c1, action
    assert 'aria-expanded="false"' in c1
    c2 = mine[mine.index('data-trg-row="c2"') : mine.index('data-trg-row="c3"')]
    assert '<span class="trg-chip is-muted">Paused</span>' in c2 and "Never" in c2
    c3 = mine[mine.index('data-trg-row="c3"') :]
    # An unknown source keeps its id; an unknown status is shown as the gateway says it.
    assert "gone.source v3" in c3 and '<span class="trg-chip is-warn">weird</span>' in c3
    assert "Test not sent: connection is paused" in c3
    # The account column only in the admin's all-accounts view.
    assert "trg-account" not in mine
    allv = r["all"]
    assert allv.count('class="trg-account"') == 3 and ">bob<" in allv and "owner not reported" in allv
    assert "No connection yet." in r["empty"] and 'colspan="5"' in r["empty"]
    assert "No account has a connection yet." in r["emptyAll"] and 'colspan="6"' in r["emptyAll"]
    opened = r["open"]
    assert 'aria-expanded="true"' in opened and 'data-trg-detail="c2"' in opened
    # A webhook row's detail: its address with a copy button.
    assert "Webhook address" in opened and 'data-trg-copy="ingress:c2"' in opened
    assert "http://127.0.0.1:8080/api/gateway/triggers/webhooks/c2" in opened
    assert "No event yet. Send a test event to see one here." in opened


def test_recent_events_newest_first_capped_and_escaped() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "trgAgo", "triggerEventsMarkup") + """
const events = [];
for (let i = 1; i <= 25; i += 1) {
  events.push({ specversion: "1.0", id: `evt-${i}`, type: "abstractframework.feed.item", source: "trigger:c1", subject: i === 25 ? "<b>Title</b>" : `item ${i}`,
    time: "2026-10-25T11:00:00Z", datacontenttype: "application/json", data: i === 24 ? { test: true } : { title: `t${i}` }, sequence: i, connectionid: "c1" });
}
console.log(JSON.stringify([triggerEventsMarkup(events), triggerEventsMarkup([])]));
"""
    full, empty = _node(harness)
    seqs = [int(s) for s in re.findall(r'<span class="trg-seq">#(\d+)</span>', full)]
    assert seqs == list(range(25, 5, -1))  # newest first, the last 20
    assert "&lt;b&gt;Title&lt;/b&gt;" in full and "<b>Title</b>" not in full
    first = full[: full.index('data-trg-event="evt-24"')]
    assert "test" not in first.split("<pre>")[0].replace("abstractframework", "")
    item24 = full[full.index('data-trg-event="evt-24"') : full.index('data-trg-event="evt-23"')]
    assert '<span class="trg-chip is-muted">test</span>' in item24
    # Each event expands to its whole JSON.
    assert "&quot;connectionid&quot;: &quot;c1&quot;" in full
    assert empty == '<p class="trg-empty">No event yet. Send a test event to see one here.</p>'


def test_connectable_sources_need_the_boolean_and_a_schema() -> None:
    source = _console_script()
    harness = _fns(source, "trgConnectableSources") + f"""
const SOURCES = {json.dumps(SOURCES)};
const out = {{}};
out.ok = trgConnectableSources(SOURCES).map((s) => s.id);
try {{ trgConnectableSources([{{ id: "schedule", version: 2, available: true, label: "Schedule" }}]); out.missing = "no throw"; }} catch (e) {{ out.missing = e.message; }}
try {{ trgConnectableSources([{{ id: "feed.item", version: 1, available: true, needs_connection: true }}]); out.noschema = "no throw"; }} catch (e) {{ out.noschema = e.message; }}
console.log(JSON.stringify([out]));
"""
    (out,) = _node(harness)
    assert out["ok"] == ["feed.item", "webhook.received"]
    # A gateway older than the connections contract is said, never read as "needs none".
    assert "needs_connection missing on schedule@2" in out["missing"]
    assert "serves no connection_schema object" in out["noschema"]


def test_connection_form_engine() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "triggerFormFields", "triggerFormMarkup", "triggerConfigFromValues") + """
const schema = { type: "object", required: ["url", "kind"], properties: {
  url: { type: "string", title: "Feed address", pattern: "^https?://", examples: ["https://example.org/feed.xml"] },
  token: { type: "string", title: "Token", format: "password" },
  secret: { type: "string", writeOnly: true, default: "never-shown" },
  every_minutes: { type: "integer", title: "Every (minutes)", default: 15 },
  ratio: { type: "number" },
  include_body: { type: "boolean", default: false },
  kind: { enum: ["rss", "atom"], default: "rss" },
  nested: { type: "object", properties: { a: { type: "string" } } },
} };
const fields = triggerFormFields(schema);
const kinds = Object.fromEntries(fields.map((f) => [f.key, f.kind]));
const html = triggerFormMarkup(fields);
const good = triggerConfigFromValues(fields, { url: "https://example.org/f", token: "abc", every_minutes: "30", ratio: "0.5", include_body: "true", kind: JSON.stringify("atom") });
const bad = triggerConfigFromValues(fields, { url: "ftp://x", every_minutes: "2.5", ratio: "x" });
console.log(JSON.stringify([{ kinds, html, good, bad }]));
"""
    (r,) = _node(harness)
    assert r["kinds"] == {"url": "string", "token": "password", "secret": "password", "every_minutes": "integer",
                          "ratio": "number", "include_body": "boolean", "kind": "enum", "nested": "readonly"}
    html = r["html"]
    assert 'for="trg-f-url">Feed address *</label>' in html and 'placeholder="https://example.org/feed.xml"' in html
    assert 'type="password"' in html and "never-shown" not in html  # a default secret is never prefilled
    assert 'value="15"' in html and "Not editable here" in html
    assert r["good"] == {"config": {"url": "https://example.org/f", "token": "abc", "every_minutes": 30, "ratio": 0.5, "include_body": True, "kind": "atom"}, "problems": []}
    assert r["bad"]["problems"] == ["Feed address does not have the expected form.", "Every (minutes) must be a whole number.", "ratio must be a number.", "kind is required."]


def test_delete_question_and_hash_link() -> None:
    source = _console_script()
    harness = _fns(source, "triggerDeleteQuestion", "triggersNewFromHash") + """
const out = [triggerDeleteQuestion("CI hook")];
globalThis.location = { hash: "#triggers?new=feed.item" }; out.push(triggersNewFromHash());
globalThis.location = { hash: "#triggers" }; out.push(triggersNewFromHash());
globalThis.location = { hash: "#skills?new=x" }; out.push(triggersNewFromHash());
console.log(JSON.stringify(out));
"""
    assert _node(harness) == ["Delete CI hook? Automations that listen to it stop receiving its events.", "feed.item", "", ""]


def test_routes_are_the_connections_contract() -> None:
    source = _console_script()
    load = _slice_function(source, "loadTriggers")
    assert "`/api/gateway/triggers/connections${trg.all ? \"?all=true\" : \"\"}`" in load
    assert "out.connections" in load
    assert "/api/gateway/triggers/connections/${encodeURIComponent(id)}/events?limit=${TRG_EVENTS_LIMIT}" in _slice_function(source, "loadTriggerEvents")
    fire = _slice_function(source, "testFireTrigger")
    assert "/test-fire`, { method: \"POST\"" in fire and "out.sequence" in fire and "out.event_id" in fire
    create = _slice_function(source, "createTriggerConnection")
    assert 'api("/api/gateway/triggers/connections", { method: "POST", body: JSON.stringify({ source_id: s.id, source_version: s.version, label: m.label, config }) })' in create
    modal = _slice_function(source, "renderTriggerModal")
    assert "The signing secret is shown once." in modal and '"created:secret"' in modal and "c.ingress_url" in modal
