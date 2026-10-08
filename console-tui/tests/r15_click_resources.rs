//! R15 (DESIGN-TUI.md §3.11, §6.1): the Resources page by mouse. A
//! synthesized SGR click for EVERY action: the ◎ Gateway card (Workflows
//! paused, Start at login, Check now, Update, Restart gateway…, Quit
//! gateway…), the head's Refresh, the Models section (Show configured /
//! cached, Load model and the load form's Cancel / Load model), each model
//! row's Estimate / Lock / Unlock / Unload and each cache row's Clear —
//! asserted on the command the worker receives or the dialog that opens;
//! confirmations are answered BY MOUSE. The meta-test enumerates the
//! page's action lists over the fixture: an action without a click test is
//! RED. The words are the web's (`tests/fixtures/r15_web_wording_resources.json`).

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::store::operator::{HostRunner, HostUpdate, StartAtLogin};
use abstractgateway_console::store::{
    host_state_from_payload, HostStateData, Loadable, ProvidersData,
};
use abstractgateway_console::ui::{self, models};
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    models::view(cx, ctx, &t)
}

fn host() -> HostStateData {
    host_state_from_payload(&json!({
        "ok": true, "host": {"host_name": "studio.local"},
        "memory": {"ram": {"total_bytes": 137438953472u64, "available_bytes": 51539607552u64,
                           "used_bytes": 85899345920u64, "percent": 62.5}},
        "gpu": {"supported": false},
        "models": [
            {"task": "text_generation", "provider": "mlx", "model": "qwen3-32b", "source": "local",
             "resident": true, "state": "provider_loaded", "locked": true, "lockable": true,
             "size_bytes": 2147483648u64, "context_length": 8192u64},
            {"task": "text_generation", "provider": "lmstudio", "model": "glm-4.6-gguf",
             "source": "provider_server", "resident": true, "locked": false, "lockable": true,
             "est_weights_bytes": 99857989632u64},
            {"task": null, "provider": "lmstudio", "model": "mystery-model", "resident": null}
        ],
        "session_caches": [
            {"key": "agw.pc.v1.s-sess1:session", "provider": "mlx", "model": "qwen3-32b",
             "session_id": "sess1", "bytes": 4096u64, "token_count": 100u64}
        ],
        "degraded": [], "reasons": {}
    }))
}

fn runner() -> Value {
    json!({"paused": false, "inflight_ticks": 0, "runner_in_process": true,
           "capabilities": {"restart": true, "shutdown": true},
           "last_hang": {"at": "2026-10-04T19:23:57Z", "reason": "runner blocked 31 s",
                         "blocked_s": 31.0, "top_frame": "runner.py:42 tick",
                         "dump_path": "/data/incidents/watchdog.threads.txt",
                         "file": "/data/incidents/watchdog.json"}})
}

fn update() -> Value {
    json!({"current": "0.13.1", "install": {"kind": "pip", "upgradable": true},
           "check": {"update_available": true, "latest": "0.13.2", "checked_at": "2026-10-08T00:00:00Z"}})
}

fn login() -> Value {
    json!({"enabled": false, "state": "off", "can_change": true, "summary": "the gateway does not start at login"})
}

fn page() -> r8w4::Harness {
    let mut h = harness((140, 44), Mount::Page(page_view));
    h.admin();
    let op = h.store.op;
    op.runner
        .set(Loadable::Ready(HostRunner::from_value(&runner())));
    op.update
        .set(Loadable::Ready(HostUpdate::from_value(&update())));
    op.start_at_login
        .set(Loadable::Ready(StartAtLogin::from_value(&login())));
    op.tray.set(Loadable::Ready("shown (pid 4242)".into()));
    h.store.host_state.set(Loadable::Ready(host()));
    h.store
        .providers
        .set(Loadable::Ready(ProvidersData::from_value(&json!({
            "items": [{"name": "mlx", "display_name": "MLX", "status": "available",
                       "local_provider": true, "authentication_required": false, "models": []}]
        }))));
    h.store.models.update(|m| {
        m.insert(
            "mlx".into(),
            Loadable::Ready(vec!["qwen3-32b".into(), "qwen3-8b".into()]),
        );
    });
    h.turns(3);
    h.sent();
    h
}

