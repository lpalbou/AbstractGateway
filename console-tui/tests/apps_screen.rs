//! Headless tests for the Apps screen (ui/apps.rs): the web console's Apps
//! tab in the terminal. The real interface through AbstractTUI's capture
//! harness; the worker is a channel the test drains; gateway payloads are
//! applied to the store exactly as the worker's posted closures would.
//! The fixture is the live `GET /api/gateway/apps` shape (2026-09-27,
//! hermetic gateway), widened to every state the web card renders.

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::{mpsc, Arc};

use abstracttui::app::Driver;
use abstracttui::prelude::*;
use abstracttui::testing::CaptureTerm;
use serde_json::{json, Value};

use abstractcore_console::screens::{ScreensCtx, ScreensOptions};
use abstractcore_console::{ConsoleTransport, TransportError};

use abstractgateway_console::store::apps::{AppOpenLink, AppVerb, AppsOverview};
use abstractgateway_console::store::{ConnPhase, Identity, Loadable, Store};
use abstractgateway_console::ui::{self, Ctx, UiState};
use abstractgateway_console::worker::Cmd;

/// The shared Models/Engines screens are not under test here.
struct NoTransport;

impl ConsoleTransport for NoTransport {
    fn host_profile(&self) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("not under test"))
    }
    fn engines_status(&self, _probe: bool) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("not under test"))
    }
    fn models_catalog(
        &self,
        _q: &str,
        _e: Option<&str>,
        _f: bool,
    ) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("not under test"))
    }
    fn models_installed(&self, _p: Option<&str>) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("not under test"))
    }
    fn start_download(
        &self,
        _p: &str,
        _a: &str,
        _expected_bytes: Option<u64>,
    ) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("not under test"))
    }
    fn delete_model(&self, _p: &str, _a: &str, _f: bool) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("not under test"))
    }
    fn engine_install(&self, _id: &str, _d: bool) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("not under test"))
    }
    fn job(&self, _id: &str) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("not under test"))
    }
    fn cancel_job(&self, _id: &str) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("not under test"))
    }
}

struct Harness {
    app: App,
    term: CaptureTerm,
    driver: Driver,
    store: Store,
    ui: UiState,
    rx: mpsc::Receiver<Cmd>,
}

fn harness() -> Harness {
    harness_with(None, Rc::new(RefCell::new(Vec::new())))
}

/// `no_display`: the console's headless verdict; `opened`: every URL the
/// opener was asked to open (recorded, never opened).
fn harness_with(no_display: Option<String>, opened: Rc<RefCell<Vec<String>>>) -> Harness {
    let size = Size::new(150, 44);
    abstracttui::app::set_theme_by_id("abstract-dark");
    let mut app = App::new(size);
    let overlays = app.overlays();
    let quitter = app.quitter();
    let (tx, rx) = mpsc::channel::<Cmd>();
    let slot: Rc<RefCell<Option<(Store, UiState)>>> = Rc::new(RefCell::new(None));
    let out = slot.clone();
    app.mount(move |cx| {
        let store = Store::create(cx);
        let ui_state = UiState::create(cx, "http://127.0.0.1:8080".to_string(), String::new());
        *out.borrow_mut() = Some((store, ui_state));
        let transport: Arc<dyn ConsoleTransport> = Arc::new(NoTransport);
        let screens = ScreensCtx::new(
            cx,
            transport.clone(),
            overlays.clone(),
            cx.signal(abstractcore_console::screens::Access::Admin),
            ScreensOptions {
                notice: Some(store.notice),
                opener: Some({
                    let opened = opened.clone();
                    Rc::new(move |url: &str| {
                        opened.borrow_mut().push(url.to_string());
                        Ok(())
                    })
                }),
                ..ScreensOptions::default()
            },
        );
        let ctx = Ctx {
            tx: tx.clone(),
            overlays: overlays.clone(),
            quitter: quitter.clone(),
            store,
            ui: ui_state,
            modal: Rc::new(RefCell::new(None)),
            entity_drawer: Rc::new(RefCell::new(None)),
            env_token_set: false,
            no_display: no_display.clone(),
            prober: Rc::new(RefCell::new(None)),
            screens,
            screens_transport: transport,
        };
        ui::root(cx, ctx)
    })
    .expect("mount");
    let mut term = CaptureTerm::new(size);
    let cfg = RunConfig {
        probe: false,
        caps: Some(abstracttui::term::Capabilities::with(|c| {
            c.truecolor = true;
            c.colors_256 = true;
            c.unicode_ok = true;
        })),
        // Never touch the clipboard of whoever runs the suite.
        platform_clipboard: false,
        ..RunConfig::default()
    };
    let driver = Driver::new(&mut app, &mut term, cfg).expect("driver");
    let (store, ui) = slot.borrow().expect("created");
    Harness {
        app,
        term,
        driver,
        store,
        ui,
        rx,
    }
}

