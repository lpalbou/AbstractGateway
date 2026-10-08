//! R15 (DESIGN-TUI.md §3.7): a synthesized mouse click for EVERY Providers
//! control — each engine row's buttons (Install / Install for all users,
//! Start, Stop, Browse models, Cancel, Set up connection / Edit / Override
//! / Add connection, Download page, Learn more, Show details) and Check
//! again; each remote preset's Configure; each Available Providers row's
//! Edit / Delete / Override / Models / Test and Add connection; the
//! endpoint form's ↻ Test / ✓ Confirm / Cancel — through the real input
//! pipeline. Installs ask with w::Confirm (answered by mouse). The
//! meta-test enumerates `engine_actions` / `preset_actions` /
//! `profile_actions` over the fixture: an action without a click test is
//! RED. The words are the web's (`tests/fixtures/r15_web_wording_providers.json`).

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::store::json::WriteState;
use abstractgateway_console::store::{Loadable, ProfilesData};
use abstractgateway_console::ui::providers::{self, engines};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn page_view(
    ctx: &abstractgateway_console::ui::Ctx,
    cx: abstracttui::prelude::Scope,
) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    providers::view(cx, ctx, &t)
}

fn profiles() -> Value {
    json!({"ok": true, "can_create_gateway_scope": true, "profiles": [
        {"id": "acme", "virtual_provider": "endpoint:acme", "display_name": "acme",
         "description": "test endpoint", "provider_family": "openai-compatible",
         "base_url": "http://127.0.0.1:9999/v1", "base_url_configured": true,
         "api_key_set": true, "api_key_fingerprint": "deadbeef1234", "scope": "gateway",
         "allowed_models": [], "enabled": true},
        {"id": "openai", "provider_id": "openai", "display_name": "OpenAI",
         "description": "env key", "provider_family": "openai", "base_url": "",
         "api_key_set": true, "api_key_fingerprint": "cafecafe0000", "scope": "environment",
         "enabled": true, "managed": false, "synthetic": true, "source": "environment"},
        {"id": "ollama", "provider_id": "ollama", "display_name": "Ollama",
         "description": "", "provider_family": "ollama", "base_url": "http://127.0.0.1:11434",
         "api_key_set": false, "scope": "environment", "enabled": true, "managed": false,
         "synthetic": true, "source": "environment"}
    ]})
}

fn engines_payload() -> Value {
    json!({"schema": "gateway_engines_v2", "install_allowed": true,
    "generated_at": "2026-10-04T02:25:51Z",
    "engines": [
        {"id": "llamacpp", "name": "llama.cpp", "supported": true, "installed": false,
         "description": "GGUF models in the gateway's own Python.",
         "install": {"method": "wheel", "notes": "Installs llama-cpp-python into the gateway's environment.",
                     "steps": ["pip install", "verify import"], "needs_admin": false,
                     "command_preview": ["pip install llama-cpp-python"]},
         "actions": [{"id": "install", "label": "Install", "enabled": true}], "active_job": null},
        {"id": "ollama", "name": "Ollama", "supported": true, "installed": true, "running": true,
         "reachable": true, "provider": "ollama", "base_url": "http://127.0.0.1:11434",
         "install": {"method": "app"},
         "actions": [{"id": "stop", "label": "Stop", "enabled": true},
                     {"id": "docs", "url": "https://docs.ollama.com", "enabled": true}]},
        {"id": "lmstudio", "name": "LM Studio", "supported": true, "installed": false, "provider": "lmstudio",
         "install": {"method": "app", "needs_admin": false},
         "actions": [{"id": "install", "label": "Install", "enabled": true},
                     {"id": "docs", "url": "https://lmstudio.ai/docs", "enabled": true}]},
        {"id": "vllm", "name": "vLLM", "supported": true, "installed": true, "running": false, "provider": "vllm",
         "install": {"method": "wheel"},
         "actions": [{"id": "start", "label": "Start", "enabled": true},
                     {"id": "open_page", "url": "https://docs.vllm.ai", "enabled": true},
                     {"id": "recheck", "enabled": true}]},
        {"id": "mlx", "name": "MLX (mlx-lm)", "supported": true, "installed": false, "running": null,
         "install": {"method": "wheel"},
         "actions": [{"id": "install", "label": "Install", "enabled": true}]}
    ]})
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_providers.json"
    );
    serde_json::from_str(&std::fs::read_to_string(p).expect(p)).expect("fixture JSON")
}

