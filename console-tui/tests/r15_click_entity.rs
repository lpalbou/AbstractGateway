//! R15 (DESIGN-TUI.md §6.1): Manage entity and Summon a new entity by
//! mouse, mounted on the Accounts page (the doors: castor's ⬖ glyph and
//! the head's Create entity button). Manage is ONE FormModal with the
//! web's tabs (a Segmented): every card button gets a synthesized click,
//! asserted on the Cmd sent or the form/confirm it opens (confirms
//! answered BY MOUSE). Summon: type, Validate & create → the dry-run Cmd,
//! [Summon] → the create Cmd; type, Cancel → "Discard changes?". The
//! meta-test enumerates `entity_manage::manage_sections`; the words are
//! the web's (`tests/fixtures/r15_web_wording_entity.json`).

#[path = "accounts_fixture/mod.rs"]
mod accounts_fixture;
mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::api::entities as ent;
use abstractgateway_console::store::accounts::accounts_from_payload;
use abstractgateway_console::store::{entities_from_payload, Loadable};
use abstractgateway_console::ui::{self, entity_create, entity_manage, users};
use abstractgateway_console::worker::entities::EntityCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    users::view(cx, ctx, &t)
}

fn page() -> r8w4::Harness {
    let mut h = harness((120, 40), Mount::Page(page_view));
    h.admin();
    let mut admin = accounts_fixture::user_row("admin", true, "", "", true, true);
    admin["own"] = json!(true);
    let castor = accounts_fixture::entity_row("castor", "awake", true);
    h.store.accounts.set(Loadable::Ready(
        accounts_from_payload(&json!({"accounts": [admin, castor]})).unwrap(),
    ));
    h.store.entities.set(Loadable::Ready(entities_from_payload(
        &json!({"entities": [{"name": "castor", "state": "awake"}]}),
    )));
    h.turns(3);
    h.sent();
    h
}

fn kit() -> ent::CreationKit {
    let (templates, template_warnings) = ent::templates_from_payload(&json!({
        "templates": [
            {"id": "framework-default", "name": "Framework default",
             "description": "The framework floor", "source": "builtin",
             "editable": false, "version": 1,
             "spark": {"name": "", "core_values": ["shared_vulnerability"]},
             "core_values": ["shared_vulnerability"]}
        ],
        "warnings": []
    }));
    let mut kit = ent::CreationKit {
        templates,
        template_warnings,
        ..Default::default()
    };
    kit.apply_defaults(Ok(&json!({
        "substrate": {"provider": null, "model": null, "source": "unset"},
        "embedding": {"provider": "huggingface", "model": "all-minilm", "source": "route"},
        "warnings": []
    })));
    kit
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_entity.json"
    );
    let text = std::fs::read_to_string(p)
        .unwrap_or_else(|e| panic!("{p}: {e} — run scripts/extract_web_wording.py --write"));
    serde_json::from_str(&text).expect("fixture JSON")
}

/// Click the LAST occurrence of `text` (a dialog sits above the page).
fn click(h: &mut r8w4::Harness, text: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(text))
        .last()
        .unwrap_or_else(|| panic!("{text:?} not on screen:\n{screen}"));
    let b = line.rfind(text).unwrap();
    let x = line[..b].chars().count() + 2;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Let a deferred dialog open (it opens on a later turn, after the press).
fn settle(h: &mut r8w4::Harness) -> String {
    std::thread::sleep(std::time::Duration::from_millis(20));
    h.turns(3)
}

/// Click `label` on the open confirmation's button row (the line holding
/// both `label` and `other`).
fn click_confirm(h: &mut r8w4::Harness, label: &str, other: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(&format!(" {label} ")) && l.contains(&format!(" {other} ")))
        .last()
        .unwrap_or_else(|| panic!("no [{label}] [{other}] row:\n{screen}"));
    let b = line.rfind(&format!(" {label} ")).unwrap() + 1;
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Open Manage — castor with castor's ⬖ glyph.
fn open_manage(h: &mut r8w4::Harness) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains(" castor ") && l.contains('⬖'))
        .unwrap_or_else(|| panic!("castor's row:\n{screen}"));
    let x = line[..line.find('⬖').unwrap()].chars().count() + 1;
    let s = h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    assert!(s.contains("Manage — castor"), "{s}");
    s
}

