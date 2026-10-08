//! R15 (DESIGN-TUI.md §3.9, §6.1): a synthesized mouse click for EVERY
//! Models control — each artifact row's actions (Download / Try again,
//! Use as default, the ⌫ trash, Cancel), the head's Check again, the
//! Catalog | Hugging Face segments, the Search button, the "Fits this
//! computer" toggle, the four filter Selects and Clear filters — through
//! the real input pipeline. Each click is asserted on the JSON-lane request
//! the worker would receive (route + body) or the confirmation it opens;
//! confirmations are answered BY MOUSE. The meta-test enumerates
//! `catalog::row_actions` over the fixture rows (and a running download, a
//! build that cannot be downloaded): an action without a click test here
//! is RED. The words are the web's (`tests/fixtures/r15_web_wording_models.json`).

mod r8w4;

use std::collections::{BTreeSet, HashMap, HashSet};

use abstractgateway_console::store::json::WriteState;
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::{self, catalog};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    catalog::view(cx, ctx, &t)
}

fn fixture(name: &str) -> Value {
    let path = format!(
        "{}/tests/fixtures/r7w2_models_{name}.json",
        env!("CARGO_MANIFEST_DIR")
    );
    serde_json::from_str(&std::fs::read_to_string(&path).expect("fixture")).expect("json")
}

fn wording() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_models.json"
    );
    let text = std::fs::read_to_string(p)
        .unwrap_or_else(|e| panic!("{p}: {e} — run scripts/extract_web_wording.py --write"));
    serde_json::from_str(&text).expect("fixture JSON")
}

fn page_as(size: (i32, i32), admin: bool) -> r8w4::Harness {
    catalog::reset_state();
    let mut h = harness(size, Mount::Page(page_view));
    if admin {
        h.admin();
    } else {
        h.identity("alice", false);
    }
    h.turns(2);
    let j = h.store.json;
    j.set(catalog::K_CATALOG, Loadable::Ready(fixture("catalog")));
    j.set(catalog::K_INSTALLED, Loadable::Ready(fixture("installed")));
    j.set(catalog::K_DEFAULTS, Loadable::Ready(fixture("defaults")));
    j.set(
        catalog::K_DOWNLOADS,
        Loadable::Ready(json!({"ok": true, "jobs": []})),
    );
    h.turns(3);
    h.sent();
    h
}

fn page() -> r8w4::Harness {
    page_as((140, 44), true)
}

/// (key, method, path, body) of every JSON-lane write sent.
fn writes(h: &mut r8w4::Harness) -> Vec<(String, String, String, Value)> {
    h.sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Send {
                key,
                method,
                path,
                body,
                ..
            }) => Some((key, method, path, body)),
            _ => None,
        })
        .collect()
}

/// GET paths sent.
fn reads(h: &mut r8w4::Harness) -> Vec<String> {
    h.sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Get { path, .. }) => Some(path),
            _ => None,
        })
        .collect()
}

fn click_at(h: &mut r8w4::Harness, x: usize, y: usize) -> String {
    h.key(format!("\x1b[<0;{};{}M\x1b[<0;{};{}m", x + 1, y + 1, x + 1, y + 1).as_bytes())
}

/// Click `needle` on `artifact`'s table row (searched right of the id).
fn click_row(h: &mut r8w4::Harness, artifact: &str, needle: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .find(|(_, l)| l.starts_with(&format!(" {artifact} ")))
        .unwrap_or_else(|| panic!("{artifact} row:\n{screen}"));
    let start = line.find(artifact).unwrap() + artifact.len();
    let byte = start
        + line[start..]
            .find(needle)
            .unwrap_or_else(|| panic!("{needle:?} not on {artifact}'s row:\n{screen}"));
    let x = line[..byte].chars().count();
    click_at(h, x, y)
}

/// Click the first occurrence of `text` (its first cell + 1).
fn click_text(h: &mut r8w4::Harness, text: &str) -> String {
    let s = h.turns(1);
    let (y, x) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| l.find(text).map(|c| (i, l[..c].chars().count() + 1)))
        .unwrap_or_else(|| panic!("{text:?} not on screen:\n{s}"));
    click_at(h, x, y)
}

