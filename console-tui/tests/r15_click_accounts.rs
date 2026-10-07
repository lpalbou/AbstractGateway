//! R15 (DESIGN-TUI.md §6.1): a synthesized mouse click for EVERY Accounts
//! button — each row action glyph, the Active toggle, the Runtime link and
//! the head buttons — through the real input pipeline (SGR press +
//! release bytes into the headless terminal). Each click is asserted on
//! the command the worker would receive or the modal/prompt that opens.
//! The meta-test enumerates `users::row_actions` over the fixture rows: an
//! action without a click test here is RED.

#[path = "accounts_fixture/mod.rs"]
mod accounts_fixture;
mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::store::accounts::accounts_from_payload;
use abstractgateway_console::store::{entities_from_payload, users_from_payload, Loadable};
use abstractgateway_console::ui::{self, users};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    users::view(cx, ctx, &t)
}

fn payload() -> Value {
    let mut alice =
        accounts_fixture::user_row("alice", false, "alice@example.test", "", true, false);
    alice["openai_api"] = json!(true);
    alice["actions"]["openai_api"] = json!({"available": true, "reason": null});
    alice["actions"]["preferences"] = json!({"available": true, "reason": null});
    let mut castor = accounts_fixture::entity_row("castor", "awake", true);
    castor["actions"]["preferences"] = json!({"available": true, "reason": null});
    let mut carol = accounts_fixture::user_row("carol", false, "", "", false, false);
    carol["archived"] = json!(true);
    for a in [
        "email",
        "workspace",
        "rotate",
        "archive",
        "suspend",
        "preferences",
    ] {
        carol["actions"][a] = json!({"available": false, "reason": "Archived accounts stay inactive: unarchive it first."});
    }
    carol["actions"]["unarchive"] = json!({"available": true, "reason": null});
    let mut admin = accounts_fixture::user_row("admin", true, "", "", true, true);
    admin["own"] = json!(true);
    admin["actions"]["preferences"] = json!({"available": true, "reason": null});
    json!({"accounts": [
        admin,
        alice, castor, carol,
    ]})
}

fn page() -> r8w4::Harness {
    let mut h = harness((120, 40), Mount::Page(page_view));
    h.admin();
    h.store
        .accounts
        .set(Loadable::Ready(accounts_from_payload(&payload()).unwrap()));
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&json!({"users": [
            {"user_id": "admin", "tenant_id": "default", "roles": ["admin"], "enabled": true},
            {"user_id": "alice", "tenant_id": "default", "roles": ["user"], "enabled": true},
            {"user_id": "carol", "tenant_id": "default", "roles": ["user"], "enabled": false}
        ]}))));
    h.store.entities.set(Loadable::Ready(entities_from_payload(
        &json!({"entities": [{"name": "castor", "state": "awake"}]}),
    )));
    h.turns(3);
    h.sent();
    h
}

/// The row line of `id` (a row starts with its id after the page padding).
fn row_anchor(id: &str) -> String {
    format!(" {id} ")
}

/// Click on `id`'s row: `needle` searched in the Actions cell (right of
/// the Active toggle) — or, for the Active toggle itself and the Runtime
/// link, the cell they own. The screen after.
fn click_row(h: &mut r8w4::Harness, id: &str, needle: &str) -> String {
    let screen = h.turns(1);
    let anchor = row_anchor(id);
    let (y, line) = screen
        .lines()
        .enumerate()
        .find(|(_, l)| l.starts_with(anchor.as_str()) || l.contains(&format!("│{anchor}")))
        .unwrap_or_else(|| panic!("{id} row:\n{screen}"));
    let chars: Vec<char> = line.chars().collect();
    let text: String = chars.iter().collect();
    // The Active cell: the toggle glyph (or "Archived").
    let toggle_at = ["━●", "●─", "Archived"]
        .iter()
        .filter_map(|g| text.find(g))
        .min()
        .unwrap_or_else(|| panic!("{id}: no Active cell:\n{screen}"));
    let byte = if needle == "━●" || needle == "●─" {
        text.find(needle).expect("toggle")
    } else if needle == "runtime" {
        // The Runtime link: the last word before the Active cell.
        let before = &text[..toggle_at];
        let end = before.trim_end().len();
        before[..end]
            .rfind(' ')
            .map(|i| i + 1)
            .expect("runtime link")
    } else {
        toggle_at
            + text[toggle_at..]
                .find(needle)
                .unwrap_or_else(|| panic!("{needle:?} not in {id}'s actions:\n{screen}"))
    };
    let x = text[..byte].chars().count() + 1;
    let click = format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1);
    h.key(click.as_bytes())
}

fn opened(s: &str, title: &str) -> bool {
    s.contains(title)
}

