//! R15 (DESIGN-TUI.md §3.10, §6.1): the Multimodal page by mouse. A
//! synthesized SGR click for EVERY row action (Edit / Configure, Clear,
//! Download, Copy), every head and banner button (Apply recommended,
//! Refresh, Download missing, Recommended for this computer, Download all,
//! Cancel downloads) and every button of the "Configure capability
//! default" modal (Cancel, Clear, Test, Save), each asserted on the
//! command the worker receives or the dialog that opens; confirmations are
//! answered BY MOUSE. The meta-test enumerates `routes::row_actions` (and
//! the head/banner/plan/editor action lists) over the fixture: an action
//! without a click test is RED. The words are the web's
//! (`tests/fixtures/r15_web_wording_multimodal.json`).

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::api::firstrun::GroupStatus;
use abstractgateway_console::store::{AvailabilityData, Loadable, ProvidersData, RoutesData};
use abstractgateway_console::ui::{self, routes};
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    routes::view(cx, ctx, &t)
}

fn routes_payload() -> Value {
    json!({
        "ok": true, "writable": true,
        "authority": "abstractcore.gateway_runtime", "source": "abstractcore.gateway_runtime",
        "errors": [],
        "routes": [
            {"key": "input.text", "kind": "input", "modality": "text", "label": "Text Input",
             "task": "text_understanding", "provider": "lmstudio", "model": "test-model-a",
             "reasoning": "medium", "source": "abstractcore.gateway_runtime", "configured": true},
            {"key": "input.image", "kind": "input", "modality": "image", "label": "Image Input",
             "provider": "lmstudio", "model": "test-model-a", "source": "abstractcore.gateway_runtime",
             "configured": true, "covered_by": "input.text", "read_only": true},
            {"key": "input.video", "kind": "input", "modality": "video", "label": "Video Input",
             "provider": "lmstudio", "model": "test-model-a", "source": "abstractcore.gateway_runtime",
             "configured": true, "covered_by": "input.text", "read_only": false, "overrideable": true},
            {"key": "output.voice", "kind": "output", "modality": "voice", "label": "Voice Output",
             "provider": "supertonic", "model": "supertonic-3", "options": {"voice": "M3"},
             "source": "abstractcore.gateway_runtime", "configured": true},
            {"key": "output.music", "kind": "output", "modality": "music", "label": "Music Output",
             "provider": "acestep", "model": "ace-step-v1",
             "source": "abstractcore.gateway_runtime", "configured": true},
            {"key": "input.sound", "kind": "input", "modality": "sound", "label": "Sound Input",
             "package_hint": "abstractsound", "source": "not_configured", "configured": false}
        ]
    })
}

fn availability_payload(gaps: bool) -> Value {
    json!({
        "ok": true,
        "routes": [
            {"key": "input.text", "provider": "lmstudio", "model": "test-model-a",
             "download_artifact": "qwen/qwen3.5-9b@4bit",
             "availability": {"provider": "lmstudio", "artifact": "qwen/qwen3.5-9b@4bit",
                              "status": "absent", "downloadable": true, "summary": "Not downloaded.",
                              "evidence": "lms ls --json", "instruction": "lms get qwen/qwen3.5-9b@4bit"}},
            {"key": "output.voice", "provider": "supertonic", "model": "supertonic-3",
             "availability": {"provider": "supertonic", "artifact": "supertonic-3",
                              "status": "installed", "downloadable": true,
                              "summary": "In AbstractVoice's cache.", "detail": "supertonic-3 is in the cache"}},
            {"key": "output.music", "provider": "acestep", "model": "ace-step-v1",
             "availability": {"provider": "acestep", "artifact": "ace-step-v1",
                              "status": "absent", "downloadable": false, "summary": "Not downloaded.",
                              "instruction": "pip install acestep"}}
        ],
        "recommended": {
            "total": 2, "installed": 1, "absent": 1, "unknown": 0,
            "recommended": [
                {"title": "Text", "route": "input.text", "provider": "lmstudio",
                 "artifact": "qwen/qwen3.5-9b@4bit", "status": "absent", "downloadable": true}
            ],
            "gaps": if gaps { json!([{"provider": "lmstudio", "artifact": "qwen/qwen3.5-9b@4bit", "route": "input.sound"}]) } else { json!([]) }
        }
    })
}

