//! R15 (DESIGN-TUI.md §6.1): a synthesized mouse click for EVERY Apps
//! button — each row's labelled actions, the status badge control, the
//! Continuum gear, the head's Check again + Apps settings gear and the
//! Node.js button — through the real input pipeline. The meta-test
//! enumerates `apps::app_actions` over the fixture rows: an action without
//! a click test here is RED.

mod r8w4;

use std::collections::BTreeSet;

use abstractgateway_console::store::apps::{AppVerb, AppsOverview};
use abstractgateway_console::store::{Loadable, RuntimeConfigData};
use abstractgateway_console::ui::{self, apps};
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount};
use serde_json::{json, Value};

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    apps::view(cx, ctx, &t)
}

fn badge(label: &str, tone: &str, action: Option<&str>, enabled: bool, tip: &str) -> Value {
    json!({"label": label, "tone": tone, "busy": false, "action": action, "enabled": enabled, "tip": tip})
}

fn overview() -> Value {
    json!({"apps": [
        // Running, managed, with an update and logs.
        {"id": "flow", "name": "Flow Editor", "kind": "web", "description": "Flows.",
         "package": "@abstractframework/flow", "installed": true, "version": "0.8.0",
         "latest_version": "0.8.1", "update_available": true,
         "update_label": "Update to 0.8.1", "update_tip": "Install the newest Flow Editor (0.8.1); a running app restarts on it",
         "running": true, "status": "running", "source": "gateway", "port": 3105,
         "actions": ["open", "stop", "logs", "update"], "install_parts": ["web"], "interfaces": [{"kind": "web"}],
         "status_control": badge("Running", "ok", Some("stop"), true, "Running — click to stop")},
        // Stopped, managed, with a terminal version installed.
        {"id": "code", "name": "Code", "kind": "web", "description": "Code.",
         "package": "@abstractframework/code", "installed": true, "version": "0.11.0",
         "running": false, "status": "stopped", "source": "gateway",
         "actions": ["launch", "logs"], "install_parts": ["web", "tui"],
         "interfaces": [{"kind": "web"}, {"kind": "tui", "installed": true, "version": "0.9.0",
                 "launch_available": true, "install_available": true,
                 "command": "abstractcode", "update_available": true, "latest_version": "0.9.1"}],
         "status_control": badge("Stopped", "muted", Some("launch"), true, "Stopped — click to start")},
        // Not installed.
        {"id": "observer", "name": "Observer", "kind": "web", "description": "Observer.",
         "package": "@abstractframework/observer", "installed": false, "running": false,
         "status": "not_installed", "actions": ["install"], "install_available": true,
         "install_parts": ["web"], "interfaces": [{"kind": "web"}]},
        // Continuum: running, with its gear.
        {"id": "continuum", "name": "Continuum", "kind": "web", "description": "Backlog.",
         "package": "@abstractframework/continuum", "installed": true, "version": "0.7.0",
         "running": true, "status": "running", "source": "gateway", "port": 3106,
         "actions": ["open", "stop", "logs"], "install_parts": ["web"], "interfaces": [{"kind": "web"}],
         "status_control": badge("Running", "ok", Some("stop"), true, "Running — click to stop")},
        // An install in flight (Cancel).
        {"id": "entity", "name": "Entity", "kind": "web", "description": "Entity.",
         "package": "@abstractframework/entity", "installed": false, "running": false,
         "status": "not_installed", "actions": ["install"], "install_available": true,
         "install_parts": ["web"], "interfaces": [{"kind": "web"}],
         "active_job": {"id": "job-1", "kind": "install", "state": "running", "progress": 0.4, "parts": []}}
    ], "runtime": {"node": {"available": false, "version": null, "source": "none", "install_available": true}},
       "install_allowed": true})
}

fn config() -> Value {
    json!({"writable": true,
        "apps": {
            "node": {"key": "apps.node", "label": "Node.js for apps", "help": "auto, managed, system or a path.",
                     "placeholder": "auto", "value": "auto", "source": "default", "default": "auto"}
        },
        "triage_repo_root": {"source": "stored", "value": "/srv/repo", "default_path": "/data/backlog",
                             "available": true, "label": "Backlog folder", "help": "The folder whose docs/backlog holds the items."},
        "backlog_exec_runner": {"value": false, "source": "default", "label": "Backlog exec runner", "help": "Runs queued items."},
        "process_manager": {"value": true, "source": "stored", "label": "Process manager", "help": "Powers Services."}
    })
}

