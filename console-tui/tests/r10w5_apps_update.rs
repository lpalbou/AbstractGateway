//! R10.6 (round 10): every Apps row shows an available update. The
//! Assistant updates with `u` like a browser app (POST /apps/assistant/update,
//! the gateway's label and tooltip word for word); a row started outside
//! the gateway shows "Latest x.y.z" and the gateway's sentence, and `u` there
//! sends nothing. Hermetic: the harness's command channel is the only way
//! out of the UI. Live drive (ignored) against a scratch gateway with a fake
//! PyPI that offers a newer Assistant:
//!
//!   R8W4_URL=http://127.0.0.1:<port> R8W4_TOKEN=<admin token> \
//!   R8W4_SHOTS_DIR=<dir> cargo test --test r10w5_apps_update -- --ignored --test-threads 1

mod r8w4;

use abstractgateway_console::store::apps::{secondary_verbs, AppRow, AppVerb, AppsOverview};
use abstractgateway_console::store::Loadable;
use abstractgateway_console::ui::{self, apps};
use abstractgateway_console::worker::Cmd;
use r8w4::{harness, live, live_env, Mount, SIZES};
use serde_json::{json, Value};

const TIP: &str = "Install the newest Assistant (0.14.0); a running app restarts on it";
const EXTERNAL: &str = "Started outside the gateway — update it where it was installed";

fn page_view(ctx: &ui::Ctx, cx: abstracttui::prelude::Scope) -> abstracttui::prelude::View {
    let t = abstracttui::prelude::use_theme(cx).get().tokens;
    apps::view(cx, ctx, &t)
}

fn assistant(update: bool) -> Value {
    json!({"id": "assistant", "name": "Assistant", "kind": "desktop", "description": "Menu-bar assistant.",
     "package": "abstractassistant", "installed": true, "version": "0.13.0",
     "latest_version": if update { "0.14.0" } else { "0.13.0" }, "update_available": update,
     "update_label": if update { json!("Update to 0.14.0") } else { Value::Null },
     "update_tip": if update { json!(TIP) } else { Value::Null },
     "running": false, "status": "stopped",
     "actions": if update { json!(["open", "update"]) } else { json!(["open"]) },
     "install_parts": ["desktop"], "interfaces": [],
     "desktop": {"location": "/venv/bin/abstractassistant", "launch_command": "/venv/bin/abstractassistant",
                 "launch_available": true, "launch_blocked": null, "launch_blocked_reason": null,
                 "other_running": null, "restart_note": null, "started_by_gateway": false, "latest_error": null}})
}

fn overview() -> Value {
    json!({"apps": [
        {"id": "continuum", "name": "Continuum", "kind": "web", "description": "Backlog.",
         "package": "@abstractframework/continuum", "installed": true, "version": "0.3.2",
         "latest_version": "0.4.0", "update_available": true, "update_label": null, "update_tip": EXTERNAL,
         "running": true, "status": "running", "managed": false, "source": "external",
         "external": {"port": 3002, "pid": 55398, "detail": "Started outside the gateway on port 3002"},
         "url": "http://127.0.0.1:3002/", "port": 3002, "actions": ["open"],
         "install_parts": ["web"], "interfaces": [{"kind": "web"}]},
        assistant(true)
    ], "runtime": {"node": {"available": true, "version": "24.0.0", "source": "system"}}, "install_allowed": true})
}

