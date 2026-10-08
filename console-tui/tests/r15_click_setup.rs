//! R15 (DESIGN-TUI.md, Setup): a synthesized mouse click for EVERY Setup
//! control — the head's Setup guide / Go to a step / Skip setup / Next /
//! ↻, the three "Go to …" cards, Use recommended defaults, Download all,
//! the second pass (♻ Replace mine too), the stepper dialog's step
//! buttons, Leave for now and Skip setup, and the guide's last step's
//! Finish / Skip setup / Start at login — through the real input
//! pipeline. The meta-test enumerates `welcome::head_actions` /
//! `recommended_actions` / `steps_actions` / `finish_actions` and the
//! cards: an action without a click test is RED. The words are the web
//! guide's (`tests/fixtures/r15_web_wording_setup.json`).

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::api::firstrun::{FirstRunState, WelcomeSummary};
use abstractgateway_console::store::{AvailabilityData, Loadable, RoutesData};
use abstractgateway_console::ui::{self, welcome};
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    welcome::view(cx, ctx, &t)
}

fn plan() -> AvailabilityData {
    AvailabilityData::from_value(&json!({
        "routes": [],
        "recommended": {
            "recommended": [
                {"route": "input.text", "provider": "mlx", "artifact": "mlx-community/Qwen3-8B-4bit",
                 "route_provider": "mlx", "route_model": "mlx-community/Qwen3-8B-4bit", "status": "installed"},
                {"route": "output.voice", "provider": "supertonic", "artifact": "supertonic-3",
                 "route_provider": "supertonic", "route_model": "supertonic-3", "status": "absent"}
            ],
            "total": 2, "installed": 1, "absent": 1, "unknown": 0, "gaps": []
        }
    }))
}

fn summary() -> WelcomeSummary {
    WelcomeSummary::from_host_state(&json!({
        "host": {"host_name": "forge.local"},
        "gateway": {"data_dir": "/srv/gw", "data_dir_source": "env", "auth_mode": "users",
                    "service": {"installed": false, "mechanism": "launchd-agent"}},
        "memory": {"ram": {"total_bytes": 137438953472u64}},
        "gpu": {"gpus": [{"name": "Apple M5 Max"}]}
    }))
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_setup.json"
    );
    serde_json::from_str(&std::fs::read_to_string(p).expect(p)).expect("fixture JSON")
}

fn fill(h: &mut r8w4::Harness) {
    h.store.availability.set(Loadable::Ready(plan()));
    h.store.routes.set(Loadable::Ready(RoutesData::from_value(
        &json!({"ok": true, "routes": []}),
    )));
    h.store.welcome.set(Loadable::Ready(summary()));
    h.store
        .first_run
        .set(Loadable::Ready(FirstRunState::from_value(
            &json!({"completed": false, "outcome": null}),
        )));
    h.turns(3);
    h.sent();
}

/// The page alone (browse mode), tall enough to show the whole body.
fn page() -> r8w4::Harness {
    let mut h = harness((130, 60), Mount::Page(page_view));
    h.admin();
    h.ui.screen.set(ui::SCREEN_WELCOME);
    fill(&mut h);
    h
}

/// The page in the guide (wizard mode).
fn guide() -> r8w4::Harness {
    let mut h = harness((130, 60), Mount::Page(page_view));
    h.admin();
    h.ui.wizard.set(true);
    h.ui.screen.set(ui::SCREEN_WELCOME);
    fill(&mut h);
    h
}

/// Click the first occurrence of `needle` on the first line holding `anchor`.
fn click_on(h: &mut r8w4::Harness, anchor: &str, needle: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains(anchor))
        .unwrap_or_else(|| panic!("{anchor:?}:\n{screen}"));
    let byte = line
        .find(needle)
        .unwrap_or_else(|| panic!("{needle:?} not on the {anchor:?} line:\n{screen}"));
    let x = line[..byte].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Click the last on-screen occurrence of `label`.
fn click_last(h: &mut r8w4::Harness, label: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(label))
        .last()
        .unwrap_or_else(|| panic!("no {label:?}:\n{screen}"));
    let x = line[..line.rfind(label).unwrap()].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn sent(h: &mut r8w4::Harness, f: impl Fn(&Cmd) -> bool) -> Vec<Cmd> {
    h.sent().into_iter().filter(|c| f(c)).collect()
}

fn offered() -> BTreeSet<(String, &'static str)> {
    let mut out = BTreeSet::new();
    for (mode, wizard) in [("browse", false), ("guide", true)] {
        for a in welcome::head_actions(wizard, true) {
            out.insert((mode.to_string(), a.id));
        }
    }
    for a in welcome::recommended_actions(true, true) {
        out.insert(("recommended".into(), a.id));
    }
    for id in welcome::GO_IDS {
        out.insert(("card".into(), id));
    }
    for a in welcome::steps_actions(ui::SCREEN_WELCOME, true) {
        out.insert(("steps".into(), a.id));
    }
    for admin in [true, false] {
        for a in welcome::finish_actions(admin) {
            out.insert((format!("finish:{admin}"), a.id));
        }
    }
    out
}

