//! Round-2 buffer snapshots of the account email page (§3).
//! `ROUND2_SHOTS_DIR=<dir> cargo test --test round2_shots_email -- --ignored`.

mod r2shots;

use abstractgateway_console::store::email::{Discovery, EmailCaps, MyEmail, MyNotifications};
use abstractgateway_console::store::{users_from_payload, Loadable};
use abstractgateway_console::ui;
use abstracttui::prelude::*;
use r2shots::{harness, Harness, SIZES};
use serde_json::{json, Value};

pub fn users_payload() -> Value {
    json!({"users": [
        {"user_id": "admin", "tenant_id": "default", "email": "admin@example.test", "roles": ["admin", "user"],
         "enabled": true, "runtime_id": "default",
         "email_account": {"configured": true, "address": "admin@example.test", "state": "connected",
                           "admin_enabled": true, "agent_tools_available": true}},
        {"user_id": "alice", "tenant_id": "default", "email": "alice@example.test", "roles": ["user"],
         "enabled": true, "runtime_id": "alice",
         "email_account": {"configured": false, "address": "", "state": "not connected",
                           "admin_enabled": true, "agent_tools_available": false}}
    ]})
}

pub fn my_email_connected() -> Value {
    json!({
        "schema": "email_settings_v1", "configured": true, "enabled": true,
        "admin_enabled": true, "effective_enabled": true, "email_available": true,
        "address": "test@fastmail.test", "username": "test@fastmail.test", "auth_kind": "password",
        "registered_address": "admin@example.test", "email_address": "admin@example.test",
        "imap": {"host": "imap.fastmail.com", "port": 993, "security": "ssl", "folder": "INBOX"},
        "smtp": {"host": "smtp.fastmail.com", "port": 465, "security": "ssl"},
        "policy": {"mode": "allowlist", "entries": ["admin@example.test", "example.org"], "default": false},
        "limits": {"per_hour": 20, "per_day": 100, "used_last_hour": 3, "used_last_day": 12},
        "status": {"last_test": "2026-09-30T18:00:00+00:00", "last_ok": "2026-09-30T18:00:00+00:00",
                   "legs": {"imap": {"ok": true}, "smtp": {"ok": true}}},
        "watcher": {"state": "watching", "last_poll": "2026-09-30T18:01:00+00:00"},
        "agent_tools": {"on": false, "enabled": false, "available": true, "active": false,
                        "unavailable_reason": null},
        "notifications": {"job_failed": true, "approval_needed": true},
        "oauth_providers": [{"id": "google", "available": true, "reason": null},
                            {"id": "microsoft", "available": true, "reason": null}]
    })
}

pub fn my_email_not_connected() -> Value {
    json!({
        "schema": "email_settings_v1", "configured": false, "enabled": true,
        "admin_enabled": true, "effective_enabled": false, "email_available": true,
        "address": "", "registered_address": "alice@fastmail.test", "email_address": "alice@fastmail.test",
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

pub fn discovery_reply() -> Value {
    json!({
        "address": "alice@fastmail.test", "domain": "fastmail.test", "found": true, "source": "known",
        "imap": {"host": "imap.fastmail.com", "port": 993, "security": "ssl"},
        "smtp": {"host": "smtp.fastmail.com", "port": 587, "security": "starttls"},
        "username": "alice@fastmail.test",
        "defaults": {
            "imap": {"host": "imap.fastmail.com", "port": 993, "security": "ssl"},
            "smtp": {"host": "smtp.fastmail.com", "port": 587, "security": "starttls"},
            "login": "alice@fastmail.test", "source": "discovered", "provider": "Fastmail",
            "message": "Settings found for fastmail.test."
        }
    })
}

pub fn email_page(size: (i32, i32), v: &Value) -> Harness {
    let mut h = harness(Size::new(size.0, size.1));
    h.admin();
    h.ui.screen.set(ui::SCREEN_USERS);
    h.turns(2);
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&users_payload())));
    h.store.op.email_caps.set(Loadable::Ready(EmailCaps::from_value(&json!({"capabilities": [
        {"id": "email", "default": true}, {"id": "email_agent_tools", "default": true},
        {"id": "email_recovery", "default": true}]}))));
    h.turns(3);
    h.key(b"@");
    h.store
        .op
        .my_email
        .set(Loadable::Ready(MyEmail::from_value(v)));
    h.store
        .op
        .my_notifications
        .set(Loadable::Ready(MyNotifications::from_value(
            &json!({"events": [], "channels": {"email": {"available": true}}, "outbox": {}}),
        )));
    h.turns(4);
    h
}

pub fn seed_discovery(h: &mut Harness) {
    h.store.op.email_discovery.set(Some((
        "alice@fastmail.test".into(),
        Loadable::Ready(Discovery::from_value(&discovery_reply())),
    )));
    h.turns(3);
}

#[test]
#[ignore = "writes screen captures; run with ROUND2_SHOTS_DIR set"]
fn capture_email_before() {
    for size in SIZES {
        let mut h = email_page(size, &my_email_not_connected());
        h.shoot("email-not-connected");
        h.click_text("Other");
        seed_discovery(&mut h);
        h.shoot("email-imap-discovered");
        h.click_text("Edit");
        h.shoot("email-imap-server-settings");
        let mut h = email_page(size, &my_email_connected());
        h.shoot("email-connected");
        h.wheel_down(20);
        h.click_text("Advanced ▸");
        h.wheel_down(20);
        h.shoot("email-connected-advanced");
    }
}
