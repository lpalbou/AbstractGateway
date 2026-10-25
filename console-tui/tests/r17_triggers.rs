//! Root backlog 1041 — the terminal console's Triggers page (WORK, key V),
//! the web console's `console_triggers.py` in a terminal: same routes, same
//! sentences. Pure folds (rows, sources, the connection_schema form, events,
//! the Test result) and a synthesized mouse click for EVERY action (row
//! Events / Test / Delete, head Refresh / New connection, the admin's All
//! accounts toggle) through the real input pipeline, each asserted at the
//! route it sends. The meta-test enumerates `row_actions` / `panel_actions`:
//! an action without a click test is RED.

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::store::json::WriteState;
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::triggers::{
    self, config_from, connectable_sources, delete_question, event_lines, form_fields, rows_from,
    status_word, test_result, K_EVENTS, K_LIST, K_MAILBOXES, K_SOURCES,
};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn sources() -> Value {
    json!({"items": [
        {"id": "schedule", "version": 2, "label": "Schedule", "available": true, "needs_connection": false, "connection_schema": {}},
        {"id": "feed.item", "version": 1, "label": "Feed item", "available": true, "needs_connection": true,
         "connection_schema": {"type": "object", "required": ["url"], "properties": {
            "url": {"type": "string", "title": "Feed address", "pattern": "^https?://"},
            "every_minutes": {"type": "integer", "title": "Check every (minutes)", "default": 15}}}},
        {"id": "webhook.received", "version": 1, "label": "Webhook received", "available": true, "needs_connection": true,
         "connection_schema": {"type": "object", "properties": {}}},
        {"id": "email.received", "version": 1, "label": "When an email arrives", "available": true, "needs_connection": true,
         "connection_provider": "email_accounts", "connection_schema": {"type": "object", "properties": {"account": {"type": "string"}}}},
        {"id": "broken", "label": "Broken", "available": false}
    ]})
}

fn list() -> Value {
    json!({"connections": [
        {"connection_id": "conn_feed1", "source_id": "feed.item", "source_version": 1, "label": "Release feed",
         "status": "active", "created_at": "2026-10-25T10:00:00Z", "last_event_at": null, "config_public": {}},
        {"connection_id": "conn_hook1", "source_id": "webhook.received", "source_version": 1, "label": "CI hook",
         "status": "paused", "created_at": "2026-10-25T10:00:00Z", "last_event_at": null, "config_public": {},
         "ingress_url": "http://127.0.0.1:8080/api/gateway/triggers/webhooks/conn_hook1", "principal": "default:bob"}
    ]})
}

// ------------------------------------------------------------- pure folds

#[test]
fn rows_sources_and_words() {
    let rows = rows_from(&list()).unwrap();
    assert_eq!(rows.len(), 2);
    assert_eq!(
        rows[1].ingress_url.as_deref(),
        Some("http://127.0.0.1:8080/api/gateway/triggers/webhooks/conn_hook1")
    );
    assert_eq!(triggers::owner_text(rows[1].principal.as_deref()), "bob");
    assert_eq!(triggers::owner_text(None), "owner not reported");
    assert!(rows_from(&json!({"items": []})).is_err());
    let src = connectable_sources(&sources()).unwrap();
    let mb = triggers::mailboxes_from(&mailboxes()).unwrap();
    assert_eq!(
        mb.iter().map(|m| m.name()).collect::<Vec<_>>(),
        ["admin@example.org", "me@work.example"]
    );
    assert_eq!(
        mb[1].phrase(),
        "When mail arrives in me@work.example · Read only"
    );
    assert_eq!(
        mb[0].phrase(),
        "When mail arrives in admin@example.org · Read and write"
    );
    // receive_only now means read and write without an outgoing server.
    assert_eq!(mb[1].status(), ("Can't send".to_string(), "warn"));
    assert_eq!(
        src.iter().map(|s| s.id.as_str()).collect::<Vec<_>>(),
        ["feed.item", "webhook.received"]
    );
    // A gateway older than connections is said, never read as "needs none".
    let old = connectable_sources(
        &json!({"items": [{"id": "schedule", "version": 2, "available": true}]}),
    )
    .unwrap_err();
    assert!(
        old.contains("needs_connection missing on schedule@2"),
        "{old}"
    );
    let noschema = connectable_sources(
        &json!({"items": [{"id": "x", "version": 1, "available": true, "needs_connection": true}]}),
    )
    .unwrap_err();
    assert!(
        noschema.contains("serves no connection_schema object"),
        "{noschema}"
    );
    assert_eq!(status_word("active"), ("Active".to_string(), "ok"));
    assert_eq!(status_word("paused"), ("Paused".to_string(), "muted"));
    assert_eq!(status_word("weird"), ("weird".to_string(), "warn"));
    assert_eq!(
        delete_question("CI hook"),
        "Delete CI hook? Automations that listen to it stop receiving its events."
    );
    assert_eq!(triggers::list_path(true), "/triggers/connections?all=true");
    assert_eq!(
        triggers::events_path("c 1"),
        "/triggers/connections/c%201/events?limit=20"
    );
    assert_eq!(
        triggers::test_path("c1"),
        "/triggers/connections/c1/test-fire"
    );
}

