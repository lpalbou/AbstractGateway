//! The terminal Triggers screen in the WHOLE console (rail, header, status bar): reached with
//! its screen key `V` from another screen, exactly like the web's sidebar entry (WORK, after
//! Workflows). Always asserts the navigation; writes text + SVG captures when
//! `ROUND2_SHOTS_DIR` is set: `ROUND2_SHOTS_DIR=<dir> cargo test --test r17_triggers_shot`.

mod r2shots;

use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui;
use abstractgateway_console::ui::triggers::{K_AUTOS, K_LIST, K_MAILBOXES, K_SOURCES};
use abstracttui::prelude::*;
use r2shots::harness;
use serde_json::json;

#[test]
fn triggers_is_reached_with_v_after_workflows_and_shows_the_web_rows() {
    let mut h = harness(Size::new(150, 42));
    h.admin();
    h.ui.screen.set(ui::SCREEN_USERS);
    h.turns(2);
    // The rail lists it in WORK right after Workflows, with its key.
    let s = h.turns(2);
    let lines: Vec<&str> = s.lines().collect();
    let row = |name: &str| {
        lines
            .iter()
            .position(|l| l.contains(&format!(" {name} ")))
            .unwrap_or_else(|| panic!("{name} not in the rail:\n{s}"))
    };
    let (wf, tr, sk) = (row("Workflows"), row("Triggers"), row("Skills & MCP"));
    assert!(
        wf + 1 == tr && tr + 1 == sk,
        "WORK order: Workflows, Triggers, Skills & MCP:\n{s}"
    );
    assert!(
        lines[tr]
            .split('│')
            .next()
            .unwrap_or("")
            .trim_end()
            .ends_with('V'),
        "the rail shows its key V:\n{s}"
    );
    h.store.json.set(K_SOURCES, Loadable::Ready(json!({"items": [
        {"id": "feed.item", "version": 1, "label": "When a feed has a new item", "available": true, "needs_connection": true,
         "connection_provider": "trigger_connections", "connection_schema": {"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}}}},
        {"id": "webhook.received", "version": 1, "label": "When a webhook is called", "available": true, "needs_connection": true,
         "connection_provider": "trigger_connections", "connection_schema": {"type": "object", "properties": {}}},
        {"id": "email.received", "version": 1, "label": "When an email arrives", "available": true, "needs_connection": true,
         "connection_provider": "email_accounts"}
    ]})));
    h.store.json.set(K_LIST, Loadable::Ready(json!({"connections": [
        {"connection_id": "conn_bbc", "source_id": "feed.item", "source_version": 1, "label": "bbc-feed", "status": "active",
         "created_at": "2026-10-25T10:00:00Z", "last_event_at": null, "config_public": {}, "paused": false, "always_listen": false,
         "listening": "listening", "used_by": 1},
        {"connection_id": "conn_gh", "source_id": "webhook.received", "source_version": 1, "label": "My GitHub hook", "status": "active",
         "created_at": "2026-10-25T10:00:00Z", "last_event_at": null, "config_public": {}, "paused": false, "always_listen": false,
         "listening": "idle", "used_by": 0,
         "ingress_url": "http://127.0.0.1:8080/api/gateway/triggers/webhooks/conn_gh"},
        {"connection_id": "conn_old", "source_id": "feed.item", "source_version": 1, "label": "Broken feed", "status": "active",
         "created_at": "2026-10-25T10:00:00Z", "last_event_at": null, "config_public": {}, "paused": true, "always_listen": false,
         "listening": "paused", "used_by": 0,
         "last_error": {"code": "feed_http_404", "cause": "The feed answered HTTP 404.", "fix": "Check the feed address."}}
    ]})));
    h.store.json.set(K_MAILBOXES, Loadable::Ready(json!({"items": [
        {"account_id": "default", "primary": true, "configured": true, "address": "admin@example.org", "can_send": true,
         "enabled": true, "mailbox": {"state": "connected"}, "watcher": {}}
    ]})));
    h.store.json.set(K_AUTOS, Loadable::Ready(json!({"items": [
        {"title": "Release digest", "status": "active", "trigger": {"source_id": "feed.item", "config": {"connection_id": "conn_bbc"}}}
    ]})));
    // The screen key, from Accounts.
    let s = h.key(b"V");
    assert_eq!(
        h.ui.screen.get_untracked(),
        ui::SCREEN_TRIGGERS,
        "V opens Triggers:\n{s}"
    );
    let s = h.turns(3);
    for w in [
        "Triggers",
        "A connection is something of yours the gateway listens to",
        "New connection",
        "bbc-feed",
        "When bbc-feed has a new item",
        "Listening · used by Release",
        "My GitHub hook",
        "Waiting for an automation to",
        "Broken feed",
        "Paused",
        "Can't read the feed: the address answered 404",
        "admin@example.org (primary)",
        "When mail arrives in admin@example.org",
        "Manage in My email",
        "Events",
        "Test",
        "Delete",
        "A connection only fetches while an automation uses it",
    ] {
        assert!(s.contains(w), "{w:?} missing:\n{s}");
    }
    h.shoot("triggers");
    // New connection offers the same sources as the web (feed, webhook; never the mailbox source).
    h.key(b"n");
    let s = h.turns(3);
    assert!(
        s.contains("What to listen to") && s.contains("When a feed has a new item"),
        "{s}"
    );
    h.shoot("triggers-new-connection");
}
