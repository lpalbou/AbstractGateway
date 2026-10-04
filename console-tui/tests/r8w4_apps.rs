//! R8.1 Apps (round 8): no "Advanced" disclosures — `a` (the toolbar
//! gear) opens "Apps settings", `g` the gear on the Continuum card opens
//! Continuum's settings (backlog folder, exec runner, process manager);
//! every row applies at once and says "Saved" beside itself; no
//! environment source for the backlog folder. Hermetic + live (ignored):
//!
//!   R8W4_URL=http://127.0.0.1:<port> R8W4_TOKEN=<admin token> \
//!   R8W4_SHOTS_DIR=<dir> cargo test --test r8w4_apps -- --ignored --test-threads 1

mod r8w4;

use abstractgateway_console::store::apps::AppsOverview;
use abstractgateway_console::store::{Loadable, RuntimeConfigData};
use abstractgateway_console::ui::{self, apps};
use abstractgateway_console::worker::Cmd;
use r8w4::{gw, harness, live, live_env, Mount, SIZES};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    apps::view(cx, ctx, &t)
}

fn overview() -> Value {
    json!({"apps": [
        {"id": "flow", "name": "Flow Editor", "kind": "web", "description": "Flows.",
         "package": "@abstractframework/flow", "installed": true, "version": "0.8.0", "running": false},
        {"id": "continuum", "name": "Continuum", "kind": "web", "description": "Backlog.",
         "package": "@abstractframework/continuum", "installed": true, "version": "0.7.0", "running": false}
    ], "node": {"available": true, "version": "22.1.0", "source": "managed"}, "installs_allowed": true})
}

fn config(runner_source: &str) -> Value {
    json!({"writable": true,
        "apps": {
            "node": {"key": "apps.node", "label": "Node.js for apps", "help": "auto, managed, system or a path.",
                     "placeholder": "auto", "value": "auto", "source": "default", "default": "auto"},
            "host": {"key": "apps.host", "label": "Where apps listen (deprecated)", "help": "Deprecated.",
                     "placeholder": "127.0.0.1", "value": "127.0.0.1", "source": "default", "default": "127.0.0.1"}
        },
        "triage_repo_root": {"source": "stored", "value": "/srv/repo", "default_path": "/data/backlog",
                             "available": true, "label": "Backlog folder", "help": "The folder whose docs/backlog holds the items."},
        "backlog_exec_runner": {"value": false, "source": runner_source, "label": "Backlog exec runner",
                                "help": "Runs queued items.", "flag": "--exec-runner on|off"},
        "process_manager": {"value": true, "source": "stored", "label": "Process manager", "help": "Powers Services."}
    })
}

fn page(size: (i32, i32), runner_source: &str) -> r8w4::Harness {
    let mut h = harness(size, Mount::Page(page_view));
    h.admin();
    h.store
        .apps
        .overview
        .set(Loadable::Ready(AppsOverview::from_value(&overview())));
    h.store
        .runtime_config
        .set(Loadable::Ready(RuntimeConfigData::from_value(&config(
            runner_source,
        ))));
    h.turns(3);
    h
}

fn saves(h: &mut r8w4::Harness) -> Vec<(Value, u64)> {
    h.sent()
        .into_iter()
        .filter_map(|c| match c {
            Cmd::SaveRuntimeConfig { body, form_id } => Some((body.0, form_id.unwrap_or(0))),
            _ => None,
        })
        .collect()
}

#[test]
fn the_page_has_no_settings_disclosures() {
    for size in SIZES {
        let mut h = page(size, "default");
        let s = h.shoot("apps");
        assert!(!s.contains("Advanced"), "{s}");
        assert!(!s.contains("backlog settings"), "{s}");
        h.assert_fits();
    }
}

#[test]
fn a_opens_apps_settings_and_a_row_applies_on_enter() {
    for size in SIZES {
        let mut h = page(size, "default");
        let s = h.key(b"a");
        h.shoot("apps-settings-overlay");
        assert!(s.contains("Apps settings"), "{s}");
        assert!(s.contains("Node.js for apps: auto"), "{s}");
        assert!(
            !s.contains("Where apps listen"),
            "deprecated host hidden while not saved:\n{s}"
        );
        assert!(!s.contains("Save"), "no Save button:\n{s}");
        h.sent();
        h.key(b"\r");
        h.type_text("managed");
        h.key(b"\r");
        let sv = saves(&mut h);
        assert_eq!(sv.len(), 1, "{sv:?}");
        assert_eq!(sv[0].0, json!({"apps.node": "managed"}));
        // The gateway answers: "Saved" beside the row.
        h.ui.write_done.set(Some((sv[0].1, Ok("applied".into()))));
        let s = h.text();
        assert!(s.contains("Saved"), "{s}");
    }
}

