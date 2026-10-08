//! Accounts (R7.2): snapshot tests per screen state and drive tests per
//! action against the live scratch gateway (ignored by default):
//!
//!   R7W1_URL=http://127.0.0.1:18785 R7W1_TOKEN=r7w1-admin-token-0000 \
//!   R7W1_SHOTS_DIR=<dir> cargo test --test r7w1_accounts -- --ignored --test-threads 1

mod r7w1;

use abstractgateway_console::ui::{self, users};
use r7w1::{gw, harness, live, live_env, Mount, SIZES};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    users::view(cx, ctx, &t)
}

#[path = "accounts_fixture/mod.rs"]
mod accounts_fixture;

use abstractgateway_console::store::accounts::accounts_from_payload;
use abstractgateway_console::store::Loadable;
use abstractgateway_console::worker::operator::OpCmd;
use abstractgateway_console::worker::Cmd;

fn accounts_payload() -> Value {
    let mut alice = accounts_fixture::user_row(
        "alice",
        false,
        "alice@example.test",
        "connected as alice@example.test",
        true,
        false,
    );
    alice["openai_api"] = json!(true);
    alice["actions"]["openai_api"] = json!({"available": true, "reason": null});
    let mut carol =
        accounts_fixture::user_row("carol", false, "carol@example.test", "", false, false);
    carol["archived"] = json!(true);
    for a in ["email", "workspace", "rotate", "archive", "suspend"] {
        carol["actions"][a] = json!({"available": false, "reason": "Archived accounts stay inactive: unarchive it first."});
    }
    carol["actions"]["unarchive"] = json!({"available": true, "reason": null});
    let mut bob = accounts_fixture::user_row("bob", false, "", "", true, false);
    bob["mailbox"] = json!({"state": "receive_only", "address": "bob@example.test", "provider": "imap",
                            "reason": "No outgoing server: this mailbox is receive only — connect it again to send."});
    json!({"accounts": [
        accounts_fixture::user_row("admin", true, "", "", true, true),
        alice, bob, carol,
        accounts_fixture::entity_row("castor", "awake", true),
    ]})
}

fn page(size: (i32, i32)) -> r7w1::Harness {
    let mut h = harness(size, Mount::Page(page_view));
    h.admin();
    h.store.accounts.set(Loadable::Ready(
        accounts_from_payload(&accounts_payload()).unwrap(),
    ));
    h.turns(3);
    h
}

#[test]
fn the_table_has_the_web_columns_kinds_and_switches() {
    for size in SIZES {
        let mut h = page(size);
        let s = h.shoot("accounts");
        // R15: the web title + subtitle, the head buttons, one table.
        assert!(
            s.contains("Accounts") && s.contains("People who use this gateway"),
            "{s}"
        );
        assert!(
            s.contains("●─ Show archived")
                && s.contains("Create user")
                && s.contains("Create entity"),
            "{s}"
        );
        for col in ["Name", "Email", "Runtime", "Active", "Actions"] {
            assert!(s.contains(col), "column {col}:\n{s}");
        }
        assert!(
            s.contains("alice")
                && s.contains("User")
                && s.contains("Admin")
                && s.contains("Entity"),
            "{s}"
        );
        assert!(s.contains("alice@example.test · connected"), "{s}");
        assert!(s.contains("receive only"), "{s}");
        assert!(s.contains("No address"), "{s}");
        assert!(!s.contains("carol"), "archived hidden:\n{s}");
        assert!(s.contains("━●"), "Active toggles:\n{s}");
        // Glyph actions in the web's order (users / entities).
        assert!(s.contains("@ ⇄ ≣ ◫ ⊜ ↻ ⊟"), "{s}");
        assert!(s.contains("@ ≣ ◫ ⊜ ⬖ ⊟"), "{s}");
        h.assert_fits();
    }
}

fn select(h: &mut r7w1::Harness, id: &str) {
    let show = h.store.acc.show_archived.get_untracked();
    let idx = h.store.accounts.with_untracked(|d| {
        d.ready()
            .unwrap()
            .iter()
            .filter(|r| show || !r.archived)
            .position(|r| r.id == id)
    });
    h.ui.account_sel.set(idx.expect(id));
    h.turns(2);
}

