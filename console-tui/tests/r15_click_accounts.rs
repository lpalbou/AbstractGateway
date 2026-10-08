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

/// Click the `label` button of the open confirmation (its button row is
/// the line holding both `label` and `other`). The screen after.
fn click_confirm(h: &mut r8w4::Harness, label: &str, other: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(&format!(" {label} ")) && l.contains(&format!(" {other} ")))
        .last()
        .unwrap_or_else(|| panic!("no [{label}] [{other}] button row:\n{screen}"));
    let b = line.rfind(&format!(" {label} ")).unwrap() + 1;
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
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
    click_confirm(&mut h, "Rotate", "Cancel");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::PatchUser { user_id, body, .. } if user_id == "alice" && body["rotate_token"] == true)));
    // Archive → the web's confirmation; answered, the archive.
    let mut h = page();
    let s = click_row(&mut h, "alice", "⊟");
    assert!(s.contains("Archive alice?"), "{s}");
    click_confirm(&mut h, "Archive", "Cancel");
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
    assert!(s.contains("Manage — castor"), "{s}"); // R15-B: the Manage FormModal
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
    click_confirm(&mut h, "Deactivate", "Cancel");
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

#[test]
fn enter_in_a_popup_commits_the_choice_never_the_form() {
    // A4: "Enter on the last field = Save" never fires from an open popup.
    // Create user: type the id, Tab to the Role picker, Enter opens its
    // popup, Enter commits the choice — no CreateUser is sent.
    let mut h = page();
    h.click_text("Create user");
    h.type_text("bob");
    h.key(b"\t");
    let s = h.key(b"\r"); // opens the Role popup
    assert!(
        s.contains("Read-only — can look"),
        "the Role popup is open:\n{s}"
    );
    h.key(b"\r"); // commits the highlighted role
    assert!(
        !h.sent().iter().any(|c| matches!(c, Cmd::CreateUser { .. })),
        "Enter in the popup created nothing"
    );
    // Preferences: Enter on the open Select commits exactly one PUT.
    let mut h = page();
    click_row(&mut h, "alice", "⊜");
    h.sent();
    h.store.json.set(
        "prefs.default.alice",
        Loadable::Ready(
            serde_json::from_str(include_str!("fixtures/r14w3_prefs_alice_set.json")).unwrap(),
        ),
    );
    h.turns(3);
    h.key(b"\r"); // open the first Select
    h.key(b"\x1b[A");
    h.key(b"\x1b[A");
    h.key(b"\r"); // commit the gateway default
    let puts: Vec<_> = h
        .sent()
        .into_iter()
        .filter(|c| matches!(c, Cmd::Json(JsonCmd::Send { method, .. }) if method == "PUT"))
        .collect();
    assert_eq!(puts.len(), 1, "one PUT for one commit: {puts:?}");
}

/// R15 F1: a confirmation is answered by its buttons — [Rotate] does it,
/// [Cancel], Esc and Enter on the default (Cancel) keep things; the
/// keyboard reaches the action with Shift+Tab, Enter then does it.
#[test]
fn a_confirmation_has_an_action_button_and_cancel_by_mouse_and_keys() {
    let rotated = |h: &mut r8w4::Harness| {
        h.sent()
            .iter()
            .any(|c| matches!(c, Cmd::PatchUser { body, .. } if body["rotate_token"] == true))
    };
    // [Cancel] by mouse: nothing sent, the dialog gone.
    let mut h = page();
    click_row(&mut h, "alice", "↻");
    let s = click_confirm(&mut h, "Cancel", "Rotate");
    let s = if s.contains("Rotate the token of alice?") {
        h.turns(2)
    } else {
        s
    };
    assert!(
        !s.contains("Rotate the token of alice?"),
        "Cancel closed it:\n{s}"
    );
    assert!(!rotated(&mut h), "Cancel sends nothing");
    // Enter on the default (Cancel): nothing.
    let mut h = page();
    click_row(&mut h, "alice", "↻");
    h.key(b"\r");
    assert!(!rotated(&mut h), "Enter on the default keeps");
    assert!(!h.turns(2).contains("Rotate the token of alice?"), "closed");
    // Esc: nothing.
    let mut h = page();
    click_row(&mut h, "alice", "↻");
    h.esc();
    assert!(!rotated(&mut h), "Esc keeps");
    // Keyboard: Shift+Tab to [Rotate], Enter.
    let mut h = page();
    click_row(&mut h, "alice", "↻");
    h.key(b"\x1b[Z");
    h.key(b"\r");
    assert!(rotated(&mut h), "Shift+Tab, Enter rotates");
    // A click on the sentence does nothing; the dialog stays.
    let mut h = page();
    click_row(&mut h, "alice", "↻");
    let s = h.click_text("Rotate the token of alice?");
    assert!(
        s.contains("Rotate the token of alice?") && !rotated(&mut h),
        "{s}"
    );
}

