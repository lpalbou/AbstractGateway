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
const TRG_FORM_KEYWORDS = new Set(["type", "title", "description", "enum", "default", "pattern", "format", "minimum", "maximum", "minLength", "maxLength", "examples", "writeOnly", "enumNames"]);
const TRG_FIELD_LABELS = { subject_path: "Subject (path in the JSON body)", tolerance_s: "Accepted clock skew (seconds)", url: "Feed address", every: "Check every", max_items: "Items per check (at most)", initial_items: "When added" };
const TRG_DURATION_UNITS = [["s", "seconds"], ["m", "minutes"], ["h", "hours"], ["d", "days"]];
const TRG_ENUM_WORDS = { initial_items: { skip: "Ignore what is already there", deliver: "Also deliver the latest items" } };
const TRG_MORE_FIELDS = new Set(["subject_path", "tolerance_s", "max_items"]);
const TRG_HTTP_WORDS = { 400: "bad request", 401: "unauthorized", 403: "forbidden", 404: "not found", 410: "gone", 429: "too many requests", 500: "server error", 502: "bad gateway", 503: "unavailable" };
const TRG_MAILBOX_STATE = { connected: ["Active", "is-ok"], receive_only: ["Can't send", "is-warn"], paused: ["Paused", "is-muted"], needs_reconnect: ["Needs reconnecting", "is-warn"], unavailable: ["Unavailable", "is-muted"], not_connected: ["Not connected", "is-muted"] };
const TRG_NEW_FLOW = "New flow for this trigger…";
const TRG_CHOOSE_FLOW = "Choose a flow…";
const TRG_NOT_IN_LIBRARY = "Not in the Flow library (ADR-0040)";
const TRG_STOP_HANDLING = "Stop handling this trigger";
const TRG_RUNS_TIP = "Its runs in Observer";
const TRG_OBSERVER_PATH = "/#automations";
const TRG_COLS = 5;
const svgIcon = (paths) => `<svg>${paths}</svg>`;
const ICONS = { logs: "<svg>logs</svg>", trash: "<svg>trash</svg>", mail: "<svg>mail</svg>" };
"""

# Handled by (operator 2026-10-26): one dropdown — the face (the flow's name, a link to it in Flow;
# "Choose a flow…"), the menu with checkmarks, Pause / Resume, Runs and × on a checked entry.
USE_FNS = ("trgConnectionMatch", "trgMailboxMatch", "trgMailKey", "trgFlowKey", "trgLibraryIds", "trgHandlers", "trgIcon", "trgChipText", "trgFlowNameMarkup", "trgFaceMarkup",
           "trgHandledMarkup", "trgLoadingMarkup", "trgMenuFlows", "trgFlowMenuMarkup", "trgCheckedEntryMarkup", "trgRemoveQuestion", "trgUseLine")

ROW_FNS = ("webhookRefusalWords", "trgErrorText", "trgAgo", "trgSourceLabel", "trgTriggerPhrase", "trgUsedBy", "trgListeningWords", "trgUserNames", "trgConnectionUsers", "trgMailboxUsers", "trgDoingText", "trgDoingShort", "trgListenSwitch", "trgAdvancedMarkup", "trgStatusCell", "trgMailboxStatusCell", "trgMailboxStatusChip", "trgMailboxAccessWords", "trgIconButton", "trgLastCell", "mailboxRowsMarkup", "trgPrincipalText", "trgStatusChip", "trgFireLine", "trgCopyRow",
           "triggerEventsMarkup", "triggerDetailMarkup", *USE_FNS, "triggersRowsMarkup")

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


def test_helper_constants_are_the_shipped_ones() -> None:
    """The constants copied into HELPERS are the shipped lines, verbatim (no drift)."""
    flat = re.sub(r"\s+", "", _console_script()).replace(",}", "}")
    for line in HELPERS.splitlines():
        if line.startswith("const TRG_"):
            assert re.sub(r"\s+", "", line) in flat, line


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
    assert "When Release &lt;feed&gt; has a new item" in c1 and "feed.item" not in c1.replace('data-trg', '')
    assert "Release &lt;feed&gt;" in c1 and "<feed>" not in c1
    assert '<span class="trg-chip is-ok">Active</span>' in c1
    assert "3 min ago" in c1
    assert "Test event #7 sent." in c1
    for action in ('data-trg-events="c1"', 'data-trg-test="c1"', 'data-trg-delete="c1"'):
        assert action in c1, action
    assert 'aria-expanded="false"' in c1
    # The actions are icon buttons: their words are the accessible name and the tooltip.
    assert 'class="trg-icon-btn" data-trg-test="c1" aria-label="Test Release &lt;feed&gt;" data-af-tip="Send a test event to Release &lt;feed&gt;"' in c1
    assert 'class="trg-icon-btn is-danger" data-trg-delete="c1" aria-label="Delete Release &lt;feed&gt;" data-af-tip="Delete Release &lt;feed&gt;"' in c1
    # One cell per column: Connection · Handled by · Status · Last event · Actions; the test result under the time.
    assert re.findall(r'<td class="([^"]+)"', c1) == ["trg-conn", "trg-handled-cell", "trg-status", "trg-last", "trg-actions-cell"]
    assert '<span class="trg-last__when">3 min ago</span><span class="trg-fire is-ok" role="status">Test event #7 sent.</span>' in c1
    c2 = mine[mine.index('data-trg-row="c2"') : mine.index('data-trg-row="c3"')]
    assert '<span class="trg-chip is-muted">Paused</span>' in c2 and "Never" in c2
    c3 = mine[mine.index('data-trg-row="c3"') :]
    # An unknown source keeps its id; an unknown status is shown as the gateway says it.
    assert "gone.source" in c3 and '<span class="trg-chip is-warn">weird</span>' in c3
    assert "Test not sent: connection is paused" in c3
    # The account column only in the admin's all-accounts view.
    assert "trg-account" not in mine
    allv = r["all"]
    # All accounts: an Account column in place of Handled by.
    assert "trg-handled-cell" not in allv and "data-trg-useflow" not in allv
    assert allv.count('class="trg-account"') == 3 and ">bob<" in allv and "owner not reported" in allv
    # UX #14: never a dead end.
    assert "Nothing to listen to yet." in r["empty"] and 'colspan="5"' in r["empty"]
    for b in ('data-trg-empty="mailbox">Add a mailbox (My email)', 'data-trg-empty="feed.item">Add a feed', 'data-trg-empty="webhook.received">Add a webhook'):
        assert b in r["empty"], b
    assert "No account has a connection yet." in r["emptyAll"] and 'colspan="5"' in r["emptyAll"]
    opened = r["open"]
    assert 'aria-expanded="true"' in opened and 'data-trg-detail="c2"' in opened
    # A webhook row's detail: its address with a copy button.
    assert "Webhook address" in opened and 'data-trg-copy="ingress:c2"' in opened
    assert "http://127.0.0.1:8080/api/gateway/triggers/webhooks/c2" in opened
    assert "No event yet. Send a test event to see one here." in opened


def test_recent_events_newest_first_capped_and_escaped() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "trgAgo", "trgWhen", "triggerEventSentence", "triggerEventsMarkup") + """
const events = [];
for (let i = 1; i <= 25; i += 1) {
  events.push({ specversion: "1.0", id: `evt-${i}`, type: "abstract.feed.item.v1", source: "trigger:c1", subject: `item ${i}`,
    time: new Date(Date.now() - (25 - i) * 60000).toISOString(), datacontenttype: "application/json",
    data: i === 24 ? { test: true } : { title: i === 25 ? "<b>Title</b>" : `t${i}` }, sequence: i, connectionid: "c1" });
}
const hook = {type: "abstract.webhook.received.v1", subject: "msg_console_1", data: {body: {}}, sequence: 1, id: "h1"};
const now = Date.parse("2026-10-25T15:00:00");
console.log(JSON.stringify([triggerEventsMarkup(events), triggerEventsMarkup([]), triggerEventSentence(hook),
  trgWhen("2026-10-25T14:59:30", now), trgWhen("2026-10-25T09:00:00", now), trgWhen("2026-10-24T09:00:00", now)]));
"""
    full, empty, hook, recent, today, before = _node(harness)
    ids = re.findall(r'data-trg-event="evt-(\d+)"', full)
    assert [int(i) for i in ids] == list(range(25, 5, -1))  # newest first, the last 20
    # UX #10: sentences, the JSON behind Details.
    assert "New item: &lt;b&gt;Title&lt;/b&gt; · just now" in full and "<b>Title</b>" not in full
    item24 = full[full.index('data-trg-event="evt-24"') : full.index('data-trg-event="evt-23"')]
    assert '<span class="trg-event__line">Test event · ' in item24
    assert "<summary>Details</summary>" in full and "&quot;connectionid&quot;: &quot;c1&quot;" in full
    assert "abstract.feed.item.v1" not in full.split("<details>")[0]
    assert hook == "Call received"
    assert (recent, today, before) == ("just now", "09:00", "Oct 24 · 09:00")
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
    harness = HELPERS + _fns(source, "trgHumanize", "trgSentence", "triggerFormFields", "trgIsDuration", "trgEnumNames", "trgControlSaysIt", "trgFieldHint", "trgFieldMarkup", "triggerFormMarkup", "triggerConfigFromValues") + """
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
    assert r["good"]["byField"] == {}
    r["good"].pop("byField")
    assert r["good"] == {"config": {"url": "https://example.org/f", "token": "abc", "every_minutes": 30, "ratio": 0.5, "include_body": True, "kind": "atom"}, "problems": []}
    assert r["bad"]["problems"] == ["Feed address: start with https:// (e.g. https://example.org/feed.xml)", "Every (minutes) must be a whole number.", "Ratio must be a number.", "Kind is required."]


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