fn providers() -> ProvidersData {
    ProvidersData::from_value(&json!({
        "items": [
            {"name": "lmstudio", "display_name": "LMStudio", "status": "available",
             "local_provider": true, "authentication_required": false, "models": []},
            {"name": "ollama", "display_name": "Ollama", "status": "available",
             "local_provider": true, "authentication_required": false, "models": []}
        ],
        "default_provider": "lmstudio", "default_model": "test-model-a"
    }))
}

fn page_with(gaps: bool) -> r8w4::Harness {
    let mut h = harness((140, 44), Mount::Page(page_view));
    h.admin();
    h.store
        .routes
        .set(Loadable::Ready(RoutesData::from_value(&routes_payload())));
    h.store
        .availability
        .set(Loadable::Ready(AvailabilityData::from_value(
            &availability_payload(gaps),
        )));
    h.store.providers.set(Loadable::Ready(providers()));
    h.store.models.update(|m| {
        m.insert(
            "lmstudio".into(),
            Loadable::Ready(vec!["test-model-a".into(), "test-model-b".into()]),
        );
    });
    h.turns(3);
    h.sent();
    h
}

fn page() -> r8w4::Harness {
    page_with(true)
}

/// Click `needle` on `key`'s table row (searched right of the key).
fn click_row(h: &mut r8w4::Harness, key: &str, needle: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .find(|(_, l)| l.starts_with(&format!(" {key} ")))
        .unwrap_or_else(|| panic!("{key} row:\n{screen}"));
    let start = line.find(key).unwrap() + key.len();
    let byte = start
        + line[start..]
            .find(needle)
            .unwrap_or_else(|| panic!("{needle:?} not on {key}'s row:\n{screen}"));
    let x = line[..byte].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Click the button labelled `label` on the LAST line holding it (a
/// dialog's button row sits above the page's).
fn click_last(h: &mut r8w4::Harness, label: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(&format!(" {label} ")))
        .last()
        .unwrap_or_else(|| panic!("no [{label}]:\n{screen}"));
    let b = line.rfind(&format!(" {label} ")).unwrap() + 1;
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_multimodal.json"
    );
    let text = std::fs::read_to_string(p)
        .unwrap_or_else(|e| panic!("{p}: {e} — run scripts/extract_web_wording.py --write"));
    serde_json::from_str(&text).expect("fixture JSON")
}

fn offered() -> BTreeSet<(String, &'static str)> {
    let d = RoutesData::from_value(&routes_payload());
    let a = AvailabilityData::from_value(&availability_payload(true));
    let mut out = BTreeSet::new();
    for r in &d.rows {
        for x in routes::row_actions(r, a.by_route.get(&r.key), false, true) {
            if x.is_enabled() {
                out.insert((r.key.clone(), x.id));
            }
        }
    }
    for x in routes::head_actions(true)
        .into_iter()
        .chain(routes::banner_actions(true))
        .chain(routes::plan_actions(true, false, true))
        .chain(routes::plan_actions(true, true, false))
    {
        out.insert(("page".to_string(), x.id));
    }
    for x in routes::editor_actions(Ok(()), Ok(()), Ok(())) {
        out.insert(("editor".to_string(), x.id));
    }
    out
}

fn covered() -> BTreeSet<(String, &'static str)> {
    [
        ("input.text", "edit"),
        ("input.text", "clear"),
        ("input.text", "download"),
        ("input.video", "configure"),
        ("output.voice", "edit"),
        ("output.voice", "clear"),
        ("output.music", "edit"),
        ("output.music", "clear"),
        ("output.music", "copy"),
        ("input.sound", "configure"),
        ("page", "apply"),
        ("page", "refresh"),
        ("page", "download_missing"),
        ("page", "plan"),
        ("page", "download_all"),
        ("page", "cancel_all"),
        ("editor", "cancel"),
        ("editor", "clear"),
        ("editor", "test"),
        ("editor", "save"),
    ]
    .into_iter()
    .map(|(a, b)| (a.to_string(), b))
    .collect()
}

