//! Round-2 buffer snapshots of the account email page (§3).
//! `ROUND2_SHOTS_DIR=<dir> cargo test --test round2_shots_email -- --ignored`.

mod r2email;

use abstractgateway_console::store::email::{Discovery, EmailCaps, MyEmail, MyNotifications};
use abstractgateway_console::store::{users_from_payload, Loadable};
use abstractgateway_console::ui;
use abstracttui::prelude::*;
use r2email::{harness, Harness, SIZES};
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
        "policy": {"mode": "allowlist", "entries": ["admin@example.test", "example.org"],
                   "always_allow": ["admin@example.test", "example.org"], "always_deny": ["xxx.gov"], "default": false},
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
    h.store
        .op
        .email_caps
        .set(Loadable::Ready(EmailCaps::from_value(
            &json!({"capabilities": [
        {"id": "email", "default": true}, {"id": "email_agent_tools", "default": true},
        {"id": "email_recovery", "default": true}]}),
        )));
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

// (The "before" captures were taken from c4c2fe6 with the Google/Microsoft/
// Other tabs; this file now captures the DESIGN-v2 §3 page.)
#[test]
#[ignore = "writes screen captures; run with ROUND2_SHOTS_DIR set"]
fn capture_email_after() {
    use abstractgateway_console::ui::my_email;
    for size in SIZES {
        // Not connected: IMAP (default), servers pre-filled (standard).
        let mut h = email_page(size, &my_email_not_connected());
        h.shoot("email-not-connected");
        // Discovery's defaults replace them; the source sentence.
        seed_discovery(&mut h);
        h.shoot("email-imap-discovered");
        // Ctrl+O: the one Login field.
        h.key(b"\x0f");
        h.shoot("email-imap-different-login");
        // Connected: Active in the Mailbox card, the different-account line.
        let mut h = email_page(size, &my_email_connected());
        h.shoot("email-connected");
        // Send a test: the gateway's sentence (rate limited).
        for _ in 0..12 {
            if h.turns(1).contains("Send a test") {
                break;
            }
            h.wheel_down(2);
        }
        h.click_text("Send a test");
        let fid = h
            .sent()
            .into_iter()
            .find_map(|c| match c {
                abstractgateway_console::worker::Cmd::Operator(
                    abstractgateway_console::worker::operator::OpCmd::Email { form_id, .. },
                ) => form_id,
                _ => None,
            })
            .expect("test sent");
        h.ui.write_done.set(Some((
            fid,
            Err(
                "Not sent: hourly limit reached (20 of 20 this hour) \u{2014} resets at 14:05."
                    .into(),
            ),
        )));
        h.turns(3);
        h.shoot("email-test-rate-limited");
        h.wheel_down(20);
        h.click_text("Advanced ▸");
        h.wheel_down(20);
        h.shoot("email-connected-advanced");
        // Another user's email (Accounts, admin): address only.
        let mut h = harness(Size::new(size.0, size.1));
        h.admin();
        h.ui.screen.set(ui::SCREEN_USERS);
        h.turns(2);
        let (cx, ctx) = h.root.borrow().clone().expect("root");
        my_email::open_other(
            cx,
            &ctx,
            "alice".into(),
            "default".into(),
            Some("alice@example.test".into()),
            String::new(),
        );
        h.shoot("email-other-user");
    }
}