fn covered() -> BTreeSet<(String, &'static str)> {
    let mut out: BTreeSet<(String, &'static str)> = [
        ("browse", "guide"),
        ("browse", "reload"),
        ("guide", "steps"),
        ("guide", "skip"),
        ("guide", "next"),
        ("guide", "reload"),
        ("recommended", "apply"),
        ("recommended", "download_all"),
        ("card", "go_engines"),
        ("card", "go_model"),
        ("card", "go_apps"),
        ("steps", "leave"),
        ("steps", "skip"),
        ("finish:true", "finish"),
        ("finish:true", "skip"),
        ("finish:false", "finish"),
    ]
    .into_iter()
    .map(|(a, b)| (a.to_string(), b))
    .collect();
    // Every step button: clicked in `the_stepper_dialog_by_mouse`.
    for a in welcome::steps_actions(ui::SCREEN_WELCOME, true) {
        if a.id.starts_with("step") {
            out.insert(("steps".into(), a.id));
        }
    }
    out
}

#[test]
fn every_offered_setup_action_has_a_click_test() {
    let missing: Vec<_> = offered().difference(&covered()).cloned().collect();
    assert!(
        missing.is_empty(),
        "Setup actions without a click test: {missing:?}"
    );
}

#[test]
fn the_page_says_the_web_guides_words() {
    let mut h = page();
    let s = h.turns(2);
    for needle in [
        "Welcome to your gateway",
        "Check this computer",
        "Your gateway is running on this computer and you are signed in as its admin.",
        "Setup guide",
        " ↻",
        "First run:",
        "forge.local",
        "Apple M5 Max",
        "/srv/gw",
        "What this guide sets up",
        "Each step takes a minute; skip any of them.",
        "Local engines",
        "Go to local engines",
        "Choose your default model",
        "Go to default model",
        "Go to apps",
        "Recommended for this computer",
        "Use recommended defaults",
        "Download all",
        "Choices you already made are kept.",
    ] {
        assert!(s.contains(needle), "{needle:?}:\n{s}");
    }
    h.assert_fits();
    let mut h = guide();
    let s = h.turns(2);
    for needle in ["Go to a step", "Skip setup", "Next"] {
        assert!(s.contains(needle), "{needle:?}:\n{s}");
    }
    assert!(!s.contains("Setup guide "), "{s}");
}

#[test]
fn the_head_by_mouse() {
    // Setup guide (browse): into the guide at its welcome step.
    let mut h = page();
    h.ui.screen.set(ui::SCREEN_USERS);
    click_on(&mut h, "Welcome to your gateway", "Setup guide");
    assert!(h.ui.wizard.get_untracked());
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_WELCOME);
    // ↻ reads this computer and the recommended set again.
    let mut h = page();
    click_on(&mut h, "Welcome to your gateway", "↻");
    let c = h.sent();
    assert!(c.iter().any(|c| matches!(c, Cmd::LoadWelcome)), "{c:?}");
    assert!(
        c.iter().any(|c| matches!(c, Cmd::LoadAvailability)),
        "{c:?}"
    );
    // The guide: ↻, Next, Skip setup, Go to a step.
    let mut h = guide();
    click_on(&mut h, "Welcome to your gateway", "↻");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::LoadFirstRun)));
    let mut h = guide();
    click_on(&mut h, "Welcome to your gateway", "Next");
    assert_eq!(
        Some(h.ui.screen.get_untracked()),
        ui::wizard_step_after(ui::SCREEN_WELCOME)
    );
    let mut h = guide();
    click_on(&mut h, "Welcome to your gateway", "Skip setup");
    let c = sent(&mut h, |c| matches!(c, Cmd::CompleteFirstRun { .. }));
    assert!(
        matches!(&c[..], [Cmd::CompleteFirstRun { outcome, .. }] if outcome == "skipped"),
        "{c:?}"
    );
    let mut h = guide();
    let s = click_on(&mut h, "Welcome to your gateway", "Go to a step");
    let s = if s.contains("Go to a step, or leave the guide.") {
        s
    } else {
        h.turns(2)
    };
    assert!(s.contains("Go to a step, or leave the guide."), "{s}");
}

#[test]
fn the_cards_go_to_their_steps() {
    for (go, screen) in [
        ("Go to local engines", ui::SCREEN_PROVIDERS),
        ("Go to default model", ui::SCREEN_ROUTES),
        ("Go to apps", ui::SCREEN_APPS),
    ] {
        let mut h = guide();
        click_last(&mut h, go);
        assert_eq!(h.ui.screen.get_untracked(), screen, "{go}");
    }
}