impl Harness {
    fn turns(&mut self, n: usize) -> String {
        let mut last = String::new();
        for _ in 0..n {
            self.driver
                .turn(&mut self.app, &mut self.term)
                .expect("turn");
            last = self.term.screen().to_text();
        }
        last
    }
    fn key(&mut self, bytes: &[u8]) -> String {
        self.term.push_input(bytes);
        self.turns(3)
    }
    fn drain(&mut self) -> Vec<Cmd> {
        let mut out = Vec::new();
        while let Ok(c) = self.rx.try_recv() {
            out.push(c);
        }
        out
    }
    fn connect(&mut self, admin: bool) {
        let id = Identity::from_me(&json!({
            "principal": {"user_id": if admin {"admin"} else {"ana"}, "tenant_id": "default",
                          "roles": if admin {json!(["admin"])} else {json!(["user"])}, "admin": admin},
            "auth": {"mode": "users"}, "routing": {"mode": "per-principal"}
        }))
        .unwrap();
        self.store.conn.set(ConnPhase::Connected(id));
        self.turns(1);
    }
    /// Browse mode on the Apps screen with the fixture loaded.
    fn on_apps(&mut self, admin: bool) -> String {
        self.connect(admin);
        self.ui.wizard.set(false);
        self.ui.screen.set(ui::SCREEN_APPS);
        self.store
            .apps
            .overview
            .set(Loadable::Ready(AppsOverview::from_value(&fixture_for(
                admin,
            ))));
        let s = self.turns(3);
        self.drain();
        s
    }
    fn select(&mut self, id: &str) -> String {
        let i = self
            .store
            .apps
            .overview
            .with_untracked(|o| o.ready().unwrap().apps.iter().position(|a| a.id == id))
            .unwrap_or_else(|| panic!("{id} not in the fixture"));
        self.store.apps.sel.set(i);
        self.turns(3)
    }
    fn notice(&self) -> String {
        self.store.notice.get_untracked().unwrap_or_default()
    }
}

/// R11.3: the badge the gateway sends for this caller (apps_manager.status_control).
fn badge(label: &str, tone: &str, action: Option<&str>, enabled: bool, tip: Option<&str>) -> Value {
    json!({"label": label, "tone": tone, "busy": false, "action": action, "enabled": enabled, "tip": tip})
}

fn fixture_for(admin: bool) -> Value {
    let mut v = fixture();
    let admin_tip = "Only an admin can start or stop apps";
    let ctl = |label: &str, tone: &str, action: &str, tip: &str| {
        if admin {
            badge(label, tone, Some(action), true, Some(tip))
        } else {
            badge(label, tone, Some(action), false, Some(admin_tip))
        }
    };
    let badges = [
        (
            "observer",
            badge("Not installed", "muted", None, false, None),
        ),
        (
            "flow",
            ctl("Running", "ok", "stop", "Running — click to stop"),
        ),
        (
            "continuum",
            badge(
                "Running",
                "ok",
                None,
                false,
                Some("Started outside the gateway — stop it where it was started"),
            ),
        ),
        (
            "code",
            ctl(
                "Stopped unexpectedly",
                "err",
                "launch",
                "Stopped unexpectedly — click to start",
            ),
        ),
        (
            "entity",
            ctl("Running", "ok", "stop", "Running — click to stop"),
        ),
        (
            "assistant",
            badge(
                "Stopped",
                "muted",
                Some("launch"),
                false,
                Some(if admin {
                    "The Assistant runs on the gateway's computer: open it there."
                } else {
                    admin_tip
                }),
            ),
        ),
    ];
    for app in v["apps"].as_array_mut().unwrap() {
        let id = app["id"].as_str().unwrap().to_string();
        let b = badges.iter().find(|(i, _)| *i == id).unwrap().1.clone();
        app["status_control"] = b;
    }
    v
}

