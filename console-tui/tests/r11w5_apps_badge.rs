//! R11.3 (round 11): the Apps status badge is the start/stop control, as in
//! the web console. The badge cell of the selected row is selectable (→
//! moves onto it, ← back); Enter / Space on it — or `s` anywhere — does the
//! badge's action: Running → POST /stop, Stopped → POST /launch (the
//! Assistant: its Open). The words are the gateway's (`status_control`):
//! the hint line says "s Running — click to stop" like the web's kit
//! tooltip; an app started outside the gateway is NOT selectable and says
//! "Started outside the gateway — stop it where it was started"; a
//! non-admin gets "Only an admin can start or stop apps". Hermetic: the
//! harness's command channel is the only way out of the UI.
//!
//! Captures (80x24 + 120x40): R8W4_SHOTS_DIR=<dir> cargo test --test r11w5_apps_badge

mod r8w4;

use abstractgateway_console::store::apps::{
    badge_tip, badge_verb, secondary_verbs, AppRow, AppVerb, AppsOverview,
};
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::{self, apps};
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, Mount, SIZES};
use serde_json::{json, Value};

const RUN_TIP: &str = "Running — click to stop";
const START_TIP: &str = "Stopped — click to start";
const QUIT_TIP: &str = "Running — click to quit";
const EXTERNAL_TIP: &str = "Started outside the gateway — stop it where it was started";
const ADMIN_TIP: &str = "Only an admin can start or stop apps";

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    apps::view(cx, ctx, &t)
}

fn badge(label: &str, tone: &str, action: Option<&str>, enabled: bool, tip: Option<&str>) -> Value {
    json!({"label": label, "tone": tone, "busy": false, "action": action, "enabled": enabled, "tip": tip})
}

fn web(id: &str, name: &str, running: bool, admin: bool) -> Value {
    let (label, tone, action, tip) = if running {
        ("Running", "ok", "stop", RUN_TIP)
    } else {
        ("Stopped", "muted", "launch", START_TIP)
    };
    json!({"id": id, "name": name, "kind": "web", "description": name, "package": format!("@abstractframework/{id}"),
     "installed": true, "version": "0.7.0", "running": running, "status": if running {"running"} else {"stopped"},
     "source": "gateway", "port": if running {json!(3105)} else {Value::Null},
     "actions": if running {json!(["open", "stop", "logs"])} else {json!(["launch", "logs"])},
     "install_parts": ["web"], "interfaces": [{"kind": "web"}],
     "status_control": if admin { badge(label, tone, Some(action), true, Some(tip)) } else { badge(label, tone, Some(action), false, Some(ADMIN_TIP)) }})
}

fn external() -> Value {
    json!({"id": "observer", "name": "Observer", "kind": "web", "description": "Observer",
     "package": "@abstractframework/observer", "installed": true, "version": "0.7.0", "running": true,
     "status": "running", "managed": false, "source": "external",
     "external": {"port": 3001, "pid": 77, "detail": "Started outside the gateway on port 3001"},
     "url": "http://127.0.0.1:3001/", "port": 3001, "actions": ["open"], "install_parts": ["web"],
     "interfaces": [{"kind": "web"}],
     "status_control": badge("Running", "ok", None, false, Some(EXTERNAL_TIP))})
}

fn assistant(running_ours: bool) -> Value {
    json!({"id": "assistant", "name": "Assistant", "kind": "desktop", "description": "Menu-bar assistant.",
     "package": "abstractassistant", "installed": true, "version": "0.13.0", "running": running_ours,
     "status": if running_ours {"running"} else {"stopped"},
     "actions": if running_ours {json!(["open", "stop"])} else {json!(["open"])},
     "install_parts": ["desktop"], "interfaces": [],
     "status_control": if running_ours { badge("Running", "ok", Some("stop"), true, Some(QUIT_TIP)) }
                       else { badge("Stopped", "muted", Some("launch"), true, Some(START_TIP)) },
     "desktop": {"location": "/venv/bin/abstractassistant", "launch_command": "/venv/bin/abstractassistant",
                 "launch_available": true, "launch_blocked": null, "launch_blocked_reason": null,
                 "started_by_gateway": running_ours}})
}

