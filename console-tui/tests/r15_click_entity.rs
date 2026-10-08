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

/// A taller page (Manage's tabs whole, no scrolling) — the 80x24 test
/// below proves the tab body scrolls.
fn mpage() -> r8w4::Harness {
    let mut h = harness((120, 72), Mount::Page(page_view));
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

/// Manage — castor on tab `tab` (by mouse), its fields inline.
fn open_tab(tab: &str) -> r8w4::Harness {
    let mut h = mpage();
    open_manage(&mut h);
    if tab != "Overview" {
        let s = click_tab(&mut h, tab);
        assert!(s.contains("Manage — castor"), "{s}");
    }
    h.sent();
    h
}

/// Click a tab on Manage's tab bar (the FIRST line holding " Overview ").
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

/// Click into the field `label` of the card titled `card` (the first
/// `label` line at or below the card's title; the field column starts
/// 19 cells after the label).
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

/// Click into the text field labelled `label` (its last line on screen;
/// the field column starts 19 cells after the label).
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

/// Click the first `text` at or below the card titled `card`.
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

/// Close Manage with unsaved edits: it asks; Keep editing returns to the
/// same tab with the edit.
fn close_asks(h: &mut r8w4::Harness, tab_marker: &str) {
    let s = click_confirm_or_close(h);
    assert!(s.contains(ui::DISCARD_QUESTION), "Close asks:\n{s}");
    let s = click(h, " Keep editing ");
    assert!(
        s.contains("Manage — castor") && s.contains(tab_marker),
        "kept:\n{s}"
    );
}

fn click_confirm_or_close(h: &mut r8w4::Harness) -> String {
    click(h, " Close ")
}

// ---- the inline cards (each form's fields live inside its card) ---------

use abstractgateway_console::store::{CandidateRow, PromptData, ToolPolicyData};
use abstractgateway_console::ui::entity_manage::{subform_actions, SubForm};

/// (form, action id) pairs the tests below click.
fn sub_covered() -> BTreeSet<(String, &'static str)> {
    [
        ("Mind", "save"),
        ("Voice", "audition"),
        ("Voice", "play"),
        ("Voice", "save"),
        ("Work", "save"),
        ("Work", "end"),
        ("OwnTime", "grant"),
        ("OwnTime", "revoke"),
        ("Freeze", "freeze"),
        ("Reembed", "rebuild"),
        ("Tools", "save"),
        ("Prompt", "save"),
        ("Candidates", "promote"),
        ("Candidates", "reject"),
        ("Candidates", "reload"),
        ("Card", "verify"),
        ("Card", "reload"),
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
fn every_inline_action_has_a_click_test() {
    let mut offered = BTreeSet::new();
    for f in SubForm::ALL {
        for a in subform_actions(f) {
            offered.insert((format!("{f:?}"), a.id));
        }
    }
    let missing: Vec<_> = offered.difference(&sub_covered()).cloned().collect();
    assert!(
        missing.is_empty(),
        "inline actions without a click test: {missing:?}"
    );
    // Every card of every tab holds its form (none opens a dialog).
    for sec in entity_manage::manage_sections(false) {
        assert!(
            sec.form.is_some() || sec.title == "Right now",
            "{}",
            sec.title
        );
    }
}

#[test]
fn manage_is_one_screen_with_the_webs_tabs_and_cards_inline() {
    let mut h = mpage();
    open_manage(&mut h);
    for (i, tab) in entity_manage::MANAGE_TABS.iter().enumerate() {
        let s = click_tab(&mut h, tab);
        assert!(s.contains("Manage — castor"), "{tab}: still Manage:\n{s}");
        for sec in entity_manage::manage_sections(false)
            .iter()
            .filter(|c| c.tab == i)
        {
            assert!(s.contains(sec.title), "{tab} shows {}:\n{s}", sec.title);
        }
    }
    // Close by mouse.
    let s = click(&mut h, " Close ");
    assert!(!s.contains("Manage — castor"), "{s}");
}

#[test]
fn state_applies_inline_and_a_cancelled_confirm_keeps_the_tab() {
    let mut h = open_tab("Lifecycle");
    let s = click_in(&mut h, "Awake or asleep", " asleep ");
    let s = if s.contains(entity_manage::SLEEP_QUESTION) {
        s
    } else {
        settle(&mut h)
    };
    assert!(s.contains(entity_manage::SLEEP_QUESTION), "{s}");
    click_confirm(&mut h, "Sleep", "Cancel");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::EntityState { name, body }
        if name == "castor" && body.0["state"] == "asleep" && body.0.get("dream").is_none())));
    // The kill switch: a danger confirm; Esc keeps — Manage stays on Lifecycle.
    let s = click_in(&mut h, "Awake or asleep", " paused ");
    let s = if s.contains("Pause castor?") {
        s
    } else {
        settle(&mut h)
    };
    assert!(s.contains("Pause castor?"), "{s}");
    let s = h.esc();
    assert!(
        s.contains("Manage — castor") && s.contains("Emergency freeze"),
        "same tab:\n{s}"
    );
    assert!(!h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::EntityState { .. })));
}