fn fixture() -> Value {
    json!({
        "ok": true,
        "runtime": {"node": {"available": true, "version": "24.14.0", "source": "system",
                              "install_available": false, "message": "Node.js 24.14.0 (on this machine).",
                              "problems": [], "path": "/usr/local/bin/node", "active_job": null}},
        "apps": [
            {"id": "observer", "name": "Observer", "kind": "web",
             "description": "Watch runs, replay/stream ledgers, submit durable commands.",
             "package": "@abstractframework/observer", "installed": false, "version": null,
             "latest_version": "0.1.13", "update_available": false, "running": false,
             "status": "not_installed", "source": null, "external": null,
             "needs_node_install": false, "install_available": true, "install_blocked_reason": null,
             "install_parts": ["web"], "actions": ["install"], "active_job": null,
             "content_summary": null, "interfaces": [{"kind": "web"}]},
            {"id": "flow", "name": "Flow Editor", "kind": "web", "description": "Design workflows.",
             "package": "@abstractframework/flow", "installed": true, "version": "0.3.20",
             "latest_version": "0.3.21", "update_available": true, "running": true,
             "status": "running", "source": "gateway", "url": "http://127.0.0.1:3003/", "port": 3003,
             "pid": 4242, "actions": ["open", "stop", "update", "logs"], "install_parts": ["web"],
             "interfaces": [{"kind": "web"}]},
            {"id": "continuum", "name": "Continuum", "kind": "web", "description": "Backlog.",
             "package": "@abstractframework/continuum", "installed": true, "version": "0.3.2",
             "running": true, "status": "running", "managed": false, "source": "external",
             "external": {"port": 3002, "pid": 55398, "detail": "Started outside the gateway on port 3002"},
             "url": "http://127.0.0.1:3002/", "port": 3002, "actions": ["open"],
             "install_parts": ["web"], "interfaces": [{"kind": "web"}]},
            {"id": "code", "name": "Code", "kind": "web", "description": "Coding assistant.",
             "package": "@abstractframework/code", "installed": true, "version": "0.5.0",
             "running": false, "status": "crashed", "source": "gateway",
             "last_error": "exit 1: EADDRINUSE", "actions": ["launch", "logs"],
             "install_parts": ["web"],
             "interfaces": [{"kind": "web"}, {"kind": "tui", "name": "Code in the terminal",
               "binary": "abstractcode", "installed": true, "version": "0.6.0",
               "install_available": false, "install_method": "release_binary",
               "launch_available": true, "launch_mode": "terminal",
               "command": "abstractcode --gateway http://127.0.0.1:8080", "active_job": null}]},
            {"id": "entity", "name": "Entity", "kind": "web", "description": "Entities.",
             "package": "@abstractframework/entity", "installed": true, "version": "0.2.2",
             "running": true, "status": "running", "source": "gateway", "url": "http://127.0.0.1:3005/",
             "actions": ["open", "stop", "logs"], "content_summary": {"entities_count": 0},
             "install_parts": ["web"], "interfaces": [{"kind": "web"}]},
            {"id": "assistant", "name": "Assistant", "kind": "desktop", "description": "Menu-bar assistant.",
             "package": "abstractassistant", "installed": true, "running": false, "status": "stopped",
             "actions": ["open"], "install_parts": ["desktop"], "interfaces": [],
             "desktop": {"location": "/Applications/AbstractAssistant.app",
                         "launch_command": "open -a /Applications/AbstractAssistant.app",
                         "launch_available": false, "launch_blocked": "other_computer",
                         "launch_blocked_reason": "The Assistant runs on the gateway's computer: open it there."}}
        ],
        "install_allowed": true,
        "registry": {"url": "https://registry.npmjs.org", "reachable": true, "error": null},
        "gateway_url": "http://127.0.0.1:8080",
        "apps_host": "127.0.0.1"
    })
}