fn overview(admin: bool, assistant_running: bool) -> Value {
    json!({"apps": [web("flow", "Flow Editor", true, admin), web("code", "Code", false, admin), external(), assistant(assistant_running)],
           "runtime": {"node": {"available": true, "version": "24.0.0", "source": "system"}}, "install_allowed": true})
}

fn page(size: (i32, i32), admin: bool, assistant_running: bool) -> r8w4::Harness {
    let mut h = harness(size, Mount::Page(page_view));
    if admin {
        h.admin();
    } else {
        h.identity("ana", false);
    }
    h.store
        .apps
        .overview
        .set(Loadable::Ready(AppsOverview::from_value(&overview(
            admin,
            assistant_running,
        ))));
    h.turns(3);
    h.sent();
    h
}

fn select(h: &mut r8w4::Harness, id: &str) -> String {
    let i = h
        .store
        .apps
        .overview
        .with_untracked(|o| o.ready().unwrap().apps.iter().position(|a| a.id == id))
        .unwrap();
    h.store.apps.sel.set(i);
    h.turns(3)
}

fn acts(cmds: Vec<Cmd>) -> Vec<(String, AppVerb)> {
    cmds.into_iter()
        .filter_map(|c| match c {
            Cmd::AppAct { app_id, verb, .. } => Some((app_id, verb)),
            _ => None,
        })
        .collect()
}