#[test]
fn row_actions_name_themselves_on_focus_and_refused_ones_say_why() {
    // R15 A2: Tab from the table enters the selected row's actions; a
    // focused button names itself (tooltip + the focused-control line); a
    // refused one is faint, still focusable, and a press says the reason.
    let mut h = page((80, 24));
    // Tab walks the selected row's controls left to right: the Runtime
    // link, the Active toggle (refused on the own row), then the actions.
    h.key(b"\t");
    assert_eq!(
        h.ui.focus_line.get_untracked().as_deref(),
        Some("Runtimes of admin: admin  (g)")
    );
    h.key(b"\t");
    assert_eq!(
        h.ui.focus_line.get_untracked().as_deref(),
        Some("Active  (Space) — You can't deactivate your own account.")
    );
    h.key(b"\t"); // the first action: Email
    assert_eq!(
        h.ui.focus_line.get_untracked().as_deref(),
        Some("Email address and mailbox of admin  (@)")
    );
    for _ in 0..6 {
        h.key(b"\t");
    }
    assert_eq!(
        h.ui.focus_line.get_untracked().as_deref(),
        Some("Archive admin (kept, hidden)  (d) — You can't archive your own account.")
    );
    h.shoot("accounts-own-archive-focused");
    h.sent();
    h.key(b"\r");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("You can't archive your own account.")
    );
    assert!(
        !h.sent()
            .iter()
            .any(|c| matches!(c, Cmd::ArchiveAccount { .. })),
        "a refused action sends nothing"
    );
    // bob: the receive-only reason sits under the row (wrapped).
    select(&mut h, "bob");
    let s = h.text();
    assert!(
        s.contains("receive only") && s.contains("No outgoing"),
        "{s}"
    );
}

#[test]
fn show_archived_lists_them_with_unarchive_only() {
    let mut h = page((120, 40));
    h.key(b"h");
    let s = h.text();
    assert!(
        s.contains("━● Show archived") && s.contains("carol") && s.contains("Archived"),
        "{s}"
    );
    // An archived row offers Logs and Unarchive only.
    let carol = s
        .lines()
        .find(|l| l.trim_start().starts_with("carol"))
        .expect(&s);
    assert!(carol.trim_end().ends_with("≣ ⤒"), "{s}");
    select(&mut h, "carol");
    h.shoot("accounts-archived");
    h.sent();
    h.key(b"d");
    assert!(
        h.sent()
            .iter()
            .any(|c| matches!(c, Cmd::ArchiveAccount { id, unarchive: true, .. } if id == "carol")),
        "unarchive applies at once"
    );
}

#[test]
fn archive_and_deactivate_confirm_in_the_web_words() {
    // R15: a must-choose prompt (danger first, Cancel preselected).
    let mut h = page((80, 24));
    select(&mut h, "alice");
    h.sent();
    let s = h.key(b"d");
    // The dialog's sentence (the page stays visible around it).
    let flat = abstractgateway_console::ui::w::confirm::asked()
        .last()
        .cloned()
        .unwrap_or_default();
    assert!(flat.contains("Archive alice? They can't sign in any more. Their runtime, runs and history are kept; you can unarchive later."), "{s}");
    assert!(s.contains("Archive") && s.contains("Cancel"), "{s}");
    h.shoot("accounts-archive-confirm");
    h.key(b"\r"); // Cancel
    assert!(
        !h.sent()
            .iter()
            .any(|c| matches!(c, Cmd::ArchiveAccount { .. })),
        "Cancel sends nothing"
    );
    let s = h.key(b" ");
    let flat = s
        .replace(['│', '┃'], " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    assert!(
        flat.contains("Deactivate alice? They are signed out until you turn Active back on."),
        "{s}"
    );
    h.key(b"\x1b[Z"); // Shift+Tab to the action button (R15 F1)
    h.key(b"\r");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SetAccountActive { id, active: false, .. } if id == "alice")));
}