/// The action ids each fixture row offers (the single source the cells,
/// hints and this test share).
fn offered() -> BTreeSet<(String, &'static str)> {
    let rows = accounts_from_payload(&payload()).unwrap();
    let mut out = BTreeSet::new();
    for r in rows {
        for a in users::row_actions(&r, true) {
            out.insert((r.id.clone(), a.id));
        }
    }
    out
}

/// Every (row, action) pair the clicks below exercise.
fn covered() -> BTreeSet<(String, &'static str)> {
    let mut out = BTreeSet::new();
    for (id, a) in [
        ("admin", "email"),
        ("admin", "openai"),
        ("admin", "logs"),
        ("admin", "workspaces"),
        ("admin", "preferences"),
        ("admin", "rotate"),
        ("admin", "archive"),
        ("alice", "email"),
        ("alice", "openai"),
        ("alice", "logs"),
        ("alice", "workspaces"),
        ("alice", "preferences"),
        ("alice", "rotate"),
        ("alice", "archive"),
        ("castor", "email"),
        ("castor", "logs"),
        ("castor", "workspaces"),
        ("castor", "preferences"),
        ("castor", "manage"),
        ("castor", "archive"),
        ("carol", "logs"),
        ("carol", "unarchive"),
    ] {
        out.insert((id.to_string(), a));
    }
    out
}

#[test]
fn every_offered_row_action_has_a_click_test() {
    let offered = offered();
    let covered = covered();
    let missing: Vec<_> = offered.difference(&covered).collect();
    assert!(
        missing.is_empty(),
        "row actions without a click test: {missing:?}"
    );
}

#[test]
fn clicks_on_alices_actions_do_what_they_say() {
    // Email → the address-only view of another user.
    let mut h = page();
    let s = click_row(&mut h, "alice", "@");
    assert!(opened(&s, "Email — alice"), "{s}");
    // OpenAI API → its modal.
    let mut h = page();
    let s = click_row(&mut h, "alice", "⇄");
    assert!(opened(&s, "OpenAI API — alice"), "{s}");
    // Logs → the activity read + its modal.
    let mut h = page();
    let s = click_row(&mut h, "alice", "≣");
    assert!(opened(&s, "Activity — alice"), "{s}");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::LoadActivity { target: Some((id, _)), .. } if id == "alice")));
    // Workspaces → the account level read + the chooser.
    let mut h = page();
    let s = click_row(&mut h, "alice", "◫");
    assert!(opened(&s, "Workspaces — alice"), "{s}");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::Json(JsonCmd::Get { path, .. }) if path == "/workspace/policy/default%3Aalice")));
    // Preferences → its read + modal.
    let mut h = page();
    let s = click_row(&mut h, "alice", "⊜");
    assert!(opened(&s, "Preferences — alice"), "{s}");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::Json(JsonCmd::Get { path, .. }) if path == "/accounts/alice/preferences")));
    // Rotate → the web's confirmation; answered, the PATCH.
    let mut h = page();
    let s = click_row(&mut h, "alice", "↻");
    assert!(s.contains("Rotate the token of alice?"), "{s}");
    h.key(b"\x1b[A");
    h.key(b"\r");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::PatchUser { user_id, body, .. } if user_id == "alice" && body["rotate_token"] == true)));
    // Archive → the web's confirmation; answered, the archive.
    let mut h = page();
    let s = click_row(&mut h, "alice", "⊟");
    assert!(s.contains("Archive alice?"), "{s}");
    h.key(b"\x1b[A");
    h.key(b"\r");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::ArchiveAccount { id, unarchive: false, .. } if id == "alice")));
}

#[test]
fn clicks_on_the_own_rows_actions_open_or_say_why() {
    // The own row: Email = your own mailbox form; Archive refused (why).
    let mut h = page();
    let s = click_row(&mut h, "admin", "@");
    assert!(opened(&s, "Email — admin"), "{s}");
    // This fixture's own row has no OpenAI API switch: faint, and a click
    // says why (nothing opens).
    let mut h = page();
    let s = click_row(&mut h, "admin", "⇄");
    assert!(!opened(&s, "OpenAI API — admin"), "{s}");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("This gateway does not offer the OpenAI API switch.")
    );
    let mut h = page();
    let s = click_row(&mut h, "admin", "≣");
    assert!(opened(&s, "Activity — admin"), "{s}");
    let mut h = page();
    let s = click_row(&mut h, "admin", "◫");
    assert!(opened(&s, "Workspaces — admin"), "{s}");
    assert!(h.sent().iter().any(
        |c| matches!(c, Cmd::Json(JsonCmd::Get { path, .. }) if path == "/workspace/policy/me")
    ));
    let mut h = page();
    let s = click_row(&mut h, "admin", "⊜");
    assert!(opened(&s, "Preferences — admin"), "{s}");
    let mut h = page();
    let s = click_row(&mut h, "admin", "↻");
    assert!(s.contains("Rotate the token of admin?"), "{s}");
    let mut h = page();
    click_row(&mut h, "admin", "⊟");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("You can't archive your own account.")
    );
    assert!(!h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::ArchiveAccount { .. })));
}