# ---- operator round (2026-10-25): mailboxes as rows, readable webhook form, setup panel ----

def test_mailbox_sources_are_rows_not_new_connections() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "trgConnectableSources", "trgAgo", "trgUsedBy", "trgListeningWords", "trgUserNames", "trgMailboxUsers", "trgMailboxStatusChip", "trgDoingShort", "trgMailboxStatusCell", "trgMailboxAccessWords", "trgIconButton", "trgLastCell", *USE_FNS, "mailboxRowsMarkup") + """
const SOURCES = [
  {id: "email.received", version: 1, label: "When an email arrives", available: true, needs_connection: true, connection_provider: "email_accounts", connection_schema: {type: "object"}},
  {id: "webhook.received", version: 1, label: "When a webhook is called", available: true, needs_connection: true, connection_provider: "trigger_connections", connection_schema: {type: "object", properties: {}}},
];
const items = [
  {account_id: "default", primary: true, configured: true, address: "me@example.org", can_send: true, can_write: true, access: "write", enabled: true, mailbox: {state: "connected"}, watcher: {}},
  {account_id: "work", primary: false, configured: true, address: "me@work.example", can_send: false, can_write: false, access: "read", enabled: true, mailbox: {state: "connected"}, watcher: {last_poll: "2026-10-25T11:59:00Z"}},
  {account_id: "nosmtp", primary: false, configured: true, address: "rw@work.example", can_send: false, can_write: true, access: "write", enabled: true, mailbox: {state: "receive_only"}, watcher: {}},
  {account_id: "old", primary: false, configured: false, address: null, can_send: false, enabled: false, mailbox: {state: "not_connected"}, watcher: {}},
];
console.log(JSON.stringify([trgConnectableSources(SOURCES).map((s) => s.id), mailboxRowsMarkup(items, {sources: SOURCES, now: Date.parse("2026-10-25T12:00:00Z")})]));
"""
    ids, rows = _node(harness)
    assert ids == ["webhook.received"]  # the mailbox source is never offered in New connection
    assert rows.count('class="trg-row trg-row--mailbox"') == 3  # configured mailboxes only
    work = rows[rows.index('data-trg-mailbox="work"'):rows.index('data-trg-mailbox="nosmtp"')]
    # The access in My email's words, a quiet tag beside the name; the trigger as a sentence under it.
    assert '<span class="trg-name">me@work.example</span><span class="trg-tag">Read only</span></div><span class="trg-sub">When mail arrives in me@work.example</span>' in work
    assert '<span class="trg-tag">Read and write</span>' in rows and "read-only" not in rows
    nosmtp = rows[rows.index('data-trg-mailbox="nosmtp"'):]
    # Read and write without an outgoing server says it can't send.
    assert '<span class="trg-tag">Read and write</span>' in nosmtp and '<span class="trg-chip is-warn">Can&#39;t send</span>' in nosmtp
    assert "trg-chip" not in work
    # The same three states with the mailbox's own switch (no used_by reported here: the switch says Listening, no sentence).
    assert '<span class="trg-doing"></span>' in work and '<span class="trg-last__when">Checked 1 min ago</span>' in work
    assert 'data-trg-mailbox-use="work"' in work and ">Listening</span>" in work and 'aria-checked="true"' in work
    assert 'data-trg-mailbox-manage="work" aria-label="Manage me@work.example in My email" data-af-tip="Manage in My email"' in work
    # Named like My email: the mailbox's name or address (never its derived id); the person's own
    # mailbox (My email's primary) is "Main mailbox" (operator 2026-10-26: never "Sends as you").
    assert ('<span class="trg-name">me@example.org</span><span class="trg-tag is-accent" data-af-tip="Your own mailbox, from My email">Main mailbox</span>'
            '<span class="trg-tag">Read and write</span>') in rows
    assert "Sends as you" not in rows
    assert "(primary)" not in rows


def test_webhook_form_is_readable_and_hides_the_generated_secret() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "trgHumanize", "trgSentence", "triggerFormFields", "trgIsDuration", "trgEnumNames", "trgControlSaysIt", "trgFieldHint", "trgFieldMarkup", "triggerFormMarkup") + """
const schema = {type: "object", additionalProperties: false, properties: {
  subject_path: {type: "string", description: "dotted path in a JSON body for the event subject (default: the webhook id)"},
  tolerance_s: {type: "integer", minimum: 60, maximum: 3600, default: 300},
  secret: {type: "string", "x-secret": true, description: "whsec_<base64>; generated when omitted"},
  some_other_key: {type: "string"},
}};
const fields = triggerFormFields(schema);
console.log(JSON.stringify([fields.map((f) => [f.key, f.kind, f.label, f.description]), triggerFormMarkup(fields)]));
"""
    fields, html = _node(harness)
    assert fields == [
        ["subject_path", "string", "Subject (path in the JSON body)", "Dotted path in a JSON body for the event subject (default: the webhook id)"],
        ["tolerance_s", "integer", "Accepted clock skew (seconds)", ""],
        ["some_other_key", "string", "Some other key", ""],
    ]
    assert "secret" not in html and "Not editable here" not in html
    assert ">Subject (path in the JSON body)</label>" in html and 'value="300"' in html


def test_webhook_setup_panel_has_the_real_address_and_secret() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "webhookCurlExample", "webhookRefusalWords", "webhookTestResultMarkup", "trgLocalAddress", "webhookSetupMarkup") + """
const c = {connection_id: "conn_1", ingress_url: "http://127.0.0.1:8080/api/gateway/triggers/webhooks/conn_1", secret: "whsec_c2VjcmV0"};
console.log(JSON.stringify([webhookCurlExample(c.ingress_url, c.secret), webhookSetupMarkup(c, null),
  webhookSetupMarkup(c, {status: 202, reason: "", name: "GitHub pushes", event: {sequence: 3, type: "abstract.webhook.received.v1"}}),
  webhookSetupMarkup(c, {status: 401, reason: "webhook_signature_invalid"}),
  webhookSetupMarkup({connection_id: "conn_1", ingress_url: "https://gw.example.net/api/gateway/triggers/webhooks/conn_1", secret: "whsec_c2VjcmV0"}, null),
  [trgLocalAddress("http://127.0.0.1:8080/x"), trgLocalAddress("http://192.168.1.4/x"), trgLocalAddress("http://mac.local/x"), trgLocalAddress("https://gw.example.net/x"), trgLocalAddress("https://100.64.1.2/x")]]));
"""
    curl, panel, ok, refused, public, local = _node(harness)
    assert local == [True, True, True, False, False]
    assert "paste the webhook address as the webhook URL, the signing secret as the secret, and choose JSON" in public
    assert "can't reach" not in public
    assert "SECRET='whsec_c2VjcmV0'" in curl and "URL='http://127.0.0.1:8080/api/gateway/triggers/webhooks/conn_1'" in curl
    assert 'KEY=$(printf %s "${SECRET#whsec_}" | base64 -d | xxd -p -c 256)' in curl
    assert "openssl dgst -sha256 -mac HMAC -macopt hexkey:$KEY -binary | base64" in curl
    assert '-H "webhook-signature: v1,$SIG"' in curl
    # A local address: say GitHub can't reach it (UX #15), never the paste instruction.
    assert "GitHub can't reach this computer yet. Turn on internet access under Network first." in panel
    assert "paste the webhook address" not in panel and 'data-trg-open-network="1"' in panel
    assert 'data-trg-hook-test="1"' in panel and ">Send a signed test call</button>" in panel
    assert 'data-trg-copy="created:curl"' in panel
    assert "It worked: GitHub pushes received the call · see it under Events." in ok and "<summary>Details</summary>" in ok
    assert "HTTP 202" not in ok and "fetch" not in ok
    assert "The call was refused: the signature did not match the signing secret." in refused and "is-failed" in refused


def test_webhook_signature_matches_standard_webhooks() -> None:
    """The browser's signature equals the Standard Webhooks HMAC (computed here in Python)."""
    import base64
    import hashlib
    import hmac

    source = _console_script()
    harness = _fns(source, "trgB64ToBytes", "trgBytesToB64", "webhookSignature") + """
const sig = await webhookSignature("whsec_" + Buffer.from("0123456789abcdef0123456789abcdef").toString("base64"), "msg_1", 1761390000, '{"a":1}');
console.log(JSON.stringify([sig]));
"""
    (sig,) = _node(harness)
    want = base64.b64encode(hmac.new(b"0123456789abcdef0123456789abcdef", b'msg_1.1761390000.{"a":1}', hashlib.sha256).digest()).decode()
    assert sig == f"v1,{want}"