/// Click the `label` button of the open confirmation.
fn click_confirm(h: &mut r8w4::Harness, label: &str, other: &str) -> String {
    let s = h.turns(1);
    let (y, line) = s
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(&format!(" {label} ")) && l.contains(&format!(" {other} ")))
        .last()
        .unwrap_or_else(|| panic!("no [{label}] [{other}] button row:\n{s}"));
    let b = line.rfind(&format!(" {label} ")).unwrap() + 1;
    let x = line[..b].chars().count();
    click_at(h, x, y)
}

/// Pick `option` in the filter Select labelled `group`, by mouse.
fn pick(h: &mut r8w4::Harness, group: &str, option: &str) -> String {
    let s = h.turns(1);
    let lines: Vec<&str> = s.lines().collect();
    let (y, x) = lines
        .iter()
        .enumerate()
        .find_map(|(i, l)| {
            l.find(&format!("{group} ▐"))
                .map(|c| (i, l[..c].chars().count() + group.chars().count() + 2))
        })
        .unwrap_or_else(|| panic!("no {group} filter:\n{s}"));
    let s = click_at(h, x, y);
    let (oy, ox) = s
        .lines()
        .enumerate()
        .filter(|(i, _)| *i > y)
        .find_map(|(i, l)| {
            l.find(&format!(" {option}"))
                .map(|c| (i, l[..c].chars().count() + 1))
        })
        .unwrap_or_else(|| panic!("no {option:?} in the {group} popup:\n{s}"));
    click_at(h, ox, oy)
}

fn split(k: &str) -> (&str, &str) {
    k.split_once('/').expect("provider/artifact")
}

// ---- the meta-test --------------------------------------------------------

/// Every action the page can offer on the fixture rows, a running
/// download, and a build that cannot be downloaded here.
fn offered() -> BTreeSet<(String, &'static str)> {
    let mut out = BTreeSet::new();
    for (k, acts) in catalog::offered_actions(
        &fixture("catalog"),
        &fixture("installed"),
        &fixture("defaults"),
        true,
    ) {
        for a in acts {
            if a.is_enabled() {
                out.insert((k.clone(), a.id));
            }
        }
    }
    for (k, a) in [running_job_actions(), unavailable_actions()] {
        for x in a {
            if x.is_enabled() {
                out.insert((k.clone(), x.id));
            }
        }
    }
    out
}

fn running_job_actions() -> (String, Vec<abstractgateway_console::ui::w::Action>) {
    let a = json!({"provider": "ollama", "artifact": "qwen3:0.6b-q8_0", "downloadable": true});
    let mut jobs = HashMap::new();
    jobs.insert(
        "ollama/qwen3:0.6b-q8_0".to_string(),
        json!({"job": "dl_1", "provider": "ollama", "artifact": "qwen3:0.6b-q8_0", "status": "running"}),
    );
    let deleted = HashSet::new();
    let cv = catalog::Cv {
        data: None,
        catalog: None,
        installed: None,
        jobs: &jobs,
        deleted: &deleted,
    };
    let st = catalog::PageState::default();
    let row = json!({"capabilities": {"text": true}});
    (
        "ollama/qwen3:0.6b-q8_0".into(),
        catalog::row_actions(&cv, &st, Some(&row), &a, true, &None),
    )
}

fn unavailable_actions() -> (String, Vec<abstractgateway_console::ui::w::Action>) {
    let a = json!({"provider": "mlx", "artifact": "x/y", "downloadable": false, "supported_on_host": false});
    let (jobs, deleted) = (HashMap::new(), HashSet::new());
    let cv = catalog::Cv {
        data: None,
        catalog: None,
        installed: None,
        jobs: &jobs,
        deleted: &deleted,
    };
    let st = catalog::PageState::default();
    let row = json!({"capabilities": {"text": true}});
    (
        "mlx/x/y".into(),
        catalog::row_actions(&cv, &st, Some(&row), &a, true, &None),
    )
}

fn covered() -> BTreeSet<(String, &'static str)> {
    let mut out: BTreeSet<(String, &'static str)> = [
        ("mlx/mlx-community/Qwen3-0.6B-4bit", "default"),
        ("mlx/mlx-community/Qwen3-0.6B-4bit", "delete"),
        ("ollama/qwen3:0.6b", "default"),
        ("ollama/qwen3:0.6b", "delete"),
        ("ollama/my-finetune:7b", "delete"),
        ("mlx/mlx-community/my-own-tune-4bit", "delete"),
        // a_running_downloads_cancel_asks_then_stops_it
        ("ollama/qwen3:0.6b-q8_0", "cancel"),
    ]
    .into_iter()
    .map(|(k, id)| (k.to_string(), id))
    .collect();
    for k in DOWNLOADABLE {
        out.insert((k.to_string(), "download"));
    }
    out
}