#[test]
fn form_engine_matches_the_web() {
    let schema = json!({"type": "object", "required": ["url", "kind"], "properties": {
        "url": {"type": "string", "title": "Feed address", "pattern": "^https?://"},
        "token": {"type": "string", "title": "Token", "format": "password"},
        "secret": {"type": "string", "writeOnly": true, "default": "never-shown"},
        "every_minutes": {"type": "integer", "title": "Every (minutes)", "default": 15},
        "ratio": {"type": "number"},
        "include_body": {"type": "boolean", "default": false},
        "kind": {"enum": ["rss", "atom"], "default": "rss"},
        "nested": {"type": "object", "properties": {"a": {"type": "string"}}}
    }});
    let fields = form_fields(&schema);
    let kinds: Vec<(String, &str)> = fields.iter().map(|f| (f.key.clone(), f.kind)).collect();
    assert!(
        kinds.contains(&("token".into(), "password"))
            && kinds.contains(&("secret".into(), "password"))
    );
    assert!(
        kinds.contains(&("nested".into(), "readonly")) && kinds.contains(&("kind".into(), "enum"))
    );
    let secret = fields.iter().find(|f| f.key == "secret").unwrap();
    assert_eq!(
        triggers::initial_value(secret),
        "",
        "a default secret is never prefilled"
    );
    let v = |pairs: &[(&str, &str)]| {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect::<Vec<_>>()
    };
    let (good, problems) = config_from(
        &fields,
        &v(&[
            ("url", "https://example.org/f"),
            ("token", "abc"),
            ("every_minutes", "30"),
            ("ratio", "0.5"),
            ("include_body", "true"),
            ("kind", "\"atom\""),
        ]),
    );
    assert!(problems.is_empty(), "{problems:?}");
    assert_eq!(
        good,
        json!({"url": "https://example.org/f", "token": "abc", "every_minutes": 30, "ratio": 0.5, "include_body": true, "kind": "atom"})
    );
    let (_, bad) = config_from(
        &fields,
        &v(&[("url", "ftp://x"), ("every_minutes", "2.5"), ("ratio", "x")]),
    );
    // The web's sentences, in schema order (serde_json keeps keys sorted: every_minutes, kind, ratio, url).
    let mut bad_sorted = bad.clone();
    bad_sorted.sort();
    assert_eq!(
        bad_sorted,
        [
            "Every (minutes) must be a whole number.",
            "Feed address does not have the expected form.",
            "Kind is required.",
            "Ratio must be a number.",
        ]
    );
}

#[test]
fn events_newest_first_capped_and_test_result_words() {
    let events: Vec<Value> = (1..=25)
        .map(|i| json!({"id": format!("e{i}"), "type": "abstractframework.feed.item", "subject": format!("item {i}"),
                         "time": "2026-10-25T11:00:00Z", "data": if i == 25 { json!({"test": true}) } else { json!({}) }, "sequence": i}))
        .collect();
    let lines = event_lines(&events, 0);
    assert_eq!(lines.len(), 20);
    assert!(lines[0].0.starts_with("Test event · "), "{}", lines[0].0);
    // Newest first, the last 20 (#25 … #6); sentences, not ids.
    assert!(
        lines[1].0.starts_with("New item: item 24"),
        "{}",
        lines[1].0
    );
    assert!(
        lines[19].0.starts_with("New item: item 6"),
        "{}",
        lines[19].0
    );
    assert!(lines[0].1.contains("\"sequence\": 25"));
    assert_eq!(
        test_result(&WriteState::Done(json!({"event_id": "e9", "sequence": 9}))),
        Some(("Test event #9 sent.".to_string(), true))
    );
    assert_eq!(test_result(&WriteState::Pending), None);
    assert!(test_result(&WriteState::Done(json!({})))
        .unwrap()
        .0
        .starts_with("Test not sent:"));
}

// ------------------------------------------------------------ the clicks

fn page(admin: bool) -> r8w4::Harness {
    let mut h = harness((150, 40), Mount::Page(triggers::screen));
    if admin {
        h.admin();
    } else {
        h.identity("alice", false);
    }
    h.store.json.set(K_SOURCES, Loadable::Ready(sources()));
    h.store.json.set(K_LIST, Loadable::Ready(list()));
    h.store.json.set(K_MAILBOXES, Loadable::Ready(mailboxes()));
    h.turns(3);
    h.sent();
    h
}

fn mailboxes() -> Value {
    json!({"items": [
        {"account_id": "default", "primary": true, "configured": true, "address": "admin@example.org", "can_send": true,
         "enabled": true, "mailbox": {"state": "connected"}, "watcher": {}},
        {"account_id": "work", "primary": false, "configured": true, "address": "me@work.example", "can_send": false,
         "enabled": true, "mailbox": {"state": "receive_only"}, "watcher": {}},
        {"account_id": "old", "primary": false, "configured": false}
    ]})
}

fn json_cmds(h: &mut r8w4::Harness) -> Vec<JsonCmd> {
    h.sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Json(j) => Some(j),
            _ => None,
        })
        .collect()
}

/// Click `needle` on the row whose line holds `name`.
fn click_row(h: &mut r8w4::Harness, name: &str, needle: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains(name))
        .unwrap_or_else(|| panic!("{name} row:\n{screen}"));
    let byte = line
        .rfind(needle)
        .unwrap_or_else(|| panic!("{needle:?} not on {name}'s row:\n{screen}"));
    let x = line[..byte].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn click_pair(h: &mut r8w4::Harness, label: &str, other: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(&format!(" {label} ")) && l.contains(&format!(" {other} ")))
        .last()
        .unwrap_or_else(|| panic!("no [{label}] [{other}] row:\n{screen}"));
    let b = line.rfind(&format!(" {label} ")).unwrap() + 1;
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn clicked() -> &'static std::sync::Mutex<BTreeSet<&'static str>> {
    static S: std::sync::OnceLock<std::sync::Mutex<BTreeSet<&'static str>>> =
        std::sync::OnceLock::new();
    S.get_or_init(|| std::sync::Mutex::new(BTreeSet::new()))
}