#[test]
fn g_on_the_continuum_card_opens_its_settings() {
    for size in SIZES {
        let mut h = page(size, "default");
        h.store.apps.sel.set(1);
        let s = h.turns(3);
        assert!(s.contains("g Settings"), "the gear beside Open:\n{s}");
        let s = h.key(b"g");
        h.shoot("apps-continuum-settings");
        assert!(s.contains("Continuum settings"), "{s}");
        assert!(s.contains("Backlog folder: /srv/repo"), "{s}");
        assert!(
            s.contains("[ ] Backlog exec runner") && s.contains("[x] Process manager"),
            "{s}"
        );
        assert!(!s.to_lowercase().contains("environment (legacy)"), "{s}");
        h.assert_fits();
    }
}

#[test]
fn continuum_switches_apply_at_once_and_the_folder_clears_to_the_gateways_own() {
    let mut h = page((120, 40), "default");
    h.store.apps.sel.set(1);
    h.turns(2);
    h.key(b"g");
    h.sent();
    h.key(b"\x1b[B");
    h.key(b" ");
    assert_eq!(saves(&mut h)[0].0, json!({"backlog_exec_runner": true}));
    // The folder: Enter, clear, Enter → null (the gateway's own folder).
    h.key(b"\x1b[A");
    h.key(b"\r");
    h.key(b"\x1b[F");
    for _ in 0..12 {
        h.key(b"\x7f");
    }
    h.key(b"\r");
    assert_eq!(saves(&mut h)[0].0, json!({"triage_repo_root": null}));
}

#[test]
fn a_flag_locked_switch_says_why_and_sends_nothing() {
    let mut h = page((120, 40), "flag");
    h.store.apps.sel.set(1);
    h.turns(2);
    h.key(b"g");
    h.sent();
    h.key(b"\x1b[B");
    let s = h.key(b" ");
    assert!(
        s.contains("Set by the launch flag --exec-runner on|off for this run."),
        "{s}"
    );
    assert!(saves(&mut h).is_empty());
}

#[test]
fn g_on_another_card_says_it_has_no_settings() {
    let mut h = page((80, 24), "default");
    let s = h.key(b"g");
    assert!(s.contains("Flow Editor has no settings of its own"), "{s}");
}

/// Live: a Continuum switch and an apps setting round-trip on the gateway.
#[test]
#[ignore]
fn live_continuum_and_apps_settings() {
    let Some((url, token)) = live_env() else {
        eprintln!("R8W4_URL/R8W4_TOKEN not set — skipped");
        return;
    };
    let mut h = live((120, 40), Mount::Page(page_view), &url, &token);
    h.tx.send(Cmd::LoadApps { latest: false }).unwrap();
    h.until("config", |h, _| {
        h.store
            .runtime_config
            .with_untracked(|r| matches!(r, Loadable::Ready(_)))
    });
    h.until("apps", |h, _| {
        h.store
            .apps
            .overview
            .with_untracked(|r| matches!(r, Loadable::Ready(_)))
    });
    let i = h
        .store
        .apps
        .overview
        .with_untracked(|o| {
            o.ready()
                .unwrap()
                .apps
                .iter()
                .position(|a| a.id == "continuum")
        })
        .expect("continuum listed");
    h.store.apps.sel.set(i);
    h.turns(2);
    let before = gw("GET", &url, &token, "/admin/runtime-config", None);
    let was = before["process_manager"]["value"]
        .as_bool()
        .unwrap_or(false);
    h.key(b"g");
    h.until_text("Continuum settings");
    h.key(b"\x1b[B");
    h.key(b"\x1b[B");
    h.key(b" ");
    // "Saved" beside the row (not the "Saved setting" source word), and
    // the console's re-read shows the new state.
    h.until("saved", |h, s| {
        s.lines()
            .any(|l| l.trim_matches(|c| c == '│' || c == ' ') == "Saved")
            && h.store.runtime_config.with_untracked(|r| match r {
                Loadable::Ready(d) => d
                    .backlog
                    .iter()
                    .any(|b| b.key == "process_manager" && (b.value == "on") == !was),
                _ => false,
            })
    });
    h.shoot("live-continuum-saved");
    let now = gw("GET", &url, &token, "/admin/runtime-config", None);
    assert_eq!(now["process_manager"]["value"], json!(!was), "{now}");
    h.key(b" ");
    h.until("restored", |_, _| {
        gw("GET", &url, &token, "/admin/runtime-config", None)["process_manager"]["value"]
            == json!(was)
    });
}