def test_rows_say_what_a_connection_is_doing_with_a_listening_toggle() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "webhookRefusalWords", "trgErrorText", "trgUsedBy", "trgListeningWords", "trgUserNames", "trgConnectionMatch", "trgConnectionUsers", "trgDoingText", "trgDoingShort", "trgListenSwitch", "trgAdvancedMarkup", "trgStatusChip", "trgStatusCell") + """
const rows = [
  {connection_id: "a", label: "bbc-feed", status: "active", paused: false, always_listen: false, listening: "listening", used_by: 2},
  {connection_id: "b", label: "hook", status: "active", paused: false, always_listen: false, listening: "listening", used_by: ["auto_1"]},
  {connection_id: "c", label: "idle", status: "active", paused: false, always_listen: false, listening: "idle", used_by: 0},
  {connection_id: "d", label: "stopped", status: "active", paused: true, always_listen: false, listening: "paused", used_by: 3},
  {connection_id: "e", label: "old gateway", status: "active"},
  {connection_id: "f", label: "eager", status: "active", paused: false, always_listen: true, listening: "listening", used_by: 0},
];
const autos = [
  {automation_id: "x1", title: "Release digest", status: "active", trigger: {source_id: "feed.item", config: {connection_id: "a"}}},
  {automation_id: "x2", title: "Morning brief", status: "active", trigger: {source_id: "feed.item", config: {connection_id: "a"}}},
  {automation_id: "x3", title: "Old one", status: "paused", trigger: {source_id: "feed.item", config: {connection_id: "a"}}},
];
const many = [1, 2, 3, 4, 5].map((i) => ({automation_id: `m${i}`, title: `Auto ${i}`, status: "active", trigger: {config: {connection_id: "a"}}}));
console.log(JSON.stringify(rows.map((r) => [trgDoingText(r), trgStatusCell(r)]).concat([[trgDoingText(rows[0], autos), ""], [trgDoingText(rows[0], many), ""]])));
"""
    out = _node(harness)
    assert [d for d, _ in out] == [
        "Listening · used by 2 automations", "Listening · used by 1 automation",
        "Waiting for an automation to use it", "Paused", "",
        "Listening · kept on even when nothing uses it",
        # Who uses it is the row's Handled by: the status counts the active ones.
        "Listening · used by 2 automations",
        "Listening · used by 5 automations"]
    on, paused, old = out[0][1], out[3][1], out[4][1]
    assert 'role="switch"' in on and 'aria-checked="true"' in on and ">Listening</span>" in on and 'data-trg-listen="a"' in on
    # One compact block: the switch (and a status chip) on the first line, the sentence under its label
    # without repeating "Listening", then the disclosure.
    assert on.startswith('<div class="trg-state"><div class="trg-state__row"><button type="button" role="switch"')
    assert '<span class="trg-doing" data-trg-doing="a">Used by 2 automations</span>' in on
    assert '<span class="trg-doing" data-trg-doing="d">Paused</span>' in paused
    assert '<span class="trg-doing" data-trg-doing="f">Kept on even when nothing uses it</span>' in out[5][1]
    assert 'aria-checked="false"' in paused
    assert "listening state not reported (update the gateway)" in old and 'role="switch"' not in old
    # Collapsed, content-named (no "Advanced" anywhere): keep fetching even when nothing uses it.
    assert '<details class="trg-advanced"><summary>When nothing uses it</summary>' in on and 'data-trg-always="a"' in on
    assert ">Keep fetching even when nothing uses it</span>" in on and 'aria-checked="true"' in out[5][1]
    for _, cell in out:
        assert " on<" not in cell and " off<" not in cell
    html = gateway_console_html()
    panel = html[html.index('id="tab-triggers"'): html.index('id="tab-skills"')]
    assert "A connection only fetches while an automation uses it. Pause it to stop fetching." in panel
    assert 'JSON.stringify({ paused })' in _slice_function(source, "setTriggerPaused")
    assert 'JSON.stringify({ always_listen: on })' in _slice_function(source, "setTriggerAlwaysListen")


def test_ux_errors_feed_form_refusals_and_more_options() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "webhookRefusalWords", "trgErrorText", "trgHumanize", "trgSentence", "triggerFormFields",
                             "trgIsDuration", "trgEnumNames", "trgControlSaysIt", "trgFieldHint", "trgFieldMarkup", "triggerFormMarkup", "triggerConfigFromValues", "trgRefusalByField") + """
const trgErr = (e) => String((e && e.message) || e);
const feed = {type: "object", required: ["url"], properties: {
  url: {type: "string", format: "uri", description: "the RSS 2.0 or Atom feed address (http or https)"},
  every: {type: "string", format: "duration", pattern: "^[1-9][0-9]*[smhd]$", default: "15m"},
  max_items: {type: "integer", minimum: 1, maximum: 50, default: 20},
  initial_items: {type: "string", enum: ["skip", "deliver"], default: "skip"},
}};
const fields = triggerFormFields(feed);
const errors = trgRefusalByField({message: "url must be an http(s) address of a feed", detail: {field: "config.url"}}, fields);
console.log(JSON.stringify([
  trgErrorText({source_id: "feed.item", last_error: {code: "feed_http_404", cause: "The feed answered HTTP 404.", fix: "Check the feed address."}}),
  trgErrorText({source_id: "webhook.received", last_error: {code: "webhook_signature_invalid", cause: "A webhook call was refused (signature_invalid)."}}),
  trgErrorText({source_id: "feed.item", last_error: null}),
  triggerFormMarkup(fields, {url: "ftp://kept.example/x"}, errors),
  errors,
  triggerConfigFromValues(fields, {url: "https://example.org/f.xml", every: "2h", initial_items: '"deliver"'}).config,
]));
"""
    e404, ehook, none, form, errors, config = _node(harness)
    assert e404 == "Can't read the feed: the address answered 404 (not found). Check the feed address."
    assert ehook == "Last call refused: the signature did not match the signing secret."
    assert none == ""
    # The refusal beside its field, the text kept.
    assert errors == {"url": "Feed address: start with https:// (e.g. https://example.org/feed.xml)"}
    assert 'value="ftp://kept.example/x"' in form and '<p class="trg-field-error" role="alert">Feed address: start with https://' in form
    # Check every [15] [minutes]; When added: words.
    assert 'data-trg-dur-n="every" value="15"' in form and '<option value="m" selected>minutes</option>' in form
    assert ">Check every</label>" in form and ">When added</label>" in form
    assert ">Ignore what is already there</option>" in form and ">Also deliver the latest items</option>" in form and "(not set)" not in form.split("When added")[1]
    # Optional settings collapsed.
    assert '<details class="trg-more trg-field--wide"><summary>More options</summary>' in form and form.index("More options") < form.index("Items per check")
    assert config == {"url": "https://example.org/f.xml", "every": "2h", "initial_items": "deliver"}


def test_lists_show_a_loading_state_never_an_empty_list() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "trgLoadingMarkup", "trgFocus", "trgFocusSelector", "renderTriggers") + """
const nodes = {"triggers-table": {innerHTML: ""}, "triggers-account-head": {hidden: false}, "triggers-handled-head": {hidden: false}};
const $ = (id) => nodes[id];
const trgMessage = () => {};
const triggersRowsMarkup = () => "ROWS";
const out = [];
globalThis.trg = {all: false, rows: null, error: "", mailboxes: null, sources: [], expanded: {}, fire: {}, automations: null, sourcesError: "", mailboxesError: ""};
const trg = globalThis.trg;
renderTriggers(); out.push(nodes["triggers-table"].innerHTML);
trg.rows = []; renderTriggers(); out.push(nodes["triggers-table"].innerHTML);      // connections in, mailboxes pending
trg.mailboxes = []; renderTriggers(); out.push(nodes["triggers-table"].innerHTML); // both answered: the rows/empty state
trg.error = "HTTP 500"; renderTriggers(); out.push(nodes["triggers-table"].innerHTML);
console.log(JSON.stringify(out));
"""
    first, half, done, err = _node(harness)
    for state in (first, half):
        assert 'class="trg-spinner"' in state and 'aria-busy="true"' in state and "Reading your connections and mailboxes..." in state
        assert "Nothing to listen to yet" not in state
    assert done == "ROWS"
    assert 'class="trg-empty is-error" role="alert"' in err and "Could not list the connections: HTTP 500" in err
    load = _slice_function(source, "loadTriggers")
    assert load.index("renderTriggers();") < load.index("await loadTriggerSources();")
    assert 'trgLoadingMarkup("Reading the events...")' in _slice_function(source, "triggerDetailMarkup")
    assert 'trgLoadingMarkup("Reading your mailboxes...")' in _slice_function(source, "renderMyMailboxes")
    assert "renderMyMailboxes(); }  // the loading state" in _slice_function(source, "loadMyMailboxes") or "renderMyMailboxes(); }" in _slice_function(source, "loadMyMailboxes")


MBX_HELPERS = """
const MBX_PROVIDERS = {
  google: { name: "Gmail", url: "https://myaccount.google.com/apppasswords", signIn: "Sign in with Google" },
  microsoft: { name: "Outlook", url: "https://account.live.com/proofs/manage/additional", signIn: "Sign in with Microsoft" },
};
const MBX_PROVIDER_NAMES = { google: "Gmail", microsoft: "Outlook", yahoo: "Yahoo Mail", icloud: "iCloud Mail", fastmail: "Fastmail" };
const MBX_STATE = { connected: "connected", receive_only: "connected, can't send", paused: "paused", needs_reconnect: "needs reconnecting", unavailable: "turned off by your admin", not_connected: "not connected" };
const MBX_ACCESS_WORDS = { read: "Read only", write: "Read and write" };
const MBX_WRITE_HELP = "Off: automations read this mailbox and never change it or send from it. On: they can mark, move, label, delete and send as it.";
"""
GMAIL = {"address": "someone@gmail.com", "provider": "google", "display_name": "Gmail", "imap": {"host": "imap.gmail.com", "port": 993, "security": "ssl"},
         "smtp": {"host": "smtp.gmail.com", "port": 465, "security": "ssl"}, "login": "someone@gmail.com", "sign_in": ["oauth", "app_password"],
         "oauth_available": True, "app_password_url": "https://myaccount.google.com/apppasswords", "source": "preset"}
FASTMAIL = {"address": "me@fastmail.com", "provider": "fastmail", "display_name": "Fastmail", "imap": {"host": "imap.fastmail.com", "port": 993, "security": "ssl"},
            "smtp": {"host": "smtp.fastmail.com", "port": 465, "security": "ssl"}, "login": "me@fastmail.com", "sign_in": ["app_password"], "oauth_available": False, "source": "preset"}
AUTOCONF = {"address": "me@example.org", "provider": "other", "display_name": "example.org", "imap": {"host": "mail.example.org", "port": 993, "security": "ssl"},
            "smtp": {"host": "mail.example.org", "port": 465, "security": "ssl"}, "login": "me@example.org", "sign_in": ["password"], "oauth_available": False, "source": "autoconfig"}
