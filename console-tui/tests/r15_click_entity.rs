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
