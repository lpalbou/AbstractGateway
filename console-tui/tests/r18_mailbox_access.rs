//! Another mailbox's access (2026-10-25): "Read only" (default) or "Read and write",
//! the web's words in the terminal. My email → Mailboxes lists your other mailboxes
//! (one line each, an Active and a Read and write switch: `PUT …/{id}/enabled`,
//! `PUT …/{id}/access {write}`), and "Add mailbox" connects another one by password
//! (the outgoing server only when Read and write is on; the id derived from the
//! address as the web does). Triggers' mailbox rows say the same access words.

mod r2email;

use abstractgateway_console::api::{ApiError, ApiErrorKind};
use abstractgateway_console::store::email::{
    add_mailbox_body, mailbox_access, mailbox_id_from_address, MyEmail, ServerSettings,
};
use abstractgateway_console::store::json::WriteState;
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::my_email::{
    self, other_mailboxes, ADD_MAILBOX, K_ACCOUNTS, K_ACCOUNTS_WRITE, WRITE_HELP,
};
use abstractgateway_console::ui::triggers::mailboxes_from;
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use abstracttui::prelude::*;
use r2email::{harness, Harness};
use serde_json::{json, Value};

fn connected() -> Value {
    json!({
        "schema": "email_settings_v1", "configured": true, "enabled": true,
        "admin_enabled": true, "effective_enabled": true, "email_available": true,
        "address": "admin@example.org", "registered_address": "admin@example.org",
        "mailbox": {"state": "connected", "address": "admin@example.org", "provider": "imap", "reason": null},
        "policy": {"mode": "allowlist", "entries": [], "default": true},
        "limits": {"per_hour": 20, "per_day": 100, "used_last_hour": 0, "used_last_day": 0},
        "status": {}, "watcher": {"state": "idle"},
        "agent_tools": {"on": false, "enabled": false, "available": true, "active": false},
        "notifications": {"job_failed": true, "approval_needed": true},
        "oauth_providers": []
    })
}

fn accounts() -> Value {
    json!({"items": [
        {"account_id": "default", "primary": true, "configured": true, "address": "admin@example.org",
         "can_read": true, "can_send": true, "can_write": true, "access": "write", "enabled": true,
         "mailbox": {"state": "connected", "address": "admin@example.org", "provider": "imap"}, "watcher": {}},
        {"account_id": "news", "primary": false, "configured": true, "name": "news@lists.example", "address": "news@lists.example",
         "can_read": true, "can_send": false, "can_write": false, "access": "read", "enabled": true,
         "mailbox": {"state": "connected", "address": "news@lists.example", "provider": "imap"}, "watcher": {}},
        {"account_id": "work", "primary": false, "configured": true, "address": "me@work.example",
         "can_read": true, "can_send": false, "can_write": true, "access": "write", "enabled": true,
         "mailbox": {"state": "receive_only", "address": "me@work.example", "provider": "google"}, "watcher": {}},
        {"account_id": "me-shop-example", "primary": false, "configured": false}
    ]})
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
        .set(Loadable::Ready(MyEmail::from_value(&connected())));
    h.store.json.set(K_ACCOUNTS, Loadable::Ready(accounts()));
    h.turns(4);
    h
}

fn sends(h: &mut Harness) -> Vec<(String, String, Value)> {
    h.sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Send {
                method, path, body, ..
            }) => Some((method, path, body)),
            _ => None,
        })
        .collect()
}