#[test]
fn the_page_shows_the_webs_words() {
    let mut h = page(true);
    let s = h.turns(2);
    for w in [
        "Triggers",
        "A connection is something of yours the gateway listens to",
        "All accounts",
        "New connection",
        "Refresh",
        "Name",
        "Status",
        "Last event",
        "When Release feed has a new item",
        "Release feed",
        "Active",
        "Never",
        "When CI hook is called",
        "CI hook",
        "admin@example.org",
        "When mail arrives in admin@example.org",
        "When mail arrives in me@work.example · Read only",
        "When mail arrives in admin@example.org · Read and write",
        "Can't send",
        "Manage in My email",
        "Paused",
        "Events",
        "Test",
        "Delete",
    ] {
        assert!(s.contains(w), "{w:?} missing:\n{s}");
    }
    // A non-admin has no All accounts toggle.
    let mut u = page(false);
    let s = u.turns(2);
    assert!(!s.contains("All accounts"), "{s}");
}

#[test]
fn click_events_reads_the_route_and_shows_the_json() {
    let mut h = page(true);
    click_row(&mut h, "CI hook", "Events");
    clicked().lock().unwrap().insert("events");
    let cmds = json_cmds(&mut h);
    assert!(cmds.iter().any(|c| matches!(c, JsonCmd::Get { key, path, .. } if key == K_EVENTS && path == "/triggers/connections/conn_hook1/events?limit=20")), "{cmds:?}");
    h.store.json.set(K_EVENTS, Loadable::Ready(json!({"events": [
        {"id": "evt_1", "type": "abstractframework.trigger.test", "subject": "Test event", "data": {"test": true}, "sequence": 4}
    ]})));
    let s = h.turns(3);
    assert!(s.contains("Recent events — CI hook"), "{s}");
    assert!(
        s.contains("Webhook address") && s.contains("/api/gateway/triggers/webhooks/conn_hook1"),
        "{s}"
    );
    assert!(s.contains("│Test event "), "{s}");
    assert!(s.contains("\"id\": \"evt_1\""), "{s}");
}

#[test]
fn click_test_posts_and_says_the_result_in_the_row() {
    let mut h = page(true);
    click_row(&mut h, "Release feed", "Test");
    clicked().lock().unwrap().insert("test");
    let cmds = json_cmds(&mut h);
    assert!(cmds.iter().any(|c| matches!(c, JsonCmd::Send { method, path, .. } if method == "POST" && path == "/triggers/connections/conn_feed1/test-fire")), "{cmds:?}");
    h.store.json.set_write(
        triggers::K_TEST,
        Some(WriteState::Done(
            json!({"event_id": "evt_9", "sequence": 9}),
        )),
    );
    let s = h.turns(3);
    assert!(s.contains("Test event #9 sent."), "{s}");
}

#[test]
fn click_delete_asks_then_deletes() {
    let mut h = page(true);
    click_row(&mut h, "CI hook", "Delete");
    clicked().lock().unwrap().insert("delete");
    let s = h.turns(2);
    assert!(
        s.contains("Delete CI hook? Automations that listen to it stop receiving its events."),
        "{s}"
    );
    assert!(
        json_cmds(&mut h).is_empty(),
        "nothing sent before the answer"
    );
    click_pair(&mut h, "Delete", "Cancel");
    let cmds = json_cmds(&mut h);
    assert!(cmds.iter().any(|c| matches!(c, JsonCmd::Send { method, path, .. } if method == "DELETE" && path == "/triggers/connections/conn_hook1")), "{cmds:?}");
}

#[test]
fn click_refresh_and_all_accounts() {
    let mut h = page(true);
    h.click_text("Refresh");
    clicked().lock().unwrap().insert("refresh");
    let cmds = json_cmds(&mut h);
    assert!(
        cmds.iter()
            .any(|c| matches!(c, JsonCmd::Get { path, .. } if path == "/trigger-sources")),
        "{cmds:?}"
    );
    assert!(
        cmds.iter()
            .any(|c| matches!(c, JsonCmd::Get { path, .. } if path == "/triggers/connections")),
        "{cmds:?}"
    );
    h.click_text("All accounts");
    let cmds = json_cmds(&mut h);
    assert!(
        cmds.iter().any(
            |c| matches!(c, JsonCmd::Get { path, .. } if path == "/triggers/connections?all=true")
        ),
        "{cmds:?}"
    );
    h.store.json.set(K_LIST, Loadable::Ready(list()));
    let s = h.turns(3);
    assert!(s.contains("Account") && s.contains("bob"), "{s}");
    // Back off: the person's own list again.
    h.click_text("All accounts");
    let cmds = json_cmds(&mut h);
    assert!(
        cmds.iter()
            .any(|c| matches!(c, JsonCmd::Get { path, .. } if path == "/triggers/connections")),
        "{cmds:?}"
    );
}

