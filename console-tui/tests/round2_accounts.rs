//! DESIGN-v2 §2 — the Accounts screen: ONE table of users and entities
//! from `GET /admin/accounts`, the Active switch for both kinds, the
//! per-row actions with their reasons, and the Logs view. Seeds
//! `store.accounts` directly (no derivation from the users registry).

mod accounts_fixture;
mod r2shots;

use abstractgateway_console::store::accounts::{
    accounts_from_payload, activity_from_payload, AccountRow,
};
use abstractgateway_console::store::{entities_from_payload, users_from_payload, Loadable};
use abstractgateway_console::ui;
use abstractgateway_console::worker::Cmd;
use abstracttui::prelude::*;
use accounts_fixture::{entity_row, user_row, ENTITY_DELETE_REASON, ENTITY_EMAIL_REASON};
use r2shots::{harness, Harness, SIZES};
use serde_json::json;

fn rows() -> Vec<AccountRow> {
    accounts_from_payload(&json!({"accounts": [
        user_row("admin", true, "admin@example.test", "connected as admin@example.test", true, true),
        user_row("alice", false, "alice@example.test", "", true, false),
        entity_row("castor", "awake", true),
    ]}))
    .unwrap()
}

fn accounts(size: (i32, i32)) -> Harness {
    let mut h = harness(Size::new(size.0, size.1));
    h.admin();
    h.ui.screen.set(ui::SCREEN_USERS);
    h.turns(2);
    h.store.accounts.set(Loadable::Ready(rows()));
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&json!({"users": [
            {"user_id": "admin", "tenant_id": "default", "email": "admin@example.test",
             "roles": ["admin", "user"], "enabled": true, "runtime_id": "default"},
            {"user_id": "alice", "tenant_id": "default", "email": "alice@example.test",
             "roles": ["user"], "enabled": true, "runtime_id": "alice"}
        ]}))));
    h.store.entities.set(Loadable::Ready(entities_from_payload(
        &json!({"entities": [
            {"name": "Castor", "slug": "castor", "handle": "castor@gw",
             "state": {"state": "awake"}}
        ]}),
    )));
    h.turns(3);
    h.sent();
    h
}

fn select(h: &mut Harness, id: &str) {
    let idx = rows().iter().position(|r| r.id == id).unwrap();
    h.ui.account_sel.set(idx);
    h.turns(3);
}

#[test]
fn one_table_lists_users_and_entities_with_kind_and_active() {
    let mut h = accounts((120, 40));
    let s = h.turns(2);
    for want in [
        "Accounts — people who use this gateway",
        "name",
        "kind",
        "email address",
        "mailbox",
        "runtime",
        "active",
        "Connected as admin@example.test",
        "Not connected",
        "castor",
        "Entity",
        "[-] You can't deactivate your own account.",
        "kind:",
    ] {
        assert!(s.contains(want), "{want:?}:\n{s}");
    }
    // The old two-table layout is gone.
    assert!(!s.contains("Users (admin)"), "{s}");
    assert!(!s.contains("Entities (n = summon"), "{s}");
}

#[test]
fn an_entity_row_shows_its_unavailable_actions_with_reasons() {
    let mut h = accounts((120, 40));
    select(&mut h, "castor");
    let s = h.turns(2);
    assert!(s.contains("Unavailable —"), "{s}");
    assert!(
        s.contains("Delete: An entity's name is kept for life"),
        "{s}"
    );
    // d says why and sends nothing.
    h.key(b"d");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some(ENTITY_DELETE_REASON)
    );
    assert!(h.sent().is_empty());
    // @ on an entity: the reason, no mailbox form.
    h.key(b"@");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some(ENTITY_EMAIL_REASON)
    );
}

#[test]
fn space_suspends_an_entity_after_the_confirm() {
    let mut h = accounts((120, 40));
    select(&mut h, "castor");
    let s = h.key(b" ");
    assert!(
        s.contains("Suspend castor? It stops acting until you turn") && s.contains("○ Suspend"),
        "{s}"
    );
    assert!(h.sent().is_empty(), "nothing sent before the confirm");
    h.key(b"\x1b[A"); // up to "Suspend" (Cancel is the default)
    h.key(b"\r");
    let sent = h.sent();
    assert!(
        sent.iter().any(|c| matches!(c,
            Cmd::SetAccountActive { id, entity: true, active: false, .. } if id == "castor")),
        "{sent:?}"
    );
}

