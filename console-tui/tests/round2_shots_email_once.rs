//! The account email page asks the address ONCE (adversary G10): no saved
//! address → the IMAP pane's Mailbox address is the only address field; a
//! saved address → "Mailbox account: x@y — use a different account
//! (Ctrl+U)". `ROUND2_SHOTS_DIR=<dir> cargo test --test round2_shots_email_once -- --ignored`.

mod r2email;

use abstractgateway_console::store::email::{EmailCaps, MyEmail, MyNotifications};
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui;
use abstracttui::prelude::*;
use r2email::{harness, Harness, SIZES};
use serde_json::{json, Value};

fn not_connected(address: &str) -> Value {
    json!({
        "schema": "email_settings_v1", "configured": false, "enabled": true,
        "admin_enabled": true, "effective_enabled": false, "email_available": true,
        "address": "", "registered_address": address, "email_address": address,
        "policy": {"mode": "allowlist", "entries": [], "default": true},
        "limits": {"per_hour": 20, "per_day": 100, "used_last_hour": 0, "used_last_day": 0},
        "status": {}, "watcher": {"state": "idle"},
        "agent_tools": {"on": false, "enabled": false, "available": false, "active": false,
                        "unavailable_reason": "Connect a mailbox first."},
        "notifications": {"job_failed": true, "approval_needed": true},
        "notifications_unavailable_reason": "Connect a mailbox first.",
        "oauth_providers": [{"id": "google", "available": true, "reason": null},
                            {"id": "microsoft", "available": true, "reason": null}]
    })
}

fn page(size: (i32, i32), v: &Value) -> Harness {
    let mut h = harness(Size::new(size.0, size.1));
    h.admin();
    h.ui.screen.set(ui::SCREEN_USERS);
    h.turns(2);
    h.store
        .op
        .email_caps
        .set(Loadable::Ready(EmailCaps::from_value(
            &json!({"capabilities": []}),
        )));
    h.turns(2);
    h.key(b"@");
    h.store
        .op
        .my_email
        .set(Loadable::Ready(MyEmail::from_value(v)));
    h.store
        .op
        .my_notifications
        .set(Loadable::Ready(MyNotifications::from_value(
            &json!({"events": [], "channels": {"email": {"available": false}}, "outbox": {}}),
        )));
    h.turns(4);
    h
}

#[test]
#[ignore = "writes screen captures; run with ROUND2_SHOTS_DIR set"]
fn capture_address_asked_once() {
    for size in SIZES {
        let mut h = page(size, &not_connected(""));
        h.shoot("email-once-no-address");
        let mut h = page(size, &not_connected("alice@fastmail.test"));
        h.shoot("email-once-address-set");
        h.key(b"\x15"); // Ctrl+U
        h.shoot("email-once-different-account");
    }
}