#[test]
fn every_offered_multimodal_action_has_a_click_test() {
    let offered = offered();
    let covered = covered();
    let missing: Vec<_> = offered.difference(&covered).cloned().collect();
    assert!(
        missing.is_empty(),
        "Multimodal actions without a click test: {missing:?}"
    );
    let stale: Vec<_> = covered.difference(&offered).cloned().collect();
    assert!(
        stale.is_empty(),
        "click tests for actions no longer offered: {stale:?}"
    );
}

#[test]
fn edit_and_configure_open_the_configure_capability_default_modal() {
    for (key, glyph, label) in [
        ("input.text", "✎", "Text Input"),
        ("output.voice", "✎", "Voice Output"),
        ("output.music", "✎", "Music Output"),
        ("input.video", "⊞", "Video Input"),
        ("input.sound", "⊞", "Sound Input"),
    ] {
        let mut h = page();
        let s = click_row(&mut h, key, glyph);
        assert!(
            s.contains("Configure capability default")
                && s.contains(&format!("Route — {label} ({key})")),
            "{key}:\n{s}"
        );
    }
}

#[test]
fn clear_asks_then_clears_by_mouse() {
    for key in ["input.text", "output.voice", "output.music"] {
        let mut h = page();
        let s = click_row(&mut h, key, "⌀");
        assert!(s.contains(&format!("Clear the override on {key}")), "{s}");
        assert!(h.sent().is_empty(), "nothing before the answer");
        click_last(&mut h, "Clear");
        let cmds = h.sent();
        assert!(
            cmds.iter()
                .any(|c| matches!(c, Cmd::ClearRoute { key: k, .. } if k == key)),
            "{key}: {cmds:?}"
        );
    }
    // Cancel keeps it.
    let mut h = page();
    click_row(&mut h, "input.text", "⌀");
    click_last(&mut h, "Cancel");
    assert!(h.sent().is_empty());
}

#[test]
fn download_and_copy_on_the_rows() {
    let mut h = page();
    click_row(&mut h, "input.text", "⤓");
    let cmds = h.sent();
    assert!(
        cmds.iter().any(|c| matches!(c, Cmd::PrepareDownload { artifact, .. } if artifact == "qwen/qwen3.5-9b@4bit")),
        "{cmds:?}"
    );
    let mut h = page();
    click_row(&mut h, "output.music", "⧉");
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("Copied: pip install acestep")
    );
}

#[test]
fn the_head_banner_and_plan_buttons() {
    // Apply recommended: asks, the click on its action applies (no force).
    let mut h = page();
    let s = h.click_text(" Apply recommended ");
    assert!(
        s.contains(routes::APPLY_QUESTION.split(" (").next().unwrap()),
        "{s}"
    );
    click_last(&mut h, "Apply recommended");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::ApplyRecommendedRoutes { force: false })));
    // Refresh re-reads the routes.
    let mut h = page();
    h.click_text("↻ Refresh");
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::LoadRoutes)));
    // Download missing: one download per gap, after a confirm.
    let mut h = page();
    let s = h.click_text("Download missing");
    assert!(
        s.contains("Download lmstudio qwen/qwen3.5-9b@4bit on the gateway host?"),
        "{s}"
    );
    click_last(&mut h, "Download");
    assert!(h.sent().iter().any(
        |c| matches!(c, Cmd::DownloadModel { artifact, .. } if artifact == "qwen/qwen3.5-9b@4bit")
    ));
    // The plan modal.
    let mut h = page();
    let s = h.click_text(" Recommended for this computer ");
    assert!(s.contains("Text model now:"), "{s}");
    // Download all.
    let mut h = page();
    let s = h.click_text("Download all");
    assert!(s.contains("Download the recommended set"), "{s}");
    click_last(&mut h, "Download all");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::DownloadRecommended)));
    // Cancel downloads (while a Download all runs).
    let mut h = page();
    h.store
        .download_group
        .set(Some(GroupStatus::from_job(&json!({
            "job_id": "grp_1", "status": "running", "state": "downloading", "percent": 10.0,
            "message": "Downloading 1 model"
        }))));
    h.turns(2);
    let s = h.click_text("Cancel downloads");
    assert!(s.contains("Cancel Download all (grp_1)?"), "{s}");
    click_last(&mut h, "Cancel downloads");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::CancelDownloadGroup { job } if job == "grp_1")));
}