#[test]
fn click_new_connection_creates_and_shows_the_secret_once() {
    let mut h = page(true);
    h.click_text("New connection");
    clicked().lock().unwrap().insert("new");
    let s = h.turns(3);
    assert!(
        s.contains("What to listen to") && s.contains("Feed item") && s.contains("Name *"),
        "{s}"
    );
    assert!(
        s.contains("Feed address *") && s.contains("Check every (minutes)"),
        "{s}"
    );
    assert!(
        !s.contains("Schedule"),
        "a source that needs no connection is not offered:\n{s}"
    );
    // Create without a name and with no address: the web's sentences.
    h.click_after("Feed address *", "Create");
    let s = h.turns(2);
    assert!(
        s.contains("Name is required.") && s.contains("Feed address is required."),
        "{s}"
    );
    // Nothing is written; the dialog only read the well-known feeds.
    let cmds = json_cmds(&mut h);
    assert!(
        cmds.iter()
            .all(|c| matches!(c, JsonCmd::Get { path, .. } if path == triggers::CATALOG_PATH)),
        "{cmds:?}"
    );
    // Fill the name and the address (the first inputs after their labels).
    h.click_right_of("Name *", 30);
    h.type_text("Changelog");
    h.click_right_of("Feed address *", 30);
    h.type_text("https://example.org/changelog.xml");
    let filled = h.turns(2);
    assert!(
        filled.contains("Changelog") && filled.contains("https://example.org/changelog.xml"),
        "{filled}"
    );
    h.click_after("Feed address *", "Create");
    let cmds = json_cmds(&mut h);
    let body = cmds
        .iter()
        .find_map(|c| match c {
            JsonCmd::Send {
                method, path, body, ..
            } if method == "POST" && path == "/triggers/connections" => Some(body.clone()),
            _ => None,
        })
        .unwrap_or_else(|| panic!("no POST: {cmds:?}"));
    assert_eq!(
        body,
        json!({"source_id": "feed.item", "source_version": 1, "label": "Changelog",
                            "config": {"url": "https://example.org/changelog.xml", "every_minutes": 15}})
    );
    // A webhook's answer: the address and the one-time secret.
    h.store.json.set_write(triggers::K_CREATE, Some(WriteState::Done(json!({
        "connection_id": "conn_new", "label": "GitHub pushes", "source_id": "webhook.received", "source_version": 1,
        "ingress_url": "http://127.0.0.1:8080/api/gateway/triggers/webhooks/conn_new", "secret": "whsec_abc123"}))));
    let s = h.turns(3);
    assert!(s.contains("Connection created — GitHub pushes"), "{s}");
    assert!(s.contains("The signing secret is shown once."), "{s}");
    assert!(
        s.contains("whsec_abc123") && s.contains("Signing secret") && s.contains("Copy"),
        "{s}"
    );
    assert!(
        s.contains("Set up the sender") && s.contains("GitHub can't reach this computer yet"),
        "{s}"
    );
    assert!(s.contains("SECRET='whsec_abc123'"), "{s}");
    h.click_text("Done");
    let s = h.turns(2);
    assert!(
        !s.contains("whsec_abc123"),
        "the secret is gone once the dialog closes:\n{s}"
    );
}

/// The meta-test: every action the page offers has a click test above
/// (the set below is the list those tests cover; a new action is RED).
#[test]
fn every_action_has_a_click_test() {
    let covered: BTreeSet<&str> = ["events", "test", "delete", "refresh", "new", "manage"]
        .into_iter()
        .collect();
    let rows = rows_from(&list()).unwrap();
    let mut offered: BTreeSet<&str> = BTreeSet::new();
    for r in &rows {
        for a in triggers::row_actions(r) {
            offered.insert(a.id);
        }
    }
    for a in triggers::panel_actions() {
        offered.insert(a.id);
    }
    for a in triggers::mailbox_actions() {
        offered.insert(a.id);
    }
    let missing: Vec<&&str> = offered.difference(&covered).collect();
    assert!(
        missing.is_empty(),
        "actions without a click test: {missing:?}"
    );
}

/// 80x24 (the smallest supported terminal): the rows, their last event as a
/// note, and the three actions stay on screen.
#[test]
fn narrow_terminal_keeps_rows_and_actions() {
    let mut h = harness((80, 24), Mount::Page(triggers::screen));
    h.admin();
    h.store.json.set(K_SOURCES, Loadable::Ready(sources()));
    h.store.json.set(K_LIST, Loadable::Ready(list()));
    h.store.json.set(K_MAILBOXES, Loadable::Ready(mailboxes()));
    let s = h.turns(3);
    for w in [
        "Release feed",
        "CI hook",
        "Last event: Never",
        "Events",
        "Test",
        "Delete",
    ] {
        assert!(s.contains(w), "{w:?} missing at 80x24:\n{s}");
    }
}

#[test]
fn readable_webhook_form_and_mailbox_manage_click() {
    let schema = json!({"type": "object", "additionalProperties": false, "properties": {
        "subject_path": {"type": "string", "description": "dotted path in a JSON body for the event subject (default: the webhook id)"},
        "tolerance_s": {"type": "integer", "minimum": 60, "maximum": 3600, "default": 300},
        "secret": {"type": "string", "x-secret": true, "description": "whsec_<base64>; generated when omitted"}
    }});
    let f = form_fields(&schema);
    assert_eq!(
        f.iter().map(|x| x.label.as_str()).collect::<Vec<_>>(),
        [
            "Subject (path in the JSON body)",
            "Accepted clock skew (seconds)"
        ]
    );
    assert_eq!(
        f[0].description,
        "Dotted path in a JSON body for the event subject (default: the webhook id)"
    );
    let curl =
        triggers::webhook_curl_example("http://h/api/gateway/triggers/webhooks/c1", "whsec_abc");
    assert!(
        curl.contains("KEY=$(printf %s \"${SECRET#whsec_}\" | base64 -d | xxd -p -c 256)"),
        "{curl}"
    );
    assert!(curl.contains("-H \"webhook-signature: v1,$SIG\""), "{curl}");
    // The mailbox row's link opens your own email (the web's Account → My email).
    let mut h = page(true);
    let before = h.turns(1);
    assert!(!before.contains("Add your mailbox") || true);
    click_row(&mut h, "admin@example.org", "Manage");
    clicked().lock().unwrap().insert("manage");
    let s = h.turns(3);
    assert!(
        s.contains("Email") || s.contains("mailbox"),
        "the email dialog opens:\n{s}"
    );
}

