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
        // One Email column (the card under the table names "Mailboxes for
        // users"; no Mailbox/Email address column).
        assert!(!header.contains("Mailbox") && !s.contains("Email address"), "{s}");
        assert!(s.contains("alice@example.test · connected"), "{s}");
        assert!(s.contains("No address"), "{s}");
        // ("reading…" is the email card's word while it loads, not a cut.)
        assert!(!s.replace("reading…", "").contains('…'), "a cell was cut:\n{s}");
        h.assert_fits();
    }
}

#[test]
fn the_highlighted_rows_actions_sit_in_one_line_in_the_web_order() {
    for size in SIZES {
        let mut h = page(size);
        select(&mut h, "alice");
        let s = h.shoot("accounts-actions-alice");
        // R15: each row's actions are glyph buttons in its Actions cell, in
        // the web's order (users: Email · OpenAI API · Logs · Workspaces ·
        // Preferences · Rotate · Archive; entities: … · Manage · Archive),
        // the Runtime a link in its own column.
        let row = |id: &str| -> String {
            s.lines()
                .find(|l| l.trim_start().starts_with(id))
                .unwrap_or_else(|| panic!("{id}:\n{s}"))
                .to_string()
        };
        assert!(row("alice").contains("@ ⇄ ≣ ◫ ⊜ ↻ ⊟"), "{s}");
        assert!(row("castor").contains("@ ≣ ◫ ⊜ ⬖ ⊟"), "{s}");
        assert!(row("alice").contains("alice    ━●"), "runtime link + Active:\n{s}");
        assert!(!s.contains('⋯'), "no menu:\n{s}");
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
fn w_opens_the_rows_workspaces_user_or_entity() {
    // R14.3: `w` opens the account-level chooser of any row, user or
    // entity (GET /workspace/policy/{tenant:id}); no screen jump.
    for who in ["bob", "castor"] {
        let mut h = page((80, 24));
        select(&mut h, who);
        h.sent();
        h.key(b"w");
        assert_eq!(h.ui.screen.get_untracked(), 0, "{who}: no jump");
        let sent = h.sent();
        let want = format!("/workspace/policy/default%3A{who}");
        assert!(
            sent.iter().any(|c| matches!(c,
                Cmd::Json(abstractgateway_console::worker::json::JsonCmd::Get { path, .. }) if *path == want)),
            "{who}: {sent:?}"
        );
    }
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
        // R15: a card under the table (no tab to switch to).
        let s = h.shoot("accounts-email-for-everyone");
        assert!(s.contains("Email for everyone"), "{s}");
        assert!(s.contains("━● Mailboxes for users"), "{s}");
        assert!(s.contains("━● Agent email tools for users"), "{s}");
        assert!(s.contains("●─ Sign-in by email"), "{s}");
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

/// Live: the whole console (sidebar, footer) on every R8 page at 80x24 and
/// 120x40 — captures for the gate (`R8W4_SHOTS_DIR`), every line fits.
#[test]
#[ignore]
fn live_capture_round8_pages_80x24() {
    capture_pages(SIZES[0]);
}

#[test]
#[ignore]
fn live_capture_round8_pages_120x40() {
    capture_pages(SIZES[1]);
}

/// One size per test: each test thread has its own UI runtime (a second
/// live harness on one thread would receive the first one's worker posts).
fn capture_pages(size: (i32, i32)) {
    let Some((url, token)) = live_env() else {
        eprintln!("R8W4_URL/R8W4_TOKEN not set — skipped");
        return;
    };
    {
        let mut h = live(size, Mount::Root, &url, &token);
        for (screen, name, wait) in [
            (ui::SCREEN_USERS, "root-accounts", "Name"),
            (
                ui::SCREEN_WORKFLOWS,
                "root-workflows",
                "Shared with everyone",
            ),
            (ui::SCREEN_SKILLS, "root-skills", "Shelf folder:"),
            (ui::SCREEN_APPS, "root-apps", "Apps"),
        ] {
            h.ui.screen.set(screen);
            h.until_text(wait);
            h.turns(3);
            h.shoot(name);
            h.assert_fits();
        }
        h.ui.screen.set(ui::SCREEN_USERS);
        h.until_text("alice");
        let idx = h.store.accounts.with_untracked(|d| {
            d.ready()
                .unwrap()
                .iter()
                .filter(|r| !r.archived)
                .position(|r| r.id == "alice")
        });
        h.ui.account_sel.set(idx.unwrap());
        h.turns(2);
        h.key(b"g");
        h.until_text("Account: alice");
        h.until("filtered list", |h, _| {
            h.store
                .runtimes
                .with_untracked(|r| matches!(r, Loadable::Ready(_)))
        });
        h.turns(3);
        h.shoot("root-runtimes-filtered");
        h.assert_fits();
        h.key(b"x");
    }
}