fn covered() -> BTreeSet<&'static str> {
    [
        "verify",
        "card",
        "candidates",
        "talk",
        "state",
        "owntime",
        "freeze",
        "substrate",
        "voice",
        "reembed",
        "work",
        "tools",
        "prompt",
    ]
    .into_iter()
    .collect()
}

#[test]
fn every_manage_action_has_a_click_test() {
    let offered: BTreeSet<&'static str> = entity_manage::manage_sections(false)
        .iter()
        .flat_map(|s| s.actions.iter().map(|a| a.id))
        .collect();
    let missing: Vec<_> = offered.difference(&covered()).copied().collect();
    assert!(
        missing.is_empty(),
        "Manage actions without a click test: {missing:?}"
    );
}

/// The window or command each Manage button leads to.
fn expect(id: &str) -> &'static str {
    match id {
        "card" => "Identity card — castor",
        "candidates" => "Candidates — castor",
        "talk" => "Talk — castor",
        "state" => "Entity state — castor",
        "owntime" => "Own time — castor",
        "substrate" => "Mind substrate — castor",
        "voice" => "Voice — castor",
        "reembed" => "Re-embed home — castor",
        "work" => "Work order — castor",
        "tools" => "Tool policy — castor",
        "prompt" => "Prompt overlay — castor",
        _ => "",
    }
}

#[test]
fn every_manage_button_by_mouse_does_what_it_says() {
    for sec in entity_manage::manage_sections(false) {
        for a in &sec.actions {
            let mut h = page();
            open_manage(&mut h);
            click(
                &mut h,
                &format!(" {} ", entity_manage::MANAGE_TABS[sec.tab]),
            );
            let s = h.turns(2);
            assert!(
                s.contains(sec.title),
                "{} tab shows {}:\n{s}",
                sec.tab,
                sec.title
            );
            h.sent();
            let s = click(&mut h, &format!(" {} ", a.label));
            match a.id {
                "verify" => assert!(
                    h.sent()
                        .iter()
                        .any(|c| matches!(c, Cmd::EntityVerify { name } if name == "castor")),
                    "Verify memory sends EntityVerify"
                ),
                "freeze" => {
                    assert!(s.contains("Freeze it now?"), "the web's confirm:\n{s}");
                    // A danger confirm opens on Cancel: Enter keeps.
                    h.key(b"\r");
                    assert!(!h.sent().iter().any(|c| matches!(c, Cmd::EntityLoop { .. })));
                    open_manage(&mut h);
                    click(&mut h, " Lifecycle ");
                    click(&mut h, " Freeze now ");
                    click(&mut h, " Freeze ");
                    assert!(
                        h.sent()
                            .iter()
                            .any(|c| matches!(c, Cmd::EntityLoop { name, start: false, body }
                            if name == "castor" && body.0["mode"] == "freeze")),
                        "[Freeze] sends the freeze"
                    );
                }
                id => assert!(s.contains(expect(id)), "{id} opens {}:\n{s}", expect(id)),
            }
        }
    }
}

#[test]
fn hovering_a_manage_button_shows_its_tooltip_and_keys_work() {
    let mut h = page();
    open_manage(&mut h);
    click(&mut h, " Lifecycle ");
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| {
            l.find(" Freeze now ")
                .map(|c| (i, l[..c].chars().count() + 2))
        })
        .expect("Freeze now");
    h.key(format!("\x1b[<35;{};{}M", col + 1, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(
        s.contains("Kill its personal-time process now (no reflection)  (f)"),
        "{s}"
    );
    // Keyboard: `v` is Verify memory, from any tab.
    let mut h = page();
    open_manage(&mut h);
    h.sent();
    h.key(b"v");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::EntityVerify { .. })));
}

