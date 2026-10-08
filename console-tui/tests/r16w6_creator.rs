//! R16.5 (operator ruling 2026-10-08): "the creator configures their entity", and the two roles
//! (admin, member). By mouse, on the Accounts page signed in as alice (a member) who created
//! Nova: the Active toggle and Unarchive go through `/me` (`admin: false`); Manage's settings
//! saves follow the gateway's `GET /entities/{name}/access` (a creator saves; anyone else gets
//! the served sentence, nothing sent); a tier-2 tool can't be GIVEN by a creator; the lifecycle
//! acts stay an admin's. The words are the web's (`tests/fixtures/r15_web_wording_accounts_roles.json`).
//! The meta-test enumerates `users::row_actions` over the creator's rows: an offered action
//! without a click here is RED.

#[path = "accounts_fixture/mod.rs"]
mod accounts_fixture;
mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::store::accounts::{accounts_from_payload, kind_help};
use abstractgateway_console::store::{
    entities_from_payload, EntityAccess, EntityDetail, Loadable, ToolPolicyData,
};
use abstractgateway_console::ui::{self, entity_manage, users};
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

const ADMIN_ONLY: &str = "Only an admin can put it to sleep or wake it, run its personal time, give it work, review its memories or rebuild its memory index.";
const TIER2: &str =
    "Only an admin can give nova a tier-2 tool: it acts on the world outside its memory and workspace.";
const NOT_YOURS: &str = "Only an admin or nova's creator can change its settings.";

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    users::view(cx, ctx, &t)
}

fn ok() -> Value {
    json!({"available": true, "reason": null})
}

/// What `GET /me/accounts?include_archived=true` answers alice (the gateway's R16.5 rows):
/// her own row, Nova (hers, live) and Lyra (hers, archived).
fn payload() -> Value {
    let mut alice = accounts_fixture::user_row("alice", false, "", "", true, true);
    alice["actions"]["preferences"] = ok();
    alice["actions"]["openai_api"] = json!({"available": false, "reason": "Only an admin can change who may use the OpenAI API."});
    let mut nova = accounts_fixture::entity_row("nova", "awake", true);
    nova["actions"]["preferences"] = ok();
    nova["actions"]["configure"] = ok();
    nova["created_by"] = json!({"tenant_id": "default", "user_id": "alice"});
    let mut lyra = accounts_fixture::entity_row("lyra", "paused", false);
    lyra["archived"] = json!(true);
    for a in [
        "email",
        "workspace",
        "archive",
        "suspend",
        "preferences",
        "manage",
        "configure",
    ] {
        lyra["actions"][a] = json!({"available": false, "reason": "Archived accounts stay inactive: unarchive it first."});
    }
    lyra["actions"]["unarchive"] = ok();
    json!({"accounts": [alice, nova, lyra], "scope": "own"})
}

fn creator_access() -> EntityAccess {
    EntityAccess {
        can_configure: true,
        as_role: Some("creator".into()),
        reason: None,
        admin_only_tools: vec!["execute_command".into()],
        admin_only_tools_reason: Some(TIER2.into()),
        admin_only_reason: Some(ADMIN_ONLY.into()),
    }
}

fn page(access: Option<EntityAccess>) -> r8w4::Harness {
    let mut h = harness((120, 72), Mount::Page(page_view));
    h.identity("alice", false);
    h.store
        .accounts
        .set(Loadable::Ready(accounts_from_payload(&payload()).unwrap()));
    h.store.entities.set(Loadable::Ready(entities_from_payload(
        &json!({"entities": [{"name": "nova", "state": "awake"}]}),
    )));
    if let Some(a) = access {
        h.store.entity_detail.set(Loadable::Ready(EntityDetail {
            name: "nova".into(),
            access: Some(Ok(a)),
            ..Default::default()
        }));
    }
    h.turns(3);
    h.sent();
    h
}