#[test]
fn freeze_asks_over_manage_and_cancel_returns_to_the_tab() {
    let mut h = open_tab("Lifecycle");
    let s = click_in(&mut h, "Emergency freeze", " Freeze now ");
    assert!(s.contains("Freeze it now?"), "{s}");
    // A danger confirm opens on Cancel: Enter keeps.
    let s = h.key(b"\r");
    assert!(
        s.contains("Manage — castor") && s.contains("Emergency freeze"),
        "{s}"
    );
    assert!(!h.sent().iter().any(|c| matches!(c, Cmd::EntityLoop { .. })));
    click_in(&mut h, "Emergency freeze", " Freeze now ");
    click_confirm(&mut h, "Freeze", "Cancel");
    assert!(h.sent().iter().any(
        |c| matches!(c, Cmd::EntityLoop { start: false, body, .. } if body.0["mode"] == "freeze")
    ));
    let s = h.turns(2);
    assert!(s.contains("Manage — castor"), "Manage stays:\n{s}");
}

#[test]
fn personal_time_switch_and_grants_inline() {
    let mut h = open_tab("Lifecycle");
    click_in(&mut h, "Personal time", "●─ Personal time");
    assert!(
        h.sent()
            .iter()
            .any(|c| matches!(c, Cmd::EntityLoop { start: true, .. })),
        "on = start"
    );
    click_field_in(&mut h, "Schedule", "Hours allowed");
    h.type_text("2");
    click_in(&mut h, "Schedule", " Grant (timer) ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SavePersonalGrant { body, .. } if body.0["mode"] == "timer")));
    click_in(&mut h, "Schedule", " Revoke grant ");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::SavePersonalGrant { body, .. } if body.0 == json!({"mode": "disabled"}))));
}

#[test]
fn mind_save_stays_in_manage_and_close_asks_on_edits() {
    let mut h = open_tab("Mind & voice");
    click_field_in(&mut h, "The model it thinks with", "provider");
    h.type_text("lmstudio");
    click_field_in(&mut h, "The model it thinks with", "model");
    h.type_text("qwen3");
    click_in(&mut h, "The model it thinks with", " Save ");
    let sent = h.sent();
    assert!(
        sent.iter()
            .any(|c| matches!(c, Cmd::SaveEntitySubstrate { name, body, .. }
            if name == "castor" && body.0 == json!({"provider": "lmstudio", "model": "qwen3"}))),
        "Save sends the pair: {sent:?}"
    );
    // An unsaved edit: Close asks; Keep editing returns to the same tab.
    let mut h = open_tab("Mind & voice");
    click_field_in(&mut h, "The model it thinks with", "provider");
    h.type_text("lmstudio");
    close_asks(&mut h, "Danger zone");
    // Esc asks too.
    let s = h.esc();
    assert!(s.contains(ui::DISCARD_QUESTION), "Esc asks:\n{s}");
}

#[test]
fn switching_tab_with_unsaved_edits_asks() {
    let mut h = open_tab("Mind & voice");
    click_field_in(&mut h, "The model it thinks with", "provider");
    h.type_text("lmstudio");
    let s = click_tab(&mut h, "Overview");
    assert!(s.contains(ui::DISCARD_QUESTION), "{s}");
    let s = click(&mut h, " Keep editing ");
    assert!(
        s.contains("Danger zone") && s.contains("lmstudio"),
        "kept on Mind & voice:\n{s}"
    );
    click_tab(&mut h, "Overview");
    let s = click(&mut h, " Discard ");
    assert!(
        s.contains("Right now") && !s.contains("Danger zone"),
        "switched:\n{s}"
    );
}