#[test]
fn the_editor_buttons_save_test_clear_and_cancel() {
    // Save: an edited base URL is sent.
    let mut h = page();
    click_row(&mut h, "input.text", "✎");
    for _ in 0..3 {
        h.key(b"\t");
    }
    h.type_text("http://127.0.0.1:1234/v1");
    click_last(&mut h, "Save");
    match h
        .sent()
        .into_iter()
        .find(|c| matches!(c, Cmd::PutRoute { .. }))
    {
        Some(Cmd::PutRoute { key, body, .. }) => {
            assert_eq!(key, "input.text");
            assert_eq!(body["base_url"], "http://127.0.0.1:1234/v1");
        }
        other => panic!("expected PutRoute, got {other:?}"),
    }
    // Test: a real generation is asked for.
    let mut h = page();
    click_row(&mut h, "input.text", "✎");
    click_last(&mut h, "Test");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::TestRoute { key, .. } if key == "input.text")));
    // Clear: pick "use default" by mouse, then Clear → the confirm → ClearRoute.
    let mut h = page();
    click_row(&mut h, "input.text", "✎");
    h.click_text("use default (engine decides)");
    let s = click_last(&mut h, "Clear");
    assert!(s.contains("Clear the override on input.text"), "{s}");
    click_last(&mut h, "Clear");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::ClearRoute { key, .. } if key == "input.text")));
    // Cancel with no edit closes at once.
    let mut h = page();
    click_row(&mut h, "input.text", "✎");
    let s = click_last(&mut h, "Cancel");
    assert!(!s.contains("Configure capability default"), "{s}");
}

#[test]
fn closing_the_editor_with_unsaved_edits_asks_first() {
    for close in ["Cancel", "✕"] {
        let mut h = page();
        click_row(&mut h, "input.text", "✎");
        for _ in 0..3 {
            h.key(b"\t");
        }
        h.type_text("http://edited");
        let s = if close == "✕" {
            h.click_text("✕")
        } else {
            click_last(&mut h, "Cancel")
        };
        assert!(s.contains("Discard changes?"), "{close}:\n{s}");
        assert!(
            s.contains("Configure capability default"),
            "still open:\n{s}"
        );
        let s = click_last(&mut h, "Discard");
        assert!(
            !s.contains("Configure capability default"),
            "{close} discarded:\n{s}"
        );
        assert!(h.sent().iter().all(|c| !matches!(c, Cmd::PutRoute { .. })));
    }
}