#[test]
fn manage_survives_the_entity_detail_reload_and_closes_by_mouse() {
    let mut h = page();
    open_manage(&mut h);
    h.store.entity_detail.set(Loadable::Loading);
    h.store.entities.set(Loadable::Ready(entities_from_payload(
        &json!({"entities": [{"name": "castor", "state": "asleep"}]}),
    )));
    let s = h.turns(3);
    assert!(s.contains("Manage — castor"), "survived:\n{s}");
    let s = click(&mut h, " Close ");
    assert!(!s.contains("Manage — castor"), "closed:\n{s}");
}

fn open_summon(h: &mut r8w4::Harness) -> String {
    let s = click(h, " Create entity");
    assert!(s.contains(entity_create::SUMMON_TITLE), "{s}");
    h.store.entity_kit.set(Loadable::Ready(kit()));
    h.turns(3)
}

#[test]
fn summon_by_mouse_validates_then_confirms_then_creates() {
    let mut h = page();
    let s = open_summon(&mut h);
    assert!(
        s.contains("Optional configuration"),
        "R15 D1 named section:\n{s}"
    );
    h.sent();
    h.type_text("Pollux");
    click(&mut h, " Validate & create ");
    let cmds = h.sent();
    assert!(
        cmds.iter().any(
            |c| matches!(c, Cmd::Entity(EntityCmd::ValidateEntity { name, .. }) if name == "Pollux")
        ),
        "the dry-run first: {cmds:?}"
    );
    h.store
        .entity_check
        .set(Loadable::Ready(ent::CreateCheck::from_value(
            "Pollux",
            &json!({"ok": true, "warnings": []}),
        )));
    let s = h.turns(3);
    assert!(s.contains("Summon Pollux?"), "{s}");
    click(&mut h, " Summon ");
    assert!(
        h.sent().iter().any(|c| matches!(c, Cmd::Entity(EntityCmd::CreateEntity { name, substrate: None, policy: None, .. }) if name == "Pollux")),
        "[Summon] creates it (defaults: no extra writes)"
    );
}

#[test]
fn summon_back_to_the_form_keeps_the_name_and_cancel_asks_first() {
    let mut h = page();
    open_summon(&mut h);
    h.type_text("Pollux");
    click(&mut h, " Validate & create ");
    h.store
        .entity_check
        .set(Loadable::Ready(ent::CreateCheck::from_value(
            "Pollux",
            &json!({"ok": true, "warnings": []}),
        )));
    h.turns(3);
    let s = click(&mut h, " Back to the form ");
    assert!(
        s.contains("Pollux") && s.contains(entity_create::SUMMON_TITLE),
        "{s}"
    );
    h.sent();
    // Cancel with a typed name asks; Discard closes, nothing written.
    let s = click(&mut h, " Cancel ");
    assert!(s.contains(ui::DISCARD_QUESTION), "{s}");
    let s = click(&mut h, " Discard ");
    assert!(!s.contains(entity_create::SUMMON_TITLE), "{s}");
    assert!(!h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::Entity(EntityCmd::CreateEntity { .. }))));
}