#[test]
fn voice_hear_a_sample_play_save_inline() {
    let mut h = open_tab("Mind & voice");
    click_field_in(&mut h, "How it sounds", "provider");
    h.type_text("openai");
    click_field_in(&mut h, "How it sounds", "model");
    h.type_text("tts-1");
    click_in(&mut h, "How it sounds", " Hear a sample ");
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
    click_in(&mut h, "How it sounds", " Play ");
    assert!(h.store.notice.get_untracked().is_some(), "Play answers");
    click_in(&mut h, "How it sounds", " Save ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SaveEntityVoice { body, .. } if body.0["provider"] == "openai")));
}

#[test]
fn rebuild_index_asks_over_manage_then_sends() {
    let mut h = open_tab("Mind & voice");
    click_field_in(&mut h, "Danger zone", "embedding model");
    h.type_text("all-minilm");
    let s = click_in(&mut h, "Danger zone", " Rebuild index ");
    assert!(s.contains("Rebuild every memory vector now?"), "{s}");
    click_confirm(&mut h, "Rebuild", "Cancel");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::EntityReembed { body, .. } if body.0["embedding_model"] == "all-minilm")));
    let s = h.turns(2);
    assert!(
        s.contains("Manage — castor") && s.contains("Danger zone"),
        "same tab:\n{s}"
    );
}

#[test]
fn work_order_give_and_end_inline() {
    let mut h = open_tab("Work & tools");
    click_field_in(&mut h, "A task it works on", "order");
    h.type_text("read the backlog");
    click_in(&mut h, "A task it works on", " Give this task ");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::SaveEntityWorkOrder { body, .. } if body.0 == json!({"order": "read the backlog"}))));
    let mut h = open_tab("Work & tools");
    click_in(&mut h, "A task it works on", " End the work order ");
    assert!(h.sent().iter().any(
        |c| matches!(c, Cmd::SaveEntityWorkOrder { body, .. } if body.0 == json!({"clear": true}))
    ));
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
fn tools_save_sends_the_changed_phase_inline() {
    let mut h = open_tab("Work & tools");
    h.store.entity_policy.set(Loadable::Ready(policy()));
    h.turns(3);
    let s = click_in(&mut h, "Tools per phase", " Save ");
    assert!(s.contains("no changes to save"), "{s}");
    // The visit phase's list (keyboard: Shift+Tab from Save → work → visit).
    h.key(b"\x1b[Z\x1b[Z");
    h.key(b"\r");
    h.key(b"\x1b[B");
    h.key(b" ");
    let s = h.key(b"\r");
    assert!(s.contains("web_search, read_file"), "{s}");
    click_in(&mut h, "Tools per phase", " Save ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SaveToolPolicy { body, .. }
        if body.0["policy"].get("visit").is_some() && body.0["policy"].get("work").is_none())));
}

#[test]
fn prompt_save_sends_every_layer_and_close_asks() {
    let data = || PromptData {
        entity: "castor".into(),
        layers: vec![("persona".into(), "kind".into())],
    };
    let type_in_layer = |h: &mut r8w4::Harness, text: &str| {
        let s = h.turns(1);
        let (y, line) = s
            .lines()
            .enumerate()
            .find(|(_, l)| l.contains("kind"))
            .expect("layer text");
        let x = line[..line.find("kind").unwrap()].chars().count() + 5;
        h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
        h.type_text(text);
    };
    let mut h = open_tab("Prompt");
    h.store.entity_prompt.set(Loadable::Ready(data()));
    h.turns(3);
    type_in_layer(&mut h, " and curious");
    click_in(&mut h, "Instructions", "│ Save ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SaveEntityPrompt { body, .. }
        if body.0["overlay"]["persona"].as_str().is_some_and(|t| t.contains("curious")))));
    let mut h = open_tab("Prompt");
    h.store.entity_prompt.set(Loadable::Ready(data()));
    h.turns(3);
    type_in_layer(&mut h, "!");
    close_asks(&mut h, "Instructions");
}