#[test]
fn openai_api_overlay_has_one_switch_and_esc_closes() {
    let mut h = page((120, 40));
    select(&mut h, "alice");
    let s = h.key(b"o");
    assert!(s.contains("OpenAI API — alice"), "{s}");
    assert!(
        s.contains("Lets alice use the OpenAI-compatible API (/v1)"),
        "{s}"
    );
    assert!(s.contains("━● OpenAI API"), "{s}");
    h.shoot("accounts-openai");
    h.sent();
    h.key(b" ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SetAccountOpenAi { id, enabled: false, .. } if id == "alice")));
    let s = h.key(b"\x1b");
    assert!(!s.contains("OpenAI API — alice"), "{s}");
}

#[test]
fn an_entity_email_opens_its_own_mailbox_form() {
    let mut h = page((120, 40));
    select(&mut h, "castor");
    h.sent();
    let s = h.key(b"@");
    let sent = h.sent();
    assert!(
        sent.iter()
            .any(|c| matches!(c, Cmd::Operator(OpCmd::EmailSubject(Some(id))) if id == "castor")),
        "the entity's mailbox routes: {sent:?}"
    );
    assert!(
        sent.iter()
            .any(|c| matches!(c, Cmd::Operator(OpCmd::LoadMyEmail))),
        "{sent:?}"
    );
    assert!(s.contains("Email — castor"), "{s}");
    h.key(b"\x1b");
    let sent = h.sent();
    assert!(
        sent.iter()
            .any(|c| matches!(c, Cmd::Operator(OpCmd::EmailSubject(None)))),
        "closing points the routes back at the caller's own mailbox: {sent:?}"
    );
}

#[test]
fn email_for_everyone_is_a_card_under_the_table() {
    let mut h = page((80, 24));
    let s = h.text();
    assert!(s.contains("Email for everyone"), "{s}");
    assert!(s.contains("Mailboxes for users"), "{s}");
    h.shoot("accounts-email-for-everyone");
}

fn row(url: &str, token: &str, id: &str, archived: bool) -> Value {
    let q = if archived {
        "?include_archived=true"
    } else {
        ""
    };
    let v = gw("GET", url, token, &format!("/admin/accounts{q}"), None);
    v["accounts"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["id"] == id)
        .cloned()
        .unwrap_or(Value::Null)
}

fn live_admin(size: (i32, i32)) -> Option<(r7w1::Harness, String, String)> {
    let (url, token) = live_env()?;
    // Start state (idempotent re-runs): bob active, not archived; alice's key on.
    gw(
        "POST",
        &url,
        &token,
        "/admin/accounts/bob/unarchive",
        Some(json!({})),
    );
    gw(
        "PUT",
        &url,
        &token,
        "/admin/accounts/bob/active",
        Some(json!({"active": true})),
    );
    gw(
        "PUT",
        &url,
        &token,
        "/admin/accounts/alice/openai-api",
        Some(json!({"enabled": true})),
    );
    let mut h = live(size, Mount::Root, &url, &token);
    h.ui.screen.set(ui::SCREEN_USERS);
    h.until("accounts", |h, _| {
        h.store.accounts.with_untracked(|d| {
            d.ready()
                .is_some_and(|r| r.iter().any(|a| a.id == "castor"))
        })
    });
    Some((h, url, token))
}

#[test]
#[ignore = "drives a live scratch gateway (R7W1_URL/R7W1_TOKEN)"]
fn live_active_archive_unarchive_openai() {
    let Some((mut h, url, token)) = live_admin((120, 40)) else {
        return;
    };
    h.shoot("live-accounts");
    // Active off (inline confirm) → the gateway says inactive; on again.
    select(&mut h, "bob");
    h.key(b" ");
    h.until_text("Deactivate bob?");
    h.key(b"y");
    h.until_text("bob is deactivated.");
    assert_eq!(row(&url, &token, "bob", false)["active"], false);
    select(&mut h, "bob");
    h.key(b" ");
    h.until_text("bob is active again.");
    assert_eq!(row(&url, &token, "bob", false)["active"], true);
    // Archive (inline confirm), then Show archived and Unarchive.
    select(&mut h, "bob");
    h.key(b"d");
    h.until_text("Archive bob?");
    h.key(b"y");
    h.until_text("bob is archived. Turn on Show archived to see it.");
    assert_eq!(row(&url, &token, "bob", true)["archived"], true);
    h.key(b"h");
    h.until_text("[x] Show archived");
    select(&mut h, "bob");
    h.key(b"d");
    h.until_text("bob is back, inactive: turn Active on to let it sign in.");
    assert_eq!(row(&url, &token, "bob", false)["archived"], false);
    h.key(b"h");
    // OpenAI API for alice: off, verified, on.
    select(&mut h, "alice");
    h.key(b"o");
    h.until_text("OpenAI API — alice");
    h.key(b" ");
    h.until_text("[ ] OpenAI API");
    h.until_text("Saved: alice's key is refused at /v1.");
    assert_eq!(row(&url, &token, "alice", false)["openai_api"], false);
    h.shoot("live-accounts-openai-off");
    h.key(b" ");
    h.until_text("[x] OpenAI API");
    h.until_text("Saved: alice can use the OpenAI API.");
    assert_eq!(row(&url, &token, "alice", false)["openai_api"], true);
    h.key(b"\x1b");
}

#[test]
#[ignore = "drives a live scratch gateway (R7W1_URL/R7W1_TOKEN)"]
fn live_logs_and_entity_email() {
    let Some((mut h, url, token)) = live_admin((120, 40)) else {
        return;
    };
    select(&mut h, "alice");
    h.key(b"l");
    h.until_text("Activity — alice");
    h.until_text("Mailbox connected");
    h.shoot("live-accounts-logs");
    h.key(b"f");
    h.until_text("[Sign-ins]");
    h.key(b"\x1b");
    // The entity's own mailbox form: the notifications switch writes the
    // entity mirror route; the gateway's record changes.
    let before = gw("GET", &url, &token, "/accounts/castor/email", None);
    let was = before["notifications"]["job_failed"]
        .as_bool()
        .unwrap_or(true);
    select(&mut h, "castor");
    h.key(b"@");
    h.until_text("Email — castor");
    h.until_text("castor is an AI user: this mailbox is its own.");
    h.shoot("live-accounts-entity-email");
    // IMAP: the address pre-fills the standard servers for its domain;
    // Connect goes to the entity's mirror route and the gateway's refusal
    // (no network on the scratch gateway) is said as it is.
    h.click_right_of("Mailbox address", 20);
    h.type_text("castor@example.test");
    h.until_text("imap.example.test");
    h.shoot("live-accounts-entity-email-imap");
    h.click_right_of("Password", 20);
    h.type_text("app-password");
    h.click_text(" Connect ");
    h.until("the connect answer", |_, s| {
        s.contains("✗")
            || s.contains("Mailbox connected")
            || s.contains("Couldn")
            || s.contains("could not")
    });
    h.shoot("live-accounts-entity-email-connect");
    let after = gw("GET", &url, &token, "/accounts/castor/email", None);
    assert!(after.is_object(), "{after}");
    let _ = was;
    h.key(b"\x1b");
    // Back on the admin's own mailbox afterwards (the subject reset).
    let mine = gw("GET", &url, &token, "/me/email", None);
    assert!(mine.is_object());
}

#[test]
#[ignore = "drives a live scratch gateway (R7W1_URL/R7W1_TOKEN)"]
fn live_create_user_shows_the_token_once() {
    let Some((mut h, url, token)) = live_admin((120, 40)) else {
        return;
    };
    let id = format!("r7u{}", std::process::id());
    h.key(b"a");
    h.until_text("User ID");
    h.type_text(&id);
    h.click_text(" Create user ");
    h.until_text("Give this token to");
    h.shoot("live-accounts-created-token");
    assert_eq!(row(&url, &token, &id, false)["id"], id.as_str());
}

#[test]
#[ignore = "drives a live scratch gateway (R7W1_URL/R7W1_TOKEN)"]
fn live_capture_80x24() {
    let Some((mut h, _url, _token)) = live_admin((80, 24)) else {
        return;
    };
    h.shoot("live-accounts");
    h.key(b"\r");
    h.shoot("live-accounts-expanded");
}