NONE = {"address": "me@unknown.example", "provider": "other", "display_name": "unknown.example", "imap": {"host": "", "port": 993, "security": "ssl"},
        "smtp": None, "login": "me@unknown.example", "sign_in": ["password"], "oauth_available": False, "source": "none"}
MS_ONLY = {"address": "me@outlook.com", "provider": "microsoft", "display_name": "Outlook / Microsoft 365", "imap": {"host": "outlook.office365.com", "port": 993, "security": "ssl"},
           "smtp": {"host": "smtp.office365.com", "port": 587, "security": "starttls"}, "login": "me@outlook.com", "sign_in": ["oauth"], "oauth_available": False, "source": "preset"}


def test_the_mailbox_form_order_and_derived_defaults() -> None:
    """One form for the primary and the other mailboxes: Email address first; the provider found; Sign in
    or Password; Server settings collapsed unless nothing was found; Sign-in name prefilled with the
    address; Read and write (default off) only for another mailbox, its outgoing server shown when on; no Name asked."""
    source = _console_script()
    harness = HELPERS + MBX_HELPERS + _fns(source, "mailboxProviderHelpHtml", "mailboxFoundLine", "mailboxSheetMarkup", "mailboxSheetBody", "mailboxIdFromAddress") + f"""
const st = (target, found, extra) => Object.assign({{target, address: found ? found.address : "", found, imap: found ? found.imap : {{}}, smtp: found ? (found.smtp || {{}}) : {{}}, login: found ? found.login : "", serversOpen: !!(found && found.source === "none"), errors: {{}}}}, extra || {{}});
const G = {json.dumps(GMAIL)}, F = {json.dumps(FASTMAIL)}, N = {json.dumps(NONE)}, M = {json.dumps(MS_ONLY)}, A = {json.dumps(AUTOCONF)};
const MO = Object.assign({{}}, M, {{oauth_available: true}});
console.log(JSON.stringify([
  mailboxSheetMarkup(st("other", null)),
  mailboxSheetMarkup(st("other", G)), mailboxSheetMarkup(st("other", G, {{usePassword: true}})),
  mailboxSheetMarkup(st("other", F)), mailboxSheetMarkup(st("primary", F)), mailboxSheetMarkup(st("other", N)), mailboxSheetMarkup(st("other", M)),
  [mailboxFoundLine(G), mailboxFoundLine(F), mailboxFoundLine(A), mailboxFoundLine(N), mailboxFoundLine(M), mailboxFoundLine(MO)],
  mailboxSheetMarkup(st("other", MO)),
  mailboxSheetBody(st("other", F, {{password: "pw"}})), mailboxSheetBody(st("primary", F, {{password: "pw", login: "custom"}})),
  mailboxSheetMarkup(st("other", F, {{write: true}})), mailboxSheetBody(st("other", F, {{password: "pw", write: true}})),
  mailboxSheetBody(st("other", N, {{password: ""}})).errors,
  mailboxSheetMarkup(st("other", F, {{password: "x", errors: {{password: "Enter the password (or an app password)."}}}})),
  [mailboxIdFromAddress("Lpalbou@AbstractFramework.ai", []), mailboxIdFromAddress("a@b.co", ["a-b-co"]), mailboxIdFromAddress("default@x", [])],
]));
"""
    (empty, g_oauth, g_pw, f_other, f_primary, none, ms_only, lines, ms_oauth, f_body, p_body, f_write, fw_body, n_errors, f_err, ids) = _node(harness)
    # Step 1: one field, Email address (autofocus); nothing else before an address is looked up.
    assert 'for="mbs-address">Email address</label>' in empty and "autofocus" in empty
    assert "mbs-password" not in empty and "Server settings" not in empty and "Name" not in empty
    # Gmail with a client: one primary sign-in button (and the app-password fallback link), no password field.
    assert 'data-mbs-oauth="google">Sign in with Google</button>' in g_oauth and "mbs-password" not in g_oauth
    assert "Use an app password instead" in g_oauth
    assert '<a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener noreferrer">Create an app password</a>' in g_pw
    # Field order: Email address, then Password, then Server settings (collapsed), Sign-in name prefilled inside.
    order = [f_other.index(k) for k in ('id="mbs-address"', 'id="mbs-password"', "<summary>Server settings</summary>", 'id="mbs-login"')]
    assert order == sorted(order)
    assert '<details class="mbs-servers">' in f_other and 'id="mbs-login" data-mbs="login" autocomplete="off" spellcheck="false" value="me@fastmail.com"' in f_other
    assert 'value="imap.fastmail.com"' in f_other
    # Read and write: another mailbox only (also when nothing was found); never the primary (always read and write).
    assert "data-mbs-write-slot" in f_other and "data-mbs-write-slot" in none and "data-mbs-write-slot" not in f_primary
    assert "data-mbs-write-slot" not in empty and "data-mbs-send-slot" not in f_other
    # The outgoing server (SMTP) appears when Read and write is on.
    assert 'id="mbs-smtp-host"' in f_primary and 'id="mbs-smtp-host"' not in f_other
    assert 'id="mbs-smtp-host"' in f_write and 'value="smtp.fastmail.com"' in f_write
    # Nothing found: Server settings open.
    assert '<details class="mbs-servers" open>' in none
    # A provider that only accepts its own sign-in, without a client here: said plainly, no password.
    assert "Microsoft mailboxes need Sign in with Microsoft; ask your gateway admin to register a Microsoft sign-in client." in ms_only
    assert "mbs-password" not in ms_only
    assert lines == ["Gmail · Sign in with Google, or use an app password", "Fastmail · use an app password", "example.org · password sign-in",
                     "We could not find this provider's settings; enter them below.", "Outlook / Microsoft 365 · needs Sign in with Microsoft",
                     "Outlook / Microsoft 365 · Sign in with Microsoft"]
    # Microsoft with a client: the sign-in button only (it takes no app password).
    assert 'data-mbs-oauth="microsoft">Sign in with Microsoft</button>' in ms_oauth and "app password" not in ms_oauth
    # An app-password-only provider without a known link still says so.
    assert "Fastmail needs an app password (create one in your account's security settings)." in f_other
    # Bodies: no name asked (it defaults to the address); read-only by default; the primary has both legs.
    assert f_body["body"] == {"address": "me@fastmail.com", "password": "pw", "test": True,
                              "imap": {"host": "imap.fastmail.com", "security": "ssl", "port": 993}, "write": False, "name": "me@fastmail.com"}
    assert fw_body["body"]["write"] is True and fw_body["body"]["smtp"] == {"host": "smtp.fastmail.com", "security": "ssl", "port": 465}
    assert p_body["body"]["smtp"] == {"host": "smtp.fastmail.com", "security": "ssl", "port": 465} and p_body["body"]["username"] == "custom"
    assert "send" not in p_body["body"] and "write" not in p_body["body"] and "name" not in p_body["body"]
    assert set(n_errors) == {"password", "imap.host"}
    # Errors inline in red, the text kept.
    assert '<p class="mbs-error" role="alert">Enter the password (or an app password).</p>' in f_err and 'value="x"' in f_err and 'aria-invalid="true"' in f_err
    assert ids == ["lpalbou-abstractframework-ai", "a-b-co-2", "default-x"]


def test_my_email_mailboxes_tab_one_list() -> None:
    source = _console_script()
    harness = HELPERS + MBX_HELPERS + _fns(source, "mbxProviderWord", "mbxAccessWords", "mailboxRemoveQuestion", "mbxRowMarkup", "mbxFurther", "mbxRows", "mailboxesMarkup") + """
const primary = {configured: true, address: "me@gmail.com", enabled: true, oauth: {provider: "google"}, mailbox: {state: "connected"}};
const items = [
  {account_id: "default", primary: true, configured: true, address: "me@gmail.com"},
  {account_id: "work", primary: false, configured: true, address: "me@work.example", name: "me@work.example", can_send: false, can_write: false, access: "read", enabled: true, folder: "INBOX", mailbox: {state: "connected", provider: "fastmail"}},
  {account_id: "rw", primary: false, configured: true, address: "rw@work.example", can_send: true, can_write: true, access: "write", enabled: true, mailbox: {state: "connected"}},
  {account_id: "old", primary: false, configured: false},
];
console.log(JSON.stringify([mailboxesMarkup(mbxRows(primary, items), {note: {}, confirm: "work"}), mailboxesMarkup(mbxRows({configured: false}, []), {})]));
"""
    html, empty = _node(harness)
    rows = re.findall(r'data-mbx-row="([^"]+)"', html)
    assert rows == ["__primary", "work", "rw"]  # the primary first, then the others (configured only)
    first = html[: html.index('data-mbx-row="work"')]
    assert ('<strong>me@gmail.com</strong> · <span class="mbx-sub">Gmail</span> · <span class="mbx-sub">connected</span> · '
            '<span class="mbx-sub" data-mbx-access-words="__primary">Read and write</span><span class="mbx-badge">Sends as you</span>') in first
    assert 'data-mbx-access="__primary"' not in html  # the primary is always read and write: no access switch
    work = html[html.index('data-mbx-row="work"'):html.index('data-mbx-row="rw"')]
    assert ('<strong>me@work.example</strong> · <span class="mbx-sub">Fastmail</span> · <span class="mbx-sub">connected</span> · '
            '<span class="mbx-sub" data-mbx-access-words="work">Read only</span>') in work
    assert '<span data-mbx-switch="work"></span><span data-mbx-access="work"></span>' in work  # Active, then Read and write
    rw = html[html.index('data-mbx-row="rw"'):]
    assert 'data-mbx-access-words="rw">Read and write</span>' in rw and 'data-mbx-access="rw"' in rw
    assert "folder" not in html and "INBOX" not in html
    assert "reads only" not in html and "read-only" not in html and "Sends as you" not in work
    for b in ('data-mbx-test="work">Test</button>', 'data-mbx-settings="work">Settings</button>', 'data-mbx-delete="work">Remove</button>'):
        assert b in work, b
    assert '<details class="mbx-menu"><summary aria-label="More for me@work.example">…</summary>' in work
    assert "Remove me@work.example? Its stored password and read position are removed" in work
    assert empty.startswith('<p class="mbx-empty">No mailbox yet.')
    page = gateway_console_html()
    card = page[page.index('id="my-mailboxes-card"'):]
    assert '>Mailboxes</h3>' in card and '<button id="my-mailbox-add" class="secondary" type="button">Add mailbox</button>' in card