fn app_acts(cmds: &[Cmd]) -> Vec<(String, AppVerb, Option<String>, bool)> {
    cmds.iter()
        .filter_map(|c| match c {
            Cmd::AppAct {
                app_id,
                verb,
                path,
                start_first,
                ..
            } => Some((app_id.clone(), *verb, path.clone(), *start_first)),
            _ => None,
        })
        .collect()
}

#[test]
fn every_web_card_state_renders() {
    let mut h = harness();
    let s = h.on_apps(true);
    assert!(s.contains("Install and open the apps that work with this gateway"), "{s}");
    // R15: the web card's words in a table — the badge is the state (an
    // external app's too), the actions are labelled buttons.
    for want in [
        "Observer",
        "Not installed",
        "Install",
        "Flow Editor",
        "Running",
        "Stopped unexpectedly",
        "Create your first entity",
        "Assistant",
        "Node.js",
        "Ready",
        "24.14.0",
        "machine only.",
        "Check again",
    ] {
        assert!(s.contains(want), "'{want}' on the Apps screen:\n{s}");
    }
    // Footer: the screen's verbs.
    assert!(s.contains("Enter Open") && s.contains("s status badge"), "{s}");
    // Detail of the external app says why it cannot be stopped.
    let s = h.select("continuum");
    assert!(
        s.contains("Started outside the gateway on port 3002"),
        "{s}"
    );
    // A crashed app says so and how to recover.
    let s = h.select("code");
    assert!(
        s.contains("Code stopped unexpectedly.") && s.contains("Open starts it again."),
        "{s}"
    );
    assert!(s.contains("EADDRINUSE"), "last error shown:\n{s}");
    assert!(s.contains("Open in Terminal"), "terminal verb:\n{s}");
    // Desktop from another computer: the reason, no action.
    let s = h.select("assistant");
    assert!(s.contains("open it there"), "{s}");
}

#[test]
fn entering_the_screen_loads_the_overview_once() {
    let mut h = harness();
    h.connect(true);
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_APPS);
    h.turns(4);
    let loads = h
        .drain()
        .into_iter()
        .filter(|c| matches!(c, Cmd::LoadApps { latest: true }))
        .count();
    assert_eq!(loads, 1);
    assert!(h.store.apps.overview.with_untracked(|o| o.is_loading()));
    h.store
        .apps
        .overview
        .set(Loadable::Ready(AppsOverview::from_value(&fixture())));
    h.turns(3);
    assert!(
        h.drain().iter().all(|c| !matches!(c, Cmd::LoadApps { .. })),
        "no reload storm"
    );
    // r = the web's "Check again".
    h.key(b"r");
    assert!(h
        .drain()
        .iter()
        .any(|c| matches!(c, Cmd::LoadApps { latest: true })));
}

#[test]
fn six_jumps_to_apps_in_browse_mode() {
    let mut h = harness();
    h.connect(true);
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_WORKFLOWS);
    h.turns(2);
    h.key(b"6");
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_APPS);
}

#[test]
fn install_confirms_then_sends_the_install_verb() {
    let mut h = harness();
    h.on_apps(true);
    h.select("observer");
    let s = h.key(b"o");
    assert!(
        s.contains("Install Observer?") && s.contains("starts nothing"),
        "confirm:\n{s}"
    );
    h.key(b"\r");
    let acts = app_acts(&h.drain());
    assert_eq!(
        acts,
        vec![("observer".to_string(), AppVerb::Install, None, false)]
    );
}

#[test]
fn open_starts_a_stopped_app_first_and_entity_lands_on_new() {
    let mut h = harness();
    h.on_apps(true);
    h.select("code");
    h.key(b"o");
    assert_eq!(
        app_acts(&h.drain()),
        vec![("code".to_string(), AppVerb::Open, None, true)]
    );
    h.select("entity");
    h.key(b"\r"); // Enter on the table = the primary action
    assert_eq!(
        app_acts(&h.drain()),
        vec![(
            "entity".to_string(),
            AppVerb::Open,
            Some("/#new".to_string()),
            false
        )]
    );
}