fn flat(s: &str) -> String {
    s.lines()
        .map(|l| l.trim_matches(|c| c == '│' || c == ' '))
        .collect::<Vec<_>>()
        .join(" ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn notice(h: &r8w4::Harness) -> String {
    h.store.notice.get_untracked().unwrap_or_default()
}


#[test]
fn badge_verb_follows_the_gateways_status_control() {
    let row = |v: Value| AppRow::from_value(&v).unwrap();
    let b = badge_verb(&row(web("flow", "Flow", true, true)), None).unwrap();
    assert_eq!(
        (b.verb, b.label.as_str(), b.available.clone()),
        (AppVerb::Stop, "Running", Ok(()))
    );
    let b = badge_verb(&row(web("code", "Code", false, true)), None).unwrap();
    assert_eq!(
        (b.verb, b.label.as_str(), b.available.clone()),
        (AppVerb::Start, "Stopped", Ok(()))
    );
    let b = badge_verb(&row(external()), None).unwrap();
    assert_eq!(b.available, Err(EXTERNAL_TIP.to_string()));
    let b = badge_verb(&row(web("flow", "Flow", true, false)), None).unwrap();
    assert_eq!(b.available, Err(ADMIN_TIP.to_string()));
    let b = badge_verb(&row(assistant(true)), None).unwrap();
    assert_eq!((b.verb, b.available.clone()), (AppVerb::Stop, Ok(())));
    let b = badge_verb(&row(assistant(false)), None).unwrap();
    assert_eq!(
        (b.verb, b.available.clone()),
        (AppVerb::DesktopOpen, Ok(()))
    );
    assert_eq!(
        badge_tip(&row(web("flow", "Flow", true, true))).as_deref(),
        Some(RUN_TIP)
    );
    // Stop / Start are the badge alone: no separate verb (one control per action).
    let r = row(web("flow", "Flow", true, true));
    assert!(secondary_verbs(&r, None, None, true)
        .iter()
        .all(|v| !matches!(v.verb, AppVerb::Stop | AppVerb::Start)));
}

#[test]
fn running_badge_is_a_button_that_stops() {
    // R15: the badge is a real button in the Status cell — a click (or Tab
    // onto it + Enter, or `s`) does its action; its LABEL is the state, the
    // gateway's sentence is its tooltip / focused-control line (A3).
    for size in SIZES {
        let mut h = page(size, true, false);
        let s = select(&mut h, "flow");
        assert!(s.contains("Running"), "{s}");
        assert!(!s.contains("click to stop") || flat(&s).contains("Running"), "{s}");
        h.key(b"\t"); // the selected row's first control: its badge
        assert_eq!(
            h.ui.focus_line.get_untracked().as_deref(),
            Some(format!("{RUN_TIP}  (s)").as_str())
        );
        let _ = h.shoot("apps-badge-running");
        h.assert_fits();
        h.key(b"\r");
        assert_eq!(acts(h.sent()), vec![("flow".to_string(), AppVerb::Stop)]);
        // …and a click on another row's badge.
        let mut h = page(size, true, false);
        h.click_after("Flow Editor", "Running");
        assert_eq!(acts(h.sent()), vec![("flow".to_string(), AppVerb::Stop)]);
    }
}

#[test]
fn stopped_badge_starts_with_a_click_and_enter_on_the_row_opens() {
    let mut h = page((120, 40), true, false);
    h.click_after("Code", "Stopped");
    let _ = h.shoot("apps-badge-stopped");
    assert_eq!(acts(h.sent()), vec![("code".to_string(), AppVerb::Start)]);
    // Enter on the table is the row's primary action: Open.
    select(&mut h, "code");
    h.key(b"\r");
    let sent = acts(h.sent());
    assert_eq!(sent, vec![("code".to_string(), AppVerb::Open)]);
}

#[test]
fn s_anywhere_is_the_badge() {
    let mut h = page((120, 40), true, false);
    select(&mut h, "flow");
    h.key(b"s");
    assert_eq!(acts(h.sent()), vec![("flow".to_string(), AppVerb::Stop)]);
    select(&mut h, "code");
    h.key(b"s");
    assert_eq!(acts(h.sent()), vec![("code".to_string(), AppVerb::Start)]);
    h.key(b"x");
    assert!(acts(h.sent()).is_empty(), "x is not a stop key any more");
}

#[test]
fn external_badge_is_not_a_control_and_says_why() {
    for size in SIZES {
        let mut h = page(size, true, false);
        let s = select(&mut h, "observer");
        assert!(
            flat(&s).contains(&format!("Running: {EXTERNAL_TIP}")),
            "the details line:\n{s}"
        );
        // A click on its badge does nothing (a plain pill, its tooltip says why).
        h.click_after("Observer", "Running");
        let _ = h.shoot("apps-badge-external");
        h.assert_fits();
        h.key(b"s");
        assert!(notice(&h).contains(EXTERNAL_TIP), "{}", notice(&h));
        assert!(
            acts(h.sent()).is_empty(),
            "nothing is sent for an external app"
        );
    }
}

#[test]
fn non_admin_badge_is_off_with_the_sentence() {
    let mut h = page((120, 40), false, false);
    let s = select(&mut h, "flow");
    assert!(flat(&s).contains(&format!("Running: {ADMIN_TIP}")), "{s}");
    h.click_after("Flow Editor", "Running");
    h.key(b"s");
    assert!(notice(&h).contains(ADMIN_TIP), "{}", notice(&h));
    assert!(acts(h.sent()).is_empty());
    let _ = h.shoot("apps-badge-non-admin");
}

#[test]
fn assistant_badge_quits_or_opens() {
    let mut h = page((120, 40), true, true);
    select(&mut h, "assistant");
    h.click_after("Assistant", "Running");
    let _ = h.shoot("apps-badge-assistant-running");
    assert_eq!(
        acts(h.sent()),
        vec![("assistant".to_string(), AppVerb::Stop)]
    );
    let mut h = page((120, 40), true, false);
    select(&mut h, "assistant");
    h.key(b"s");
    assert_eq!(
        acts(h.sent()),
        vec![("assistant".to_string(), AppVerb::DesktopOpen)]
    );
}