def test_the_runtime_feed_schema_reads_as_words() -> None:
    """UX re-review #4/#15: the runtime's feed schema (enumNames, descriptions naming the raw values)
    renders as controls with words, never raw JSON; a failing feed's status line is its red error."""
    source = _console_script()
    harness = HELPERS + _fns(source, "trgHumanize", "trgSentence", "triggerFormFields", "trgIsDuration", "trgEnumNames",
                             "trgControlSaysIt", "trgFieldHint", "trgFieldMarkup", "triggerFormMarkup", "triggerConfigFromValues") + "\n" + _fns(source, *ROW_FNS) + """
const feed = {type: "object", additionalProperties: false, required: ["url"], properties: {
  url: {type: "string", format: "uri", title: "Feed address", description: "The address of the RSS or Atom feed (starts with http:// or https://)."},
  every: {type: "string", format: "duration", pattern: "^[1-9][0-9]*[smhd]$", default: "15m", title: "Check every",
          description: "How often to check the feed: a number and a unit (s, m, h or d), for example 15m. At least 60s."},
  max_items: {type: "integer", minimum: 1, maximum: 50, default: 20, title: "Most new items per check", description: "The most new items one check delivers; older new items are skipped."},
  initial_items: {type: "string", enum: ["skip", "deliver"], enumNames: ["Ignore what is already there", "Also deliver the latest items"], default: "skip",
                  title: "When added", description: "What to do with the items already in the feed when it is first checked: ignore what is already there (skip), or also deliver the latest items (deliver)."},
}};
const fields = triggerFormFields(feed);
const failing = {connection_id: "f", source_id: "feed.item", label: "Python news", status: "active", paused: false, always_listen: true, used_by: 0,
  last_error: {code: "feed_http_404", cause: "The feed answered HTTP 404."}};
console.log(JSON.stringify([fields.map((f) => [f.key, f.kind]), triggerFormMarkup(fields, {}, {}),
  triggerConfigFromValues(fields, {url: "https://e.org/f.xml", every: "15m", initial_items: '"deliver"'}).config,
  trgStatusCell(failing, []), trgStatusCell(Object.assign({}, failing, {paused: true}), []),
  triggersRowsMarkup([failing], {sources: [], now: Date.parse("2026-10-25T12:00:00Z")})]));
"""
    kinds, form, config, failing, paused, row = _node(harness)
    assert dict(kinds)["initial_items"] == "enum"
    assert "Not editable here" not in form and "enumNames" not in form
    assert '<option value="&quot;skip&quot;" selected>Ignore what is already there</option>' in form
    assert '<option value="&quot;deliver&quot;">Also deliver the latest items</option>' in form
    # Descriptions that restate the control are not shown; the others are.
    assert "(skip)" not in form and "(deliver)" not in form and "s, m, h or d" not in form
    assert "The address of the RSS or Atom feed" in form and "older new items are skipped" in form
    assert 'data-trg-dur-n="every" value="15"' in form and '<option value="m" selected>minutes</option>' in form
    assert config == {"url": "https://e.org/f.xml", "every": "15m", "initial_items": "deliver"}
    # A failing feed that is on: its red error is the status line, not "Listening · kept on…".
    assert '<span class="trg-doing trg-error" role="status" data-trg-doing="f">Can&#39;t read the feed: the address answered 404 (not found). Check the feed address.</span>' in failing
    assert "Listening ·" not in failing and 'data-trg-listen="f"' in failing
    assert '<span class="trg-doing" data-trg-doing="f">Paused</span>' in paused
    assert row.count("Can&#39;t read the feed") == 1  # said once (the status line), not also under the name



def test_mailbox_access_controls_are_wired_to_the_access_route() -> None:
    """Read only / Read and write (framework backlog 1044 follow-up): the per-row switch PUTs
    …/access {write}; the form's switch says what each position means, its value goes out as `write`
    (password and Google/Microsoft sign-in), and a mailbox's Settings open with its current access."""
    source = _console_script()
    flat = re.sub(r"\s+", "", source).replace(",}", "}")
    for line in MBX_HELPERS.splitlines():
        if line.startswith(("const MBX_STATE", "const MBX_ACCESS_WORDS", "const MBX_WRITE_HELP")):
            assert re.sub(r"\s+", "", line) in flat, line
    render = _slice_function(source, "renderMyMailboxes")
    assert 'label: "Read and write", checked: r.access === "write"' in render
    assert 'api(mbxRoute(r.key, "/access"), { method: "PUT", body: JSON.stringify({ write: next }), slow: true })' in render
    sheet = _slice_function(source, "renderMailboxSheet")
    assert 'afSwitchCreate({ id: "mbs-write", label: "Read and write", description: MBX_WRITE_HELP, checked: !!st.write' in sheet
    assert "Read and send" not in source and "mbs-send" not in source
    assert "if (st.target === \"other\") start.write = !!st.write;" in _slice_function(source, "startMailboxOAuth")
    assert 'write: !primary && !!o.write' in _slice_function(source, "openMailboxSheet")
    assert 'm.access === "write"' in _slice_function(source, "bindMyMailboxes")


# ---- the feed catalogue in New connection → Feed (GET /triggers/feeds/catalog) ----

def test_feed_catalog_picker_fills_the_name_and_the_address() -> None:
    from abstractgateway.feed_catalog import shipped_catalog

    source = _console_script()
    assert 'api("/api/gateway/triggers/feeds/catalog")' in _slice_function(source, "loadFeedCatalog")
    assert "trgCatalogMarkup(m)" in _slice_function(source, "renderTriggerModal")
    assert "trgCheckFeedParam(m, byField)" in _slice_function(source, "createTriggerConnection")
    feeds = [dict(f, source="shipped") for f in shipped_catalog()["feeds"]]
    harness = HELPERS + _fns(source, "trgCatalogFeed", "trgSiteName", "trgFeedAddress", "trgFeedName", "trgFeedParamProblem",
                             "trgCatalogMarkup", "pickCatalogFeed", "trgPickEvery", "trgFeedParamTyped", "trgCheckFeedParam") + """
const TRG_FEED_SOURCE = "feed.item";
const TRG_ANOTHER_ADDRESS = "__another";
const trg = { feedCatalog: { feeds: FEEDS }, modal: null };
const els = { "trg-f-label": { value: "" }, "trg-f-url": { value: "", dataset: { trgField: "url" } }, "trg-f-feed-param": { value: "" } };
const $ = (id) => els[id] || null;
globalThis.document = { querySelectorAll: () => [els["trg-f-url"]] };
const trgModalValues = () => ({ url: els["trg-f-url"].value });
let rendered = 0;
function renderTriggerModal() { rendered += 1; els["trg-f-label"].value = trg.modal.label; els["trg-f-url"].value = trg.modal.values.url || ""; els["trg-f-feed-param"].value = trg.modal.feedParam || ""; }
trg.modal = { feedPick: "", feedParam: "", autoName: "", autoUrl: "", values: {}, errors: {} };
const out = {};
out.empty = trgCatalogMarkup(trg.modal);
pickCatalogFeed("bbc-world");
out.picked = [els["trg-f-label"].value, els["trg-f-url"].value, trgCatalogMarkup(trg.modal)];
els["trg-f-label"].value = "My world news";
pickCatalogFeed("hacker-news");
out.ownName = [els["trg-f-label"].value, els["trg-f-url"].value];
els["trg-f-label"].value = "";
pickCatalogFeed("reddit-subreddit");
out.template = [els["trg-f-label"].value, els["trg-f-url"].value, trgCatalogMarkup(trg.modal)];
els["trg-f-feed-param"].value = "Python"; trgFeedParamTyped("Python");
out.typed = [els["trg-f-label"].value, els["trg-f-url"].value];
let byField = { url: "Feed address is required." };
trg.modal.values = { url: "" }; els["trg-f-feed-param"].value = ""; trg.modal.feedParam = "";
trgCheckFeedParam(trg.modal, byField); out.missing = byField;
byField = {}; els["trg-f-feed-param"].value = "no spaces allowed"; trg.modal.values = { url: "https://www.reddit.com/r/no spaces allowed/.rss" };
trgCheckFeedParam(trg.modal, byField); out.badForm = byField;
byField = {}; els["trg-f-feed-param"].value = ""; trg.modal.values = { url: "https://example.org/typed-by-me.xml" };
trgCheckFeedParam(trg.modal, byField); out.ownAddress = byField;
pickCatalogFeed("__another");
out.another = [els["trg-f-label"].value, els["trg-f-url"].value, trgCatalogMarkup(trg.modal)];
trg.feedCatalog = { feeds: [], error: "HTTP 500" };
out.failed = trgCatalogMarkup(trg.modal);
console.log(JSON.stringify([out]));
""".replace("FEEDS", json.dumps(feeds))
    (r,) = _node(harness)
    empty = r["empty"]
    assert '<label for="trg-f-catalog">Choose a well-known feed</label>' in empty
    for group in ("World news", "Technology", "Science", "Business and finance", "Fill in your own"):
        assert f'<optgroup label="{group}">' in empty
    assert '<option value="bbc-world">BBC News · World — bbc.co.uk</option>' in empty
    assert '<option value="reddit-subreddit">Reddit · A subreddit…</option>' in empty
    assert empty.index('value="__another">Another address</option>') > empty.index("Fill in your own")
    name, url, markup = r["picked"]
    assert (name, url) == ("BBC News · World", "https://feeds.bbci.co.uk/news/world/rss.xml")
    assert '<option value="bbc-world" selected>' in markup and "World headlines from the BBC." in markup and 'href="https://www.bbc.co.uk/news"' in markup
    assert r["ownName"] == ["My world news", "https://news.ycombinator.com/rss"]  # a name the person wrote is kept
    name, url, markup = r["template"]
    assert (name, url) == ("Reddit · A subreddit", "")
    assert '<label for="trg-f-feed-param">Subreddit (the name after r/) *</label>' in markup and 'placeholder="python"' in markup
    assert r["typed"] == ["Reddit · r/Python", "https://www.reddit.com/r/Python/.rss"]
    assert r["missing"] == {"__feed_param": "Subreddit (the name after r/) is required."}
    assert r["badForm"] == {"__feed_param": "Subreddit (the name after r/): not the expected form (e.g. python)."}
    assert r["ownAddress"] == {}  # an address typed over the template stands on its own
    name, url, markup = r["another"]
    assert (name, url) == ("", "") and 'value="__another" selected' in markup and "trg-f-feed-param" not in markup
    assert "Could not read the well-known feeds (HTTP 500). Type the feed address below." in r["failed"]


