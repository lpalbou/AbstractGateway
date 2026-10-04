//! R8.2 Accounts (round 8) + R8.1 Email for everyone: Name · Email (ONE
//! column) · Runtime · Active at every width, the row's actions in one
//! line (no menu), `g` → the Runtimes page filtered to the account (chip
//! `Account: <id>` with ×), `w` → the Workspaces page on that account;
//! "Email for everyone" shows its three switches directly (no Advanced).
//! Hermetic snapshots + key drives; live drives against a scratch gateway
//! (ignored by default):
//!
//!   R8W4_URL=http://127.0.0.1:<port> R8W4_TOKEN=<admin token> \
//!   R8W4_SHOTS_DIR=<dir> cargo test --test r8w4_accounts -- --ignored --test-threads 1

#[path = "accounts_fixture/mod.rs"]
mod accounts_fixture;
mod r8w4;

use abstractgateway_console::store::accounts::accounts_from_payload;
use abstractgateway_console::store::email::EmailCaps;
use abstractgateway_console::store::{runtimes_from_payload, Loadable, RuntimeFilter};
use abstractgateway_console::ui::{self, runtimes, users};
use abstractgateway_console::worker::operator::{EmailAction, OpCmd};
use abstractgateway_console::worker::Cmd;
use r8w4::{gw, harness, live, live_env, Mount, SIZES};
use serde_json::{json, Value};

fn users_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    users::view(cx, ctx, &t)
}

fn runtimes_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    runtimes::view(cx, ctx, &t)
}

fn accounts() -> Value {
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
    let bob = accounts_fixture::user_row("bob", false, "", "", true, false);
    json!({"accounts": [
        accounts_fixture::user_row("admin", true, "", "", true, true),
        alice, bob,
        accounts_fixture::entity_row("castor", "awake", true),
    ]})
}

fn page(size: (i32, i32)) -> r8w4::Harness {
    let mut h = harness(size, Mount::Page(users_view));
    h.admin();
    h.store
        .accounts
        .set(Loadable::Ready(accounts_from_payload(&accounts()).unwrap()));
    h.turns(3);
    h
}

/// The page's text with the block borders and line breaks folded away
/// (a wrapped action line reads as one; the wrap's trailing "·" joins).
fn flat(s: &str) -> String {
    s.lines()
        .map(|l| l.trim_matches(|c| c == '│' || c == ' '))
        .collect::<Vec<_>>()
        .join(" ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .replace("· ·", "·")
}

fn select(h: &mut r8w4::Harness, id: &str) {
    let idx = h.store.accounts.with_untracked(|d| {
        d.ready()
            .unwrap()
            .iter()
            .filter(|r| !r.archived)
            .position(|r| r.id == id)
    });
    h.ui.account_sel.set(idx.expect(id));
    h.turns(2);
}

#[test]
fn four_columns_at_every_width_and_nothing_scrolls_sideways() {
    for size in SIZES {
        let mut h = page(size);
        let s = h.shoot("accounts");
        let header = s
            .lines()
            .find(|l| l.contains("Name") && l.contains("Active"))
            .unwrap_or_else(|| panic!("header:\n{s}"));
        for col in ["Name", "Email", "Runtime", "Active"] {
            assert!(header.contains(col), "{col}:\n{s}");
        }
        assert!(
            !s.contains("Mailbox") && !s.contains("Email address"),
            "{s}"
        );
        assert!(s.contains("alice@example.test · connected"), "{s}");
        assert!(s.contains("No address"), "{s}");
        assert!(!s.contains('…'), "a cell was cut:\n{s}");
        h.assert_fits();
    }
}

#[test]
fn the_highlighted_rows_actions_sit_in_one_line_in_the_web_order() {
    for size in SIZES {
        let mut h = page(size);
        select(&mut h, "alice");
        let s = h.shoot("accounts-actions-alice");
        assert!(
            flat(&s).contains("alice: @ Email · o OpenAI API (on) · l Logs · w Workspace · t Rotate · d Archive · g Runtime"),
            "{s}"
        );
        // Never split inside an action.
        assert!(
            !s.lines()
                .any(|l| l.trim_end_matches(['│', ' ']).ends_with(" d")),
            "{s}"
        );
        assert!(!s.contains('⋯'), "no menu:\n{s}");
        select(&mut h, "castor");
        let s = h.text();
        assert!(
            flat(&s).contains("castor: @ Email · l Logs · m Manage · d Archive · g Runtime"),
            "{s}"
        );
    }
}

#[test]
fn g_opens_the_runtimes_page_filtered_to_the_account() {
    let mut h = page((80, 24));
    select(&mut h, "alice");
    h.sent();
    h.key(b"g");
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_RUNTIMES);
    assert_eq!(
        h.store.runtime_filter.get_untracked(),
        Some(RuntimeFilter {
            account: "alice".into(),
            tenant_id: "default".into()
        })
    );
    let sent = h.sent();
    assert!(
        sent.iter()
            .any(|c| matches!(c, Cmd::LoadRuntimesFor { filter } if filter.account == "alice")),
        "GET /admin/runtimes?account=alice: {sent:?}"
    );
}

#[test]
fn w_opens_the_workspaces_page_on_that_account() {
    let mut h = page((80, 24));
    select(&mut h, "bob");
    h.key(b"w");
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_WORKSPACES);
    assert_eq!(
        h.store.ws.focus.get_untracked(),
        Some(("default".to_string(), "bob".to_string()))
    );
    // An entity has no per-account policy: the reason, no jump.
    let mut h = page((80, 24));
    select(&mut h, "castor");
    h.key(b"w");
    assert_eq!(h.ui.screen.get_untracked(), 0, "no jump");
    let n = h.store.notice.get_untracked().unwrap_or_default();
    assert!(
        n.contains("castor's file access is set on the entity itself"),
        "{n}"
    );
}