/// Click `needle` on the row holding `name` (searched right of the name).
/// (A row's buttons may wrap onto its next lines: those are searched too.)
fn click_row(h: &mut r8w4::Harness, name: &str, needle: &str) -> String {
    let screen = h.turns(1);
    let lines: Vec<&str> = screen.lines().collect();
    let first = lines
        .iter()
        .position(|l| l.contains(&format!(" {name} ")) && !l.contains("·"))
        .unwrap_or_else(|| panic!("{name}'s row:\n{screen}"));
    let start = lines[first].find(name).unwrap() + name.len();
    let (y, byte) = match lines[first][start..].find(needle) {
        Some(b) => (first, start + b),
        None => (1..=2)
            .map(|k| first + k)
            .find_map(|y| {
                let l = lines.get(y)?;
                l.get(start..)?.find(needle).map(|b| (y, start + b))
            })
            .unwrap_or_else(|| panic!("{needle:?} on {name}'s row:\n{screen}")),
    };
    let x = lines[y][..byte].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Click the button labelled `label` on the LAST line holding it.
fn click_last(h: &mut r8w4::Harness, label: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(&format!(" {label} ")) || l.ends_with(&format!(" {label}")))
        .last()
        .unwrap_or_else(|| panic!("no [{label}]:\n{screen}"));
    let b = line.rfind(&format!(" {label}")).unwrap() + 1;
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Click a confirmation's action button: the line holding `label` AND its
/// keep button (`Cancel` / `Not now` / `Leave it`).
fn click_confirm(h: &mut r8w4::Harness, label: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| {
            l.contains(&format!(" {label} "))
                && (l.contains(" Cancel ") || l.contains(" Not now ") || l.contains(" Leave it "))
        })
        .last()
        .unwrap_or_else(|| panic!("no confirmation [{label}]:\n{screen}"));
    let b = line.rfind(&format!(" {label} ")).unwrap() + 1;
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

fn ops(cmds: &[Cmd]) -> Vec<String> {
    cmds.iter()
        .filter_map(|c| match c {
            Cmd::Operator(o) => Some(format!("{o:?}")),
            _ => None,
        })
        .collect()
}

fn fixture() -> Value {
    let p = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/r15_web_wording_resources.json"
    );
    let text = std::fs::read_to_string(p)
        .unwrap_or_else(|e| panic!("{p}: {e} — run scripts/extract_web_wording.py --write"));
    serde_json::from_str(&text).expect("fixture JSON")
}

fn offered() -> BTreeSet<(String, &'static str)> {
    let d = host();
    let mut out = BTreeSet::new();
    for r in &d.models {
        for a in models::row_actions(r, true) {
            out.insert((r.model.clone().unwrap(), a.id));
        }
    }
    for c in &d.caches {
        for a in models::cache_actions(c, true) {
            out.insert((c.session_id.clone(), a.id));
        }
    }
    let r = HostRunner::from_value(&runner());
    let u = HostUpdate::from_value(&update());
    for a in models::gateway_actions(Some(&r), Some(&u), true)
        .into_iter()
        .chain(models::head_actions())
        .chain(models::models_actions(true))
    {
        out.insert(("page".into(), a.id));
    }
    // The switches and the load form's buttons.
    for id in ["pause", "login", "show_cached", "form_cancel", "form_load"] {
        out.insert(("page".into(), id));
    }
    out
}

fn covered() -> BTreeSet<(String, &'static str)> {
    [
        ("qwen3-32b", "estimate"),
        ("qwen3-32b", "unlock"),
        ("qwen3-32b", "unload"),
        ("glm-4.6-gguf", "estimate"),
        ("glm-4.6-gguf", "lock"),
        ("glm-4.6-gguf", "unload"),
        ("mystery-model", "estimate"),
        ("sess1", "clear"),
        ("page", "check"),
        ("page", "update"),
        ("page", "restart"),
        ("page", "quit"),
        ("page", "refresh"),
        ("page", "load"),
        ("page", "pause"),
        ("page", "login"),
        ("page", "show_cached"),
        ("page", "form_cancel"),
        ("page", "form_load"),
    ]
    .into_iter()
    .map(|(a, b)| (a.to_string(), b))
    .collect()
}