/// Click `needle` on `id`'s row, right of the Active cell (or the toggle itself).
fn click_row(h: &mut r8w4::Harness, id: &str, needle: &str) -> String {
    let screen = h.turns(1);
    let anchor = format!(" {id} ");
    let (y, line) = screen
        .lines()
        .enumerate()
        .find(|(_, l)| l.starts_with(anchor.as_str()) || l.contains(&format!("│{anchor}")))
        .unwrap_or_else(|| panic!("{id} row:\n{screen}"));
    let text: String = line.to_string();
    let toggle_at = ["━●", "●─", "Archived"]
        .iter()
        .filter_map(|g| text.find(g))
        .min()
        .unwrap_or_else(|| panic!("{id}: no Active cell:\n{screen}"));
    let byte = if needle == "━●" || needle == "●─" {
        text.find(needle).expect("toggle")
    } else {
        toggle_at
            + text[toggle_at..]
                .find(needle)
                .unwrap_or_else(|| panic!("{needle:?} not in {id}'s actions:\n{screen}"))
    };
    let x = text[..byte].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

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

fn click_tab(h: &mut r8w4::Harness, tab: &str) -> String {
    let s = h.turns(1);
    let (y, line) = s
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains(" Overview ") && l.contains(" Prompt "))
        .unwrap_or_else(|| panic!("tab bar:\n{s}"));
    let b = line.find(&format!(" {tab} ")).unwrap() + 1;
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn click_field_in(h: &mut r8w4::Harness, card: &str, label: &str) {
    let s = h.turns(1);
    let lines: Vec<&str> = s.lines().collect();
    let start = lines
        .iter()
        .position(|l| l.contains(card))
        .unwrap_or_else(|| panic!("card {card:?}:\n{s}"));
    let (y, line) = lines
        .iter()
        .enumerate()
        .skip(start + 1)
        .find(|(_, l)| l.contains(&format!("│{label} ")))
        .unwrap_or_else(|| panic!("{label:?} under {card:?}:\n{s}"));
    let x = line[..line.find(label).unwrap()].chars().count() + 21;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
}

fn click_in(h: &mut r8w4::Harness, card: &str, text: &str) -> String {
    let s = h.turns(1);
    let lines: Vec<&str> = s.lines().collect();
    let start = lines
        .iter()
        .position(|l| l.contains(card))
        .unwrap_or_else(|| panic!("card {card:?}:\n{s}"));
    let (y, line) = lines
        .iter()
        .enumerate()
        .skip(start + 1)
        .find(|(_, l)| l.contains(text))
        .unwrap_or_else(|| panic!("{text:?} under {card:?}:\n{s}"));
    let x = line[..line.find(text).unwrap()].chars().count() + 2;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Manage — nova by its row glyph, then the Mind card: type a pair, Save. The commands sent.
fn save_mind(access: Option<EntityAccess>) -> (String, Vec<Cmd>) {
    let mut h = page(access);
    let s = click_row(&mut h, "nova", "⬖");
    assert!(s.contains("Manage — nova"), "{s}");
    let lead = s.clone();
    click_tab(&mut h, "Mind & voice");
    click_field_in(&mut h, "The model it thinks with", "provider");
    h.type_text("endpoint:demo");
    click_field_in(&mut h, "The model it thinks with", "model");
    h.type_text("demo-large");
    let after = click_in(&mut h, "The model it thinks with", " Save ");
    (format!("{lead}\n=====\n{after}"), h.sent())
}

#[test]
fn the_creator_saves_its_mind_and_reads_what_stays_an_admins() {
    let (s, sent) = save_mind(Some(creator_access()));
    assert!(
        s.contains(entity_manage::CREATOR_LEAD) || s.contains("You created it: you change its"),
        "the creator's lead:\n{s}"
    );
    assert!(
        sent.iter().any(|c| matches!(c, Cmd::SaveEntitySubstrate { name, body, .. }
            if name == "nova" && body.0 == json!({"provider": "endpoint:demo", "model": "demo-large"}))),
        "a creator's Save is sent: {sent:?}"
    );
}

#[test]
fn someone_who_may_not_configure_gets_the_served_sentence_and_nothing_is_sent() {
    let denied = EntityAccess {
        can_configure: false,
        reason: Some(NOT_YOURS.into()),
        ..Default::default()
    };
    let (s, sent) = save_mind(Some(denied));
    assert!(
        s.contains(NOT_YOURS),
        "the gateway's sentence is shown:\n{s}"
    );
    assert!(
        !sent
            .iter()
            .any(|c| matches!(c, Cmd::SaveEntitySubstrate { .. })),
        "nothing written: {sent:?}"
    );
}

#[test]
fn an_unread_access_answer_refuses_instead_of_guessing() {
    let mut h = page(None);
    h.store.entity_detail.set(Loadable::Ready(EntityDetail {
        name: "nova".into(),
        access: Some(Err("HTTP 502".into())),
        ..Default::default()
    }));
    h.turns(2);
    let s = click_row(&mut h, "nova", "⬖");
    assert!(s.contains("Manage — nova"), "{s}");
    click_tab(&mut h, "Mind & voice");
    click_field_in(&mut h, "The model it thinks with", "provider");
    h.type_text("p");
    click_field_in(&mut h, "The model it thinks with", "model");
    h.type_text("m");
    let s = click_in(&mut h, "The model it thinks with", " Save ");
    assert!(
        s.contains("who may change its settings could not be read: HTTP 502"),
        "{s}"
    );
    assert!(!h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SaveEntitySubstrate { .. })));
}