/// The fixture's downloadable rows (each Download is clicked below).
const DOWNLOADABLE: [&str; 12] = [
    "ollama/qwen3:0.6b-q8_0",
    "mlx/mlx-community/Qwen3-0.6B-8bit",
    "huggingface/unsloth/Qwen3-0.6B-GGUF:Q4_K_M",
    "huggingface/unsloth/Qwen3-0.6B-GGUF:Q8_0",
    "lmstudio/text-embedding-nomic-embed-text-v1.5",
    "ollama/nomic-embed-text",
    "huggingface/nomic-ai/nomic-embed-text-v1.5-GGUF:Q4_K_M",
    "huggingface/nomic-ai/nomic-embed-text-v1.5-GGUF:Q8_0",
    "mlx-gen/AbstractFramework/flux.2-klein-4b-8bit",
    "diffusers/black-forest-labs/FLUX.2-klein-4B",
    "mlx/mlx-community/minimax-m2",
    "lmstudio/minimax/minimax-m2",
];

#[test]
fn every_offered_models_action_has_a_click_test() {
    let missing: Vec<_> = offered().difference(&covered()).cloned().collect();
    assert!(
        missing.is_empty(),
        "Models actions without a click test: {missing:?}"
    );
}

// ---- row actions ------------------------------------------------------------

#[test]
fn clicking_each_download_sends_its_download() {
    for k in DOWNLOADABLE {
        let mut h = page();
        let (provider, artifact) = split(k);
        click_row(&mut h, artifact, "Download");
        let w = writes(&mut h);
        let (key, method, path, body) = w
            .iter()
            .find(|(key, ..)| key.starts_with("catalog.download:"))
            .unwrap_or_else(|| panic!("{k}: no download sent ({w:?})"));
        assert_eq!(key, &format!("catalog.download:{k}"));
        assert_eq!(
            (method.as_str(), path.as_str()),
            ("POST", "/models/download")
        );
        assert_eq!(body["provider"], provider, "{k}");
        assert_eq!(body["artifact"], artifact, "{k}");
    }
}

#[test]
fn clicking_use_as_default_puts_the_default_text_model() {
    for k in ["mlx/mlx-community/Qwen3-0.6B-4bit", "ollama/qwen3:0.6b"] {
        let mut h = page();
        let (provider, artifact) = split(k);
        click_row(&mut h, artifact, "Use as default");
        let w = writes(&mut h);
        let (_, method, path, body) = w
            .iter()
            .find(|(key, ..)| key == "catalog.default")
            .unwrap_or_else(|| panic!("{k}: no default sent ({w:?})"));
        assert_eq!(
            (method.as_str(), path.as_str()),
            ("PUT", "/config/capability-defaults/output/text")
        );
        assert_eq!(body["provider"], provider);
        assert_eq!(
            body["model"],
            json!(catalog::served_model_id(provider, artifact))
        );
    }
}

#[test]
fn clicking_the_trash_asks_the_dry_run_then_the_confirm_deletes() {
    for k in [
        "mlx/mlx-community/Qwen3-0.6B-4bit",
        "ollama/qwen3:0.6b",
        "ollama/my-finetune:7b",
        "mlx/mlx-community/my-own-tune-4bit",
    ] {
        let mut h = page();
        let (provider, artifact) = split(k);
        click_row(&mut h, artifact, "⌫");
        let w = writes(&mut h);
        let (_, _, path, body) = w
            .iter()
            .find(|(key, ..)| *key == format!("catalog.delplan:{k}"))
            .unwrap_or_else(|| panic!("{k}: no dry run ({w:?})"));
        assert_eq!(path, catalog::DELETE_URL);
        assert_eq!(
            body,
            &json!({"provider": provider, "artifact": artifact, "dry_run": true})
        );
        h.store.json.set_write(
            &format!("catalog.delplan:{k}"),
            Some(WriteState::Done(
                json!({"ok": true, "freed_bytes": 351000000}),
            )),
        );
        let s = h.turns(3);
        assert!(
            s.contains(
                "Deletes 351 MB from this computer. Files only — nothing in your runs is touched."
            ),
            "{k}: the confirm sentence\n{s}"
        );
        // [Delete] [Keep], answered by mouse.
        click_confirm(&mut h, "Delete", "Keep");
        let w = writes(&mut h);
        let (_, _, _, body) = w
            .iter()
            .find(|(key, ..)| *key == format!("catalog.delete:{k}"))
            .unwrap_or_else(|| panic!("{k}: no delete after [Delete] ({w:?})"));
        assert_eq!(body["dry_run"], json!(false));
    }
}