#[test]
fn the_recommended_set_by_mouse() {
    let mut h = page();
    click_last(&mut h, "Use recommended defaults");
    let c = sent(&mut h, |c| matches!(c, Cmd::ApplyRecommendedRoutes { .. }));
    assert!(
        matches!(&c[..], [Cmd::ApplyRecommendedRoutes { force: false }]),
        "{c:?}"
    );
    // The second pass: the web's button under the outcome.
    h.store.apply_followup.set(Some("Replace mine too".into()));
    let s = h.turns(3);
    assert!(s.contains("routes you configured were kept"), "{s}");
    click_last(&mut h, "♻ Replace mine too");
    let c = sent(&mut h, |c| matches!(c, Cmd::ApplyRecommendedRoutes { .. }));
    assert!(
        matches!(&c[..], [Cmd::ApplyRecommendedRoutes { force: true }]),
        "{c:?}"
    );
    let s = h.turns(2);
    assert!(!s.contains("Replace mine too"), "the pass is spent:\n{s}");
    // Download all (the web starts it at once).
    let mut h = page();
    click_last(&mut h, "Download all");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::DownloadRecommended)));
}

#[test]
fn the_stepper_dialog_by_mouse() {
    // Every step button goes to its step.
    for (i, step) in ui::WIZARD_STEPS.iter().enumerate() {
        let mut h = guide();
        welcome_steps(&mut h);
        let label = format!("{}. ", i + 1);
        let s = h.turns(1);
        let (y, line) = s
            .lines()
            .enumerate()
            .find(|(_, l)| l.contains(&label) && l.contains(welcome::step_copy(*step).0))
            .unwrap_or_else(|| panic!("{label}:\n{s}"));
        let x = line[..line.find(&label).unwrap()].chars().count() + 1;
        h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
        h.turns(2);
        assert_eq!(h.ui.screen.get_untracked(), *step, "step {}", i + 1);
    }
    // Leave for now: browse, nothing recorded.
    let mut h = guide();
    welcome_steps(&mut h);
    click_last(&mut h, "Leave for now");
    assert!(!h.ui.wizard.get_untracked());
    assert!(sent(&mut h, |c| matches!(c, Cmd::CompleteFirstRun { .. })).is_empty());
    // Skip setup: recorded.
    let mut h = guide();
    welcome_steps(&mut h);
    click_last(&mut h, "Skip setup");
    let c = sent(&mut h, |c| matches!(c, Cmd::CompleteFirstRun { .. }));
    assert!(
        matches!(&c[..], [Cmd::CompleteFirstRun { outcome, .. }] if outcome == "skipped"),
        "{c:?}"
    );
}

fn welcome_steps(h: &mut r8w4::Harness) {
    click_on(h, "Welcome to your gateway", "Go to a step");
    h.turns(2);
}

/// The guide's last step (Review), the whole console mounted.
fn last_step(admin: bool) -> r8w4::Harness {
    let mut h = harness((130, 44), Mount::Root);
    h.identity(if admin { "admin" } else { "bob" }, admin);
    h.ui.wizard.set(true);
    h.ui.screen.set(ui::SCREEN_REVIEW);
    h.turns(3);
    h.sent();
    h
}

#[test]
fn the_last_steps_finish_row_by_mouse() {
    let mut h = last_step(true);
    click_on(&mut h, "Skip setup", " Finish ");
    let c = sent(&mut h, |c| matches!(c, Cmd::CompleteFirstRun { .. }));
    assert!(
        matches!(&c[..], [Cmd::CompleteFirstRun { outcome, .. }] if outcome == "finished"),
        "{c:?}"
    );
    let mut h = last_step(true);
    click_on(&mut h, " Finish ", "Skip setup");
    let c = sent(&mut h, |c| matches!(c, Cmd::CompleteFirstRun { .. }));
    assert!(
        matches!(&c[..], [Cmd::CompleteFirstRun { outcome, .. }] if outcome == "skipped"),
        "{c:?}"
    );
    // A non-admin: Leave the guide (nothing recorded).
    let mut h = last_step(false);
    click_last(&mut h, "Leave the guide");
    assert!(!h.ui.wizard.get_untracked());
    assert!(sent(&mut h, |c| matches!(c, Cmd::CompleteFirstRun { .. })).is_empty());
}