def test_feed_catalog_pick_prefills_check_every_with_the_recommended_interval() -> None:
    """A pick sets Check every (number + unit) to the feed's `check_every` with "Recommended for this
    feed" under it; a value changed before any pick is overwritten, one changed after a pick is kept;
    a custom feed without `check_every` (or Another address) leaves the form default."""
    from abstractgateway.feed_catalog import shipped_catalog

    source = _console_script()
    assert "trgPickEvery(m, feed);" in _slice_function(source, "pickCatalogFeed")
    assert "triggerFormMarkup(m.fields, m.values, m.errors, trgFieldNotes(m))" in _slice_function(source, "renderTriggerModal")
    assert "autoEvery: null, everyRecommended: false" in _slice_function(source, "openTriggerModal")
    feeds = [dict(f, source="shipped") for f in shipped_catalog()["feeds"]]
    feeds.append({"id": "mine", "name": "Mine", "url": "https://example.org/feed.xml", "category": "My feeds", "source": "custom"})
    harness = HELPERS + _fns(source, "trgHumanize", "trgSentence", "triggerFormFields", "trgIsDuration", "trgEnumNames", "trgControlSaysIt",
                             "trgFieldMarkup", "triggerFormMarkup", "trgCatalogFeed", "trgFeedName", "pickCatalogFeed", "trgPickEvery",
                             "trgFieldNotes") + """
const TRG_ANOTHER_ADDRESS = "__another";
const TRG_EVERY_RECOMMENDED = "Recommended for this feed";
const trg = { feedCatalog: { feeds: FEEDS }, modal: null };
const fields = triggerFormFields({type: "object", required: ["url"], properties: {
  url: {type: "string", format: "uri", title: "Feed address"},
  every: {type: "string", format: "duration", pattern: "^[1-9][0-9]*[smhd]$", default: "15m", title: "Check every"}}});
const els = { "trg-f-label": { value: "" }, "trg-f-url": { value: "" }, every: { value: "15m" } };
const $ = (id) => els[id] || null;
const trgModalValues = () => ({ url: els["trg-f-url"].value, every: els.every.value });
let markup = "";
function renderTriggerModal() {
  const m = trg.modal;
  els["trg-f-label"].value = m.label; els["trg-f-url"].value = m.values.url || "";
  els.every.value = m.values.every === undefined ? "15m" : m.values.every;
  markup = triggerFormMarkup(m.fields, m.values, m.errors, trgFieldNotes(m));
}
const fresh = () => ({ fields, feedPick: "", feedParam: "", autoName: "", autoUrl: "", autoEvery: null, everyRecommended: false, values: {}, errors: {} });
const out = {};
const step = (name) => { out[name] = [els.every.value, markup]; };
trg.modal = fresh();
els.every.value = "2h"; pickCatalogFeed("arxiv-cs-ai"); step("beforeAnyPick");
pickCatalogFeed("hacker-news"); step("unchanged");
els.every.value = "45m"; pickCatalogFeed("nature"); step("changedAfterPick");
pickCatalogFeed("__another"); step("anotherKeeps");
trg.modal = fresh(); els.every.value = "15m";
pickCatalogFeed("mine"); step("customDefault");
pickCatalogFeed("youtube-channel"); step("template");
pickCatalogFeed("mine"); step("backToDefault");
console.log(JSON.stringify([out]));
""".replace("FEEDS", json.dumps(feeds))
    (r,) = _node(harness)
    note = '<p class="trg-help" data-trg-note="every">Recommended for this feed</p>'
    hours = '<option value="h" selected>hours</option>'
    every, markup = r["beforeAnyPick"]
    assert every == "12h" and note in markup and 'data-trg-dur-n="every" value="12"' in markup and hours in markup
    every, markup = r["unchanged"]
    assert every == "10m" and note in markup and 'value="10"' in markup and '<option value="m" selected>minutes</option>' in markup
    every, markup = r["changedAfterPick"]
    assert every == "45m" and note not in markup and 'value="45"' in markup
    assert r["anotherKeeps"][0] == "45m" and note not in r["anotherKeeps"][1]
    assert r["customDefault"][0] == "15m" and note not in r["customDefault"][1]
    every, markup = r["template"]
    assert every == "1h" and note in markup and 'value="1"' in markup and hours in markup
    assert r["backToDefault"][0] == "15m" and note not in r["backToDefault"][1]


# ---- Handled by + Use in a flow (operator 2026-10-25) ----

AUTOS = [
    {"automation_id": "a1", "title": "Release digest", "status": "active", "flow_name": "Digest", "target": {"workflow_id": "digest@1.0.0:digest", "bundle_ref": "digest@1.0.0", "flow_id": "digest"},
     "trigger": {"source_id": "feed.item", "source_version": 1, "config": {"connection_id": "c1"}}},
    {"automation_id": "a2", "title": "Old <one>", "status": "paused", "flow_name": "Summarize", "target": {"workflow_id": "sum@2.0.0:sum", "bundle_ref": "sum@2.0.0", "flow_id": "sum"},
     "trigger": {"source_id": "feed.item", "source_version": 1, "config": {"connection_id": "c1"}}},
    {"automation_id": "a3", "title": "Gone", "status": "archived", "flow_name": "Digest", "target": {"bundle_ref": "digest@1.0.0", "flow_id": "digest"},
     "trigger": {"source_id": "feed.item", "source_version": 1, "config": {"connection_id": "c1"}}},
    {"automation_id": "a4", "title": "Triage", "status": "active", "flow_name": "Triage", "target": {"bundle_ref": "triage@1.0.0", "flow_id": "triage"},
     "trigger": {"source_id": "email.received", "source_version": 1, "config": {"account": "self"}}},
]
BUNDLES = [
    # Published from Flow: its manifest keeps the library flow it came from (source.root_flow_id).
    {"bundle_id": "digest", "bundle_version": "1.0.0", "bundle_ref": "digest@1.0.0", "source": "published",
     "metadata": {"source": {"root_flow_id": "vf-digest", "root_flow_name": "Digest"}},
     "automation_defaults": {"digest": {"schema_version": 1, "title": "Digest", "trigger": {"source_id": "feed.item", "source_version": 1, "config": {"connection_id": "author_conn"}},
                                         "context": {"mode": "growing", "growing": {"max_tokens": 9000}}, "input_data": {"prompt": "Summarize the new items"}}},
     "entrypoints": [{"flow_id": "digest", "name": "Digest", "workflow_id": "digest@1.0.0:digest", "triggerable": True},
                     {"flow_id": "chat", "name": "Chat helper", "workflow_id": "digest@1.0.0:chat", "triggerable": False}]},
    # Ships with the gateway: no library flow (ADR-0040).
    {"bundle_id": "triage", "bundle_version": "1.0.0", "bundle_ref": "triage@1.0.0", "source": "shipped", "metadata": {},
     "automation_defaults": {"triage": {"schema_version": 1, "trigger": {"source_id": "email.received", "source_version": 1, "config": {"account": "author", "from": ["boss@x.test"]}},
                                         "context": {"mode": "independent"}, "input_data": {}}},
     "entrypoints": [{"flow_id": "triage", "name": "Triage", "workflow_id": "triage@1.0.0:triage", "triggerable": True}]},
    # Imported with a source id from elsewhere: not this library's.
    {"bundle_id": "other", "bundle_version": "1.0.0", "bundle_ref": "other@1.0.0", "source": "imported", "metadata": {"source": {"root_flow_id": "vf-other"}},
     "entrypoints": [{"flow_id": "other", "name": "Other", "workflow_id": "other@1.0.0:other", "triggerable": False}]},
]