#[test]
fn the_runtimes_chip_names_the_account_and_x_clears_it() {
    for size in SIZES {
        let mut h = harness(size, Mount::Page(runtimes_view));
        h.admin();
        h.store.runtime_filter.set(Some(RuntimeFilter {
            account: "alice".into(),
            tenant_id: "default".into(),
        }));
        h.store
            .runtimes
            .set(Loadable::Ready(runtimes_from_payload(&json!({
            "runtimes": [{"runtime_id": "alice", "tenant_id": "default", "kind": "user",
                          "owners": ["alice"], "state": "active", "data_dir": "/data/rt/alice"}],
            "filter": {"account": "alice", "tenant_id": "default"}}))));
        let s = h.shoot("runtimes-filtered");
        assert!(s.contains("[Account: alice ×]"), "{s}");
        assert!(s.contains("x shows every runtime"), "{s}");
        h.sent();
        let s = h.key(b"x");
        assert!(!s.contains("Account: alice"), "{s}");
        assert_eq!(h.store.runtime_filter.get_untracked(), None);
        assert!(
            h.sent().iter().any(|c| matches!(c, Cmd::LoadRuntimes)),
            "every runtime again"
        );
    }
}

#[test]
fn email_for_everyone_shows_its_three_switches_directly() {
    for size in SIZES {
        let mut h = page(size);
        h.store.op.email_caps.set(Loadable::Ready(EmailCaps {
            email: true,
            agent_tools: true,
            recovery: false,
        }));
        let s = h.key(b"\t");
        h.shoot("accounts-email-for-everyone");
        assert!(s.contains("[Email for everyone]"), "{s}");
        assert!(s.contains("[x] Mailboxes for users"), "{s}");
        assert!(s.contains("[x] Agent email tools for users"), "{s}");
        assert!(s.contains("[ ] Sign-in by email"), "{s}");
        assert!(!s.contains("Advanced"), "no Advanced:\n{s}");
        h.assert_fits();
    }
}

#[test]
fn the_agent_tools_switch_applies_at_once() {
    let mut h = page((120, 40));
    h.store.op.email_caps.set(Loadable::Ready(EmailCaps {
        email: true,
        agent_tools: true,
        recovery: false,
    }));
    let s = h.key(b"\t");
    h.sent();
    h.click_text("Agent email tools for users");
    let _ = s;
    let body = h
        .sent()
        .into_iter()
        .find_map(|c| match c {
            Cmd::Operator(OpCmd::Email {
                action: EmailAction::CapsDefaults(b),
                ..
            }) => Some(b.0),
            _ => None,
        })
        .expect("PUT /admin/email/capabilities");
    assert_eq!(body, json!({"email_agent_tools": false}));
}

// ------------------------------------------------------------------ live

/// Live: the Runtime jump reads the gateway's filtered inventory.
#[test]
#[ignore]
fn live_runtime_jump_filters_on_the_gateway() {
    let Some((url, token)) = live_env() else {
        eprintln!("R8W4_URL/R8W4_TOKEN not set — skipped");
        return;
    };
    let user = std::env::var("R8W4_USER").unwrap_or_else(|_| "alice".into());
    let mut h = live((120, 40), Mount::Page(users_view), &url, &token);
    h.tx.send(Cmd::LoadAccounts).unwrap();
    h.until_text(&user);
    select(&mut h, &user);
    h.key(b"g");
    h.until("filtered inventory", |h, _| {
        h.store
            .runtimes
            .with_untracked(|r| matches!(r, Loadable::Ready(_)))
    });
    let rows = h
        .store
        .runtimes
        .with_untracked(|r| r.ready().cloned().unwrap());
    let truth = gw(
        "GET",
        &url,
        &token,
        &format!("/admin/runtimes?account={user}&tenant_id=default"),
        None,
    );
    assert_eq!(
        rows.len(),
        truth["runtimes"].as_array().map(Vec::len).unwrap_or(0),
        "{truth}"
    );
    assert!(
        rows.iter().all(|r| r.owners.iter().any(|o| o == &user)),
        "{rows:?}"
    );
    assert_eq!(truth["filter"]["account"], json!(user));
}

/// Live: the three email switches write the gateway's capabilities.
#[test]
#[ignore]
fn live_email_switches_apply() {
    let Some((url, token)) = live_env() else {
        eprintln!("R8W4_URL/R8W4_TOKEN not set — skipped");
        return;
    };
    let before = gw("GET", &url, &token, "/admin/email/capabilities", None);
    let mut h = live((120, 40), Mount::Page(users_view), &url, &token);
    h.key(b"\t");
    h.until_text("Sign-in by email");
    h.shoot("live-email-for-everyone");
    h.click_text("Sign-in by email");
    h.until("recovery flipped", |_, _| {
        let now = gw("GET", &url, &token, "/admin/email/capabilities", None);
        now != before
    });
    // The switch is busy until the console's verify lands.
    h.until("verified", |h, _| {
        h.store
            .notice
            .get_untracked()
            .unwrap_or_default()
            .contains("verified")
    });
    let now = gw("GET", &url, &token, "/admin/email/capabilities", None);
    // Put it back.
    h.click_text("Sign-in by email");
    h.until("restored", |_, _| {
        gw("GET", &url, &token, "/admin/email/capabilities", None) == before
    });
    assert_ne!(now, before);
}