#[test]
fn the_entity_words_are_the_webs() {
    let fx = fixture();
    let tabs: Vec<&str> = fx["tabs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(tabs, entity_manage::MANAGE_TABS.to_vec());
    let cards = fx["cards"].as_object().unwrap();
    for sec in entity_manage::manage_sections(false) {
        assert_eq!(
            cards.get(sec.title).and_then(Value::as_str),
            Some(sec.desc),
            "card {:?}",
            sec.title
        );
    }
    let b = &fx["buttons"];
    let label = |id: &str| -> String {
        entity_manage::manage_sections(false)
            .into_iter()
            .flat_map(|s| s.actions)
            .find(|a| a.id == id)
            .unwrap()
            .label
    };
    for id in ["verify", "talk", "freeze", "reembed"] {
        assert_eq!(b[id].as_str(), Some(label(id).as_str()), "{id}");
    }
    assert_eq!(
        fx["freeze_question"].as_str(),
        Some(entity_manage::FREEZE_QUESTION)
    );
    let c = &fx["create"];
    assert_eq!(c["title"].as_str(), Some(entity_create::SUMMON_TITLE));
    assert_eq!(c["lead"].as_str(), Some(entity_create::SUMMON_LEAD));
    assert_eq!(
        c["admin_note"].as_str(),
        Some(entity_create::OPTIONAL_ADMIN_NOTE)
    );
    assert_eq!(
        c["optional_title"].as_str(),
        Some(entity_create::OPTIONAL_TITLE)
    );
    assert_eq!(
        c["optional_lead"].as_str(),
        Some(entity_create::OPTIONAL_LEAD)
    );
    assert_eq!(
        c["create_label"].as_str(),
        Some(entity_create::CREATE_LABEL)
    );
    assert_eq!(c["create_tip"].as_str(), Some(entity_create::CREATE_TIP));
}

// ---- the sub-forms Manage opens (each a FormModal on w:: widgets) -------

use abstractgateway_console::store::{CandidateRow, PromptData, ToolPolicyData};
use abstractgateway_console::ui::entity_manage::{subform_actions, SubForm};

/// (form, action id) pairs the tests below click.
fn sub_covered() -> BTreeSet<(String, &'static str)> {
    [
        ("State", "close"),
        ("Mind", "save"),
        ("Mind", "close"),
        ("Voice", "audition"),
        ("Voice", "play"),
        ("Voice", "save"),
        ("Voice", "close"),
        ("Work", "save"),
        ("Work", "end"),
        ("Work", "close"),
        ("OwnTime", "grant"),
        ("OwnTime", "revoke"),
        ("OwnTime", "freeze"),
        ("OwnTime", "close"),
        ("Reembed", "rebuild"),
        ("Reembed", "close"),
        ("Tools", "save"),
        ("Tools", "close"),
        ("Prompt", "save"),
        ("Prompt", "close"),
        ("Candidates", "promote"),
        ("Candidates", "reject"),
        ("Candidates", "reload"),
        ("Candidates", "close"),
        ("Card", "reload"),
        ("Card", "close"),
        ("Talk", "open"),
        ("Talk", "send"),
        ("Talk", "close_visit"),
        ("Talk", "close"),
    ]
    .into_iter()
    .map(|(f, a)| (f.to_string(), a))
    .collect()
}

#[test]
fn every_sub_form_action_has_a_click_test() {
    let mut offered = BTreeSet::new();
    for f in SubForm::ALL {
        for a in subform_actions(f) {
            offered.insert((format!("{f:?}"), a.id));
        }
    }
    let missing: Vec<_> = offered.difference(&sub_covered()).cloned().collect();
    assert!(
        missing.is_empty(),
        "sub-form actions without a click test: {missing:?}"
    );
}

/// Manage — castor, then the card key `k` (the sub-form opens on top).
fn open_sub(k: &[u8], title: &str) -> r8w4::Harness {
    let mut h = page();
    open_manage(&mut h);
    let s = h.key(k);
    assert!(s.contains(title), "{title}:\n{s}");
    h.sent();
    h
}

/// Click into the text field labelled `label` (the field column starts
/// 19 cells after the label).
fn click_field(h: &mut r8w4::Harness, label: &str) {
    let s = h.turns(1);
    let (y, line) = s
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(label))
        .last()
        .unwrap_or_else(|| panic!("{label:?}:\n{s}"));
    let x = line[..line.rfind(label).unwrap()].chars().count() + 21;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
}

/// The visit phase's list: Tab (Empty-phase switch, then visit), Enter
/// opens, ↓ to read_file, Space ticks, Enter commits.
fn pick_read_file(h: &mut r8w4::Harness, walk: &[u8]) {
    h.key(walk);
    h.key(b"\r");
    h.key(b"\x1b[B");
    h.key(b" ");
    let s = h.key(b"\r");
    assert!(s.contains("web_search, read_file"), "{s}");
}

/// Type, then Close: the form asks before dropping the edit.
fn close_asks(h: &mut r8w4::Harness, title: &str) {
    let s = click(h, " Close ");
    assert!(
        s.contains(ui::DISCARD_QUESTION),
        "{title}: Close asks:\n{s}"
    );
    let s = click(h, " Keep editing ");
    assert!(s.contains(title), "{title}: kept:\n{s}");
}