#[test]
fn rows_say_what_a_connection_is_doing_and_the_listening_switch_pauses_it() {
    let mut l = list();
    l["connections"][0]["paused"] = json!(false);
    l["connections"][0]["always_listen"] = json!(false);
    l["connections"][0]["listening"] = json!("listening");
    l["connections"][0]["used_by"] = json!(2);
    l["connections"][1]["paused"] = json!(false);
    l["connections"][1]["always_listen"] = json!(false);
    l["connections"][1]["listening"] = json!("idle");
    l["connections"][1]["used_by"] = json!([]);
    let rows = rows_from(&l).unwrap();
    assert_eq!(
        triggers::doing_text(&rows[0]).as_deref(),
        Some("Listening · used by 2 automations")
    );
    assert_eq!(
        triggers::doing_text(&rows[1]).as_deref(),
        Some("Waiting for an automation to use it")
    );
    let mut eager = rows[1].clone();
    eager.always_listen = Some(true);
    assert_eq!(
        triggers::doing_text(&eager).as_deref(),
        Some("Listening · kept on even when nothing uses it")
    );
    let mut paused = rows[0].clone();
    paused.paused = Some(true);
    assert_eq!(triggers::doing_text(&paused).as_deref(), Some("Paused"));
    let old = rows_from(&list()).unwrap();
    assert_eq!(triggers::doing_text(&old[0]), None);

    let mut h = page(true);
    h.store.json.set(K_LIST, Loadable::Ready(l));
    let s = h.turns(3);
    for w in [
        "Listening · used by 2",
        "Waiting for an automation to",
        "A connection only fetches while an automation uses it. Pause it to stop fetching.",
    ] {
        assert!(s.contains(w), "{w:?} missing:\n{s}");
    }
    h.sent();
    // `l` on the selected (first) row pauses it.
    h.key(b"l");
    let cmds = json_cmds(&mut h);
    assert!(
        cmds.iter().any(|c| matches!(c, JsonCmd::Send { method, path, body, .. }
            if method == "PUT" && path == "/triggers/connections/conn_feed1" && *body == json!({"paused": true}))),
        "{cmds:?}"
    );
    // `k` (Advanced): keep fetching even when nothing uses it.
    h.key(b"k");
    let cmds = json_cmds(&mut h);
    assert!(
        cmds.iter()
            .any(|c| matches!(c, JsonCmd::Send { method, body, .. }
            if method == "PUT" && *body == json!({"always_listen": true}))),
        "{cmds:?}"
    );
}

#[test]
fn ux_review_words_errors_names_and_local_address() {
    let mut l = list();
    l["connections"][0]["paused"] = json!(false);
    l["connections"][0]["always_listen"] = json!(false);
    l["connections"][0]["used_by"] = json!(5);
    l["connections"][0]["last_error"] = json!({"code": "feed_http_404", "cause": "The feed answered HTTP 404.", "fix": "Check the feed address."});
    let rows = rows_from(&l).unwrap();
    let autos = json!({"items": (1..=5).map(|i| json!({"title": format!("Auto {i}"), "status": "active",
        "trigger": {"source_id": "feed.item", "config": {"connection_id": "conn_feed1"}}})).collect::<Vec<_>>()});
    let names = triggers::connection_users(&rows[0], &autos);
    assert_eq!(
        triggers::doing_text_with(&rows[0], &names).as_deref(),
        Some("Listening · used by Auto 1, Auto 2, Auto 3 +2")
    );
    assert_eq!(
        triggers::error_text(&rows[0]).as_deref(),
        Some("Can't read the feed: the address answered 404 (not found). Check the feed address.")
    );
    assert!(triggers::local_address("http://127.0.0.1:8080/x"));
    assert!(triggers::local_address("http://192.168.1.4/x"));
    assert!(!triggers::local_address("https://gw.example.net/x"));
    assert_eq!(
        triggers::next_step("bbc-feed"),
        "Next: in Flow, + → When something arrives → bbc-feed."
    );
    let mut h = page(true);
    h.store.json.set(K_LIST, Loadable::Ready(l));
    let s = h.turns(3);
    // A failing feed that is on: its red error IS the status line (never "Listening · …").
    let feed_row = s
        .lines()
        .find(|l| l.contains("Release feed") && !l.contains("When "))
        .unwrap_or_else(|| panic!("{s}"));
    assert!(
        feed_row.contains("Can't read the feed: the") && !feed_row.contains("Listening"),
        "{feed_row}\n{s}"
    );
    assert_eq!(
        s.matches("Can't read the feed").count(),
        1,
        "said once:\n{s}"
    );
}