#[test]
fn overview_card_verify_reload_and_candidates_inline() {
    let mut h = open_tab("Overview");
    click_in(&mut h, "Identity", " Verify memory ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::EntityVerify { name } if name == "castor")));
    click_in(&mut h, "Identity", " Reload ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::Entity(EntityCmd::LoadCard { .. }))));
    h.store.entity_candidates.set(Loadable::Ready((
        "castor".to_string(),
        vec![CandidateRow {
            record_id: "rec_1".into(),
            title: "likes rain".into(),
            digest: "a digest".into(),
            kind: "fact".into(),
        }],
    )));
    h.turns(3);
    // The Overview is taller than the modal: the body scrolls.
    h.wheel_down(10);
    let card = "Memories from sleep";
    click_field_in(&mut h, card, "reason");
    h.type_text("seen twice");
    click_in(&mut h, card, " Reject ");
    assert!(h.sent().iter().any(
        |c| matches!(c, Cmd::CandidateAct { promote: false, record_id, .. } if record_id == "rec_1")
    ));
    click_field_in(&mut h, card, "corroborating");
    h.type_text("a,b");
    click_field_in(&mut h, card, "reason");
    h.type_text("backed up");
    click_in(&mut h, card, " Promote (accept) ");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::CandidateAct { promote: true, corroborating_ids, .. } if corroborating_ids.len() == 2)));
    click_in(&mut h, card, " Reload ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::LoadCandidates { .. })));
}

#[test]
fn talk_tab_opens_sends_and_closes_the_visit_inline() {
    let mut h = open_tab("Talk");
    click_in(&mut h, "Visit", "│ Open visit ");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::Entity(EntityCmd::ChatOpen { .. }))));
    h.store.entity_chat.update(|c| {
        c.busy = false;
        c.apply_open(&json!({"chat_id": "chat_7", "yielded_loop": false}));
    });
    h.turns(2);
    click(&mut h, "▐say something");
    h.type_text("hello");
    click_in(&mut h, "Visit", "Send    Close");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::Entity(EntityCmd::ChatTurn { text, .. }) if text == "hello")));
    h.store.entity_chat.update(|c| c.busy = false);
    h.turns(2);
    // The button row (the hint line above also says "Close visit").
    let s = h.turns(1);
    let (y, line) = s
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains("Send    Close visit"))
        .expect("Talk buttons");
    let b = line.find("Send    Close visit").unwrap() + "Send    ".len();
    let x = line[..b].chars().count() + 2;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    assert!(h.sent().iter().any(
        |c| matches!(c, Cmd::Entity(EntityCmd::ChatClose { chat_id, .. }) if chat_id == "chat_7")
    ));
    let s = h.turns(2);
    assert!(s.contains("Manage — castor"), "still Manage:\n{s}");
}

#[test]
fn the_standalone_talk_panel_closes_by_mouse() {
    // `c` on Accounts opens the Talk panel alone (its own Close).
    let mut h = mpage();
    // Select castor's row, then `c` opens the Talk panel alone.
    click(&mut h, " castor ");
    let s = h.key(b"c");
    assert!(s.contains("Talk — castor"), "{s}");
    let s = click(&mut h, " Close ");
    assert!(!s.contains("Talk — castor"), "{s}");
}

#[test]
fn hovering_freeze_now_shows_its_tooltip() {
    let mut h = open_tab("Lifecycle");
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
        s.contains("Kill its personal-time process now (no reflection)"),
        "{s}"
    );
}

#[test]
fn the_tab_body_scrolls_at_80x24() {
    let mut h = harness((80, 24), Mount::Page(page_view));
    h.admin();
    let castor = accounts_fixture::entity_row("castor", "awake", true);
    h.store.accounts.set(Loadable::Ready(
        accounts_from_payload(&json!({"accounts": [castor]})).unwrap(),
    ));
    h.store.entities.set(Loadable::Ready(entities_from_payload(
        &json!({"entities": [{"name": "castor", "state": "awake"}]}),
    )));
    h.turns(3);
    open_manage(&mut h);
    click_tab(&mut h, "Mind & voice");
    let s = h.turns(2);
    assert!(
        !s.contains("Danger zone"),
        "the rebuild card is below the fold:\n{s}"
    );
    let s = h.wheel_down(30);
    assert!(s.contains("Danger zone"), "the body scrolls to it:\n{s}");
}

#[test]
fn manage_survives_the_entity_detail_reload() {
    let mut h = open_tab("Lifecycle");
    h.store.entity_detail.set(Loadable::Loading);
    h.store.entities.set(Loadable::Ready(entities_from_payload(
        &json!({"entities": [{"name": "castor", "state": "asleep"}]}),
    )));
    let s = h.turns(3);
    assert!(
        s.contains("Manage — castor") && s.contains("Emergency freeze"),
        "survived:\n{s}"
    );
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
    let label = |f: SubForm, id: &str| -> String {
        subform_actions(f)
            .into_iter()
            .find(|a| a.id == id)
            .unwrap()
            .label
    };
    assert_eq!(
        b["verify"].as_str(),
        Some(label(SubForm::Card, "verify").as_str())
    );
    assert_eq!(
        b["talk"].as_str(),
        Some(label(SubForm::Talk, "open").as_str())
    );
    assert_eq!(
        b["freeze"].as_str(),
        Some(label(SubForm::Freeze, "freeze").as_str())
    );
    assert_eq!(
        b["reembed"].as_str(),
        Some(label(SubForm::Reembed, "rebuild").as_str())
    );
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
        label(SubForm::Freeze, "freeze"),
        fx["buttons"]["freeze"].as_str().unwrap()
    );
    assert_eq!(
        label(SubForm::Talk, "open"),
        fx["buttons"]["talk"].as_str().unwrap()
    );
}