/// R15 F2 (the web's accountOtherEmailCard): another account's address
/// field has the web's inline Save button; a click on it sends the PATCH. Closing with an unsaved edit
/// asks "Discard changes?" — [Discard] closes and sends nothing, [Keep
/// editing] returns to the field with the edit.
#[test]
fn another_accounts_email_has_save_and_close_asks_before_dropping_an_edit() {
    let patched = |h: &mut r8w4::Harness| {
        h.sent().into_iter().find_map(|c| match c {
            Cmd::PatchUser { user_id, body, .. } if user_id == "alice" => {
                body["email"].as_str().map(str::to_string)
            }
            _ => None,
        })
    };
    // Type, click Save → the PATCH with the new address.
    let mut h = page();
    let s = click_row(&mut h, "alice", "@ ⇄");
    assert!(s.contains("Email — alice"), "{s}");
    h.sent();
    assert!(s.contains(" Save "), "the web's inline Save is there:\n{s}");
    h.click_text("▐alice@example.test");
    h.key(b"\x1b[F"); // End
    h.type_text(".uk");
    h.click_text(" Save ");
    assert_eq!(patched(&mut h).as_deref(), Some("alice@example.test.uk"));

    // Type, click Close → the discard question; Keep editing keeps the edit.
    let mut h = page();
    click_row(&mut h, "alice", "@ ⇄");
    h.sent();
    h.type_text(".uk");
    let s = click_confirm_any(&mut h, "Close");
    assert!(s.contains("Discard changes?"), "Close asks first:\n{s}");
    let s = click_confirm(&mut h, "Keep editing", "Discard");
    let s = if s.contains("Discard changes?") {
        h.turns(2)
    } else {
        s
    };
    assert!(
        s.contains("Email — alice") && s.contains(".uk"),
        "back to the edit:\n{s}"
    );
    // Esc asks too; Discard closes, nothing sent.
    h.esc();
    let s = h.turns(2);
    assert!(s.contains("Discard changes?"), "Esc asks first:\n{s}");
    let s = click_confirm(&mut h, "Discard", "Keep editing");
    let s = if s.contains("Email — alice") {
        h.turns(2)
    } else {
        s
    };
    assert!(!s.contains("Email — alice"), "Discard closed it:\n{s}");
    assert_eq!(patched(&mut h), None, "nothing saved");

    // The title ✕ asks too.
    let mut h = page();
    click_row(&mut h, "alice", "@ ⇄");
    h.type_text(".uk");
    let s = click_confirm_any(&mut h, "✕");
    let s = if s.contains("Discard changes?") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Discard changes?"), "✕ asks first:\n{s}");

    // Untouched: Close closes at once.
    let mut h = page();
    click_row(&mut h, "alice", "@ ⇄");
    let s = click_confirm_any(&mut h, "Close");
    let s = if s.contains("Email — alice") {
        h.turns(2)
    } else {
        s
    };
    assert!(
        !s.contains("Email — alice") && !s.contains("Discard changes?"),
        "{s}"
    );
}

/// Click the last ` label ` on screen (a dialog's bottom button).
fn click_confirm_any(h: &mut r8w4::Harness, label: &str) -> String {
    let screen = h.turns(1);
    let needle = format!(" {label} ");
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(&needle))
        .last()
        .unwrap_or_else(|| panic!("no {label:?} button:\n{screen}"));
    let b = line.rfind(&needle).unwrap() + 1;
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// R15 gate (c) + F2 ruling: Create user is a FormModal (title ✕), and
/// its ✕ / Esc with typed work ask "Discard changes?".
#[test]
fn create_user_has_a_title_close_that_asks_before_dropping_typed_work() {
    let mut h = page();
    let s = h.click_text("Create user");
    let s = if s.contains("User ID") { s } else { h.turns(2) };
    assert!(s.contains("User ID"), "{s}");
    // Clean: ✕ closes at once.
    let s = click_confirm_any(&mut h, "✕");
    let s = if s.contains("User ID") { h.turns(2) } else { s };
    assert!(!s.contains("User ID"), "clean ✕ closes:\n{s}");
    // Typed: ✕ asks; Discard closes.
    h.click_text("Create user");
    h.turns(2);
    h.type_text("zed");
    let s = click_confirm_any(&mut h, "✕");
    let s = if s.contains("Discard changes?") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Discard changes?"), "typed ✕ asks:\n{s}");
    let s = click_confirm(&mut h, "Discard", "Keep editing");
    let s = if s.contains("User ID") { h.turns(2) } else { s };
    assert!(
        !s.contains("User ID") && !s.contains("Discard changes?"),
        "{s}"
    );
    assert!(
        h.sent()
            .iter()
            .all(|c| !matches!(c, Cmd::CreateUser { .. })),
        "nothing created"
    );
}

/// A press that opens a dialog never keeps the pointer (R15-B trace): the
/// toggle flips on the press, its confirm opens, the release lands in the
/// confirm — the next press after the dialog still goes where it lands.
#[test]
fn a_press_that_opens_a_dialog_never_keeps_the_pointer() {
    let mut h = page();
    let s = click_row(&mut h, "alice", "━●");
    assert!(s.contains("Deactivate alice?"), "{s}");
    click_confirm(&mut h, "Cancel", "Deactivate");
    let s = h.turns(2);
    assert!(!s.contains("Deactivate alice?"), "{s}");
    // The next press lands on Create user, not on the toggle.
    let s = h.click_text("Create user");
    let s = if s.contains("User ID") { s } else { h.turns(2) };
    assert!(
        s.contains("User ID") && !s.contains("Deactivate alice?"),
        "the press reached Create user:\n{s}"
    );
}

/// R15-A AV4 note: Retained runtimes is a FormModal (title ✕ + Close).
#[test]
fn retained_runtimes_is_a_form_modal_with_a_title_close() {
    let mut h = page();
    let s = h.key(b"v");
    let s = if s.contains("Retained runtimes") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Retained runtimes"), "{s}");
    let s = click_confirm_any(&mut h, "✕");
    let s = if s.contains("Retained runtimes") {
        h.turns(2)
    } else {
        s
    };
    assert!(!s.contains("Retained runtimes"), "✕ closed it:\n{s}");
}