#[test]
fn space_on_your_own_row_says_why() {
    let mut h = accounts((120, 40));
    select(&mut h, "admin");
    h.key(b" ");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("You can't deactivate your own account.")
    );
    assert!(h.sent().is_empty());
}

#[test]
fn email_on_another_user_opens_the_address_only_view() {
    let mut h = accounts((120, 40));
    select(&mut h, "alice");
    let s = h.key(b"@");
    assert!(
        s.contains("Where alice's sign-in codes and notifications go."),
        "{s}"
    );
    assert!(
        s.contains("only alice can connect a mailbox. You never see anyone's mail."),
        "{s}"
    );
}

#[test]
fn logs_read_the_accounts_activity_and_filter() {
    let mut h = accounts((120, 40));
    select(&mut h, "alice");
    h.key(b"l");
    let sent = h.sent();
    assert!(
        sent.iter().any(|c| matches!(c,
            Cmd::LoadActivity { target: Some((id, _)), kind, .. } if id == "alice" && kind.is_empty())),
        "{sent:?}"
    );
    let data = activity_from_payload(&json!({"events": [
        {"ts": "2026-09-30T14:05:00+00:00", "kind": "run", "title": "Run started",
         "detail": "basic-agent", "run_id": "run-42", "observer_path": "/runs/run-42", "ok": true},
        {"ts": "2026-09-30T13:00:00+00:00", "kind": "sign_in", "title": "Signed in",
         "detail": null, "run_id": null, "observer_path": null, "ok": true}
    ], "source": "audit_log", "oldest_ts": "2026-09-01T00:00:00Z", "truncated": false,
       "note": "From the gateway's audit log (last 30 days available)."}))
    .unwrap();
    h.store.activity.set(Some((
        "default/alice".into(),
        String::new(),
        Loadable::Ready(data),
    )));
    let s = h.turns(3);
    for want in [
        "Activity — alice",
        "[All]",
        "Sep 30 14:05",
        "Run started",
        "run-42 · Observer /runs/run-42",
        "From the gateway's audit log",
        "Read-only requests",
    ] {
        assert!(s.contains(want), "{want:?}:\n{s}");
    }
    h.key(b"f");
    let sent = h.sent();
    assert!(
        sent.iter().any(|c| matches!(c,
            Cmd::LoadActivity { kind, .. } if kind == "sign_in")),
        "{sent:?}"
    );
}

#[test]
#[ignore = "writes screen captures; run with ROUND2_SHOTS_DIR set"]
fn capture_accounts_after() {
    for size in SIZES {
        let mut h = accounts(size);
        h.shoot("accounts");
        select(&mut h, "castor");
        h.shoot("accounts-entity-selected");
        h.key(b" ");
        h.shoot("accounts-suspend-confirm");
        let mut h = accounts(size);
        select(&mut h, "alice");
        h.key(b"@");
        h.shoot("accounts-other-user-email");
        let mut h = accounts(size);
        select(&mut h, "alice");
        h.key(b"l");
        h.store.activity.set(Some((
            "default/alice".into(),
            String::new(),
            Loadable::Ready(
                activity_from_payload(&json!({"events": [
                    {"ts": "2026-09-30T14:05:00+00:00", "kind": "run", "title": "Run started",
                     "detail": "basic-agent", "run_id": "run-42", "observer_path": "/runs/run-42", "ok": true},
                    {"ts": "2026-09-30T13:00:00+00:00", "kind": "email", "title": "Notification failed",
                     "detail": "Not sent: hourly limit reached (20 of 20 this hour)", "ok": false}
                ], "note": "From the gateway's audit log (last 30 days available)."}))
                .unwrap(),
            ),
        )));
        h.shoot("accounts-activity");
    }
}