def test_handled_by_is_one_dropdown_its_face_names_the_flow() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, *ROW_FNS, "trgTriggeredFlows") + f"""
const SOURCES = {json.dumps(SOURCES)};
const AUTOS = {json.dumps(AUTOS)};
const rows = [
  {{ connection_id: "c1", source_id: "feed.item", source_version: 1, label: "Release feed", status: "active", paused: false, always_listen: false, used_by: 1, last_event_at: null }},
  {{ connection_id: "c2", source_id: "webhook.received", source_version: 1, label: "CI hook", status: "active", paused: false, always_listen: false, used_by: 0, last_event_at: null }},
];
const mailboxes = [{{ account_id: "default", primary: true, configured: true, address: "me@x.test", enabled: true, mailbox: {{ state: "connected" }}, watcher: {{}} }},
                   {{ account_id: "work", primary: false, configured: true, address: "w@x.test", enabled: true, mailbox: {{ state: "connected" }}, watcher: {{}} }}];
const flows = trgTriggeredFlows({json.dumps(BUNDLES)});
const library = trgLibraryIds({json.dumps(BUNDLES)});
const o = {{ sources: SOURCES, expanded: {{}}, fire: {{}}, mailboxes, automations: AUTOS, flows, flowsError: "", library, flowMenu: "", flowConfirm: "", useMsg: {{}} }};
const one = AUTOS.slice(0, 1), onePaused = AUTOS.slice(1, 2);
console.log(JSON.stringify({{
  library,
  mine: triggersRowsMarkup(rows, o),
  one: triggersRowsMarkup(rows.slice(0, 1), Object.assign({{}}, o, {{ automations: one, mailboxes: [] }})),
  onePaused: triggersRowsMarkup(rows.slice(0, 1), Object.assign({{}}, o, {{ automations: onePaused, mailboxes: [] }})),
  unknown: triggersRowsMarkup(rows, Object.assign({{}}, o, {{ automations: null }})),
  all: triggersRowsMarkup(rows, Object.assign({{}}, o, {{ all: true }})),
}}));
"""
    r = _node(harness)
    # The library flow behind a workflow published from Flow (its manifest's source.root_flow_id);
    # shipped and imported workflows have none.
    assert r["library"] == {"digest": "vf-digest"}
    mine = r["mine"]
    c1 = mine[mine.index('data-trg-row="c1"'):mine.index('data-trg-row="c2"')]
    c2 = mine[mine.index('data-trg-row="c2"'):mine.index('data-trg-mailbox="default"')]
    primary = mine[mine.index('data-trg-mailbox="default"'):mine.index('data-trg-mailbox="work"')]
    work = mine[mine.index('data-trg-mailbox="work"'):]
    # The face: a toggle covering the box (aria-haspopup, closed), the first flow's name as a link to
    # it in Flow, "+1" for the second (paused) one, no × with several; never the archived one.
    assert ('<div class="trg-dd has-value"><button type="button" class="trg-dd__toggle" data-trg-useflow="c1" aria-haspopup="menu" aria-expanded="false" aria-label="Flows handling Release feed">') in c1
    assert ('<span class="trg-dd__value"><button type="button" class="trg-dd__name" data-trg-openflow="vf-digest" data-af-tip="Open Digest in Flow" aria-label="Open Digest in Flow">Digest</button>'
            '<span class="trg-dd__more" aria-label="and 1 more">+1</span></span></div>') in c1
    assert "data-trg-unhandle" not in c1 and "Gone" not in c1 and "trg-hchip" not in mine and "data-trg-observer" not in mine
    # Nothing handles it: "Choose a flow…", muted (never "Nothing yet").
    assert '<span class="trg-dd__value"><span class="trg-dd__text is-placeholder">Choose a flow…</span></span>' in c2 and "Nothing yet" not in mine
    # The status counts the active ones (the face names them), without repeating "Listening".
    assert '<span class="trg-doing" data-trg-doing="c1">Used by 1 automation</span>' in c1
    # A mailbox: the primary is matched by "self"; Triage ships with the gateway, so its name is
    # plain, said why (ADR-0040); the lone flow has its × ("Stop handling this trigger").
    assert ('<span class="trg-dd__name is-off" tabindex="0" role="note" data-af-tip="Not in the Flow library (ADR-0040)" '
            'aria-label="Triage: Not in the Flow library (ADR-0040)">Triage</span>') in primary
    assert ('<button type="button" class="trg-dd__x" data-trg-unhandle="a4" aria-label="Stop handling: remove Triage from me@x.test" data-af-tip="Stop handling this trigger">') in primary
    assert "Choose a flow…" in work
    for key in ("c1", "c2", "mail:default", "mail:work"):
        assert f'data-trg-useflow="{key}" aria-haspopup="menu"' in mine, key
    # One flow: its name and its ×; the only one paused: a muted "paused" after the name.
    assert 'data-trg-openflow="vf-digest"' in r["one"] and 'data-trg-unhandle="a1"' in r["one"] and "trg-dd__more" not in r["one"] and "paused" not in r["one"]
    paused = r["onePaused"]
    assert '<div class="trg-dd has-value is-paused">' in paused and '<span class="trg-dd__state">paused</span>' in paused
    assert 'aria-label="Summarize: Not in the Flow library (ADR-0040)">Summarize</span>' in paused and 'data-trg-unhandle="a2"' in paused
    # Automations not read: said, never a false "Choose a flow…".
    unknown = r["unknown"]
    assert "Choose a flow…" not in unknown and "Automations not read" in unknown and 'data-trg-useflow="c1"' in unknown
    # The admin's All accounts view: other people's connections, no Handled by.
    assert "data-trg-useflow" not in r["all"] and "trg-dd" not in r["all"]
    html = gateway_console_html()
    head = html[html.index('id="tab-triggers"'): html.index('id="tab-skills"')]
    thead = head[head.index("<thead>"):head.index("</thead>")]
    assert re.findall(r"<th(?:\s[^>]*)?>([^<]*)<", thead) == ["Connection", "Handled by", "Account", "Status", "Last event", ""]
    assert 'id="triggers-handled-head"' in head and '$("triggers-handled-head").hidden = trg.all;' in _slice_function(source, "renderTriggers")


def test_the_menu_new_flow_then_triggered_flows_with_checkmarks_and_remove() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, *USE_FNS, "trgTriggeredFlows", "trgFlowPath", "trgLibraryPath") + f"""
const AUTOS = {json.dumps(AUTOS)};
const flows = trgTriggeredFlows({json.dumps(BUNDLES)});
const library = trgLibraryIds({json.dumps(BUNDLES)});
const handlers = trgHandlers(AUTOS, trgConnectionMatch({{ connection_id: "c1" }}), library);
const o = (extra) => Object.assign({{ flows, flowsError: "", flowMenu: "c1", flowConfirm: "", useMsg: {{}} }}, extra || {{}});
const cell = (hs, extra) => trgHandledMarkup("c1", "Release feed", hs, o(extra));
const twins = trgHandlers([AUTOS[0], Object.assign({{}}, AUTOS[0], {{ automation_id: "a9", title: "Morning brief" }})], trgConnectionMatch({{ connection_id: "c1" }}), library);
console.log(JSON.stringify({{
  flows, handlers,
  paths: [trgFlowPath("conn_abc"), trgFlowPath("mail:default"), trgFlowPath("mail:a b"), trgFlowPath(trgMailKey("work")), trgLibraryPath("vf-digest"), trgLibraryPath("a b")],
  open: cell(handlers),
  closed: cell(handlers, {{ flowMenu: "c2" }}),
  none: cell([], {{ flows: [] }}),
  loading: cell([], {{ flows: null }}),
  failed: cell([], {{ flows: null, flowsError: "HTTP 500" }}),
  said: cell([], {{ flowMenu: "", useMsg: {{ c1: {{ ok: false, text: "Triage cannot read it." }} }} }}),
  confirm: cell(handlers, {{ flowConfirm: "a1" }}),
  twins: cell(twins),
}}));
"""
    r = _node(harness)
    # Triggered entrypoints only (the chat entrypoint of the same bundle is not offered), by name.
    assert [f["name"] for f in r["flows"]] == ["Digest", "Triage"]
    assert r["flows"][0]["bundleRef"] == "digest@1.0.0" and r["flows"][0]["defaults"]["input_data"] == {"prompt": "Summarize the new items"}
    assert [(h["id"], h["library"]) for h in r["handlers"]] == [("a1", "vf-digest"), ("a2", "")]
    # Flow's link forms, exactly: ?trigger=<connection id>, ?trigger=mail:<mailbox id>, ?flow=<library id>.
    assert r["paths"] == ["/?trigger=conn_abc", "/?trigger=mail:default", "/?trigger=mail:a%20b", "/?trigger=mail:work", "/?flow=vf-digest", "/?flow=a%20b"]
    menu = r["open"]
    assert 'aria-expanded="true"' in menu and 'role="menu" aria-label="Flows handling Release feed"' in menu
    # Anchored under the face.
    assert '</div><div class="trg-hb__anchor"><div class="trg-flowmenu" role="menu"' in menu
    # New flow first; then every triggered flow, plus a flow handling it that is no longer published
    # (Summarize), each checked when it handles this trigger.
    items = re.findall(r'role="(menuitem(?:checkbox)?)"(?: aria-checked="(\w+)")?[^>]*>(?:<span class="trg-flowmenu__mark"[^>]*>.*?</span>)?<span class="trg-flowmenu__text">(?:<span class="trg-flowmenu__name">)?([^<]+)', menu)
    assert items == [("menuitem", "", "New flow for this trigger…"), ("menuitemcheckbox", "true", "Digest"),
                     ("menuitemcheckbox", "true", "Summarize"), ("menuitemcheckbox", "false", "Triage")], items
    assert 'data-trg-newflow="c1"' in menu
    # A checked entry: ✓, the name, "Handles it · <title>" (the title when it is not the flow's
    # name), then on the same row Pause (Resume when paused), Runs (Observer) and × (Stop handling).
    digest = menu[menu.index('data-trg-entry="a1"'):].split('<div class="trg-flowmenu__entry')[0]
    assert '<span class="trg-flowmenu__meta">Handles it · Release digest</span>' in digest and "<svg>" in digest
    assert ('<span class="trg-flowmenu__tools"><button type="button" role="menuitem" class="trg-flowmenu__tool" data-trg-pause="a1" aria-label="Pause Release digest" data-af-tip="Pause it">Pause</button>'
            '<button type="button" role="menuitem" class="trg-flowmenu__tool is-link" data-trg-runs="a1" aria-label="Runs of Release digest in Observer" data-af-tip="Its runs in Observer">Runs</button>'
            '<button type="button" role="menuitem" class="trg-flowmenu__x" data-trg-remove="a1" aria-label="Stop handling: remove Digest from Release feed" data-af-tip="Stop handling this trigger">') in digest
    summarize = menu[menu.index('data-trg-entry="a2"'):].split('<div class="trg-flowmenu__entry')[0]
    assert '<div class="trg-flowmenu__entry is-paused" data-trg-entry="a2">' in menu and '<span class="trg-flowmenu__meta">Paused · Old &lt;one&gt;</span>' in summarize
    assert 'data-trg-resume="a2" aria-label="Resume Old &lt;one&gt;" data-af-tip="Turn it on again">Resume</button>' in summarize and "data-trg-pause" not in summarize
    assert 'data-trg-remove="a2"' in summarize and 'data-trg-runs="a2"' in summarize
    triage = menu[menu.index('data-trg-flow="triage@1.0.0:triage"'):].split("</button>")[0]
    assert 'data-trg-pickflow="c1"' in menu and "trg-flowmenu__meta" not in triage and " disabled" not in triage
    assert 'role="menu"' not in r["closed"] and 'aria-expanded="false"' in r["closed"]
    assert "New flow for this trigger…" in r["none"] and "No triggered flow yet: start with a new one." in r["none"]
    assert "Reading your flows..." in r["loading"] and "Could not read your flows: HTTP 500" in r["failed"]
    assert '<span class="trg-fire is-failed" role="status">Triage cannot read it.</span>' in r["said"]
    # × asks first, under its entry: Remove / Keep; the entry's tools give way to the question.
    confirm = r["confirm"]
    assert ('<div class="trg-flowmenu__confirm-box" role="group" aria-label="Remove Digest"><p class="trg-flowmenu__confirm" role="alert">'
            'Remove Digest from this trigger? Its runs stay in Observer.</p>') in confirm
    assert 'class="danger" data-trg-remove-now="a1">Remove</button>' in confirm and 'data-trg-remove-cancel="a1">Keep</button>' in confirm
    assert 'data-trg-remove="a1"' not in confirm and 'data-trg-remove="a2"' in confirm
    # Two automations of one flow on this trigger: an entry each (their titles), each its own ×.
    assert re.findall(r'data-trg-entry="(a\d)"', r["twins"]) == ["a1", "a9"]
    assert '<span class="trg-flowmenu__meta">Handles it · Morning brief</span>' in r["twins"]
    assert '<span class="trg-dd__more" aria-label="and 1 more">+1</span>' in r["twins"] and "Digest · Release digest</button>" in r["twins"]