fn page() -> r8w4::Harness {
    let mut h = harness((150, 90), Mount::Page(page_view));
    h.admin();
    h.store
        .profiles
        .set(Loadable::Ready(ProfilesData::from_value(&profiles())));
    h.store
        .json
        .set(engines::SLOT_ENGINES, Loadable::Ready(engines_payload()));
    h.turns(3);
    h.sent();
    h
}

fn sends(h: &mut r8w4::Harness) -> Vec<(String, Value)> {
    h.sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Send { path, body, .. }) => Some((path, body)),
            _ => None,
        })
        .collect()
}

/// Click `needle` on the line of `name` (searched after the name).
fn click_row(h: &mut r8w4::Harness, name: &str, needle: &str) -> String {
    click_row_after(h, "Local providers", name, needle)
}

/// [`click_row`] among the lines below the first line holding `anchor`
/// (a section heading).
fn click_row_after(h: &mut r8w4::Harness, anchor: &str, name: &str, needle: &str) -> String {
    let s = h.turns(1);
    let from = s
        .lines()
        .position(|l| l.contains(anchor))
        .unwrap_or_else(|| panic!("{anchor:?}:\n{s}"));
    let (y, line) = s
        .lines()
        .enumerate()
        .skip(from)
        .find(|(_, l)| l.starts_with(&format!(" {name} ")) && l[name.len()..].contains(needle))
        .unwrap_or_else(|| panic!("{needle:?} on {name}'s row:\n{s}"));
    let b = name.len() + 1 + line[name.len() + 1..].find(needle).unwrap();
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// The screen's words on one line (a dialog's wrapped sentence whole).
fn flat(s: &str) -> String {
    s.lines()
        .map(|l| l.trim_matches(|c: char| c == '│' || c == '┃' || c.is_whitespace()))
        .collect::<Vec<_>>()
        .join(" ")
}

fn click_pair(h: &mut r8w4::Harness, label: &str, other: &str) -> String {
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

fn offered() -> BTreeSet<(String, &'static str)> {
    let mut out = BTreeSet::new();
    let profs = ProfilesData::from_value(&profiles()).profiles;
    // engine_actions reads the location plans from a store: a scratch one.
    let h = page();
    for e in engines_payload()["engines"].as_array().unwrap() {
        for a in engines::engine_actions(&h.store, e, None, true, &profs) {
            out.insert((e["id"].as_str().unwrap().to_string(), a.id));
        }
    }
    for (id, label, ..) in providers::REMOTE_PRESETS {
        for a in providers::preset_actions(label) {
            out.insert((format!("preset:{id}"), a.id));
        }
    }
    for p in &profs {
        for a in providers::profile_actions(p) {
            out.insert((format!("profile:{}", p.id), a.id));
        }
    }
    out
}

fn covered() -> BTreeSet<(String, &'static str)> {
    let mut v: Vec<(String, &'static str)> = [
        ("llamacpp", "install"),
        ("llamacpp", "models"),
        ("lmstudio", "models"),
        ("mlx", "models"),
        ("ollama", "models"),
        ("ollama", "stop"),
        ("ollama", "connection"),
        ("ollama", "connect"),
        ("ollama", "learn"),
        ("lmstudio", "install"),
        ("lmstudio", "install_system"),
        ("lmstudio", "connect"),
        ("lmstudio", "learn"),
        ("vllm", "start"),
        ("vllm", "connect"),
        ("vllm", "download"),
        ("vllm", "refresh"),
        ("mlx", "install"),
        ("profile:acme", "edit"),
        ("profile:acme", "delete"),
        ("profile:acme", "models"),
        ("profile:acme", "test"),
        ("profile:openai", "override"),
        ("profile:openai", "models"),
        ("profile:openai", "test"),
        ("profile:ollama", "override"),
        ("profile:ollama", "models"),
        ("profile:ollama", "test"),
    ]
    .into_iter()
    .map(|(a, b)| (a.to_string(), b))
    .collect();
    for (id, ..) in providers::REMOTE_PRESETS {
        v.push((format!("preset:{id}"), "configure"));
    }
    v.into_iter().collect()
}

#[test]
fn every_offered_provider_action_has_a_click_test() {
    let missing: Vec<_> = offered().difference(&covered()).cloned().collect();
    assert!(
        missing.is_empty(),
        "Providers actions without a click test: {missing:?}"
    );
}

#[test]
fn the_three_sections_stack_with_the_web_words() {
    let mut h = page();
    let s = h.turns(2);
    for needle in [
        "Providers",
        "Local engines and remote provider connections",
        "Local providers",
        "Engines that run models on this computer, and their server connections.",
        "Check again",
        "Remote providers",
        "Cloud accounts and OpenAI-compatible servers.",
        "Available Providers",
        "Configured providers available to this Gateway principal.",
        "endpoint:acme",
        "Add connection",
    ] {
        assert!(s.contains(needle), "{needle:?}:\n{s}");
    }
}

#[test]
fn a_wheel_install_asks_then_posts_auto() {
    let mut h = page();
    let s = flat(&click_row(&mut h, "llama.cpp", "Install"));
    assert!(
        s.contains("Install llama.cpp on this machine?")
            && s.contains("Installs llama-cpp-python into the gateway's environment."),
        "{s}"
    );
    assert!(sends(&mut h).is_empty(), "nothing before the answer");
    click_pair(&mut h, "Install now", "Not now");
    assert!(sends(&mut h)
        .iter()
        .any(|(p, b)| p == "/engines/llamacpp/install"
            && b == &json!({"dry_run": false, "location": "auto"})));
    let mut h = page();
    click_row(&mut h, "MLX (mlx-lm)", "Install");
    click_pair(&mut h, "Install now", "Not now");
    assert!(sends(&mut h)
        .iter()
        .any(|(p, _)| p == "/engines/mlx/install"));
}

#[test]
fn an_app_install_reads_the_plans_then_asks_for_its_location() {
    for (needle, loc, button) in [
        (
            "Install for all users",
            "system",
            "Install for all users (administrator)",
        ),
        ("Install ", "user", "Install"),
    ] {
        let mut h = page();
        click_row(&mut h, "LM Studio", needle);
        let dry: Vec<Value> = sends(&mut h)
            .into_iter()
            .filter(|(p, _)| p == "/engines/lmstudio/install")
            .map(|(_, b)| b)
            .collect();
        assert_eq!(
            dry,
            vec![
                json!({"dry_run": true, "location": "user"}),
                json!({"dry_run": true, "location": "system"})
            ]
        );
        h.store.json.set_write(
            &engines::plan_key("lmstudio", "user"),
            Some(WriteState::Done(
                json!({"plan": {"target": "/Users/me/Applications/LM Studio.app"}}),
            )),
        );
        h.store.json.set_write(
            &engines::plan_key("lmstudio", "system"),
            Some(WriteState::Done(
                json!({"plan": {"target": "/Applications/LM Studio.app", "needs_admin": true}}),
            )),
        );
        let s = h.turns(3);
        assert!(s.contains("Install LM Studio on this machine?"), "{s}");
        click_pair(&mut h, button, "Not now");
        assert!(
            sends(&mut h)
                .iter()
                .any(|(p, b)| p == "/engines/lmstudio/install"
                    && b == &json!({"dry_run": false, "location": loc})),
            "{loc}"
        );
    }
}

#[test]
fn the_other_engine_buttons() {
    let mut h = page();
    click_row(&mut h, "Ollama", "Stop");
    assert!(sends(&mut h)
        .iter()
        .any(|(p, _)| p == "/engines/ollama/stop"));
    let mut h = page();
    click_row(&mut h, "Ollama", "Browse models");
    assert_eq!(
        h.ui.screen.get_untracked(),
        abstractgateway_console::ui::SCREEN_CATALOG
    );
    let mut h = page();
    let s = click_row(&mut h, "Ollama", "Override");
    assert!(s.contains("Configure Ollama"), "{s}");
    let mut h = page();
    let s = click_row(&mut h, "Ollama", "Add connection");
    assert!(s.contains("Configure Ollama"), "{s}");
    let mut h = page();
    click_row(&mut h, "Ollama", "Learn more");
    assert!(h
        .store
        .notice
        .get_untracked()
        .unwrap_or_default()
        .contains("https://docs.ollama.com"));
    let mut h = page();
    let s = click_row(&mut h, "LM Studio", "Set up connection");
    assert!(s.contains("Configure LM Studio"), "{s}");
    let mut h = page();
    click_row(&mut h, "LM Studio", "Learn more");
    assert!(h
        .store
        .notice
        .get_untracked()
        .unwrap_or_default()
        .contains("lmstudio.ai"));
    let mut h = page();
    click_row(&mut h, "vLLM", "Start");
    assert!(sends(&mut h)
        .iter()
        .any(|(p, _)| p == "/engines/vllm/start"));
    let mut h = page();
    let s = click_row(&mut h, "vLLM", "Set up connection");
    assert!(s.contains("Configure"), "{s}");
    let mut h = page();
    click_row(&mut h, "vLLM", "Download page");
    assert!(h
        .store
        .notice
        .get_untracked()
        .unwrap_or_default()
        .contains("docs.vllm.ai"));
    let mut h = page();
    click_row(&mut h, "vLLM", "I installed it, check again");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::Json(JsonCmd::Get { path, .. }) if path == "/engines?probe=1")));
    // Check again (the section's own button).
    let mut h = page();
    h.click_text("Check again");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::Json(JsonCmd::Get { path, .. }) if path == "/engines?probe=1")));
}