#[test]
fn s_is_the_status_badge_stop_in_one_key() {
    // R11.3: the badge stops in one action, as the web badge's one click.
    let mut h = harness();
    h.on_apps(true);
    let s = h.select("flow");
    // The badge's label is the state; "Running — click to stop" is its
    // tooltip (A3) — `s` is its key.
    assert!(s.contains("Running"), "{s}");
    h.key(b"s");
    assert_eq!(
        app_acts(&h.drain()),
        vec![("flow".to_string(), AppVerb::Stop, None, false)]
    );
    // `x` no longer stops (one control per action).
    h.key(b"x");
    assert!(app_acts(&h.drain()).is_empty());
}

#[test]
fn update_and_log_and_terminal_keys() {
    let mut h = harness();
    h.on_apps(true);
    h.select("flow");
    let s = h.key(b"u");
    assert!(
        s.contains("0.3.21") && s.contains("restarts on the new version"),
        "{s}"
    );
    h.key(b"\r");
    assert_eq!(
        app_acts(&h.drain()),
        vec![("flow".to_string(), AppVerb::Update, None, false)]
    );
    h.key(b"l");
    let cmds = h.drain();
    assert!(
        cmds.iter()
            .any(|c| matches!(c, Cmd::LoadAppLog { app_id, tail: 200 } if app_id == "flow")),
        "{cmds:?}"
    );
    // The log modal renders the head line honestly.
    h.store.apps.log.set(Loadable::Ready(
        abstractgateway_console::store::apps::AppLog {
            app_id: "flow".into(),
            path: Some("/data/logs/apps/flow.log".into()),
            lines: vec!["listening on 3003".into(); 3],
            tail: 200,
        },
    ));
    let s = h.turns(3);
    assert!(
        s.contains("The whole log · 3 lines") && s.contains("flow.log"),
        "{s}"
    );
}

#[test]
fn non_admin_gets_the_reason_not_the_action() {
    let mut h = harness();
    h.on_apps(false);
    h.select("observer");
    h.key(b"o");
    assert!(
        h.notice().contains("Only an admin can install apps"),
        "{}",
        h.notice()
    );
    h.select("flow");
    h.key(b"s");
    assert!(
        h.notice().contains("Only an admin can start or stop apps"),
        "{}",
        h.notice()
    );
    // Opening a running app is for any signed-in principal.
    h.key(b"o");
    assert_eq!(
        app_acts(&h.drain()),
        vec![("flow".to_string(), AppVerb::Open, None, false)]
    );
    // An external app cannot be stopped even by an admin — and says why.
    h.select("continuum");
    h.key(b"s");
    assert!(
        h.notice()
            .contains("Started outside the gateway — stop it where it was started"),
        "{}",
        h.notice()
    );
}

#[test]
fn a_minted_link_opens_its_modal_with_the_tunnel_hint() {
    let mut h = harness();
    h.on_apps(true);
    let link = AppOpenLink::from_value(
        "http://127.0.0.1:8080",
        "flow",
        "Flow Editor",
        false,
        &json!({"open_url": "/apps/handover/abc123", "app_url": "http://127.0.0.1:3003/", "expires_in_s": 120}),
    )
    .unwrap();
    h.store.apps.open_link.set(Some(link));
    let s = h.turns(4);
    assert!(
        s.contains("http://127.0.0.1:8080/apps/handover/abc123"),
        "{s}"
    );
    assert!(s.contains("within 120 seconds"), "{s}");
    assert!(
        s.contains("ssh -L 8080:127.0.0.1:8080 -L 3003:127.0.0.1:3003"),
        "{s}"
    );
    assert!(h.store.apps.open_link.get_untracked().is_none(), "consumed");
}