def test_pause_resume_remove_go_through_the_automation_commands() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "trgErr", "trgCommandWords", "trgCommandRefusal") + """
const err = (status, code, message) => Object.assign(new Error(message), { status, detail: { reason_code: code, message } });
console.log(JSON.stringify({
  words: ["pause", "resume", "remove"].map((v) => trgCommandWords(v, "Release digest", "Release feed")),
  refusals: [trgCommandRefusal(err(409, "invalid_state", "Automation is already paused."), "Release digest was not paused"),
             trgCommandRefusal(err(401, "unauthorized", "x"), "Release digest was not removed")],
}));
"""
    r = _node(harness)
    assert [(w["type"], w["status"], w["done"]) for w in r["words"]] == [
        ("automation.pause", "paused", "Release digest is paused."),
        ("automation.resume", "active", "Release digest is on again."),
        ("automation.archive", "", "Release digest no longer handles Release feed."),
    ]
    assert r["refusals"] == ["Release digest was not paused: Automation is already paused.", "Your session expired: sign in again, then try again."]
    # The existing route (POST /automations/{id}/commands) with a fresh command id, then the list is
    # read again until it shows the change (bounded).
    run = _slice_function(source, "automationCommandForTrigger")
    assert 'api(`/api/gateway/automations/${encodeURIComponent(automationId)}/commands`, { method: "POST", body: JSON.stringify({ command_id: trgRequestId(), type: words.type, payload: {} }) })' in run
    assert "await trgAwaitAutomation(automationId, words.status)" in run
    wait = _slice_function(source, "trgAwaitAutomation")
    assert "for (let i = 0; i < 12; i += 1)" in wait and "await loadTriggerAutomations();" in wait
    # The command types exist at the gateway (one source of truth).
    from abstractgateway.automation_command_types import AUTOMATION_COMMAND_TYPES
    assert {"automation.pause", "automation.resume", "automation.archive"} <= set(AUTOMATION_COMMAND_TYPES)
    # The menu by keyboard (arrows, Home, End; Esc back to "+") and a click elsewhere closes it.
    bind = _slice_function(source, "bindTriggersPage")
    assert '["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)' in bind and 'if (e.key === "Escape") { e.preventDefault(); closeFlowMenu(true); return; }' in bind
    assert 'document.addEventListener("click", (e) => {' in bind
    # ArrowDown on a closed face opens its menu; × and Keep go through one place (askRemoveInMenu).
    assert 'if (at0 && e.key === "ArrowDown" && trg.flowMenu !== at0) { e.preventDefault(); toggleFlowMenu(at0); return; }' in bind
    assert "else if (t.dataset.trgUnhandle) askRemoveInMenu(trgRowKey(t), t.dataset.trgUnhandle);" in bind
    assert "else if (t.dataset.trgRemove) askRemoveInMenu(trgRowKey(t), t.dataset.trgRemove);" in bind


def test_a_pick_creates_the_automation_from_the_flows_defaults_with_this_trigger() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "trgErr", "trgTriggeredFlows", "trgUseFlowBody", "trgUseFlowRefusal") + f"""
const flows = trgTriggeredFlows({json.dumps(BUNDLES)});
const digest = flows[0], triage = flows[1];
const err = (status, code, message) => Object.assign(new Error(message), {{ status, detail: {{ reason_code: code, message }} }});
console.log(JSON.stringify({{
  feed: trgUseFlowBody(digest, {{ kind: "connection", id: "conn_1", source_id: "feed.item", source_version: 1 }}, "rq1"),
  hook: trgUseFlowBody(digest, {{ kind: "connection", id: "conn_2", source_id: "webhook.received", source_version: 1 }}, "rq2"),
  mail: trgUseFlowBody(triage, {{ kind: "mailbox", id: "work", source_id: "email.received", source_version: 1 }}, "rq3"),
  refusals: [
    trgUseFlowRefusal(err(422, "email_account_cannot_read", "x"), "Triage", "w@x.test"),
    trgUseFlowRefusal(err(422, "email_account_not_found", "x"), "Triage", "w@x.test"),
    trgUseFlowRefusal(err(404, "connection_not_found", "x"), "Digest", "CI hook"),
    trgUseFlowRefusal(err(403, "workflow_refused", "x"), "Digest", "CI hook"),
    trgUseFlowRefusal(err(422, "invalid_definition", "workflow digest@1.0.0:digest is not loaded on this gateway"), "Digest", "CI hook"),
  ],
}}));
"""
    r = _node(harness)
    # Named after the flow, its inputs and context, THIS connection (the author's never kept).
    assert r["feed"] == {"request_id": "rq1", "title": "Digest",
                         "target": {"bundle_ref": "digest@1.0.0", "flow_id": "digest", "input_data": {"prompt": "Summarize the new items"}},
                         "trigger": {"source_id": "feed.item", "source_version": 1, "config": {}, "connection_id": "conn_1"},
                         "context": {"mode": "growing", "growing": {"max_tokens": 9000}}}
    # Another kind of trigger: the flow's trigger settings do not apply, its inputs do.
    assert r["hook"]["trigger"] == {"source_id": "webhook.received", "source_version": 1, "config": {}, "connection_id": "conn_2"}
    # A mailbox: `config.account` is this mailbox; the flow's own mail filters stay.
    assert r["mail"]["trigger"] == {"source_id": "email.received", "source_version": 1, "config": {"from": ["boss@x.test"], "account": "work"}}
    assert r["mail"]["title"] == "Triage" and r["mail"]["context"] == {"mode": "independent"}
    assert r["refusals"] == [
        "Triage cannot read w@x.test: it has no incoming server. Connect it again with its incoming server (My email).",
        "w@x.test is not connected any more: add it again in My email.",
        "CI hook is not one of your connections any more.",
        "You may not run Digest on this gateway: ask its administrator.",
        "Digest was not turned on: workflow digest@1.0.0:digest is not loaded on this gateway",
    ]
    # The create is the automations route; the flows are GET /bundles; the face's name opens Flow at
    # ?flow=<library id>, New flow at ?trigger=, Runs Observer, all through the app door; automations
    # are read active AND paused.
    assert 'api("/api/gateway/automations", { method: "POST"' in _slice_function(source, "useFlowForTrigger")
    assert 'api("/api/gateway/bundles")' in _slice_function(source, "loadTriggerFlows")
    assert "/api/gateway/apps/${appId}/open" in _slice_function(source, "trgOpenApp")
    assert '"/api/gateway/automations?status=active,paused&limit=200"' in _slice_function(source, "loadTriggerAutomations")
    assert 'trgOpenApp("flow", "Flow", trgFlowPath(k), k)' in source and 'trgOpenApp("observer", "Observer", TRG_OBSERVER_PATH, k)' in source
    assert 'trgOpenApp("flow", "Flow", trgLibraryPath(t.dataset.trgOpenflow), trgRowKey(t))' in source
    assert "trg.library = trgLibraryIds(out.items);" in _slice_function(source, "loadTriggerFlows")