#[test]
fn every_preset_opens_its_configure_form() {
    for (_, label, ..) in providers::REMOTE_PRESETS {
        let mut h = page();
        let s = click_row(&mut h, label, "Configure");
        assert!(s.contains(&format!("Configure {label}")), "{label}:\n{s}");
    }
}

#[test]
fn available_rows_edit_delete_override_models_test() {
    let mut h = page();
    let s = click_row(&mut h, "acme", "Edit");
    assert!(
        s.contains("Configure Custom OpenAI-compatible") && s.contains("Provider type"),
        "{s}"
    );
    let mut h = page();
    let s = click_row(&mut h, "acme", "Delete");
    assert!(
        flat(&s).contains("Delete endpoint:acme? Existing workflows"),
        "{s}"
    );
    click_pair(&mut h, "Delete endpoint", "Cancel");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::DeleteProfile { id } if id == "acme")));
    for name in ["acme", "OpenAI", "Ollama"] {
        let mut h = page();
        let s = click_row(&mut h, name, "Models");
        assert!(s.contains("Models — "), "{name}:\n{s}");
        let mut h = page();
        click_row(&mut h, name, "Test");
        let _ = h.sent();
    }
    for name in ["OpenAI", "Ollama"] {
        let mut h = page();
        let s = click_row_after(&mut h, "Available Providers", name, "Override");
        assert!(
            flat(&s).contains("already usable from environment config"),
            "{name}:\n{s}"
        );
    }
    let mut h = page();
    let s = h.click_after("Available Providers", "Add connection");
    assert!(s.contains("Configure provider"), "{s}");
}