#[test]
fn keep_and_escape_on_the_delete_confirm_send_nothing() {
    let k = "ollama/qwen3:0.6b";
    let mut h = page();
    click_row(&mut h, "qwen3:0.6b", "⌫");
    h.sent();
    let answer = |h: &mut r8w4::Harness| {
        h.store.json.set_write(
            &format!("catalog.delplan:{k}"),
            Some(WriteState::Done(json!({"ok": true, "freed_bytes": 1000}))),
        );
        h.turns(3)
    };
    answer(&mut h);
    let s = click_confirm(&mut h, "Keep", "Delete");
    assert!(!s.contains("Files only — nothing"), "{s}");
    assert!(writes(&mut h).is_empty(), "Keep sends nothing");
    // The trash is back (no delete in progress): a new click asks again.
    click_row(&mut h, "qwen3:0.6b", "⌫");
    assert!(writes(&mut h)
        .iter()
        .any(|(key, ..)| *key == format!("catalog.delplan:{k}")));
    answer(&mut h);
    // A destructive confirm opens on Keep: Enter keeps.
    let s = h.key(b"\r");
    assert!(!s.contains("Files only — nothing"), "{s}");
    assert!(
        writes(&mut h).is_empty(),
        "Enter on the default (Keep) sends nothing"
    );
}

#[test]
fn a_running_downloads_cancel_asks_then_stops_it() {
    let mut h = page();
    click_row(&mut h, "qwen3:0.6b-q8_0", "Download");
    let job = json!({"job": "dl_1", "provider": "ollama", "artifact": "qwen3:0.6b-q8_0",
                     "status": "running", "state": "downloading", "percent": 10.0});
    h.store.json.set_write(
        "catalog.download:ollama/qwen3:0.6b-q8_0",
        Some(WriteState::Done(json!({"ok": true, "job": job}))),
    );
    h.turns(3);
    h.sent();
    let s = click_row(&mut h, "qwen3:0.6b-q8_0", "Cancel");
    assert!(s.contains("Stop this download?"), "{s}");
    h.shoot("models-cancel-confirm");
    assert!(writes(&mut h).is_empty(), "a click only asks");
    click_confirm(&mut h, "Stop download", "Keep downloading");
    let w = writes(&mut h);
    let (_, _, path, body) = w
        .iter()
        .find(|(key, ..)| key == "catalog.cancel:dl_1")
        .unwrap_or_else(|| panic!("no cancel ({w:?})"));
    assert_eq!(path, "/models/download/dl_1/cancel");
    assert_eq!(body, &json!({"via": "console"}));
}

#[test]
fn a_refused_action_says_why_and_sends_nothing() {
    let mut h = page_as((140, 44), false);
    click_row(&mut h, "qwen3:0.6b-q8_0", "Download");
    assert!(writes(&mut h).is_empty());
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("Only an admin can download models")
    );
    click_row(&mut h, "qwen3:0.6b", "⌫");
    assert!(writes(&mut h).is_empty());
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("Only an admin can delete downloaded models")
    );
}

// ---- head, bar, filters -------------------------------------------------------

#[test]
fn check_again_rereads_the_page() {
    let mut h = page();
    click_text(&mut h, "Check again");
    let r = reads(&mut h);
    for p in [
        "/models/catalog",
        "/models/installed",
        "/config/capability-defaults",
    ] {
        assert!(r.contains(&p.to_string()), "{p}: {r:?}");
    }
}

