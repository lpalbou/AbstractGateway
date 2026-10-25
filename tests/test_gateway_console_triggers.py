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
const TRG_FIELD_LABELS = { subject_path: "Subject (path in the JSON body)", tolerance_s: "Accepted clock skew (seconds)", url: "Feed address", every: "Check every", max_items: "Items per check (at most)", initial_items: "Items already in the feed" };
const TRG_MAILBOX_STATE = { connected: ["Active", "is-ok"], receive_only: ["Read-only", "is-ok"], paused: ["Paused", "is-muted"], needs_reconnect: ["Needs reconnecting", "is-warn"], unavailable: ["Unavailable", "is-muted"], not_connected: ["Not connected", "is-muted"] };
"""

ROW_FNS = ("trgAgo", "trgSourceLabel", "trgTriggerPhrase", "trgUsedBy", "trgListeningWords", "trgDoingText", "trgListenSwitch", "trgAdvancedMarkup", "trgStatusCell", "trgMailboxStatusCell", "trgMailboxStatusChip", "mailboxRowsMarkup", "trgPrincipalText", "trgStatusChip", "trgFireLine", "trgCopyRow",
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
    assert "When Release &lt;feed&gt; has a new item" in c1 and "feed.item" not in c1.replace('data-trg', '')
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
    assert "gone.source" in c3 and '<span class="trg-chip is-warn">weird</span>' in c3
    assert "Test not sent: connection is paused" in c3
    # The account column only in the admin's all-accounts view.
    assert "trg-account" not in mine
    allv = r["all"]
    assert allv.count('class="trg-account"') == 3 and ">bob<" in allv and "owner not reported" in allv
    assert "No connection yet." in r["empty"] and 'colspan="4"' in r["empty"]
    assert "No account has a connection yet." in r["emptyAll"] and 'colspan="5"' in r["emptyAll"]
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
    harness = HELPERS + _fns(source, "trgHumanize", "trgSentence", "triggerFormFields", "triggerFormMarkup", "triggerConfigFromValues") + """
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
    assert r["bad"]["problems"] == ["Feed address does not have the expected form.", "Every (minutes) must be a whole number.", "Ratio must be a number.", "Kind is required."]


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
    harness = HELPERS + _fns(source, "trgConnectableSources", "trgAgo", "trgUsedBy", "trgListeningWords", "trgMailboxStatusChip", "trgMailboxStatusCell", "mailboxRowsMarkup") + """
const SOURCES = [
  {id: "email.received", version: 1, label: "When an email arrives", available: true, needs_connection: true, connection_provider: "email_accounts", connection_schema: {type: "object"}},
  {id: "webhook.received", version: 1, label: "When a webhook is called", available: true, needs_connection: true, connection_provider: "trigger_connections", connection_schema: {type: "object", properties: {}}},
];
const items = [
  {account_id: "default", primary: true, configured: true, address: "me@example.org", can_send: true, enabled: true, mailbox: {state: "connected"}, watcher: {}},
  {account_id: "work", primary: false, configured: true, address: "me@work.example", can_send: false, enabled: true, mailbox: {state: "receive_only"}, watcher: {last_poll: "2026-10-25T11:59:00Z"}},
  {account_id: "old", primary: false, configured: false, address: null, can_send: false, enabled: false, mailbox: {state: "not_connected"}, watcher: {}},
];
console.log(JSON.stringify([trgConnectableSources(SOURCES).map((s) => s.id), mailboxRowsMarkup(items, {sources: SOURCES, now: Date.parse("2026-10-25T12:00:00Z")})]));
"""
    ids, rows = _node(harness)
    assert ids == ["webhook.received"]  # the mailbox source is never offered in New connection
    assert rows.count('class="trg-row trg-row--mailbox"') == 2  # configured mailboxes only
    work = rows[rows.index('data-trg-mailbox="work"'):]
    assert '<span class="trg-name">work</span>' in work and "When mail arrives in me@work.example · read-only" in work
    # The same three states with the mailbox's own switch (no used_by reported here: "Listening").
    assert '<span class="trg-doing">Listening</span>' in work and "checked 1 min ago" in work
    assert 'data-trg-mailbox-use="work"' in work and ">Use this mailbox</span>" in work and 'aria-checked="true"' in work
    assert 'data-trg-mailbox-manage="work"' in work and "Manage under Account → My email" in work
    assert "me@example.org (primary)" in rows


def test_webhook_form_is_readable_and_hides_the_generated_secret() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "trgHumanize", "trgSentence", "triggerFormFields", "triggerFormMarkup") + """
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
    harness = HELPERS + _fns(source, "webhookCurlExample", "webhookTestResultMarkup", "webhookSetupMarkup") + """