fn page() -> r8w4::Harness {
    let mut h = harness((140, 44), Mount::Page(page_view));
    h.admin();
    h.store
        .apps
        .overview
        .set(Loadable::Ready(AppsOverview::from_value(&overview())));
    h.store
        .runtime_config
        .set(Loadable::Ready(RuntimeConfigData::from_value(&config())));
    h.turns(3);
    h.sent();
    h
}

fn acts(cmds: Vec<Cmd>) -> Vec<(String, AppVerb)> {
    cmds.into_iter()
        .filter_map(|c| match c {
            Cmd::AppAct { app_id, verb, .. } => Some((app_id, verb)),
            _ => None,
        })
        .collect()
}

/// Click `needle` on `name`'s table row (searched right of the name).
fn click_row(h: &mut r8w4::Harness, name: &str, needle: &str) -> String {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .find(|(_, l)| l.starts_with(&format!(" {name} ")))
        .unwrap_or_else(|| panic!("{name} row:\n{screen}"));
    let start = line.find(name).unwrap() + name.len();
    let byte = start
        + line[start..]
            .find(needle)
            .unwrap_or_else(|| panic!("{needle:?} not on {name}'s row:\n{screen}"));
    let x = line[..byte].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes())
}

/// Answer an install/update confirmation BY MOUSE (R15 F1): click its
/// action button — the button left of "Not now" on the dialog's button row.
fn confirm(h: &mut r8w4::Harness) {
    let screen = h.turns(1);
    let (y, line) = screen
        .lines()
        .enumerate()
        .filter(|(_, l)| l.contains(" Not now "))
        .last()
        .unwrap_or_else(|| panic!("no confirmation button row:\n{screen}"));
    let not_now = line.rfind(" Not now ").unwrap();
    // The action button: the last non-space run before "Not now".
    let before = line[..not_now].trim_end();
    let start = before
        .char_indices()
        .rev()
        .find(|(_, c)| *c == '│')
        .map(|(i, c)| i + c.len_utf8())
        .unwrap_or(0);
    let label = before[start..].trim();
    assert!(
        !label.is_empty(),
        "no action button left of Not now:\n{screen}"
    );
    let b = before.rfind(label).unwrap();
    let x = line[..b].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
}

fn offered() -> BTreeSet<(String, &'static str)> {
    let d = AppsOverview::from_value(&overview());
    let mut out = BTreeSet::new();
    for a in &d.apps {
        for x in apps::app_actions(a, a.active_job.as_ref(), None, true) {
            if x.is_enabled() {
                out.insert((a.id.clone(), x.id));
            }
        }
    }
    out
}

fn covered() -> BTreeSet<(String, &'static str)> {
    [
        ("flow", "open"),
        ("flow", "log"),
        ("flow", "update"),
        ("code", "open"),
        ("code", "log"),
        ("code", "terminal"),
        ("code", "install_terminal"),
        ("observer", "install"),
        ("continuum", "open"),
        ("continuum", "log"),
        ("continuum", "settings"),
        ("entity", "cancel"),
    ]
    .into_iter()
    .map(|(a, b)| (a.to_string(), b))
    .collect()
}

#[test]
fn every_enabled_app_action_has_a_click_test() {
    let missing: Vec<_> = offered().difference(&covered()).cloned().collect();
    assert!(
        missing.is_empty(),
        "app actions without a click test: {missing:?}"
    );
}

#[test]
fn the_badges_are_buttons() {
    let mut h = page();
    click_row(&mut h, "Flow Editor", "Running");
    assert_eq!(acts(h.sent()), vec![("flow".to_string(), AppVerb::Stop)]);
    let mut h = page();
    click_row(&mut h, "Code", "Stopped");
    assert_eq!(acts(h.sent()), vec![("code".to_string(), AppVerb::Start)]);
    let mut h = page();
    click_row(&mut h, "Continuum", "Running");
    assert_eq!(
        acts(h.sent()),
        vec![("continuum".to_string(), AppVerb::Stop)]
    );
}