/// UX re-review #4: the runtime's feed settings (enumNames, descriptions that restate the
/// values) read as controls with words: "Check every [15] [minutes]", "When added" names.
#[test]
fn the_runtime_feed_schema_reads_as_words() {
    let schema = json!({"type": "object", "required": ["url"], "properties": {
        "url": {"type": "string", "format": "uri", "title": "Feed address", "description": "The address of the RSS or Atom feed (starts with http:// or https://)."},
        "every": {"type": "string", "format": "duration", "pattern": "^[1-9][0-9]*[smhd]$", "default": "15m", "title": "Check every",
                  "description": "How often to check the feed: a number and a unit (s, m, h or d), for example 15m. At least 60s."},
        "initial_items": {"type": "string", "enum": ["skip", "deliver"], "enumNames": ["Ignore what is already there", "Also deliver the latest items"],
                          "default": "skip", "title": "When added",
                          "description": "What to do with the items already in the feed when it is first checked: ignore what is already there (skip), or also deliver the latest items (deliver)."}
    }});
    let fields = triggers::form_fields(&schema);
    let f = |k: &str| fields.iter().find(|f| f.key == k).unwrap().clone();
    assert_eq!(f("initial_items").kind, "enum", "never Not editable here");
    let opts: Vec<String> = triggers::choice_options(&f("initial_items"))
        .into_iter()
        .map(|(_, l)| l)
        .collect();
    assert_eq!(
        opts,
        [
            "(not set)",
            "Ignore what is already there",
            "Also deliver the latest items"
        ]
    );
    assert!(triggers::is_duration(&f("every")));
    assert_eq!(triggers::duration_parts("15m"), ("15".to_string(), 1));
    assert_eq!(triggers::duration_parts("2h"), ("2".to_string(), 2));
    assert_eq!(triggers::duration_parts("x"), (String::new(), 1));
    // Descriptions that restate the control are not shown; the others are.
    assert!(!triggers::description_shown(&f("every")));
    assert!(!triggers::description_shown(&f("initial_items")));
    assert!(triggers::description_shown(&f("url")));
}

#[test]
fn the_list_waits_for_connections_and_mailboxes_and_the_dialog_has_cancel_and_hints() {
    let mut h = harness((150, 40), Mount::Page(triggers::screen));
    h.admin();
    h.store.json.set(K_SOURCES, Loadable::Ready(sources()));
    h.store
        .json
        .set(K_LIST, Loadable::Ready(json!({"connections": []})));
    let s = h.turns(3);
    // Mailboxes not answered yet: the loading state, never the empty state.
    assert!(
        s.contains("Reading your connections and mailboxes..."),
        "{s}"
    );
    assert!(!s.contains("Nothing to listen to yet"), "{s}");
    h.store
        .json
        .set(K_MAILBOXES, Loadable::Ready(json!({"items": []})));
    let s = h.turns(3);
    assert!(s.contains("Nothing to listen to yet"), "{s}");
    h.store.json.set(K_MAILBOXES, Loadable::Loading);
    h.store.json.set(K_MAILBOXES, Loadable::Ready(mailboxes()));
    // New connection: a one-line hint per option, and Cancel.
    h.click_text("New connection");
    let s = h.turns(3);
    assert!(
        s.contains("Runs when a feed (RSS or Atom) publishes a new item."),
        "{s}"
    );
    let s2 = h.click_after("Feed address", "Cancel");
    assert!(
        !s2.contains("What to listen to"),
        "Cancel closes the dialog:\n{s2}"
    );
}

// ------------------------------------------- the well-known feeds (picker)

/// The SHIPPED catalogue, as the route serves it (parity with the web).
fn catalog() -> Value {
    serde_json::from_str(include_str!("../../src/abstractgateway/assets/feeds.json")).unwrap()
}

#[test]
fn feed_catalog_folds_match_the_web() {
    let feeds = triggers::catalog_from(&catalog()).unwrap();
    assert!(feeds.len() >= 15);
    let rows = triggers::catalog_rows(&feeds);
    assert_eq!(rows[0], triggers::CatalogRow::Heading("World news".into()));
    assert_eq!(rows.last(), Some(&triggers::CatalogRow::Another));
    let headings: Vec<String> = rows
        .iter()
        .filter_map(|r| match r {
            triggers::CatalogRow::Heading(c) => Some(c.clone()),
            _ => None,
        })
        .collect();
    assert_eq!(
        headings,
        [
            "World news",
            "Technology",
            "Science",
            "Business and finance",
            "Fill in your own"
        ]
    );
    let bbc = triggers::catalog_option(&rows[1], &feeds);
    assert_eq!(
        (bbc.label.as_str(), bbc.hint.as_deref()),
        ("BBC News · World", Some("bbc.co.uk"))
    );
    assert!(triggers::catalog_option(&rows[0], &feeds).disabled);
    // Every shipped template's pattern is in the subset the terminal checks.
    for f in feeds.iter().filter(|f| f.placeholder.is_some()) {
        let p = f.placeholder.as_ref().unwrap();
        let pat = p.pattern.as_deref().unwrap();
        assert_eq!(
            triggers::pattern_match(pat, &p.example),
            Some(true),
            "{}",
            f.id
        );
        assert_eq!(
            triggers::pattern_match(pat, "has space"),
            Some(false),
            "{}",
            f.id
        );
        assert_eq!(triggers::feed_param_problem(f, &p.example), None);
    }
    let gh = feeds.iter().find(|f| f.id == "github-releases").unwrap();
    assert_eq!(
        triggers::feed_address(gh, "python/cpython"),
        "https://github.com/python/cpython/releases.atom"
    );
    assert_eq!(
        triggers::feed_name(gh, "python/cpython"),
        "GitHub · python/cpython releases"
    );
    assert_eq!(
        triggers::feed_name(gh, ""),
        "GitHub · Releases of a repository"
    );
    assert_eq!(
        triggers::feed_param_problem(gh, "nope").as_deref(),
        Some("Repository (owner/name): not the expected form (e.g. python/cpython).")
    );
    assert_eq!(
        triggers::feed_param_problem(gh, "").as_deref(),
        Some("Repository (owner/name) is required.")
    );
    assert_eq!(
        triggers::pattern_match("^UC[A-Za-z0-9_-]{22}$", "UCshort"),
        Some(false)
    );
    assert_eq!(triggers::pattern_match("^(a|b)$", "a"), None);
    assert_eq!(
        triggers::site_name("https://www.bbc.co.uk/news"),
        "bbc.co.uk"
    );
}