#[test]
fn the_mode_segments_and_the_search_button() {
    let mut h = page();
    let s = click_text(&mut h, " Hugging Face ");
    assert!(s.contains("Search Hugging Face, then press Enter"), "{s}");
    // Type, then click [Search]: one Hub search.
    click_text(&mut h, "Search Hugging Face, then");
    h.type_text("qwen");
    h.sent();
    click_text(&mut h, " Search ");
    let r = reads(&mut h);
    assert!(
        r.contains(&"/models/catalog?q=qwen&hub=true".to_string()),
        "{r:?}"
    );
    // Back to the catalog.
    let s = click_text(&mut h, " Catalog ");
    assert!(
        s.contains("Search by model, organisation or artifact id") || s.contains("qwen"),
        "{s}"
    );
    assert!(catalog::with_state(|p| !p.filters.hf_mode()));
}

#[test]
fn the_fits_toggle_and_each_filter_select() {
    let mut h = page();
    let s = click_text(&mut h, "●─ Fits this computer");
    assert!(s.contains("━● Fits this computer"), "{s}");
    assert!(!s.contains("minimax"), "too-large builds hidden:\n{s}");
    let s = click_text(&mut h, "━● Fits this computer");
    assert!(s.contains("●─ Fits this computer"), "{s}");
    let s = pick(&mut h, "Quantization", "8-bit");
    assert!(s.contains("· 8-bit") && !s.contains("qwen3:0.6b "), "{s}");
    // `x` (Clear filters' key) with the Select still focused.
    let s = h.key(b"x");
    assert!(s.contains("4 of 4 models"), "{s}");
    let s = pick(&mut h, "Provider", "Ollama");
    assert!(s.contains("· Ollama"), "{s}");
    h.key(b"x");
    let s = pick(&mut h, "Capability", "Embedding");
    assert!(
        s.contains("· Embedding") && s.contains("nomic-embed-text"),
        "{s}"
    );
    h.key(b"x");
    let s = pick(&mut h, "Status", "Downloaded");
    assert!(s.contains("· Downloaded"), "{s}");
    // No read for any of it (filters hide, client side).
    assert!(reads(&mut h).is_empty());
}

#[test]
fn no_match_offers_clear_filters() {
    let mut h = page();
    click_text(&mut h, "Search by model");
    let s = h.type_text("zzzz-nothing");
    assert!(s.contains("No model matches these filters."), "{s}");
    assert!(
        s.contains("Change or clear the filters to see the rest of the catalog."),
        "{s}"
    );
    h.esc();
    let s = click_text(&mut h, "Clear filters");
    assert!(s.contains("4 of 4 models"), "{s}");
}

// ---- hover, keyboard, survival ------------------------------------------------