#[test]
fn every_row_action_button_does_what_it_says() {
    // Open (a running app: the signed-in link).
    let mut h = page();
    click_row(&mut h, "Flow Editor", "Open");
    assert_eq!(acts(h.sent()), vec![("flow".to_string(), AppVerb::Open)]);
    // Show log → the log modal + its read.
    let mut h = page();
    let s = click_row(&mut h, "Flow Editor", "Show log");
    assert!(
        h.sent()
            .iter()
            .any(|c| matches!(c, Cmd::LoadAppLog { app_id, .. } if app_id == "flow")),
        "{s}"
    );
    // Update → the gateway's tooltip as the confirmation; then the update.
    let mut h = page();
    let s = click_row(&mut h, "Flow Editor", "Update to 0.8.1");
    assert!(s.contains("Install the newest Flow Editor (0.8.1)"), "{s}");
    confirm(&mut h);
    assert_eq!(acts(h.sent()), vec![("flow".to_string(), AppVerb::Update)]);
    // Code: Open (start first), its log, the terminal version.
    let mut h = page();
    click_row(&mut h, "Code", "Open");
    assert_eq!(acts(h.sent()), vec![("code".to_string(), AppVerb::Open)]);
    let mut h = page();
    click_row(&mut h, "Code", "Show log");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::LoadAppLog { app_id, .. } if app_id == "code")));
    let mut h = page();
    let s = click_row(&mut h, "Code", ">_ Open in Terminal");
    let _ = s;
    assert_eq!(
        acts(h.sent()),
        vec![("code".to_string(), AppVerb::OpenTerminal)]
    );
    let mut h = page();
    click_row(&mut h, "Code", "terminal app");
    let s = h.turns(3);
    let flat = s.split_whitespace().collect::<Vec<_>>().join(" ");
    assert!(
        flat.contains("terminal app?"),
        "[{flat}] notice={:?}",
        h.store.notice.get_untracked()
    );
    confirm(&mut h);
    assert_eq!(
        acts(h.sent()),
        vec![("code".to_string(), AppVerb::InstallTerminal)]
    );
    // Observer: Install (confirmed).
    let mut h = page();
    let s = click_row(&mut h, "Observer", "Install");
    assert!(s.contains("Install Observer?"), "{s}");
    confirm(&mut h);
    assert_eq!(
        acts(h.sent()),
        vec![("observer".to_string(), AppVerb::Install)]
    );
    // Continuum: Open, log, its gear (Continuum settings).
    let mut h = page();
    click_row(&mut h, "Continuum", "Open");
    assert_eq!(
        acts(h.sent()),
        vec![("continuum".to_string(), AppVerb::Open)]
    );
    let mut h = page();
    click_row(&mut h, "Continuum", "Show log");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::LoadAppLog { app_id, .. } if app_id == "continuum")));
    let mut h = page();
    let s = click_row(&mut h, "Continuum", "⊛");
    assert!(
        s.contains("Continuum settings") && s.contains("Backlog folder"),
        "{s}"
    );
    // Entity: its install in flight → Cancel.
    let mut h = page();
    click_row(&mut h, "Entity", "Cancel");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::CancelAppJob { job_id, .. } if job_id == "job-1")));
}

#[test]
fn the_head_and_node_buttons_are_clickable() {
    let mut h = page();
    h.click_text("Check again");
    assert!(h
        .sent()
        .iter()
        .any(|c| matches!(c, Cmd::LoadApps { latest: true })));
    let mut h = page();
    let s = h.turns(1);
    let head = s
        .lines()
        .find(|l| l.contains("Check again"))
        .expect(&s)
        .to_string();
    let x = head[..head.find('⊛').expect("the Apps settings gear")]
        .chars()
        .count()
        + 1;
    let s = h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", 1, 1).as_bytes());
    assert!(
        s.contains("Apps settings") && s.contains("Node.js for apps"),
        "{s}"
    );
    let mut h = page();
    let s = h.click_text("Install Node.js");
    assert!(
        s.contains("Install Node.js into the gateway's own folder?"),
        "{s}"
    );
    confirm(&mut h);
    assert!(h.sent().iter().any(|c| matches!(c, Cmd::InstallAppsNode)));
}