#[test]
fn the_endpoint_form_tests_confirms_and_asks_before_dropping() {
    let mut h = page();
    click_row(&mut h, "acme", "Edit");
    click_pair(&mut h, "↻ Test", "✓ Confirm");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::DiscoverModels { body } if body.0["provider_family"] == "openai-compatible")));
    click_pair(&mut h, "✓ Confirm", "↻ Test");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::SaveProfile { create: false, id, .. } if id == "acme")));
    let mut h = page();
    click_row(&mut h, "acme", "Edit");
    h.click_text("Enabled");
    let s = click_pair(&mut h, "Cancel", "↻ Test");
    assert!(s.contains("Discard changes?"), "{s}");
}

#[test]
fn hovering_a_button_says_its_tooltip() {
    let mut h = page();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| {
            l.starts_with(" llama.cpp ")
                .then(|| l.find("Install").map(|c| (i, l[..c].chars().count())))
                .flatten()
        })
        .expect("llama.cpp Install");
    h.key(format!("\x1b[<35;{};{}M", col + 2, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(s.contains("Install llama.cpp on this computer  (i)"), "{s}");
}

#[test]
fn the_keyboard_reaches_the_engine_and_provider_rows() {
    let mut h = page();
    // The engines table has the keyboard: ↓ to Ollama, x stops it.
    let order: Vec<String> = engines::ordered_engines(&engines_payload())
        .iter()
        .map(|e| e["id"].as_str().unwrap().to_string())
        .collect();
    let at = order.iter().position(|i| i == "ollama").unwrap();
    for _ in 0..at {
        h.key(b"\x1b[B");
    }
    h.key(b"x");
    assert!(sends(&mut h)
        .iter()
        .any(|(p, _)| p == "/engines/ollama/stop"));
    let s = h.key(b"p");
    assert!(s.contains("Configure OpenAI"), "{s}");
}

#[test]
fn fits_at_80x24() {
    let mut h = harness((80, 24), Mount::Page(page_view));
    h.admin();
    h.store
        .profiles
        .set(Loadable::Ready(ProfilesData::from_value(&profiles())));
    h.store
        .json
        .set(engines::SLOT_ENGINES, Loadable::Ready(engines_payload()));
    let s = h.turns(3);
    assert!(s.contains("Local providers") && s.contains("Ollama"), "{s}");
    h.assert_fits();
}

#[test]
fn the_words_are_the_webs() {
    let fx = fixture();
    let w = |k: &str| {
        fx[k]
            .as_str()
            .unwrap_or_else(|| panic!("fixture {k}"))
            .to_string()
    };
    assert_eq!(providers::SECTIONS[0], w("section_local"));
    assert_eq!(providers::SECTIONS[1], w("section_remote"));
    assert_eq!(providers::SECTIONS[2], w("section_available"));
    assert_eq!(providers::SECTION_NOTES[0], w("note_local"));
    assert_eq!(providers::SECTION_NOTES[1], w("note_remote"));
    assert_eq!(providers::SECTION_NOTES[2], w("note_available"));
    assert_eq!(providers::FORM_DESCRIPTION, w("form_description"));
    assert_eq!(providers::KEY_PLACEHOLDER, w("key_placeholder"));
    assert_eq!(providers::BASE_URL_PLACEHOLDER, w("base_url_placeholder"));
    assert_eq!(
        providers::DESCRIPTION_PLACEHOLDER,
        w("description_placeholder")
    );
    assert_eq!(providers::VISIBLE_MODELS, w("visible_models"));
    assert_eq!(providers::VISIBLE_MODELS_HELP, w("visible_models_help"));
    assert_eq!(providers::CLEAR_RESTRICTION_TIP, w("clear_restriction_tip"));
    assert_eq!(providers::TEST_TIP, w("test_tip"));
    assert_eq!(providers::CONFIRM_TIP, w("confirm_tip"));
    assert_eq!(providers::SCOPES[0], w("scope_gateway"));
    assert_eq!(providers::SCOPES[1], w("scope_user"));
    assert_eq!(engines::CONNECT_TIP, w("connect_tip"));
    let p = ProfilesData::from_value(&profiles()).profiles[0].clone();
    assert_eq!(
        providers::delete_question(&p),
        w("delete_confirm").replace("{n}", "endpoint:acme")
    );
    let labels = |acts: Vec<abstractgateway_console::ui::w::Action>| -> Vec<String> {
        acts.into_iter().map(|a| a.label).collect()
    };
    let profs = ProfilesData::from_value(&profiles()).profiles;
    assert!(labels(providers::profile_actions(&profs[0])).starts_with(&[w("edit"), w("delete")]));
    assert_eq!(
        labels(providers::profile_actions(&profs[1]))[0],
        w("override")
    );
}