#[test]
fn hovering_the_trash_shows_the_web_tooltip() {
    let mut h = page();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| {
            l.starts_with(" qwen3:0.6b ")
                .then(|| l.find('⌫').map(|c| (i, l[..c].chars().count())))
                .flatten()
        })
        .expect("qwen3:0.6b's trash");
    h.key(format!("\x1b[<35;{};{}M", col + 1, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(
        s.contains("Delete qwen3:0.6b from this computer (files only)  (d)"),
        "{s}"
    );
    h.shoot("models-trash-tooltip");
}

#[test]
fn keyboard_only_tab_into_the_row_actions_and_enter() {
    let mut h = page();
    // The list has the keyboard: ↓ selects the second row (qwen3:0.6b).
    h.key(b"\x1b[B");
    assert_eq!(
        catalog::with_state(|p| p.sel.clone()).as_deref(),
        Some("ollama/qwen3:0.6b")
    );
    // Tab: the table; Tab: the selected row's first button (Use as default).
    h.key(b"\t");
    let s = h.key(b"\t");
    assert!(s.contains("Use as default"), "{s}");
    h.key(b"\r");
    let w = writes(&mut h);
    assert!(
        w.iter().any(|(key, ..)| key == "catalog.default"),
        "Tab + Enter pressed Use as default: {w:?}"
    );
}

#[test]
fn the_delete_confirm_survives_the_catalog_reload() {
    let k = "ollama/qwen3:0.6b";
    let mut h = page();
    click_row(&mut h, "qwen3:0.6b", "⌫");
    h.store.json.set_write(
        &format!("catalog.delplan:{k}"),
        Some(WriteState::Done(json!({"ok": true, "freed_bytes": 1000}))),
    );
    let s = h.turns(3);
    assert!(s.contains("Files only — nothing"), "{s}");
    h.shoot("models-delete-confirm");
    // The catalog answers again (a poll, a reload): the table rebuilds.
    h.store
        .json
        .set(catalog::K_CATALOG, Loadable::Ready(fixture("catalog")));
    let s = h.turns(3);
    assert!(
        s.contains("Files only — nothing"),
        "the confirm stays:\n{s}"
    );
    click_confirm(&mut h, "Delete", "Keep");
    assert!(writes(&mut h)
        .iter()
        .any(|(key, ..)| *key == format!("catalog.delete:{k}")));
}

#[test]
fn every_line_fits_at_80_and_120() {
    for size in r8w4::SIZES {
        let mut h = page_as(size, true);
        h.assert_fits();
        let s = h.turns(1);
        assert!(s.contains("Qwen3 0.6B"), "{s}");
    }
}

// ---- wording ----------------------------------------------------------------

#[test]
fn the_models_words_are_the_webs() {
    let fx = wording();
    let w = |k: &str| {
        fx[k]
            .as_str()
            .unwrap_or_else(|| panic!("fixture has no {k}"))
            .to_string()
    };
    // Row actions, from the single source.
    let offered = catalog::offered_actions(
        &fixture("catalog"),
        &fixture("installed"),
        &fixture("defaults"),
        true,
    );
    let mut seen = BTreeSet::new();
    for (k, acts) in &offered {
        let artifact = split(k).1;
        for a in acts {
            seen.insert(a.id);
            match a.id {
                "download" => assert_eq!(a.label, w("download")),
                "default" => assert_eq!(a.label, w("use_default")),
                "delete" => assert_eq!(
                    a.tooltip.as_deref(),
                    Some(w("delete_tip").replace("{n}", artifact).as_str())
                ),
                other => panic!("unexpected action {other}"),
            }
        }
    }
    assert_eq!(seen.len(), 3, "{seen:?}");
    // Refusals for a non-admin are the web's titles.
    for (_, acts) in catalog::offered_actions(
        &fixture("catalog"),
        &fixture("installed"),
        &fixture("defaults"),
        false,
    ) {
        for a in acts {
            let why = a.enabled.clone().unwrap_err();
            let want = match a.id {
                "download" => w("download_refused"),
                "default" => w("use_default_refused"),
                "delete" => w("delete_refused"),
                other => panic!("{other}"),
            };
            assert_eq!(why, want, "{}", a.id);
        }
    }
    // Cancel + Not available here.
    let (_, run) = running_job_actions();
    assert_eq!(run[0].label, w("cancel"));
    let (_, un) = unavailable_actions();
    assert_eq!(un[0].label, w("unavailable"));
    assert_eq!(un[0].enabled.clone().unwrap_err(), w("unavailable_engine"));
    // The confirmations.
    assert!(catalog::confirm_sentence(&json!({"freed_bytes": 1000})).ends_with(&w("confirm_tail")));
    assert_eq!(
        catalog::confirm_sentence(&json!({})),
        format!("{} {}", w("confirm_no_size"), w("confirm_tail"))
    );
    assert_eq!(catalog::CANCEL_QUESTION, w("cancel_question"));
    // The page's controls, on screen.
    let mut h = page();
    let s = h.turns(1);
    for k in [
        "check_again",
        "fits",
        "placeholder_catalog",
        "not_in_catalog",
    ] {
        assert!(s.contains(&w(k)), "{k} = {:?} on screen:\n{s}", w(k));
    }
    let modes: Vec<String> = fx["modes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_string())
        .collect();
    assert_eq!(catalog::MODES.to_vec(), modes);
    let groups: Vec<String> = fx["groups"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap().to_string())
        .collect();
    assert_eq!(catalog::GROUPS.to_vec(), groups);
    for g in &groups {
        assert!(s.contains(&format!("{g} ▐")), "{g}:\n{s}");
    }
    let quant: Vec<&str> = catalog::QUANT_CHIPS.iter().map(|c| c.1).collect();
    assert_eq!(
        quant,
        fx["quant_chips"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect::<Vec<_>>()
    );
    let status: Vec<&str> = catalog::STATUS_CHIPS.iter().map(|c| c.1).collect();
    assert_eq!(
        status,
        fx["status_chips"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect::<Vec<_>>()
    );
    // The empty state and Clear filters.
    click_text(&mut h, "Search by model");
    let s = h.type_text("zzzz-nothing");
    for k in ["empty", "empty_hint", "clear"] {
        assert!(s.contains(&w(k)), "{k}:\n{s}");
    }
}