const c = {connection_id: "conn_1", ingress_url: "http://127.0.0.1:8080/api/gateway/triggers/webhooks/conn_1", secret: "whsec_c2VjcmV0"};
console.log(JSON.stringify([webhookCurlExample(c.ingress_url, c.secret), webhookSetupMarkup(c, null),
  webhookSetupMarkup(c, {status: 202, reason: "", event: {sequence: 3, type: "abstract.webhook.received.v1"}}),
  webhookSetupMarkup(c, {status: 401, reason: "webhook_signature_invalid"})]));
"""
    curl, panel, ok, refused = _node(harness)
    assert "SECRET='whsec_c2VjcmV0'" in curl and "URL='http://127.0.0.1:8080/api/gateway/triggers/webhooks/conn_1'" in curl
    assert 'KEY=$(printf %s "${SECRET#whsec_}" | base64 -d | xxd -p -c 256)' in curl
    assert "openssl dgst -sha256 -mac HMAC -macopt hexkey:$KEY -binary | base64" in curl
    assert '-H "webhook-signature: v1,$SIG"' in curl
    assert "paste the webhook address as the webhook URL, the signing secret as the secret, and choose JSON" in panel
    assert 'data-trg-hook-test="1"' in panel and ">Send a signed test call</button>" in panel
    assert 'data-trg-copy="created:curl"' in panel
    assert "HTTP 202" in ok and "The event it created (#3)" in ok
    assert "HTTP 401 · webhook_signature_invalid" in refused and "is-failed" in refused


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


def test_read_only_mailbox_body_and_rows() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "mailboxConnectBody", "mbxFurther", "mbxStateText", "mailboxDeleteQuestion", "mailboxRowMarkup", "mailboxesMarkup") + """
const MBX_ID_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const MBX_STATE = { connected: "Connected", receive_only: "Read-only: watched by your automations, never sends", paused: "Paused", needs_reconnect: "Needs reconnecting", unavailable: "Mailboxes are turned off for this account", not_connected: "Not connected" };
const good = mailboxConnectBody({id: "abstractframework", address: "lpalbou@abstractframework.ai", host: "imap.example.ai", port: "993", security: "ssl", username: "", password: "pw"});
const bad = mailboxConnectBody({id: "Default!", address: "x", host: "", port: "99a", password: ""});
const reserved = mailboxConnectBody({id: "default", address: "a@b.c", host: "h", password: "p"});
const items = [
  {account_id: "default", primary: true, configured: true, enabled: true},
  {account_id: "abstractframework", primary: false, configured: true, address: "lpalbou@abstractframework.ai", folder: "INBOX", can_send: false, enabled: true, mailbox: {state: "receive_only"}, watcher: {}},
];
console.log(JSON.stringify([good, bad.problems, reserved.problems, mailboxesMarkup(items, {note: {}, confirm: "abstractframework"}), mailboxesMarkup([items[0]], {})]));
"""
    good, bad, reserved, rows, empty = _node(harness)
    assert good == {"id": "abstractframework", "problems": [], "body": {
        "address": "lpalbou@abstractframework.ai", "password": "pw", "username": "",
        "imap": {"host": "imap.example.ai", "security": "ssl", "port": 993}, "send": False, "test": True}}
    assert bad == ["Name: 1 to 32 lowercase letters, digits, - or _, starting with a letter or digit.", "Address is required.",
                   "IMAP server is required.", "Port must be a number.", "Password is required."]
    assert reserved == ["“default” is your primary mailbox: choose another name."]
    assert 'data-mbx-row="abstractframework"' in rows and "read-only" in rows and 'data-mbx-switch="abstractframework"' in rows
    assert 'data-mbx-test="abstractframework"' in rows and 'data-mbx-delete-now="abstractframework"' in rows
    assert "Delete the mailbox abstractframework (lpalbou@abstractframework.ai)?" in rows
    assert empty == '<p class="mbx-empty">No further mailbox yet.</p>'


def test_my_email_has_the_further_mailboxes_card() -> None:
    html = gateway_console_html()
    sec = html[html.index('id="my-email-section"'):]
    card = sec[sec.index('id="my-mailboxes-card"'): sec.index('aria-labelledby="my-email-notify-title"')]
    for el in ('>Add a mailbox (read-only)</button>', 'id="my-mailbox-id"', 'id="my-mailbox-imap-host"', 'id="my-mailbox-imap-port"',
               'id="my-mailbox-username"', 'id="my-mailbox-password"', '>Add mailbox</button>'):
        assert el in card, el
    source = _console_script()
    assert "loadMyMailboxes();" in _slice_function(source, "loadMyEmail")
    assert "bindMyMailboxes();" in source


def test_rows_say_what_a_connection_is_doing_with_a_listening_toggle() -> None:
    source = _console_script()
    harness = HELPERS + _fns(source, "trgUsedBy", "trgListeningWords", "trgDoingText", "trgListenSwitch", "trgAdvancedMarkup", "trgStatusChip", "trgStatusCell") + """