// ---- Spark templates (Accounts `s`) --------------------------------------

use abstractgateway_console::ui::entity_create::{
    template_actions, template_editor_actions, TplMode,
};

/// A kit whose FIRST template is an operator one (editable), then the
/// builtin floor.
fn tpl_kit(operator_first: bool) -> ent::CreationKit {
    let op = json!({"id": "researcher", "name": "Researcher", "description": "Reads widely",
        "source": "operator", "editable": true, "version": 3,
        "spark": {"name": "", "core_values": ["shared_vulnerability"]}, "core_values": ["shared_vulnerability"]});
    let builtin = json!({"id": "framework-default", "name": "Framework default",
        "description": "The framework floor", "source": "builtin", "editable": false, "version": 1,
        "spark": {"name": "", "core_values": ["shared_vulnerability"]}, "core_values": ["shared_vulnerability"]});
    let list = if operator_first {
        vec![op, builtin]
    } else {
        vec![builtin]
    };
    let (templates, template_warnings) =
        ent::templates_from_payload(&json!({"templates": list, "warnings": []}));
    ent::CreationKit {
        templates,
        template_warnings,
        ..Default::default()
    }
}

fn open_templates(operator_first: bool) -> r8w4::Harness {
    let mut h = page();
    let s = h.key(b"s");
    assert!(s.contains(entity_create::TPL_TITLE), "{s}");
    h.store
        .entity_kit
        .set(Loadable::Ready(tpl_kit(operator_first)));
    h.turns(3);
    h.sent();
    h
}

#[test]
fn every_template_action_has_a_click_test() {
    let kit = tpl_kit(true);
    let mut offered: BTreeSet<String> = template_actions(kit.templates.first())
        .iter()
        .map(|a| a.id.to_string())
        .collect();
    for m in [TplMode::View, TplMode::Edit, TplMode::New] {
        for a in template_editor_actions(m, None) {
            offered.insert(format!("editor.{}", a.id));
        }
    }
    let covered: BTreeSet<String> = [
        "view",
        "edit",
        "new",
        "close",
        "editor.save",
        "editor.cancel",
        "editor.close",
    ]
    .into_iter()
    .map(String::from)
    .collect();
    let missing: Vec<_> = offered.difference(&covered).collect();
    assert!(
        missing.is_empty(),
        "template actions without a click test: {missing:?}"
    );
}

#[test]
fn templates_view_and_close_by_mouse() {
    let mut h = open_templates(true);
    let s = click(&mut h, " View ");
    assert!(s.contains("Template 'researcher' — view"), "{s}");
    let s = click(&mut h, " Close ");
    assert!(!s.contains("Template 'researcher'"), "{s}");
    let mut h = open_templates(true);
    let s = click(&mut h, " Close ");
    assert!(!s.contains(entity_create::TPL_TITLE), "{s}");
}

#[test]
fn templates_edit_saves_a_version_and_cancel_asks() {
    let mut h = open_templates(true);
    let s = click(&mut h, " Edit ");
    assert!(s.contains("Template 'researcher' — edit"), "{s}");
    click_field(&mut h, "Display name");
    h.type_text("2");
    click(&mut h, " Save ");
    assert!(
        h.sent().iter().any(|c| matches!(c, Cmd::Entity(EntityCmd::SaveTemplate { id, create: false, body, .. })
            if id == "researcher" && body.0["name"].as_str().is_some_and(|n| n.contains("Researcher") && n.contains('2')))),
        "Save writes a new version"
    );
    let mut h = open_templates(true);
    click(&mut h, " Edit ");
    click_field(&mut h, "Display name");
    h.type_text("2");
    let s = click(&mut h, " Cancel ");
    assert!(s.contains(ui::DISCARD_QUESTION), "{s}");
}