#[test]
fn hovering_the_weights_pill_shows_its_sentence() {
    let mut h = page();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| {
            l.starts_with(" output.voice ")
                .then(|| l.find("installed").map(|c| (i, l[..c].chars().count())))
                .flatten()
        })
        .expect("output.voice's pill");
    h.key(format!("\x1b[<35;{};{}M", col + 2, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(s.contains("In AbstractVoice's cache."), "{s}");
    assert!(s.contains("supertonic-3 is in the cache."), "{s}");
}

#[test]
fn keyboard_tab_into_the_row_actions_and_enter() {
    // The table has the focus: Down to input.video, Tab into its actions,
    // Enter presses the first (Override → the editor).
    let mut h = page();
    h.key(b"\x1b[B");
    h.key(b"\x1b[B");
    h.key(b"\t");
    let s = h.key(b"\r");
    assert!(s.contains("Route — Video Input (input.video)"), "{s}");
}

#[test]
fn the_editor_survives_the_routes_reload() {
    let mut h = page();
    click_row(&mut h, "output.voice", "✎");
    h.store
        .routes
        .set(Loadable::Ready(RoutesData::from_value(&routes_payload())));
    h.store
        .availability
        .set(Loadable::Ready(AvailabilityData::from_value(
            &availability_payload(false),
        )));
    let s = h.turns(3);
    assert!(s.contains("Route — Voice Output (output.voice)"), "{s}");
}

#[test]
fn the_multimodal_words_are_the_webs() {
    let fx = fixture();
    assert_eq!(fx["title"], routes::TITLE);
    assert_eq!(fx["subtitle"], routes::SUBTITLE);
    assert_eq!(fx["scope_admin"], routes::SCOPE_ADMIN);
    assert_eq!(fx["scope_user"], routes::SCOPE_USER);
    assert_eq!(fx["apply"]["title"], routes::APPLY_TIP);
    assert_eq!(fx["refresh"]["title"], routes::REFRESH_TIP);
    assert_eq!(fx["empty"], routes::EMPTY);
    let head = routes::head_actions(true);
    assert_eq!(fx["apply"]["label"], head[0].label.as_str());
    assert!(head[1]
        .label
        .ends_with(fx["refresh"]["label"].as_str().unwrap()));
    // Row actions: labels and tooltips from the web's templates.
    let d = RoutesData::from_value(&routes_payload());
    let a = AvailabilityData::from_value(&availability_payload(true));
    let labels: Vec<&str> = fx["action_labels"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    let tips = &fx["tips"];
    for r in &d.rows {
        let w = a.by_route.get(&r.key);
        assert!(
            labels.contains(&routes::action_label(r).as_str()),
            "{}",
            r.key
        );
        for x in routes::row_actions(r, w, false, true) {
            let want = match x.id {
                "edit" | "configure" => tips["configure"]
                    .as_str()
                    .unwrap()
                    .replace("{label}", &routes::action_label(r))
                    .replace("{key}", &r.key),
                "clear" => tips["clear"].as_str().unwrap().replace("{key}", &r.key),
                "download" => tips["download"]
                    .as_str()
                    .unwrap()
                    .replace("{artifact}", &w.unwrap().artifact)
                    .replace("{provider}", r.provider.as_deref().unwrap()),
                "copy" => tips["copy"]
                    .as_str()
                    .unwrap()
                    .replace("{instruction}", w.unwrap().instruction.trim()),
                other => panic!("{other} has no web tooltip"),
            };
            assert_eq!(
                x.tooltip.as_deref(),
                Some(want.as_str()),
                "{} {}",
                r.key,
                x.id
            );
        }
        // Status and source vocabulary.
        let st = routes::status_label(r);
        let known = fx["status"].as_array().unwrap().iter().any(|v| {
            let v = v.as_str().unwrap();
            v == st
                || v.strip_suffix("{parent}")
                    .is_some_and(|p| st.starts_with(p))
        });
        assert!(known, "{} status {st:?}", r.key);
    }
    // The Weights pill vocabulary.
    for w in a.by_route.values() {
        let (label, _, _) = routes::weight_view(w);
        let ws = &fx["weights"];
        let want = match (w.status.as_str(), w.downloadable) {
            ("unknown", true) => ws["unknown_downloadable"].as_str(),
            ("unknown", false) => ws["unknown_not_downloadable"].as_str(),
            (s, _) => ws[s].as_str(),
        };
        assert_eq!(Some(label), want);
    }
    // The banner sentence and button.
    let b = &fx["banner"];
    assert_eq!(
        routes::gaps_sentence(1, "input.sound", "lmstudio x"),
        format!(
            "{} {} (input.sound). {}lmstudio x.",
            b["one"].as_str().unwrap(),
            b["tail"].as_str().unwrap(),
            b["recommended"].as_str().unwrap()
        )
    );
    assert!(routes::banner_actions(true)[0]
        .label
        .ends_with(b["button"].as_str().unwrap()));
    // The modal.
    let dl = &fx["dialog"];
    assert_eq!(dl["title"], routes::DIALOG_TITLE);
    assert_eq!(dl["lead"], routes::DIALOG_LEAD);
    let ours = routes::editor_actions(Ok(()), Ok(()), Ok(()));
    for (web, a) in dl["buttons"].as_array().unwrap().iter().zip(&ours) {
        assert_eq!(web["label"].as_str(), Some(a.label.as_str()));
        let t = web["title"].as_str().unwrap();
        if !t.is_empty() {
            assert_eq!(a.tooltip.as_deref(), Some(t), "{}", a.id);
        }
    }
    assert_eq!(dl["buttons"].as_array().unwrap().len(), ours.len());
}

/// The editor's [Cancel] [Clear] [Test] [Save] row is right-aligned under
/// the fields (w::form::button_row): Save ends one padding cell before the
/// dialog's right border, at 80x24 and 120x40, before and after the
/// provider's model list lands (the row's region rebuilds then).
#[test]
fn the_editor_buttons_are_right_aligned() {
    for size in [(80, 24), (120, 40)] {
        let mut h = harness(size, Mount::Page(page_view));
        h.admin();
        h.store
            .routes
            .set(Loadable::Ready(RoutesData::from_value(&routes_payload())));
        h.store.providers.set(Loadable::Ready(providers()));
        h.turns(3);
        click_row(&mut h, "input.text", "✎");
        for phase in ["open", "models landed"] {
            if phase == "models landed" {
                h.store.models.update(|m| {
                    m.insert(
                        "lmstudio".into(),
                        Loadable::Ready(vec!["test-model-a".into()]),
                    );
                });
            }
            let s = h.turns(3);
            let line = s
                .lines()
                .find(|l| l.contains(" Cancel ") && l.contains(" Save"))
                .unwrap_or_else(|| panic!("{size:?} {phase}: no button row:\n{s}"));
            let border = line.rfind('│').expect("the dialog's right border");
            let save_end = line.rfind("Save").unwrap() + "Save".len();
            let gap = line[save_end..border].chars().count();
            assert!(
                gap <= 3,
                "{size:?} {phase}: Save ends {gap} cells before the border:\n{s}"
            );
        }
    }
}

#[test]
fn the_grid_has_the_webs_columns_wide_and_its_narrow_form() {
    // Wide: the web's eight columns, Route and Capability separate cells.
    let mut h = page();
    let s = h.turns(1);
    let head = s
        .lines()
        .find(|l| l.contains("Route") && l.contains("Actions"))
        .expect("header");
    let names = [
        "Route",
        "Capability",
        "Provider",
        "Model",
        "Weights",
        "Source",
        "Status",
        "Actions",
    ];
    let mut at = 0;
    for n in names {
        let i = head[at..]
            .find(n)
            .unwrap_or_else(|| panic!("{n} missing or out of order:\n{head}"));
        at += i + n.len();
    }
    // An absent provider/model is the web's "-" (input.sound).
    let row = s
        .lines()
        .find(|l| l.starts_with(" input.sound "))
        .expect("input.sound");
    assert!(row.contains(" - ") && !row.contains('—'), "{row}");
    // Narrow (80x24): Route · Model · Weights · Actions; the status,
    // capability and provider on the row's second line.
    let mut h = harness((80, 24), Mount::Page(page_view));
    h.admin();
    h.store
        .routes
        .set(Loadable::Ready(RoutesData::from_value(&routes_payload())));
    let s = h.turns(3);
    let head = s
        .lines()
        .find(|l| l.contains("Route") && l.contains("Actions"))
        .expect("header");
    assert!(
        head.contains("Model") && head.contains("Weights") && !head.contains("Capability"),
        "{head}"
    );
    let second = s
        .lines()
        .skip_while(|l| !l.starts_with(" input.text "))
        .nth(1)
        .unwrap_or_default();
    assert!(
        second.trim_start().starts_with("configured · Text"),
        "second line:\n{s}"
    );
}

#[test]
fn model_ids_wrap_at_slash_or_dash_never_mid_word() {
    for (id, w) in [
        ("mlx-community/Qwen3.8-Flash-Next-4bit", 20),
        ("AbstractFramework/wan2.2-t2v-a14b-diffusers-8bit", 22),
        ("qwen/qwen3.5-9b@4bit", 12),
    ] {
        let lines = routes::id_lines(id, w);
        assert_eq!(lines.concat(), id, "nothing lost: {lines:?}");
        for l in &lines[..lines.len() - 1] {
            assert!(abstracttui::text::width(l) <= w, "{l:?} wider than {w}");
            assert!(
                l.ends_with('/') || l.ends_with('-'),
                "{id}: broke mid-word at {l:?} ({lines:?})"
            );
        }
    }
    // Only a token longer than the cell is cut.
    let lines = routes::id_lines("averyveryverylongtoken", 8);
    assert!(
        lines.iter().all(|l| abstracttui::text::width(l) <= 8),
        "{lines:?}"
    );
}