/// Click `needle` on the first line at or below the first line holding `anchor`.
fn click_below(h: &mut Harness, anchor: &str, needle: &str, dx: usize) -> String {
    let s = h.turns(1);
    let lines: Vec<&str> = s.lines().collect();
    let a = lines
        .iter()
        .position(|l| l.contains(anchor))
        .unwrap_or_else(|| panic!("{anchor:?} not on screen:\n{s}"));
    let (y, l) = lines
        .iter()
        .enumerate()
        .skip(a)
        .find(|(_, l)| l.contains(needle))
        .unwrap_or_else(|| panic!("{needle:?} not below {anchor:?}:\n{s}"));
    let x = l[..l.find(needle).unwrap()].chars().count() + dx + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

// ------------------------------------------------------------ pure folds

#[test]
fn access_words_and_the_id_from_the_address() {
    // `access` when given; else from can_write; the primary is always read and write.
    assert_eq!(
        mailbox_access(&json!({"access": "read", "can_write": true})),
        "read"
    );
    assert_eq!(mailbox_access(&json!({"access": "write"})), "write");
    assert_eq!(mailbox_access(&json!({"can_write": true})), "write");
    assert_eq!(mailbox_access(&json!({"can_send": true})), "read");
    assert_eq!(
        mailbox_access(&json!({"primary": true, "access": "read"})),
        "write"
    );
    let rows = other_mailboxes(&accounts());
    assert_eq!(
        rows.iter().map(|m| m.line()).collect::<Vec<_>>(),
        [
            "news@lists.example · connected · Read only",
            "me@work.example · Gmail · connected, can't send · Read and write",
        ]
    );
    let mut off = rows[0].clone();
    off.enabled = false;
    assert_eq!(off.line(), "news@lists.example · not in use · Read only");
    // The web's mailboxIdFromAddress.
    let none: Vec<String> = vec![];
    assert_eq!(
        mailbox_id_from_address("Me.Work@Example.ORG", &none),
        "me-work-example-org"
    );
    assert_eq!(mailbox_id_from_address("  --a__b@@c-- ", &none), "a-b-c");
    assert_eq!(mailbox_id_from_address("@@@", &none), "mailbox");
    assert_eq!(mailbox_id_from_address("default", &none), "default-2");
    assert_eq!(
        mailbox_id_from_address("averyveryverylong.name-x@example.org", &none),
        "averyveryverylong-name-x-exa"
    );
    // 28 characters ending on a "-": the trailing "-" goes.
    assert_eq!(
        mailbox_id_from_address("abcdefghijklmnopqrstuvwxyz0@x.org", &none),
        "abcdefghijklmnopqrstuvwxyz0"
    );
    let taken = vec![
        "me-shop-example".to_string(),
        "me-shop-example-2".to_string(),
    ];
    assert_eq!(
        mailbox_id_from_address("me@shop.example", &taken),
        "me-shop-example-3"
    );
    // The add body: the outgoing server only when Read and write.
    let sv = ServerSettings {
        imap_host: "imap.x.org".into(),
        imap_port: "993".into(),
        imap_security: "ssl".into(),
        smtp_host: "smtp.x.org".into(),
        smtp_port: "not a port".into(),
        smtp_security: "ssl".into(),
    };
    let b = add_mailbox_body("me@x.org", "pw", &sv, None, false).unwrap();
    assert_eq!(b["write"], json!(false));
    assert_eq!(b["name"], json!("me@x.org"));
    assert_eq!(b["test"], json!(true));
    assert_eq!(b["imap"]["host"], json!("imap.x.org"));
    assert!(b.get("smtp").is_none(), "{b}");
    assert!(
        add_mailbox_body("me@x.org", "pw", &sv, None, true).is_err(),
        "the shown SMTP port is checked"
    );
    let sv = ServerSettings {
        smtp_port: "465".into(),
        ..sv
    };
    let b = add_mailbox_body("me@x.org", "pw", &sv, Some("me"), true).unwrap();
    assert_eq!(b["write"], json!(true));
    assert_eq!(b["smtp"]["host"], json!("smtp.x.org"));
    assert_eq!(b["username"], json!("me"));
    assert!(add_mailbox_body("me", "pw", &sv, None, true).is_err());
}

#[test]
fn triggers_mailbox_rows_say_the_access() {
    let rows = mailboxes_from(&accounts()).unwrap();
    let phrase = |id: &str| rows.iter().find(|m| m.id == id).unwrap().phrase();
    assert_eq!(
        phrase("default"),
        "When mail arrives in admin@example.org · Read and write"
    );
    assert_eq!(
        phrase("news"),
        "When mail arrives in news@lists.example · Read only"
    );
    assert_eq!(
        phrase("work"),
        "When mail arrives in me@work.example · Read and write"
    );
    let work = rows.iter().find(|m| m.id == "work").unwrap();
    assert_eq!(work.status().0, "Can't send");
}

// ------------------------------------------------------------ the page

#[test]
fn my_email_lists_other_mailboxes_with_active_and_read_and_write() {
    let mut h = page();
    let s = h.turns(2);
    for w in [
        "Read and write  Sends as you",
        "news@lists.example · connected · Read only",
        "me@work.example · Gmail · connected, can't send · Read and write",
        ADD_MAILBOX,
    ] {
        assert!(s.contains(w), "{w:?} missing:\n{s}");
    }
    h.shoot("my-email-tui-other-mailboxes");
    h.sent();
    // news: Read and write is off → turning it on sends PUT …/access {write: true}.
    click_below(
        &mut h,
        "news@lists.example · connected",
        "Read and write",
        0,
    );
    let cmds = sends(&mut h);
    assert!(
        cmds.iter().any(|(m, p, b)| m == "PUT"
            && p == "/me/email/accounts/news/access"
            && *b == json!({"write": true})),
        "{cmds:?}"
    );
    // The gateway refuses: its cause is the page's status line.
    let mut e = ApiError::new(ApiErrorKind::Http(400), "refused");
    e.body = Some(
        json!({"cause": "The outgoing server refused the sign-in.", "fix": "Check the password."}),
    );
    h.store
        .json
        .set_write(K_ACCOUNTS_WRITE, Some(WriteState::Failed(e)));
    let s = h.turns(3);
    assert!(
        s.contains("✗ The outgoing server refused the sign-in. Fix: Check the password."),
        "{s}"
    );
    // work: Active is on → off sends PUT …/enabled {enabled: false}.
    click_below(&mut h, "me@work.example · Gmail", "Active", 0);
    let cmds = sends(&mut h);
    assert!(
        cmds.iter().any(|(m, p, b)| m == "PUT"
            && p == "/me/email/accounts/work/enabled"
            && *b == json!({"enabled": false})),
        "{cmds:?}"
    );
    h.store.json.set_write(
        K_ACCOUNTS_WRITE,
        Some(WriteState::Done(json!({"ok": true}))),
    );
    let s = h.turns(3);
    assert!(s.contains("✓ me@work.example: Active off."), "{s}");
}

#[test]
fn add_mailbox_form_shows_smtp_only_when_read_and_write_and_sends_it() {
    let mut h = page();
    h.click_text(ADD_MAILBOX);
    let s = h.turns(2);
    assert!(s.contains(WRITE_HELP.split(" On:").next().unwrap()), "{s}");
    // Email address, then Password (the input sits right of its 18-cell label).
    click_below(&mut h, ADD_MAILBOX, "Email address", 20);
    h.key(b"me@shop.example");
    click_below(&mut h, ADD_MAILBOX, "Password", 20);
    h.key(b"secret");
    click_below(&mut h, ADD_MAILBOX, "Server settings", 0);
    let s = h.turns(2);
    assert!(s.contains("Incoming server (IMAP)"), "{s}");
    assert!(
        !s.contains("Outgoing server (SMTP)"),
        "Read only: no outgoing server:\n{s}"
    );
    // Read and write on: the outgoing server appears.
    click_below(&mut h, "Password", "Read and write", 0);
    let s = h.turns(2);
    assert!(s.contains("Outgoing server (SMTP)"), "{s}");
    h.shoot("my-email-tui-add-mailbox");
    h.sent();
    click_below(&mut h, "Sign-in name", ADD_MAILBOX, 0);
    let cmds = sends(&mut h);
    let (_, path, body) = cmds
        .iter()
        .find(|(m, p, _)| m == "PUT" && p.starts_with("/me/email/accounts/"))
        .unwrap_or_else(|| panic!("no PUT: {cmds:?}"));
    // "me-shop-example" is taken (a not configured one): -2.
    assert_eq!(path, "/me/email/accounts/me-shop-example-2");
    assert_eq!(body["address"], json!("me@shop.example"));
    assert_eq!(body["password"], json!("secret"));
    assert_eq!(body["write"], json!(true));
    assert_eq!(body["name"], json!("me@shop.example"));
    assert_eq!(body["test"], json!(true));
    assert!(body["imap"]["host"].is_string(), "{body}");
    assert!(body["smtp"]["host"].is_string(), "{body}");
    // Landed: the form closes and the status says it.
    h.store.json.set_write(
        K_ACCOUNTS_WRITE,
        Some(WriteState::Done(json!({"ok": true}))),
    );
    let s = h.turns(3);
    assert!(
        s.contains("✓ Mailbox added: me@shop.example · Read and write."),
        "{s}"
    );
    assert!(!s.contains("Sign-in name"), "the form closed:\n{s}");
}

#[test]
fn add_mailbox_read_only_sends_no_outgoing_server() {
    let mut h = page();
    h.click_text(ADD_MAILBOX);
    click_below(&mut h, ADD_MAILBOX, "Email address", 20);
    h.key(b"me@other.example");
    click_below(&mut h, ADD_MAILBOX, "Password", 20);
    h.key(b"pw");
    h.sent();
    click_below(&mut h, "Server settings", ADD_MAILBOX, 0);
    let cmds = sends(&mut h);
    let (_, path, body) = cmds
        .iter()
        .find(|(m, p, _)| m == "PUT" && p.starts_with("/me/email/accounts/"))
        .unwrap_or_else(|| panic!("no PUT: {cmds:?}"));
    assert_eq!(path, "/me/email/accounts/me-other-example");
    assert_eq!(body["write"], json!(false));
    assert!(body.get("smtp").is_none(), "{body}");
}