#[test]
fn the_creator_never_switches_its_sleep_by_itself() {
    let mut h = page(Some(creator_access()));
    click_row(&mut h, "nova", "⬖");
    let s = click_tab(&mut h, "Lifecycle");
    assert!(
        s.contains("Awake or asleep") && s.contains("admin-only"),
        "the state card says why:\n{s}"
    );
    assert!(
        !h.sent()
            .iter()
            .any(|c| matches!(c, Cmd::EntityState { .. })),
        "no state write"
    );
}

#[test]
fn a_creator_cannot_give_a_tier2_tool_but_may_keep_or_remove_one() {
    let d = ToolPolicyData {
        entity: "nova".into(),
        phases: vec![
            ("visit".into(), vec!["read_memory".into()], "custom".into()),
            (
                "work".into(),
                vec!["read_memory".into(), "execute_command".into()],
                "custom".into(),
            ),
        ],
        all_tools: vec!["read_memory".into(), "execute_command".into()],
    };
    let a = creator_access();
    let give = json!({"visit": ["read_memory", "execute_command"]});
    assert_eq!(
        entity_manage::admin_only_tool_problem(&a, "nova", &d, give.as_object().unwrap())
            .as_deref(),
        Some(TIER2)
    );
    let keep = json!({"work": ["execute_command"]});
    assert_eq!(
        entity_manage::admin_only_tool_problem(&a, "nova", &d, keep.as_object().unwrap()),
        None
    );
    let remove = json!({"work": ["read_memory"]});
    assert_eq!(
        entity_manage::admin_only_tool_problem(&a, "nova", &d, remove.as_object().unwrap()),
        None
    );
    // An admin's access lists no admin-only tools: nothing is refused.
    let admin = EntityAccess {
        can_configure: true,
        as_role: Some("admin".into()),
        ..Default::default()
    };
    assert_eq!(
        entity_manage::admin_only_tool_problem(&admin, "nova", &d, give.as_object().unwrap()),
        None
    );
}

#[test]
fn the_creator_switches_nova_off_and_on_through_me() {
    let mut h = page(Some(creator_access()));
    let s = click_row(&mut h, "nova", "━●");
    assert!(
        s.contains("Suspend nova? It stops acting until you turn Active back on."),
        "{s}"
    );
    click_confirm(&mut h, "Suspend", "Cancel");
    let sent = h.sent();
    assert!(
        sent.iter().any(|c| matches!(c, Cmd::SetAccountActive { id, active: false, admin: false, entity: true, .. } if id == "nova")),
        "Suspend goes through /me (admin: false): {sent:?}"
    );
}

#[test]
fn the_creator_unarchives_lyra_through_me() {
    let mut h = page(Some(creator_access()));
    h.store.acc.show_archived.set(true);
    h.turns(2);
    click_row(&mut h, "lyra", "⤒");
    let sent = h.sent();
    assert!(
        sent.iter().any(|c| matches!(c, Cmd::ArchiveAccount { id, unarchive: true, admin: false, .. } if id == "lyra")),
        "Unarchive goes through /me: {sent:?}"
    );
}

