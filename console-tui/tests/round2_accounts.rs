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
use accounts_fixture::{entity_row, user_row, OWN_ARCHIVE_REASON};
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
        "People who use this gateway and the entities that act on it",
        "Name",
        "Email",
        "Runtime",
        "Active",
        "admin@example.test · connected",
        "not connected",
        "castor",
        "Entity",
        "━●",
        "@ ≣ ◫ ⊜ ⬖ ⊟",
    ] {
        assert!(s.contains(want), "{want:?}:\n{s}");
    }
    // The old two-table layout is gone.
    assert!(!s.contains("Users (admin)"), "{s}");
    assert!(!s.contains("Entities (n = summon"), "{s}");
}

#[test]
fn d_archives_an_entity_after_the_confirm_never_deletes() {
    let mut h = accounts((120, 40));
    select(&mut h, "castor");
    let s = h.turns(2);
    assert!(!s.contains("Delete"), "no Delete anywhere on the row:\n{s}");
    // R15: the entity row's Actions cell ends with its Archive button (⊟).
    let row = s.lines().find(|l| l.contains(" castor ")).expect(&s);
    assert!(row.trim_end().ends_with("⬖ ⊟"), "{s}");
    let s = h.key(b"d");
    // The confirm wraps; the sentence itself is DESIGN-v3 §1.3's, word for word.
    let flat = s.split_whitespace().collect::<Vec<_>>().join(" ");
    assert!(
        flat.contains("Archive castor? It stops acting and never wakes. Its memory, runs and history are kept; you can unarchive later."),
        "{s}"
    );
    assert!(
        s.contains("Archive") && s.contains("Cancel"),
        "confirm:\n{s}"
    );
    assert!(h.sent().is_empty(), "nothing sent before the confirm");
    h.key(b"\x1b[A");
    h.key(b"\r");
    let sent = h.sent();
    assert!(
        sent.iter().any(|c| matches!(c,
            Cmd::ArchiveAccount { id, unarchive: false, admin: true, .. } if id == "castor")),
        "{sent:?}"
    );
}

#[test]
fn d_on_your_own_row_says_why() {
    let mut h = accounts((120, 40));
    select(&mut h, "admin");
    h.key(b"d");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some(OWN_ARCHIVE_REASON)
    );
    assert!(h.sent().is_empty());
}

#[test]
fn space_suspends_an_entity_after_the_confirm() {
    let mut h = accounts((120, 40));
    select(&mut h, "castor");
    let s = h.key(b" ");
    let flat = s.split_whitespace().collect::<Vec<_>>().join(" ");
    assert!(
        flat.contains("Suspend castor? It stops acting until you turn") && s.contains("Suspend"),
        "{s}"
    );
    assert!(h.sent().is_empty(), "nothing sent before the confirm");
    h.key(b"\x1b[A");
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
    // The web sentence (wrapped inside the modal).
    assert!(
        s.contains("Mailbox: not connected — only alice can connect a mailbox.")
            && s.contains("anyone's mail."),
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
    // The time in the viewer's LOCAL zone (14:05 UTC is 16:05 in Paris).
    let when = abstractgateway_console::store::accounts::activity_time(
        "2026-09-30T14:05:00+00:00",
        &abstractgateway_console::localtime::local_today(),
    );
    assert!(s.contains(&when), "{when:?}:\n{s}");
    for want in [
        "Activity — alice",
        "All",
        "Sign-ins",
        "Run started",
        "From the gateway's audit log",
        "Open in Observer",
    ] {
        assert!(s.contains(want), "{want:?}:\n{s}");
    }
    // R15: the filter chips are a Segmented — a click on "Sign-ins".
    h.click_text("Sign-ins");
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