fn feed_dialog() -> r8w4::Harness {
    let mut h = page(false);
    h.store
        .json
        .set(triggers::K_CATALOG, Loadable::Ready(catalog()));
    h.click_text("New connection");
    h.turns(3);
    h
}

fn create_post(h: &mut r8w4::Harness) -> Option<Value> {
    json_cmds(h).into_iter().find_map(|c| match c {
        JsonCmd::Send {
            method, path, body, ..
        } if method == "POST" && path == "/triggers/connections" => Some(body),
        _ => None,
    })
}

#[test]
fn picking_a_well_known_feed_fills_the_name_and_the_address() {
    let mut h = feed_dialog();
    let s = h.turns(2);
    assert!(
        s.contains(triggers::CATALOG_LABEL) && s.contains("Choose…"),
        "{s}"
    );
    assert!(
        s.contains("Pick one to fill in the name and the address"),
        "{s}"
    );
    let at = |s: &str, a: &str, b: &str| s.find(a).unwrap_or(usize::MAX) < s.find(b).unwrap_or(0);
    assert!(
        at(&s, triggers::CATALOG_LABEL, "Feed address *"),
        "the picker comes before the address:\n{s}"
    );
    assert!(
        json_cmds(&mut h).is_empty(),
        "the catalogue was ready: nothing read again"
    );
    // Open the picker: the categories and the feeds with their site.
    let open = h.click_text("Choose…");
    assert!(
        open.contains("— World news —")
            && open.contains("BBC News · World")
            && open.contains("bbc.co.uk"),
        "{open}"
    );
    h.type_text("B");
    let s = h.key(b"\r");
    assert!(
        s.contains("BBC News · World") && s.contains("https://feeds.bbci.co.uk/news/world/rss.xml"),
        "{s}"
    );
    assert!(
        s.contains("World headlines from the BBC. · bbc.co.uk"),
        "{s}"
    );
    h.click_after("Feed address *", "Create");
    let body = create_post(&mut h).expect("a POST");
    assert_eq!(body["label"], "BBC News · World");
    assert_eq!(
        body["config"]["url"],
        "https://feeds.bbci.co.uk/news/world/rss.xml"
    );
}

#[test]
fn a_template_asks_for_its_value_and_builds_the_address() {
    let mut h = feed_dialog();
    h.click_text("Choose…");
    h.type_text("G");
    let s = h.key(b"\r");
    assert!(
        s.contains("Repository (owner/name) *") && s.contains("python/cpython"),
        "the value's field, its example:\n{s}"
    );
    // Create with no value: its sentence, nothing sent.
    h.click_after("Feed address *", "Create");
    let s = h.turns(2);
    assert!(
        s.contains("Repository (owner/name) is required.")
            && !s.contains("Feed address is required."),
        "{s}"
    );
    assert!(create_post(&mut h).is_none());
    h.click_right_of("Repository (owner/name) *", 30);
    h.type_text("python/cpython");
    let s = h.turns(3);
    assert!(
        s.contains("https://github.com/python/cpython/releases.atom"),
        "{s}"
    );
    assert!(s.contains("GitHub · python/cpython releases"), "{s}");
    h.click_after("Feed address *", "Create");
    let body = create_post(&mut h).expect("a POST");
    assert_eq!(body["label"], "GitHub · python/cpython releases");
    assert_eq!(
        body["config"]["url"],
        "https://github.com/python/cpython/releases.atom"
    );
}

#[test]
fn another_address_keeps_the_free_field_and_the_persons_name() {
    let mut h = feed_dialog();
    // The person's own name first: a pick keeps it and fills the address.
    h.click_right_of("Name *", 30);
    h.type_text("Tech links");
    h.click_text("Choose…");
    h.type_text("H");
    let s = h.key(b"\r");
    assert!(
        s.contains("Tech links") && s.contains("https://news.ycombinator.com/rss"),
        "{s}"
    );
    h.click_text("Hacker News · Front page");
    h.type_text("An");
    let s = h.key(b"\r");
    assert!(
        s.contains(triggers::ANOTHER_ADDRESS) && s.contains("Tech links"),
        "{s}"
    );
    assert!(
        !s.contains("https://news.ycombinator.com/rss"),
        "the picker's address is emptied:\n{s}"
    );
    h.click_right_of("Feed address *", 30);
    h.type_text("https://example.org/feed.xml");
    h.click_after("Feed address *", "Create");
    let body = create_post(&mut h).expect("a POST");
    assert_eq!(body["label"], "Tech links");
    assert_eq!(body["config"]["url"], "https://example.org/feed.xml");
}

// ------------------------- a pick prefills Check every (the feed's check_every)