#[test]
fn the_creator_archives_nova_after_the_question() {
    let mut h = page(Some(creator_access()));
    click_row(&mut h, "nova", "⊟");
    click_confirm(&mut h, "Archive", "Cancel");
    let sent = h.sent();
    assert!(
        sent.iter().any(|c| matches!(c, Cmd::ArchiveAccount { id, unarchive: false, admin: false, .. } if id == "nova")),
        "{sent:?}"
    );
}

#[test]
fn every_other_creator_row_action_opens_its_dialog() {
    for (glyph, title) in [
        ("@", "nova"),
        ("≣", "Activity"),
        ("◫", "Workspaces"),
        ("⊜", "Preferences"),
    ] {
        let mut h = page(Some(creator_access()));
        let s = click_row(&mut h, "nova", glyph);
        assert!(s.contains(title), "{glyph}: {title}:\n{s}");
    }
    let mut h = page(Some(creator_access()));
    h.store.acc.show_archived.set(true);
    h.turns(2);
    let s = click_row(&mut h, "lyra", "≣");
    assert!(s.contains("Activity"), "{s}");
}

/// The (row, action) pairs alice's rows offer, from the single source.
fn offered() -> BTreeSet<(String, &'static str)> {
    let rows = accounts_from_payload(&payload()).unwrap();
    let mut out = BTreeSet::new();
    for r in rows.iter().filter(|r| r.is_entity()) {
        for a in users::row_actions(r, false) {
            if a.is_enabled() {
                out.insert((r.id.clone(), a.id));
            }
        }
    }
    out
}

#[test]
fn every_offered_creator_action_has_a_click_test() {
    let covered: BTreeSet<(String, &'static str)> = [
        ("nova", "email"),
        ("nova", "logs"),
        ("nova", "workspaces"),
        ("nova", "preferences"),
        ("nova", "manage"),
        ("nova", "archive"),
        ("lyra", "logs"),
        ("lyra", "unarchive"),
    ]
    .into_iter()
    .map(|(r, a)| (r.to_string(), a))
    .collect();
    let missing: Vec<_> = offered().difference(&covered).cloned().collect();
    assert!(
        missing.is_empty(),
        "offered without a click test: {missing:?}"
    );
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_accounts_roles.json"
    );
    let text = std::fs::read_to_string(p)
        .unwrap_or_else(|e| panic!("{p}: {e} — run scripts/extract_web_wording.py --write"));
    serde_json::from_str(&text).expect("fixture JSON")
}

#[test]
fn the_roles_words_are_the_webs() {
    let f = fixture();
    let s = |k: &str| f[k].as_str().unwrap().to_string();
    assert_eq!(
        f["role_options"],
        json!([users::ROLE_OPTION_MEMBER, users::ROLE_OPTION_ADMIN]),
        "exactly two roles, the web's words"
    );
    assert_eq!(s("show_archived_tip_admin"), users::SHOW_ARCHIVED_TIP_ADMIN);
    assert_eq!(
        s("show_archived_tip_member"),
        users::SHOW_ARCHIVED_TIP_MEMBER
    );
    for (kind, row) in [("admin", 0usize), ("user", 1), ("entity", 2)] {
        let _ = row;
        let label = f["kind_labels"][kind].as_str().unwrap();
        assert_eq!(
            kind_help(label),
            f["kind_titles"][kind].as_str().unwrap(),
            "{kind}"
        );
    }
    let rows = accounts_from_payload(&payload()).unwrap();
    assert_eq!(
        rows[0].kind_label(),
        f["kind_labels"]["user"].as_str().unwrap()
    );
    assert_eq!(
        rows[1].kind_label(),
        f["kind_labels"]["entity"].as_str().unwrap()
    );
}

#[test]
fn the_member_page_offers_show_archived_with_the_members_sentence() {
    let mut h = page(Some(creator_access()));
    let s = h.turns(1);
    assert!(s.contains("Show archived"), "{s}");
    assert!(s.contains(" Member"), "a human non-admin is a Member:\n{s}");
    assert!(!s.contains("Create user"), "{s}");
}