#[test]
fn every_offered_resources_action_has_a_click_test() {
    let (o, c) = (offered(), covered());
    let missing: Vec<_> = o.difference(&c).cloned().collect();
    assert!(
        missing.is_empty(),
        "Resources actions without a click test: {missing:?}"
    );
    let stale: Vec<_> = c.difference(&o).cloned().collect();
    assert!(
        stale.is_empty(),
        "click tests for actions no longer offered: {stale:?}"
    );
}

#[test]
fn the_model_rows_buttons() {
    // Estimate (any named row).
    for name in ["qwen3-32b", "glm-4.6-gguf"] {
        let mut h = page();
        click_row(&mut h, name, "Estimate");
        assert!(
            h.sent()
                .iter()
                .any(|c| matches!(c, Cmd::ContextEstimate { model, .. } if model == name)),
            "{name}"
        );
    }
    // Unlock asks, then unlocks.
    let mut h = page();
    let s = click_row(&mut h, "qwen3-32b", "Unlock");
    assert!(s.contains("Unlock mlx/qwen3-32b?"), "{s}");
    click_confirm(&mut h, "Unlock");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::LockModel { lock: false, model, .. } if model == "qwen3-32b")));
    // Lock (adopts the sweep row) at once.
    let mut h = page();
    click_row(&mut h, "glm-4.6-gguf", "Lock");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::LockModel { lock: true, model, .. } if model == "glm-4.6-gguf")));
    // Unload asks with the web's sentence, then unloads.
    for name in ["qwen3-32b", "glm-4.6-gguf"] {
        let mut h = page();
        click_row(&mut h, name, "Unload");
        let s = h.turns(1);
        assert!(
            s.contains("from host memory? The next request that needs it pays the"),
            "{s}"
        );
        click_confirm(&mut h, "Unload");
        assert!(
            h.sent().iter().any(
                |c| matches!(c, Cmd::UnloadModel { force: false, model, .. } if model == name)
            ),
            "{name}"
        );
    }
}

#[test]
fn show_configured_cached_reveals_the_other_rows() {
    let mut h = page();
    let s = h.turns(1);
    assert!(!s.contains("mystery-model"), "{s}");
    let s = h.click_text("Show configured / cached (1)");
    assert!(s.contains("mystery-model"), "{s}");
    click_row(&mut h, "mystery-model", "Estimate");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::ContextEstimate { model, .. } if model == "mystery-model")));
}

#[test]
fn the_session_caches_clear() {
    let mut h = page();
    h.click_text("⌸ Session caches");
    let s = click_row(&mut h, "sess1", "Clear");
    assert!(
        s.contains("Clear every prompt cache for session sess1?"),
        "{s}"
    );
    click_confirm(&mut h, "Clear");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::ClearSessionCaches { session_id, .. } if session_id == "sess1")));
}

#[test]
fn the_gateway_card() {
    // Workflows paused (a switch): one write.
    let mut h = page();
    h.click_text("Workflows paused");
    assert!(ops(&h.sent())
        .iter()
        .any(|o| o.contains("SetPaused { pause: true }")));
    // Check now.
    let mut h = page();
    h.click_text(" Check now ");
    assert!(ops(&h.sent()).iter().any(|o| o.contains("UpdateCheck")));
    // Update: a plain confirm, then the install.
    let mut h = page();
    h.click_text(" Update ");
    click_confirm(&mut h, "Update now");
    assert!(ops(&h.sent()).iter().any(|o| o.contains("UpdateStart")));
    // Restart gateway…: the web's question, [Restart] by mouse.
    let mut h = page();
    let s = h.click_text("Restart gateway…");
    assert!(
        s.contains(models::RESTART_QUESTION.split(" Running").next().unwrap()),
        "{s}"
    );
    click_confirm(&mut h, "Restart");
    assert!(ops(&h.sent()).iter().any(|o| o == "Restart"));
    // Quit gateway…: [Quit] by mouse.
    let mut h = page();
    h.click_text("Quit gateway…");
    click_confirm(&mut h, "Quit");
    assert!(ops(&h.sent()).iter().any(|o| o == "Shutdown"));
    // Start at login (a switch): confirmed, then written.
    let mut h = page();
    let s = h.turns(1);
    let (y, line) = s
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains("Start at login") && l.contains('●'))
        .expect("the Start at login row");
    let x = line[..line.find('●').unwrap()].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    click_confirm(&mut h, "Start at login");
    assert!(ops(&h.sent())
        .iter()
        .any(|o| o.contains("SetStartAtLogin { enabled: true")));
    // Refresh: re-reads the host state and the card.
    let mut h = page();
    h.click_text("↻ Refresh");
    assert!(ops(&h.sent()).iter().any(|o| o.contains("LoadHost")));
}