#[test]
fn a_headless_or_ssh_session_never_runs_a_url_opener() {
    let opened = Rc::new(RefCell::new(Vec::new()));
    let why = "this is an SSH session: a browser opened here would appear on the remote machine, not in front of you";
    let mut h = harness_with(Some(why.to_string()), opened.clone());
    h.on_apps(true);
    let link = AppOpenLink::from_value(
        "http://127.0.0.1:8080",
        "flow",
        "Flow Editor",
        false,
        &json!({"open_url": "/apps/handover/abc123", "app_url": "http://127.0.0.1:3003/", "expires_in_s": 120}),
    )
    .unwrap();
    h.store.apps.open_link.set(Some(link));
    let s = h.turns(4);
    // The link, the copy verb and the tunnel — never "Open in a browser here".
    assert!(
        s.contains("http://127.0.0.1:8080/apps/handover/abc123"),
        "{s}"
    );
    assert!(s.contains("No browser here: this is an SSH session"), "{s}");
    assert!(s.contains("Copy link (y)"), "{s}");
    assert!(!s.contains("Open in a browser here"), "{s}");
    assert!(
        s.contains("ssh -L 8080:127.0.0.1:8080 -L 3003:127.0.0.1:3003"),
        "{s}"
    );
    // `o` refuses with the reason; the opener is never called.
    h.key(b"o");
    assert!(
        h.notice()
            .starts_with("not opening a browser: this is an SSH session"),
        "{}",
        h.notice()
    );
    assert!(
        opened.borrow().is_empty(),
        "opener ran: {:?}",
        opened.borrow()
    );
}

#[test]
fn with_a_display_o_opens_the_link() {
    let opened = Rc::new(RefCell::new(Vec::new()));
    let mut h = harness_with(None, opened.clone());
    h.on_apps(true);
    let link = AppOpenLink::from_value(
        "http://127.0.0.1:8080",
        "flow",
        "Flow Editor",
        false,
        &json!({"open_url": "/apps/handover/abc123", "expires_in_s": 120}),
    )
    .unwrap();
    h.store.apps.open_link.set(Some(link));
    let s = h.turns(4);
    assert!(s.contains("Open in a browser here (o)"), "{s}");
    h.key(b"o");
    assert_eq!(
        *opened.borrow(),
        vec!["http://127.0.0.1:8080/apps/handover/abc123".to_string()]
    );
}

/// The headless verdict is AbstractCore's rule (one rule for both
/// consoles): an SSH session never opens a browser, on any OS.
#[test]
fn the_display_rule_is_abstractcores() {
    let ssh =
        |k: &str| (k == "SSH_CONNECTION").then(|| std::ffi::OsString::from("1.2.3.4 5 6.7.8.9 22"));
    assert!(abstractcore_console::display_from("macos", &ssh).is_err());
    assert!(abstractcore_console::display_from("unix", &|_| None).is_err());
    assert!(abstractcore_console::display_from("macos", &|_| None).is_ok());
}

#[test]
fn a_running_job_shows_progress_and_c_cancels_it() {
    let mut h = harness();
    h.on_apps(true);
    h.select("observer");
    h.store.apps.set_job(
        "app:observer",
        abstractgateway_console::store::apps::AppJob::from_value(&json!({
            "id": "job-7", "kind": "install", "title": "Installing Observer", "state": "running",
            "percent": 40, "message": "Downloading the app"
        }))
        .unwrap(),
    );
    let s = h.turns(3);
    assert!(
        s.contains("Installing Observer · 40% · Downloading the app"),
        "{s}"
    );
    assert!(s.contains("Installing"), "pill:\n{s}");
    h.key(b"c");
    let cmds = h.drain();
    assert!(
        cmds.iter().any(|c| matches!(c, Cmd::CancelAppJob { key, job_id, .. } if key == "app:observer" && job_id == "job-7")),
        "{cmds:?}"
    );
}

#[test]
fn a_gateway_reset_forgets_the_apps() {
    let mut h = harness();
    h.on_apps(true);
    let gen = h.store.apps.poll_gen.get_untracked();
    h.store.reset_domains();
    assert!(
        h.store
            .apps
            .overview
            .with_untracked(|o| o.ready().is_none()),
        "the old rows are gone"
    );
    assert!(
        h.store.apps.poll_gen.get_untracked() > gen,
        "live poll chains die"
    );
}