fn page(size: (i32, i32)) -> r8w4::Harness {
    let mut h = harness(size, Mount::Page(page_view));
    h.admin();
    h.store
        .apps
        .overview
        .set(Loadable::Ready(AppsOverview::from_value(&overview())));
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

#[test]
fn the_assistant_row_offers_u_with_the_gateways_label() {
    let row = AppRow::from_value(&assistant(true)).unwrap();
    let v = secondary_verbs(&row, None, None, true);
    let u = v
        .iter()
        .find(|v| v.verb == AppVerb::Update)
        .expect("u on the Assistant");
    assert_eq!(u.label, "Update to 0.14.0");
    assert_eq!(u.available, Ok(()));
    // Up to date: still listed, off, with the reason.
    let row = AppRow::from_value(&assistant(false)).unwrap();
    let v = secondary_verbs(&row, None, None, true);
    let u = v.iter().find(|v| v.verb == AppVerb::Update).unwrap();
    assert!(u.available.is_err());
    // Not an admin: the reason, never the action.
    let row = AppRow::from_value(&assistant(true)).unwrap();
    let v = secondary_verbs(&row, None, None, false);
    let u = v.iter().find(|v| v.verb == AppVerb::Update).unwrap();
    assert_eq!(u.available, Err("Only an admin can update apps".into()));
}

#[test]
fn u_on_the_assistant_confirms_with_the_tooltip_then_sends_update() {
    for size in SIZES {
        let mut h = page(size);
        let s = select(&mut h, "assistant");
        assert!(flat(&s).contains("u Update to 0.14.0"), "{s}");
        assert!(flat(&s).contains("Latest 0.14.0"), "{s}");
        let s = h.shoot("apps-assistant-update");
        h.assert_fits();
        let _ = s;
        let s = h.key(b"u");
        assert!(
            flat(&s).contains(&format!("{TIP}.")),
            "the web tooltip, word for word:\n{s}"
        );
        assert!(acts(h.sent()).is_empty(), "nothing before the confirmation");
        h.key(b"\r");
        assert_eq!(
            acts(h.sent()),
            vec![("assistant".to_string(), AppVerb::Update)]
        );
    }
}

#[test]
fn an_external_row_shows_latest_and_u_sends_nothing() {
    let mut h = page((120, 40));
    let s = select(&mut h, "continuum");
    let f = flat(&s);
    assert!(f.contains(&format!("Latest 0.4.0 · {EXTERNAL}")), "{s}");
    assert!(
        !f.contains("u Update"),
        "no update verb on an external row:\n{s}"
    );
    let _ = h.shoot("apps-external-latest");
    let s = h.key(b"u");
    let sent = acts(h.sent());
    assert!(
        sent.is_empty(),
        "u on an external row sends nothing: {sent:?}"
    );
    assert!(
        !flat(&s).contains("Install the newest"),
        "no confirmation opened:\n{s}"
    );
    let n = h.store.notice.get_untracked().unwrap_or_default();
    assert!(n.contains(EXTERNAL), "the reason is said: {n:?}");
}

#[test]
fn restart_and_other_artifact_notes_render() {
    let mut a = assistant(false);
    a["running"] = json!(true);
    a["desktop"]["restart_note"] = json!("Quit it and open it again to run 0.14.0");
    let mut h = harness((120, 40), Mount::Page(page_view));
    h.admin();
    h.store
        .apps
        .overview
        .set(Loadable::Ready(AppsOverview::from_value(
            &json!({"apps": [a]}),
        )));
    let s = h.turns(3);
    assert!(
        flat(&s).contains("Quit it and open it again to run 0.14.0"),
        "{s}"
    );
}

#[test]
fn a_source_checkout_assistant_shows_latest_and_u_sends_nothing() {
    let mut a = assistant(false);
    a["latest_version"] = json!("0.14.0");
    a["update_available"] = json!(true);
    a["update_tip"] = json!("Installed from a source checkout — update it there");
    a["desktop"]["source_checkout"] = json!(true);
    let mut h = harness((120, 40), Mount::Page(page_view));
    h.admin();
    h.store
        .apps
        .overview
        .set(Loadable::Ready(AppsOverview::from_value(
            &json!({"apps": [a]}),
        )));
    let s = h.turns(3);
    let f = flat(&s);
    assert!(
        f.contains("Latest 0.14.0 · Installed from a source checkout — update it there"),
        "{s}"
    );
    assert!(!f.contains("u Update"), "{s}");
    let _ = h.shoot("apps-assistant-source-checkout");
    h.sent();
    let s = h.key(b"u");
    assert!(
        acts(h.sent()).is_empty(),
        "u on a source checkout sends nothing"
    );
    assert!(!flat(&s).contains("Install the newest"), "{s}");
}

/// Live: the scratch gateway's fake PyPI offers a newer Assistant; the
/// row says so with the gateway's words.
#[test]
#[ignore]
fn live_assistant_row_shows_the_update() {
    let Some((url, token)) = live_env() else {
        eprintln!("R8W4_URL/R8W4_TOKEN not set — skipped");
        return;
    };
    let mut h = live((120, 40), Mount::Page(page_view), &url, &token);
    h.until("signed in", |h, _| {
        h.store
            .conn
            .with_untracked(abstractgateway_console::store::ConnPhase::is_connected)
    });
    h.tx.send(Cmd::LoadApps { latest: true }).unwrap();
    h.until("the overview", |h, _| {
        h.store
            .apps
            .overview
            .with_untracked(|o| o.ready().is_some())
    });
    let s = select(&mut h, "assistant");
    let _ = h.shoot("live-apps-assistant");
    assert!(flat(&s).contains("u Update to"), "{s}");
}
