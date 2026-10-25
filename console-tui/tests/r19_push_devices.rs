//! My email → Notifications → Devices (framework backlog 1049), the web's
//! Devices card in the terminal: the page reads `GET /me/push/devices` when it
//! opens (your own page only) and lists each watch or iPhone — name · app,
//! "Registered … · last alert …", the last error in words — with nothing to
//! configure. Never a token.

mod r2email;

use abstractgateway_console::store::email::MyEmail;
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::my_email::{
    self, push_devices, DEVICES_HELP, K_ACCOUNTS, K_DEVICES, NO_DEVICES,
};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use abstracttui::prelude::*;
use r2email::{harness, Harness};
use serde_json::{json, Value};

fn settings() -> Value {
    json!({
        "schema": "email_settings_v1", "configured": false, "enabled": true,
        "admin_enabled": true, "effective_enabled": true, "email_available": true,
        "address": "", "registered_address": "admin@example.org",
        "mailbox": {"state": "not_connected", "address": "", "provider": "", "reason": null},
        "policy": {"mode": "allowlist", "entries": [], "default": true},
        "limits": {"per_hour": 20, "per_day": 100, "used_last_hour": 0, "used_last_day": 0},
        "status": {}, "watcher": {"state": "idle"},
        "agent_tools": {"on": false, "enabled": false, "available": true, "active": false},
        "notifications": {"job_failed": true, "approval_needed": true},
        "oauth_providers": []
    })
}

fn devices() -> Value {
    json!([
        {"device_id": "aw1", "app": "watch", "name": "Wrist", "refreshed_at": "2020-01-01T00:00:00+00:00",
         "last_sent_at": "2020-01-01T00:00:00+00:00", "last_error": null},
        {"device_id": "ap1", "app": "phone", "name": null, "refreshed_at": "2020-01-01T00:00:00+00:00",
         "last_sent_at": null, "last_error": "The push relay refused an alert: BadTopic"}
    ])
}

fn page() -> Harness {
    let mut h = harness(Size::new(150, 70));
    h.admin();
    h.turns(2);
    let (cx, ctx) = h.root.borrow().clone().expect("root");
    my_email::open(cx, &ctx);
    h.turns(2);
    h.store
        .op
        .my_email
        .set(Loadable::Ready(MyEmail::from_value(&settings())));
    h.store
        .json
        .set(K_ACCOUNTS, Loadable::Ready(json!({"items": []})));
    h.turns(4);
    h
}

/// Scroll the page until `needle` is on screen.
fn scroll_to(h: &mut Harness, needle: &str) -> String {
    for _ in 0..30 {
        let s = h.turns(1);
        if s.contains(needle) {
            return s;
        }
        h.wheel_down(3);
    }
    panic!("{needle:?} never on screen:\n{}", h.turns(1));
}

#[test]
fn rows_say_app_name_registered_last_alert_and_error() {
    let rows = push_devices(&devices());
    assert_eq!(rows.len(), 2);
    assert_eq!(rows[0].title(), "Wrist · Apple Watch");
    assert_eq!(rows[1].title(), "iPhone");
    assert!(rows[0]
        .meta(1_600_000_000 + 400 * 86400)
        .starts_with("Registered "));
    assert!(rows[1].meta(0).ends_with("no alert yet"));
    assert!(push_devices(&json!({"not": "a list"})).is_empty());
}

#[test]
fn the_page_reads_and_lists_the_devices() {
    let mut h = page();
    let reads: Vec<String> = h
        .sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Get { key, path, .. }) if key == K_DEVICES => Some(path),
            _ => None,
        })
        .collect();
    assert_eq!(reads, vec!["/me/push/devices".to_string()]);
    let s = scroll_to(&mut h, "Reading your devices");
    assert!(s.contains(&DEVICES_HELP[..40]), "{s}");
    h.store.json.set(K_DEVICES, Loadable::Ready(devices()));
    let s = scroll_to(&mut h, "Wrist · Apple Watch");
    for w in [
        "Wrist · Apple Watch",
        "last alert",
        "iPhone",
        "no alert yet",
        "The push relay refused an alert: BadTopic",
    ] {
        assert!(s.contains(w), "{w:?} missing:\n{s}");
    }
    h.shoot("my-email-tui-push-devices");
    h.store.json.set(K_DEVICES, Loadable::Ready(json!([])));
    let s = scroll_to(&mut h, &NO_DEVICES[..30]);
    assert!(!s.contains("Wrist"), "{s}");
}
