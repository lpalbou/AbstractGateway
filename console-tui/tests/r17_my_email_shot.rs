//! The terminal My email page in the WHOLE console, captured for review (the web's My email
//! rework, 2026-10-25): the web's tabs are the page's sections (Mailboxes, Notifications,
//! Agents) and the not-connected mailbox form reads Email address → sign-in way → Password →
//! Server settings → Connect. Always asserts the order; writes text + SVG captures when
//! `ROUND2_SHOTS_DIR` is set: `ROUND2_SHOTS_DIR=<dir> cargo test --test r17_my_email_shot`.

mod r2shots;

use abstractgateway_console::store::accounts::accounts_from_payload;
use abstractgateway_console::store::email::{Discovery, MyEmail};
use abstractgateway_console::store::{users_from_payload, Loadable};
use abstractgateway_console::ui;
use abstracttui::prelude::*;
use r2shots::{harness, Harness};
use serde_json::{json, Value};

fn not_connected(address: &str) -> Value {
    json!({
        "schema": "email_settings_v1", "configured": false, "enabled": true,
        "admin_enabled": true, "effective_enabled": false, "email_available": true,
        "address": "", "registered_address": address,
        "policy": {"mode": "allowlist", "entries": [], "default": true},
        "limits": {"per_hour": 20, "per_day": 100, "used_last_hour": 0, "used_last_day": 0},
        "status": {}, "watcher": {"state": "idle"},
        "agent_tools": {"on": false, "enabled": false, "available": false,
                        "unavailable_reason": "Connect a mailbox first.", "active": false},
        "notifications": {"job_failed": true, "approval_needed": true},
        "notifications_unavailable_reason": "Connect a mailbox first.",
        "oauth_providers": [{"id": "google", "available": true, "reason": null},
                            {"id": "microsoft", "available": false, "reason": "No Microsoft sign-in client on this gateway."}]
    })
}

fn page(v: &Value) -> Harness {
    let mut h = harness(Size::new(150, 46));
    h.admin();
    h.ui.screen.set(ui::SCREEN_USERS);
    h.turns(2);
    h.store.users.set(Loadable::Ready(users_from_payload(&json!({"users": [
        {"user_id": "admin", "tenant_id": "default", "email": "", "roles": ["admin", "user"], "scopes": [],
         "enabled": true, "runtime_id": "default", "created_at": "2026-05-30T05:17:35Z", "updated_at": "2026-07-15T16:38:57Z"}
    ]}))));
    let ok = json!({"available": true, "reason": null});
    h.store.accounts.set(Loadable::Ready(
        accounts_from_payload(&json!({"scope": "all", "accounts": [
            {"id": "admin", "tenant_id": "default", "kind": "user", "role": "admin", "own": true,
             "email_address": null, "mailbox": {"state": "not_connected", "address": null, "provider": null, "reason": null},
             "runtime_id": "default", "active": true, "entity_state": null, "archived": false,
             "actions": {"email": ok, "logs": ok, "workspace": ok, "rotate": ok, "manage": ok, "archive": ok, "unarchive": ok, "suspend": ok, "openai": ok, "preferences": ok}}
        ]}))
        .unwrap(),
    ));
    h.turns(3);
    h.key(b"@");
    h.turns(2);
    h.store
        .op
        .my_email
        .set(Loadable::Ready(MyEmail::from_value(v)));
    h.turns(4);
    h
}

fn found(h: &mut Harness, address: &str, imap: &str, smtp: &str) {
    h.store.op.email_discovery.set(Some((
        address.into(),
        Loadable::Ready(Discovery::from_value(&json!({
            "address": address, "found": true,
            "defaults": {
                "imap": {"host": imap, "port": 993, "security": "ssl"},
                "smtp": {"host": smtp, "port": 465, "security": "ssl"},
                "login": address, "source": "discovered",
                "message": format!("Settings found for {}.", address.split('@').nth(1).unwrap_or(""))
            }
        }))),
    )));
}

#[test]
fn my_email_sections_and_the_mailbox_form_order() {
    let mut h = page(&not_connected("me@fastmail.com"));
    found(
        &mut h,
        "me@fastmail.com",
        "imap.fastmail.com",
        "smtp.fastmail.com",
    );
    let s = h.turns(3);
    let at = |needle: &str| {
        s.find(needle)
            .unwrap_or_else(|| panic!("{needle:?} missing:\n{s}"))
    };
    assert!(at("Mailboxes") < at("Email address"), "{s}");
    assert!(
        at("Email address") < at("Settings found for fastmail.com."),
        "{s}"
    );
    assert!(
        at("Settings found for fastmail.com.") < at("Password"),
        "{s}"
    );
    assert!(at("Password") < at("Server settings"), "{s}");
    assert!(at("Server settings") < at(" Connect "), "{s}");
    h.shoot("my-email-tui-form-fastmail");
    h.key(b"\x0f"); // Ctrl+O: Server settings
    let s = h.turns(2);
    assert!(
        s.contains("Incoming server (IMAP)") && s.contains("Sign-in name"),
        "{s}"
    );
    h.shoot("my-email-tui-server-settings");
    // A Gmail address on a gateway with a Google sign-in client: Sign in with Google.
    let mut h = page(&not_connected("me@gmail.com"));
    found(&mut h, "me@gmail.com", "imap.gmail.com", "smtp.gmail.com");
    let s = h.turns(3);
    assert!(s.contains("Sign in with Google"), "{s}");
    h.shoot("my-email-tui-form-gmail");
    // The Notifications and Agents sections, lower on the same page.
    h.wheel_down(30);
    let s = h.turns(2);
    assert!(
        s.contains("Agents") && s.contains("Recipients and limits"),
        "{s}"
    );
    h.shoot("my-email-tui-sections-below");
}