#[test]
fn clicks_on_the_entitys_actions_do_what_they_say() {
    let mut h = page();
    let s = click_row(&mut h, "castor", "@");
    assert!(opened(&s, "Email — castor"), "{s}");
    let mut h = page();
    let s = click_row(&mut h, "castor", "≣");
    assert!(opened(&s, "Activity — castor"), "{s}");
    let mut h = page();
    let s = click_row(&mut h, "castor", "◫");
    assert!(opened(&s, "Workspaces — castor"), "{s}");
    let mut h = page();
    let s = click_row(&mut h, "castor", "⊜");
    assert!(opened(&s, "Preferences — castor"), "{s}");
    let mut h = page();
    let s = click_row(&mut h, "castor", "⬖");
    assert!(s.contains("Manage entity 'castor'"), "{s}");
    let mut h = page();
    let s = click_row(&mut h, "castor", "⊟");
    assert!(s.contains("Archive castor? It stops acting"), "{s}");
}

#[test]
fn clicks_on_an_archived_rows_actions() {
    let mut h = page();
    h.click_text("Show archived");
    assert!(
        h.store.acc.show_archived.get_untracked(),
        "the toggle shows archived"
    );
    let s = click_row(&mut h, "carol", "≣");
    assert!(opened(&s, "Activity — carol"), "{s}");
    let mut h = page();
    h.click_text("Show archived");
    click_row(&mut h, "carol", "⤒");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::ArchiveAccount { id, unarchive: true, .. } if id == "carol")));
}

#[test]
fn the_active_toggle_and_the_runtime_link_are_clickable() {
    // Active: a click on alice's ━● asks (Deactivate); answered, the PUT.
    let mut h = page();
    let s = click_row(&mut h, "alice", "━●");
    assert!(s.contains("Deactivate alice?"), "{s}");
    h.key(b"\x1b[A");
    h.key(b"\r");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SetAccountActive { id, active: false, .. } if id == "alice")));
    // The own row's toggle refuses with the reason.
    let mut h = page();
    click_row(&mut h, "admin", "━●");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("You can't deactivate your own account.")
    );
    // Runtime: the link opens the Runtimes page filtered to that account.
    let mut h = page();
    click_row(&mut h, "alice", "runtime");
    assert_eq!(
        h.ui.screen.get_untracked(),
        ui::SCREEN_RUNTIMES,
        "the Runtime link opens Runtimes"
    );
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::LoadRuntimesFor { .. })));
}

#[test]
fn the_head_buttons_are_clickable() {
    let mut h = page();
    let s = h.click_text("Eligible workspaces");
    assert!(
        opened(&s, "Eligible workspaces") && s.contains("The workspaces accounts may choose from"),
        "{s}"
    );
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::Json(JsonCmd::Get { path, .. }) if path == "/workspace/policy")));
    let mut h = page();
    let s = h.click_text("Create user");
    assert!(s.contains("User ID"), "{s}");
    let mut h = page();
    let s = h.click_text("Create entity");
    assert!(s.contains("Summon"), "{s}");
}

#[test]
fn hovering_a_glyph_shows_its_tooltip() {
    // Motion with no button held (SGR 35), then the hover delay passes.
    let mut h = page();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| {
            l.find(" alice ")
                .and_then(|_| l.find('⊟').map(|c| (i, l[..c].chars().count())))
        })
        .expect("alice's archive glyph");
    let ev = format!("\x1b[<35;{};{}M", col + 1, row + 1);
    h.key(ev.as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(s.contains("Archive alice (kept, hidden)  (d)"), "{s}");
}

#[test]
fn a_modal_opened_from_a_row_survives_the_accounts_reload_it_causes() {
    // The OpenAI API switch refreshes the accounts list; the table region
    // re-renders — the modal it opened lives on the PAGE scope and stays.
    let mut h = page();
    let s = click_row(&mut h, "alice", "⇄");
    assert!(s.contains("OpenAI API — alice"), "{s}");
    h.store
        .accounts
        .set(Loadable::Ready(accounts_from_payload(&payload()).unwrap()));
    let s = h.turns(3);
    assert!(
        s.contains("OpenAI API — alice"),
        "the modal survived the reload:\n{s}"
    );
}