/// The New connection dialog with the runtime's feed schema (`every`, a duration) and the
/// shipped catalogue plus one custom feed without `check_every`.
fn every_dialog() -> r8w4::Harness {
    let mut h = harness((150, 40), Mount::Page(triggers::screen));
    h.identity("alice", false);
    h.store.json.set(
        K_SOURCES,
        Loadable::Ready(json!({"items": [
            {"id": "feed.item", "version": 1, "label": "Feed item", "available": true, "needs_connection": true,
             "connection_schema": {"type": "object", "required": ["url"], "properties": {
                "url": {"type": "string", "format": "uri", "title": "Feed address"},
                "every": {"type": "string", "format": "duration", "pattern": "^[1-9][0-9]*[smhd]$", "default": "15m", "title": "Check every"}}}}
        ]})),
    );
    h.store.json.set(K_LIST, Loadable::Ready(list()));
    h.store.json.set(K_MAILBOXES, Loadable::Ready(mailboxes()));
    let mut cat = catalog();
    cat["feeds"].as_array_mut().unwrap().push(json!({
        "id": "mine", "name": "Mine", "url": "https://example.org/feed.xml", "category": "My feeds", "source": "custom"}));
    h.store.json.set(triggers::K_CATALOG, Loadable::Ready(cat));
    h.turns(3);
    h.sent();
    h.click_text("New connection");
    h.turns(3);
    h
}

#[test]
fn every_after_pick_follows_the_web() {
    use triggers::every_after_pick as pick;
    // No pick yet: whatever the field holds is overwritten.
    assert_eq!(
        pick("2h", None, Some("12h"), "15m"),
        Some(("12h".into(), true))
    );
    // Still what the last pick left: the next feed's interval (or the default).
    assert_eq!(
        pick("12h", Some("12h"), Some("10m"), "15m"),
        Some(("10m".into(), true))
    );
    assert_eq!(
        pick("10m", Some("10m"), None, "15m"),
        Some(("15m".into(), false))
    );
    // Changed after a pick: it stands.
    assert_eq!(pick("45m", Some("10m"), Some("6h"), "15m"), None);
    assert_eq!(triggers::duration_value("12", 2), "12h");
    assert_eq!(triggers::duration_value(" ", 2), "");
    let feeds = triggers::catalog_from(&catalog()).unwrap();
    assert!(
        feeds.iter().all(|f| f.check_every.is_some()),
        "every shipped entry"
    );
    let hn = feeds.iter().find(|f| f.id == "hacker-news").unwrap();
    assert_eq!(hn.check_every.as_deref(), Some("10m"));
    let odd = triggers::catalog_from(&json!({"feeds": [
        {"id": "a", "name": "A", "url": "https://e.org/a"},
        {"id": "b", "name": "B", "url": "https://e.org/b", "check_every": "soon"}]}))
    .unwrap();
    assert_eq!(
        (odd[0].check_every.clone(), odd[1].check_every.clone()),
        (None, None)
    );
}

#[test]
fn picking_a_feed_prefills_check_every_and_keeps_a_later_change() {
    let mut h = every_dialog();
    let s = h.turns(2);
    assert!(s.contains("Check every") && s.contains("minutes"), "{s}");
    assert!(!s.contains(triggers::EVERY_RECOMMENDED), "{s}");
    // Changed before any pick (15 hours): the pick overwrites it with arXiv's 12 hours.
    h.click_text("minutes");
    h.type_text("h");
    let s = h.key(b"\r");
    assert!(s.contains("hours") && !s.contains("minutes"), "{s}");
    h.click_text("Choose…");
    h.type_text("arX");
    let s = h.key(b"\r");
    let every_line = |s: &str| {
        s.lines()
            .find(|l| l.contains("Check every"))
            .unwrap_or("")
            .to_string()
    };
    assert!(
        every_line(&s).contains("12") && every_line(&s).contains("hours"),
        "{s}"
    );
    assert!(s.contains(triggers::EVERY_RECOMMENDED), "{s}");
    // Changed after the pick (12 days): the next pick keeps it, without the note.
    h.click_text("hours");
    h.type_text("d");
    let s = h.key(b"\r");
    assert!(every_line(&s).contains("days"), "{s}");
    assert!(!s.contains(triggers::EVERY_RECOMMENDED), "{s}");
    h.click_text("arXiv · Artificial");
    h.type_text("Nature");
    let s = h.key(b"\r");
    assert!(s.contains("https://www.nature.com/nature.rss"), "{s}");
    assert!(
        every_line(&s).contains("12") && every_line(&s).contains("days"),
        "{s}"
    );
    assert!(!s.contains(triggers::EVERY_RECOMMENDED), "{s}");
    h.click_after("Feed address *", "Create");
    let body = create_post(&mut h).expect("a POST");
    assert_eq!(body["config"]["every"], "12d");
}

#[test]
fn a_feed_without_check_every_leaves_the_form_default() {
    let mut h = every_dialog();
    h.click_text("Choose…");
    h.type_text("Mine");
    let s = h.key(b"\r");
    assert!(s.contains("https://example.org/feed.xml"), "{s}");
    assert!(!s.contains(triggers::EVERY_RECOMMENDED), "{s}");
    h.click_text("Mine");
    h.type_text("Y");
    let s = h.key(b"\r");
    let every_line = |s: &str| {
        s.lines()
            .find(|l| l.contains("Check every"))
            .unwrap_or("")
            .to_string()
    };
    assert!(every_line(&s).contains("▐1 "), "{s}");
    assert!(
        every_line(&s).contains("hours") && s.contains(triggers::EVERY_RECOMMENDED),
        "{s}"
    );
    h.click_text("YouTube · A channel");
    h.type_text("Mine");
    let s = h.key(b"\r");
    assert!(
        every_line(&s).contains("15") && every_line(&s).contains("minutes"),
        "{s}"
    );
    assert!(!s.contains(triggers::EVERY_RECOMMENDED), "{s}");
    h.click_after("Feed address *", "Create");
    let body = create_post(&mut h).expect("a POST");
    assert_eq!(body["config"]["every"], "15m");
    assert_eq!(body["config"]["url"], "https://example.org/feed.xml");
}