#[test]
fn load_model_form_by_mouse() {
    let mut h = page();
    let s = h.click_text(" Load model ");
    assert!(
        s.contains("Load (warm up) this model on the host now") && s.contains("lock in memory"),
        "{s}"
    );
    let s = click_last(&mut h, "Cancel");
    assert!(!s.contains("lock in memory"), "{s}");
    let mut h = page();
    h.click_text(" Load model ");
    h.turns(2);
    click_last(&mut h, "Load model");
    assert!(
        h.sent().iter().any(|c| matches!(c, Cmd::WarmupModel { provider, model, .. } if provider == "mlx" && model == "qwen3-32b")),
        "the prefilled pair loads"
    );
}

#[test]
fn hovering_last_restart_shows_the_incident() {
    let mut h = page();
    let s = h.turns(1);
    let (row, col) = s
        .lines()
        .enumerate()
        .find_map(|(i, l)| {
            l.find("Gateway restarted at")
                .map(|c| (i, l[..c].chars().count()))
        })
        .expect("the Last restart row");
    h.key(format!("\x1b[<35;{};{}M", col + 3, row + 1).as_bytes());
    std::thread::sleep(std::time::Duration::from_millis(400));
    let s = h.turns(3);
    assert!(s.contains("Blocked 31 s in runner.py:42 tick"), "{s}");
}

#[test]
fn keyboard_tab_into_a_row_and_enter() {
    // The table has the keyboard: Tab enters the selected row's actions,
    // Enter presses the first (Estimate).
    let mut h = page();
    h.key(b"\t");
    h.key(b"\r");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::ContextEstimate { model, .. } if model == "qwen3-32b")));
}

#[test]
fn the_load_form_survives_the_host_state_poll() {
    let mut h = page();
    h.click_text(" Load model ");
    for _ in 0..3 {
        h.store.host_state.set(Loadable::Ready(host()));
        h.turns(2);
    }
    let s = h.turns(1);
    assert!(s.contains("lock in memory"), "{s}");
}