const rows = [
  {connection_id: "a", label: "bbc-feed", status: "active", paused: false, always_listen: false, listening: "listening", used_by: 2},
  {connection_id: "b", label: "hook", status: "active", paused: false, always_listen: false, listening: "listening", used_by: ["auto_1"]},
  {connection_id: "c", label: "idle", status: "active", paused: false, always_listen: false, listening: "idle", used_by: 0},
  {connection_id: "d", label: "stopped", status: "active", paused: true, always_listen: false, listening: "paused", used_by: 3},
  {connection_id: "e", label: "old gateway", status: "active"},
  {connection_id: "f", label: "eager", status: "active", paused: false, always_listen: true, listening: "listening", used_by: 0},
];
console.log(JSON.stringify(rows.map((r) => [trgDoingText(r), trgStatusCell(r)])));
"""
    out = _node(harness)
    assert [d for d, _ in out] == [
        "Listening · used by 2 automations", "Listening · used by 1 automation",
        "Listening · nothing uses it yet, so nothing is fetched until an automation does", "Paused", "",
        "Listening · nothing uses it yet; fetching anyway (Keep fetching even when nothing uses it)"]
    on, paused, old = out[0][1], out[3][1], out[4][1]
    assert 'role="switch"' in on and 'aria-checked="true"' in on and ">Listening</span>" in on and 'data-trg-listen="a"' in on
    assert 'aria-checked="false"' in paused
    assert "listening state not reported (update the gateway)" in old and 'role="switch"' not in old
    # Advanced (collapsed): keep fetching even when nothing uses it.
    assert '<details class="trg-advanced"><summary>Advanced</summary>' in on and 'data-trg-always="a"' in on
    assert ">Keep fetching even when nothing uses it</span>" in on and 'aria-checked="true"' in out[5][1]
    for _, cell in out:
        assert " on<" not in cell and " off<" not in cell
    html = gateway_console_html()
    panel = html[html.index('id="tab-triggers"'): html.index('id="tab-skills"')]
    assert "A connection only fetches while an automation uses it; pause it to stop even then." in panel
    assert 'JSON.stringify({ paused })' in _slice_function(source, "setTriggerPaused")
    assert 'JSON.stringify({ always_listen: on })' in _slice_function(source, "setTriggerAlwaysListen")


def test_gmail_help_and_google_sign_in_offer() -> None:
    source = _console_script()
    harness = _fns(source, "mailboxProviderHelp", "mbxGoogleAvailable") + """
const MBX_GMAIL_HELP = "Gmail refuses account passwords. Use an app password (Google Account → Security → 2-Step Verification → App passwords), or Sign in with Google below.";
console.log(JSON.stringify([
  mailboxProviderHelp("imap.gmail.com", "x@abstractframework.ai"), mailboxProviderHelp("", "me@gmail.com"),
  mailboxProviderHelp("imap.fastmail.com", "me@fastmail.com"),
  mbxGoogleAvailable({providers: {google: {configured: false, builtin_available: true}}}),
  mbxGoogleAvailable({providers: {google: {configured: true, builtin_available: false}}}),
  mbxGoogleAvailable({providers: {google: {configured: false, builtin_available: false}}}),
  mbxGoogleAvailable({}),
]));
"""
    a, b, c, g1, g2, g3, g4 = _node(harness)
    assert a == b and a.startswith("Gmail refuses account passwords. Use an app password")
    assert c == ""
    assert (g1, g2, g3, g4) == (True, True, False, False)
    html = gateway_console_html()
    card = html[html.index('id="my-mailboxes-card"'):]
    assert '<button id="my-mailbox-google" class="secondary" type="button" hidden>Sign in with Google</button>' in card
    start = _slice_function(source, "startMailboxGoogle")
    assert "/api/gateway/me/email/accounts/${encodeURIComponent(id)}/oauth" in start and "`${base}/start`" in start and "`${base}/finish`" in start
    assert "Not added. ${help}" in _slice_function(source, "addMyMailbox")