#[test]
fn state_applies_at_once_and_sleep_asks_the_webs_question() {
    let mut h = open_sub(b"s", "Entity state — castor");
    click(&mut h, " asleep ");
    let s = settle(&mut h);
    assert!(s.contains(entity_manage::SLEEP_QUESTION), "{s}");
    click_confirm(&mut h, "Sleep", "Cancel");
    assert!(
        h.sent().iter().any(|c| matches!(c, Cmd::EntityState { name, body } if name == "castor" && body.0["state"] == "asleep")),
        "the state applies at once"
    );
    // The kill switch asks first (a danger confirm: focus on Cancel); Esc
    // keeps the state.
    click(&mut h, " paused ");
    let s = settle(&mut h);
    assert!(s.contains("Pause castor?"), "{s}");
    let s = h.esc();
    assert!(!s.contains("Pause castor?"), "{s}");
    assert!(!h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::EntityState { .. })));
    let s = click(&mut h, " Close ");
    assert!(!s.contains("Entity state — castor"), "{s}");
}

#[test]
fn mind_save_sends_and_close_asks() {
    let mut h = open_sub(b"n", "Mind substrate — castor");
    h.type_text("lmstudio");
    click_field(&mut h, "model");
    h.type_text("qwen3");
    click(&mut h, " Save ");
    assert!(
        h.sent()
            .iter()
            .any(|c| matches!(c, Cmd::SaveEntitySubstrate { name, body, .. }
            if name == "castor" && body.0 == json!({"provider": "lmstudio", "model": "qwen3"}))),
        "Save sends the pair"
    );
    let mut h = open_sub(b"n", "Mind substrate — castor");
    h.type_text("lmstudio");
    close_asks(&mut h, "Mind substrate — castor");
}

#[test]
fn voice_hear_a_sample_play_save_and_close_asks() {
    let mut h = open_sub(b"c", "Voice — castor");
    h.type_text("openai");
    click_field(&mut h, "model");
    h.type_text("tts-1");
    click(&mut h, " Hear a sample ");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::Entity(EntityCmd::VoiceAudition { provider, .. }) if provider == "openai")));
    h.store
        .entity_audition
        .set(Loadable::Ready(ent::AuditionOutcome {
            entity: "castor".into(),
            summary: "Synthesized".into(),
            path: Some("/nonexistent/r15-castor-audition.wav".into()),
            bytes: 10,
            error: None,
            player: Some("afplay".into()),
        }));
    h.turns(2);
    h.store.notice.set(None);
    click(&mut h, " Play ");
    assert!(h.store.notice.get_untracked().is_some(), "Play answers");
    click(&mut h, " Save ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SaveEntityVoice { body, .. } if body.0["provider"] == "openai")));
    let mut h = open_sub(b"c", "Voice — castor");
    h.type_text("openai");
    close_asks(&mut h, "Voice — castor");
}

#[test]
fn work_order_give_end_and_close_asks() {
    let mut h = open_sub(b"w", "Work order — castor");
    h.type_text("read the backlog");
    click(&mut h, " Give this task ");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::SaveEntityWorkOrder { body, .. } if body.0 == json!({"order": "read the backlog"}))));
    let mut h = open_sub(b"w", "Work order — castor");
    click(&mut h, " End the work order ");
    assert!(h.sent().iter().any(
        |c| matches!(c, Cmd::SaveEntityWorkOrder { body, .. } if body.0 == json!({"clear": true}))
    ));
    let mut h = open_sub(b"w", "Work order — castor");
    h.type_text("x");
    close_asks(&mut h, "Work order — castor");
}

#[test]
fn personal_time_switch_grants_and_freeze() {
    let mut h = open_sub(b"o", "Own time — castor");
    click(&mut h, "●─ Personal time");
    assert!(
        h.sent()
            .iter()
            .any(|c| matches!(c, Cmd::EntityLoop { start: true, .. })),
        "on = start"
    );
    click_field(&mut h, "Hours allowed");
    h.type_text("2");
    click(&mut h, " Grant (timer) ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SavePersonalGrant { body, .. } if body.0["mode"] == "timer")));
    click(&mut h, " Revoke grant ");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::SavePersonalGrant { body, .. } if body.0 == json!({"mode": "disabled"}))));
    let s = click(&mut h, " Freeze now ");
    assert!(s.contains("Freeze it now?"), "{s}");
    click_confirm(&mut h, "Freeze", "Cancel");
    assert!(h.sent().iter().any(
        |c| matches!(c, Cmd::EntityLoop { start: false, body, .. } if body.0["mode"] == "freeze")
    ));
    let s = click(&mut h, " Close ");
    assert!(!s.contains("Own time — castor"), "{s}");
}