#[test]
fn the_resources_words_are_the_webs() {
    let fx = fixture();
    let heads: Vec<(String, String)> = fx["headings"]
        .as_array()
        .unwrap()
        .iter()
        .map(|h| {
            (
                h["icon"].as_str().unwrap().into(),
                h["title"].as_str().unwrap().into(),
            )
        })
        .collect();
    assert_eq!(
        heads,
        vec![
            ("◎".to_string(), models::GATEWAY_TITLE.to_string()),
            ("▦".into(), models::MEMORY_TITLE.into()),
            ("▣".into(), models::MODELS_TITLE.into()),
            ("⌸".into(), models::CACHES_TITLE.into()),
        ]
    );
    assert_eq!(fx["gateway_note"], models::GATEWAY_NOTE);
    assert_eq!(fx["refresh_tip"], models::REFRESH_TIP);
    assert_eq!(fx["pause"]["label"], models::PAUSE_LABEL);
    assert_eq!(fx["pause"]["title"], models::PAUSE_TIP);
    assert_eq!(fx["show_cached"]["label"], "Show configured / cached");
    assert_eq!(fx["show_cached"]["title"], models::SHOW_CACHED_TIP);
    assert_eq!(fx["lock_in_memory"]["label"], models::LOCK_IN_MEMORY);
    assert_eq!(fx["lock_in_memory"]["title"], models::LOCK_IN_MEMORY_TIP);
    assert_eq!(fx["load"]["label"], models::LOAD_TITLE);
    assert_eq!(fx["load"]["title"], models::LOAD_TIP);
    assert_eq!(fx["empty"]["models"], models::MODELS_EMPTY);
    assert_eq!(fx["empty"]["caches"], models::CACHES_EMPTY);
    let c = &fx["confirms"];
    assert_eq!(c["restart"], models::RESTART_QUESTION);
    assert_eq!(c["quit"], models::QUIT_QUESTION);
    assert_eq!(
        c["unload"].as_str().unwrap().replace("{name}", "p/m"),
        models::unload_question("p/m")
    );
    assert_eq!(
        c["force"].as_str().unwrap().replace("{name}", "p/m"),
        models::force_unload_question("p/m")
    );
    assert_eq!(
        c["clear_cache"]
            .as_str()
            .unwrap()
            .replace("{session}", "s1"),
        models::clear_cache_question("s1")
    );
    // The Gateway card's buttons.
    let r = HostRunner::from_value(&runner());
    let u = HostUpdate::from_value(&update());
    for a in models::gateway_actions(Some(&r), Some(&u), true) {
        let web = &fx[a.id];
        assert_eq!(web["label"].as_str(), Some(a.label.as_str()), "{}", a.id);
        let t = web["title"].as_str().unwrap();
        if !t.is_empty() {
            assert_eq!(a.tooltip.as_deref(), Some(t), "{}", a.id);
        }
    }
    // The row buttons: label + tooltip per state.
    let rows = &fx["rows"];
    let d = host();
    for m in &d.models {
        for a in models::row_actions(m, true) {
            let (label, tip) = match a.id {
                "estimate" => (
                    rows["estimate"]["label"].as_str(),
                    rows["estimate"]["title"].as_str(),
                ),
                "unload" => (
                    rows["unload"]["label"].as_str(),
                    rows["unload"]["title"].as_str(),
                ),
                "unlock" => (
                    rows["lock_labels"][0].as_str(),
                    rows["unlock_resident"].as_str(),
                ),
                "lock" => (
                    rows["lock_labels"][1].as_str(),
                    if m.source.as_deref() == Some("provider_server") {
                        rows["lock_adopt"].as_str()
                    } else {
                        rows["lock"].as_str()
                    },
                ),
                other => panic!("{other} has no web button"),
            };
            assert_eq!(Some(a.label.as_str()), label, "{:?} {}", m.model, a.id);
            assert_eq!(a.tooltip.as_deref(), tip, "{:?} {}", m.model, a.id);
        }
    }
    for cache in &d.caches {
        for a in models::cache_actions(cache, true) {
            assert_eq!(Some(a.label.as_str()), rows["clear"]["label"].as_str());
            assert_eq!(a.tooltip.as_deref(), rows["clear"]["title"].as_str());
        }
    }
    // The columns: the web's, with the TUI's documented differences (the
    // KV-cache column on a wide terminal; Created needs a store field).
    let web_cols: Vec<&str> = fx["models_columns"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| v.as_str().unwrap())
        .collect();
    assert_eq!(
        web_cols,
        ["Modality", "Provider", "Model", "Resident", "Size", "Context", "Flags", "Actions"]
    );
}

#[test]
fn destructive_confirms_open_on_cancel() {
    // Restart, Quit, Unload and Clear ask with the focus on Cancel: Enter
    // keeps things as they are (R15 F1).
    for open in ["Restart gateway…", "Quit gateway…"] {
        let mut h = page();
        h.click_text(open);
        h.key(b"\r");
        assert!(
            ops(&h.sent())
                .iter()
                .all(|o| o != "Restart" && o != "Shutdown"),
            "{open}: Enter on the default kept it running"
        );
    }
    let mut h = page();
    click_row(&mut h, "glm-4.6-gguf", "Unload");
    h.key(b"\r");
    assert!(h
        .sent()
        .iter()
        .all(|c| !matches!(c, Cmd::UnloadModel { .. })));
}