#[test]
fn templates_new_from_selected_creates_and_edit_is_refused_on_the_floor() {
    let mut h = open_templates(true);
    let s = click(&mut h, " New from selected ");
    assert!(s.contains("New template — seeded from 'researcher'"), "{s}");
    click_field(&mut h, "New template id");
    h.type_text("scout");
    click(&mut h, " Save ");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::Entity(EntityCmd::SaveTemplate { id, create: true, .. }) if id == "scout")));
    // The builtin floor: Edit says why and opens nothing.
    let mut h = open_templates(false);
    let s = click(&mut h, " Edit ");
    assert!(
        s.contains(entity_create::TPL_TITLE) && !s.contains("— edit"),
        "{s}"
    );
    assert!(
        h.store
            .notice
            .get_untracked()
            .unwrap_or_default()
            .contains("Only operator templates can be edited"),
        "{:?}",
        h.store.notice.get_untracked()
    );
}

#[test]
fn the_template_words_are_the_webs() {
    let fx = fixture();
    let t = &fx["templates"];
    assert_eq!(t["title"].as_str(), Some(entity_create::TPL_TITLE));
    assert_eq!(t["lead"].as_str(), Some(entity_create::TPL_LEAD));
    assert_eq!(
        t["picker_tip"].as_str(),
        Some(entity_create::TPL_PICKER_TIP)
    );
    assert_eq!(
        t["spark_label"].as_str(),
        Some(entity_create::TPL_SPARK_LABEL)
    );
    let kit = tpl_kit(true);
    let acts = template_actions(kit.templates.first());
    for id in ["view", "edit", "new", "close"] {
        let a = acts.iter().find(|a| a.id == id).unwrap();
        assert_eq!(t[id]["label"].as_str(), Some(a.label.as_str()), "{id}");
        let tip = t[id]["tip"].as_str().unwrap();
        if !tip.is_empty() {
            assert_eq!(a.tooltip.as_deref(), Some(tip), "{id}");
        }
    }
    let ed = template_editor_actions(TplMode::Edit, None);
    for (id, key) in [("save", "save"), ("cancel", "cancel")] {
        let a = ed.iter().find(|a| a.id == id).unwrap();
        assert_eq!(t[key]["label"].as_str(), Some(a.label.as_str()));
    }
    assert_eq!(ed[0].tooltip.as_deref(), t["save"]["tip"].as_str());
}

/// Headless captures of Manage (each tab, top and scrolled to its end), both console
/// themes, 80x24 and 120x40 — written only when `R8W4_SHOTS_DIR` is set
/// (`cargo test --test r15_click_entity capture -- --ignored`).
#[test]
#[ignore]
fn capture_manage_and_sub_forms() {
    if std::env::var("R8W4_SHOTS_DIR").is_err() {
        return;
    }
    abstractgateway_console::ui::w::theme::register();
    for theme in ["gateway-dark", "gateway-light"] {
        for size in [(80, 24), (120, 40)] {
            let fresh = || {
                let mut h = harness(size, Mount::Page(page_view));
                abstracttui::app::set_theme_by_id(theme);
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
                h
            };
            let tag = theme.trim_start_matches("gateway-");
            for (i, tab) in entity_manage::MANAGE_TABS.iter().enumerate() {
                let mut h = fresh();
                open_manage(&mut h);
                if i > 0 {
                    click(&mut h, &format!(" {tab} "));
                }
                h.shoot(&format!("manage-tab{i}-{tag}"));
            }
            // The taller tabs, scrolled to their end (the body scrolls).
            for (i, tab) in entity_manage::MANAGE_TABS.iter().enumerate() {
                let mut h = fresh();
                open_manage(&mut h);
                if i > 0 {
                    click(&mut h, &format!(" {tab} "));
                }
                h.wheel_down(40);
                h.shoot(&format!("manage-tab{i}-end-{tag}"));
            }
            let mut h = fresh();
            open_manage(&mut h);
            click(&mut h, " Lifecycle ");
            h.wheel_down(40);
            click(&mut h, " Freeze now ");
            h.shoot(&format!("freeze-confirm-{tag}"));
        }
    }
}

#[test]
fn keyboard_tab_walks_the_tab_bar_and_enter_picks() {
    let mut h = mpage();
    open_manage(&mut h);
    // The focus starts on the chosen tab (Overview); Tab, Tab → Lifecycle.
    h.key(b"\t");
    h.key(b"\t");
    let s = h.key(b"\r");
    assert!(
        s.contains("Awake or asleep") && s.contains("Emergency freeze"),
        "{s}"
    );
}