#[test]
fn rebuild_index_asks_then_sends_and_close_asks() {
    let mut h = open_sub(b"x", "Re-embed home — castor");
    h.type_text("all-minilm");
    let s = click(&mut h, " Rebuild index ");
    assert!(s.contains("Rebuild every memory vector now?"), "{s}");
    click_confirm(&mut h, "Rebuild", "Cancel");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::EntityReembed { body, .. } if body.0["embedding_model"] == "all-minilm")));
    let mut h = open_sub(b"x", "Re-embed home — castor");
    h.type_text("all-minilm");
    close_asks(&mut h, "Re-embed home — castor");
}

fn policy() -> ToolPolicyData {
    ToolPolicyData {
        entity: "castor".into(),
        phases: vec![
            ("visit".into(), vec!["web_search".into()], "default".into()),
            ("work".into(), vec!["web_search".into()], "custom".into()),
        ],
        all_tools: vec!["web_search".into(), "read_file".into()],
    }
}

#[test]
fn tools_save_sends_the_changed_phase_and_close_asks() {
    let mut h = open_sub(b"p", "Tool policy — castor");
    h.store.entity_policy.set(Loadable::Ready(policy()));
    h.turns(3);
    // Unchanged: Save says so.
    let s = click(&mut h, " Save ");
    assert!(s.contains("no changes to save"), "{s}");
    // The visit phase: add read_file (keyboard: the engine list opens on
    // the press), commit, then Save by mouse.
    // (The focus is on Save: Shift+Tab twice reaches visit.)
    pick_read_file(&mut h, b"\x1b[Z\x1b[Z");
    click(&mut h, " Save ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SaveToolPolicy { body, .. }
        if body.0["policy"].get("visit").is_some() && body.0["policy"].get("work").is_none())));
    let mut h = open_sub(b"p", "Tool policy — castor");
    h.store.entity_policy.set(Loadable::Ready(policy()));
    h.turns(3);
    pick_read_file(&mut h, b"\t\t");
    close_asks(&mut h, "Tool policy — castor");
}

#[test]
fn prompt_save_sends_every_layer_and_close_asks() {
    let data = || PromptData {
        entity: "castor".into(),
        layers: vec![("persona".into(), "kind".into())],
    };
    let mut h = open_sub(b"e", "Prompt overlay — castor");
    h.store.entity_prompt.set(Loadable::Ready(data()));
    let s = h.turns(3);
    // Click into the layer's text, add a word, Save.
    let (y, line) = s
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains("kind"))
        .expect("layer text");
    let x = line[..line.find("kind").unwrap()].chars().count() + 5;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    h.type_text(" and curious");
    click(&mut h, " Save ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SaveEntityPrompt { body, .. }
        if body.0["overlay"]["persona"].as_str().is_some_and(|t| t.contains("curious")))));
    let mut h = open_sub(b"e", "Prompt overlay — castor");
    h.store.entity_prompt.set(Loadable::Ready(data()));
    let s = h.turns(3);
    let (y, line) = s
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains("kind"))
        .expect("layer text");
    let x = line[..line.find("kind").unwrap()].chars().count() + 5;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    h.type_text("!");
    close_asks(&mut h, "Prompt overlay — castor");
}