#[test]
fn start_at_login_toggles_through_the_confirm() {
    use abstractgateway_console::store::operator::StartAtLogin;
    use abstractgateway_console::worker::operator::OpCmd;
    let mut h = last_step(true);
    h.store
        .op
        .start_at_login
        .set(Loadable::Ready(StartAtLogin::from_value(&json!({
        "schema": "gateway_start_at_login_v1", "enabled": false, "state": "off",
        "mechanism": "systemd-user", "mechanism_label": "a systemd user unit",
        "can_change": true, "reason": null,
        "summary": "Off — nothing starts the gateway at login"}))));
    let s = h.turns(3);
    assert!(s.contains("●─ Start at login"), "{s}");
    click_last(&mut h, "Start at login");
    let s = h.turns(2);
    let asked = ui::w::confirm::asked();
    assert!(!asked.is_empty(), "a confirm opened:\n{s}");
    let c = h.sent();
    assert!(
        !c.iter()
            .any(|c| matches!(c, Cmd::Operator(OpCmd::SetStartAtLogin { .. }))),
        "nothing before the answer"
    );
}

#[test]
fn hovering_a_button_shows_the_web_tooltip() {
    let mut h = guide();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| {
            l.contains("Welcome to your gateway")
                .then(|| l.find("Skip setup").map(|c| (i, l[..c].chars().count())))
                .flatten()
        })
        .expect("Skip setup");
    h.key(format!("\x1b[<35;{};{}M", col + 1, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(
        s.contains("Close the guide and do not open it automatically again"),
        "{s}"
    );
}

#[test]
fn the_keyboard_reaches_the_recommended_set() {
    let mut h = page();
    h.key(b"a");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::ApplyRecommendedRoutes { force: false })));
    h.key(b"D");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::DownloadRecommended)));
    // A user: no buttons, the keys refuse.
    let mut h = harness((130, 60), Mount::Page(page_view));
    h.identity("bob", false);
    fill(&mut h);
    let s = h.turns(2);
    assert!(!s.contains("Use recommended defaults"), "{s}");
    h.key(b"a");
    h.key(b"D");
    assert!(h.sent().iter().all(|c| !matches!(
        c,
        Cmd::ApplyRecommendedRoutes { .. } | Cmd::DownloadRecommended
    )));
}

#[test]
fn the_page_fits_80x24() {
    let mut h = harness((80, 24), Mount::Page(page_view));
    h.admin();
    fill(&mut h);
    let s = h.turns(2);
    assert!(s.contains("Welcome to your gateway"), "{s}");
    h.assert_fits();
}

#[test]
fn the_words_are_the_web_guides() {
    let fx = fixture();
    let w = |k: &str| {
        fx[k]
            .as_str()
            .unwrap_or_else(|| panic!("fixture {k}"))
            .to_string()
    };
    assert_eq!(welcome::TITLE, w("title"));
    assert_eq!(welcome::HINT, w("hint"));
    assert_eq!(welcome::ADMIN_LEDE, w("admin_lede"));
    assert_eq!(welcome::SKIP_TIP, w("skip_tip"));
    assert_eq!(welcome::GUIDE_TIP, w("guide_tip"));
    assert_eq!(welcome::SETS_UP, w("sets_up"));
    assert_eq!(welcome::SETS_UP_SUB, w("sets_up_sub"));
    assert_eq!(welcome::APPLY, w("apply"));
    assert_eq!(welcome::DOWNLOAD_ALL, w("download_all"));
    assert_eq!(welcome::RECOMMENDED_NOTE, w("recommended_note"));
    for (i, (_, title, lede, go)) in welcome::step_cards().into_iter().enumerate() {
        assert_eq!(title, w(&format!("card_{i}_title")));
        assert_eq!(lede, w(&format!("card_{i}_lede")));
        assert_eq!(go, w(&format!("card_{i}_go")));
    }
    let labels = |acts: Vec<ui::w::Action>| acts.into_iter().map(|a| a.label).collect::<Vec<_>>();
    assert!(labels(welcome::head_actions(true, true)).contains(&w("skip")));
    assert!(labels(welcome::head_actions(true, true)).contains(&w("next")));
    assert_eq!(
        labels(welcome::finish_actions(true)),
        vec![w("finish"), w("skip")]
    );
    assert_eq!(welcome::text_model_now(None), w("no_text_model"));
    let mut h = page();
    h.store.apply_followup.set(Some(w("replace")));
    let s = h.turns(3);
    assert!(s.contains(&w("replace")), "{s}");
    for k in ["recommended", "sets_up", "sets_up_sub"] {
        assert!(s.contains(&w(k)), "{k}:\n{s}");
    }
}

#[test]
fn the_stepper_dialog_survives_the_reloads_under_it() {
    let mut h = guide();
    welcome_steps(&mut h);
    h.store.welcome.set(Loadable::Ready(summary()));
    h.store.availability.set(Loadable::Ready(plan()));
    let s = h.turns(3);
    assert!(
        s.contains("Go to a step, or leave the guide.") && s.contains("Leave for now"),
        "{s}"
    );
}