#[test]
fn candidates_promote_reject_reload_close() {
    let rows = || {
        (
            "castor".to_string(),
            vec![CandidateRow {
                record_id: "rec_1".into(),
                title: "likes rain".into(),
                digest: "a digest".into(),
                kind: "fact".into(),
            }],
        )
    };
    let mut h = open_sub(b"m", "Candidates — castor");
    h.store.entity_candidates.set(Loadable::Ready(rows()));
    h.turns(3);
    click_field(&mut h, "reason");
    h.type_text("seen twice");
    click(&mut h, " Reject ");
    assert!(h.sent().iter().any(
        |c| matches!(c, Cmd::CandidateAct { promote: false, record_id, .. } if record_id == "rec_1")
    ));
    click_field(&mut h, "corroborating");
    h.type_text("a,b");
    click_field(&mut h, "reason");
    h.type_text("backed up");
    click(&mut h, " Promote (accept) ");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::CandidateAct { promote: true, corroborating_ids, .. } if corroborating_ids.len() == 2)));
    click(&mut h, " Reload ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::LoadCandidates { .. })));
    let s = click(&mut h, " Close ");
    assert!(!s.contains("Candidates — castor"), "{s}");
}

#[test]
fn identity_card_reload_and_close() {
    let mut h = open_sub(b"i", "Identity card — castor");
    click(&mut h, " Reload ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::Entity(EntityCmd::LoadCard { .. }))));
    let s = click(&mut h, " Close ");
    assert!(!s.contains("Identity card — castor"), "{s}");
}

#[test]
fn talk_open_send_close_visit_and_close() {
    let mut h = open_sub(b"t", "Talk — castor");
    click(&mut h, " Open visit ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::Entity(EntityCmd::ChatOpen { .. }))));
    h.store.entity_chat.update(|c| {
        c.busy = false;
        c.apply_open(&json!({"chat_id": "chat_7", "yielded_loop": false}));
    });
    h.turns(2);
    click(&mut h, "say something");
    h.type_text("hello");
    click(&mut h, " Send ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::Entity(EntityCmd::ChatTurn { text, .. }) if text == "hello")));
    h.store.entity_chat.update(|c| c.busy = false);
    h.turns(2);
    click(&mut h, " Close visit ");
    assert!(h.sent().iter().any(
        |c| matches!(c, Cmd::Entity(EntityCmd::ChatClose { chat_id, .. }) if chat_id == "chat_7")
    ));
    let s = click(&mut h, " Close ");
    assert!(!s.contains("Talk — castor"), "{s}");
}

#[test]
fn the_sub_form_words_are_the_webs() {
    let fx = fixture();
    let sub = &fx["sub"];
    let w = |k: &str| {
        sub[k]
            .as_str()
            .unwrap_or_else(|| panic!("fixture sub.{k}"))
            .to_string()
    };
    assert_eq!(entity_manage::SLEEP_QUESTION, w("sleep_question"));
    assert_eq!(entity_manage::REEMBED_QUESTION, w("reembed_question"));
    assert_eq!(entity_manage::STATE_REASON_HELP, w("reason_help"));
    assert_eq!(entity_manage::EMPTY_PHASE_LABEL, w("empty_phase_label"));
    assert_eq!(entity_manage::EMPTY_PHASE_DESC, w("empty_phase_desc"));
    assert_eq!(entity_manage::SCHEDULE_HELP, w("schedule_help"));
    assert_eq!(
        entity_manage::TOOLS_DESC,
        fx["cards"]["Tools per phase"].as_str().unwrap()
    );
    assert_eq!(
        entity_manage::PERSONAL_TIME_DESC,
        fx["cards"]["Personal time"].as_str().unwrap()
    );
    let label = |f: SubForm, id: &str| -> String {
        subform_actions(f)
            .into_iter()
            .find(|a| a.id == id)
            .unwrap()
            .label
    };
    assert_eq!(label(SubForm::Voice, "audition"), w("audition_label"));
    assert_eq!(label(SubForm::Work, "save"), w("give_task_label"));
    assert_eq!(label(SubForm::Work, "end"), w("end_task_label"));
    assert_eq!(label(SubForm::Talk, "close_visit"), w("close_visit_label"));
    assert_eq!(
        label(SubForm::Reembed, "rebuild"),
        fx["buttons"]["reembed"].as_str().unwrap()
    );
    assert_eq!(
        label(SubForm::OwnTime, "freeze"),
        fx["buttons"]["freeze"].as_str().unwrap()
    );
    assert_eq!(
        label(SubForm::Talk, "open"),
        fx["buttons"]["talk"].as_str().unwrap()
    );
}
