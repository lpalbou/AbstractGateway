//! R7-W2 fork E — Runtimes and Apps at parity with the web console
//! (R7.2): snapshot per screen state + the command each action sends.
//! Headless (AbstractTUI capture harness), no network. Live drive tests:
//! tests/live_r7w2_work.rs.

#![allow(dead_code)]

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::{mpsc, Arc};

use abstracttui::app::Driver;
use abstracttui::prelude::*;
use abstracttui::testing::CaptureTerm;
use serde_json::{json, Value};

use abstractcore_console::screens::{ScreensCtx, ScreensOptions};
use abstractcore_console::{ConsoleTransport, TransportError};

use abstractgateway_console::store::apps::AppsOverview;
use abstractgateway_console::store::{
    runtimes_from_payload, RunRow, RunScope, RunsData, RuntimeConfigData,
};
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
    harness_sized(Size::new(150, 44))
}

fn harness_sized(size: Size) -> Harness {
    let no_display: Option<String> = None;
    let opened: Rc<RefCell<Vec<String>>> = Rc::new(RefCell::new(Vec::new()));
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
            .set(Loadable::Ready(AppsOverview::from_value(&fixture())));
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

/// The screen text with the block borders stripped and lines joined, so
/// a WRAPPED sentence can be asserted whole (R7.2: never cut).
fn flat(s: &str) -> String {
    s.lines()
        .map(|l| l.trim_matches(|c: char| c == '│' || c == '┃' || c.is_whitespace()))
        .collect::<Vec<_>>()
        .join(" ")
}

fn runtime_config() -> Value {
    json!({
        "writable": true,
        "apps": {
            "host": {"key": "apps.host", "label": "Where apps listen", "help": "127.0.0.1 = this computer only.",
                     "placeholder": "127.0.0.1", "value": "127.0.0.1", "source": "default", "default": "127.0.0.1"},
            "node": {"key": "apps.node", "label": "Node.js for apps", "help": "auto, managed, system or a path.",
                     "placeholder": "auto", "value": "system", "source": "stored", "default": "auto"}
        },
        "triage_repo_root": {"source": "default", "value": "/d/backlog", "default_path": "/d/backlog",
                             "available": false, "reason": "the folder does not exist", "label": "Backlog folder"},
        "backlog_exec_runner": {"value": false, "source": "default", "label": "Backlog exec runner"},
        "process_manager": {"value": true, "source": "stored", "label": "Process manager"}
    })
}

// ---------------------------------------------------------------- Apps

#[test]
fn apps_page_speaks_the_web_words() {
    let mut h = harness();
    let s = h.on_apps(true);
    let f = flat(&s);
    assert!(
        f.contains("Apps open in your browser at http://127.0.0.1:8080/apps/…, already signed in to this gateway."),
        "the web intro:\n{s}"
    );
    // The web card blurb (APP_COPY), not the gateway's description.
    assert!(
        f.contains("Watch runs live, replay them, steer running work."),
        "{s}"
    );
    assert!(!f.contains("submit durable commands"), "{s}");
    assert!(f.contains("npm npx @abstractframework/observer"), "{s}");
    // A gateway-served app: Address (through the gateway) + On this machine.
    let s = h.select("flow");
    let f = flat(&s);
    assert!(
        f.contains("Address http://127.0.0.1:8080/apps/flow/"),
        "{s}"
    );
    assert!(f.contains("On this machine http://127.0.0.1:3003/"), "{s}");
    assert!(f.contains("Version 0.3.20"), "{s}");
    // Started outside: the web's technical line.
    let s = h.select("continuum");
    assert!(
        flat(&s).contains("Started outside the gateway on port 3002"),
        "{s}"
    );
}

#[test]
fn apps_lines_wrap_at_80x24_and_never_cut() {
    let mut h = harness_sized(Size::new(80, 24));
    let s = h.on_apps(true);
    let f = flat(&s);
    assert!(
        f.contains(
            "already signed in to this gateway. They listen on 127.0.0.1: this machine only."
        ),
        "the intro wraps whole at 80 columns:\n{s}"
    );
    // No line inside the Apps block is cut with an ellipsis.
    for l in s.lines().filter(|l| l.starts_with('│')) {
        assert!(
            !l.trim_end_matches(['│', '┃', ' ']).ends_with('…'),
            "cut line {l:?}:\n{s}"
        );
    }
}

#[test]
fn apps_reads_the_runtime_config_for_admins_only() {
    let mut h = harness();
    h.connect(true);
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_APPS);
    h.turns(3);
    assert!(
        h.drain()
            .iter()
            .any(|c| matches!(c, Cmd::LoadRuntimeConfig)),
        "an admin's Apps page reads GET /admin/runtime-config"
    );
    let mut h = harness();
    h.connect(false);
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_APPS);
    h.turns(3);
    assert!(
        !h.drain()
            .iter()
            .any(|c| matches!(c, Cmd::LoadRuntimeConfig)),
        "never for a non-admin (admin route)"
    );
}

#[test]
fn apps_settings_live_behind_the_gears_not_on_the_page() {
    // R8.1: no "Advanced" disclosures under the cards; `a` (the toolbar
    // gear) opens "Apps settings", `g` on the Continuum card its settings.
    let mut h = harness_sized(Size::new(150, 60));
    h.on_apps(true);
    h.store
        .runtime_config
        .set(Loadable::Ready(RuntimeConfigData::from_value(
            &runtime_config(),
        )));
    let s = h.turns(3);
    assert!(!s.contains("Advanced: apps settings"), "{s}");
    assert!(!s.contains("Advanced: backlog settings"), "{s}");
    let s = h.key(b"a");
    let f = flat(&s);
    assert!(
        s.contains("Apps settings"),
        "a opens the apps overlay:\n{s}"
    );
    assert!(f.contains("Node.js for apps: system  Saved setting"), "{s}");
    // The deprecated host shows only while it holds a saved value.
    assert!(!f.contains("Where apps listen"), "{s}");
    h.key(b"\x1b");
    // A bare Esc resolves after the reader's 30 ms deadline.
    std::thread::sleep(std::time::Duration::from_millis(45));
    h.turns(3);
    let s = h.select("continuum");
    assert!(s.contains("g Settings"), "the card's gear:\n{s}");
    let s = h.key(b"g");
    let f = flat(&s);
    assert!(s.contains("Continuum settings"), "{s}");
    assert!(
        f.contains("Backlog folder: /d/backlog  The gateway's own folder"),
        "{s}"
    );
    assert!(
        f.contains("Not available: the folder does not exist"),
        "{s}"
    );
    assert!(f.contains("[x] Process manager  Saved setting"), "{s}");
    assert!(
        !f.contains("environment (legacy)") && !f.contains("Environment (legacy)"),
        "{s}"
    );
}

#[test]
fn apps_settings_keys_refuse_for_a_non_admin() {
    let mut h = harness();
    h.on_apps(false);
    h.key(b"a");
    assert!(
        h.notice().to_lowercase().contains("admin"),
        "{}",
        h.notice()
    );
}

// ------------------------------------------------------------ Runtimes

fn runtimes_fixture() -> Value {
    json!({"runtimes": [
        {"kind": "default", "tenant_id": "default", "runtime_id": "default", "label": "Gateway default runtime",
         "owners": ["admin"], "size_bytes": 1024},
        {"kind": "user", "tenant_id": "default", "runtime_id": "alice", "label": "default/alice",
         "owners": ["alice"], "size_bytes": 0}
    ]})
}

fn run(id: &str, status: &str) -> RunRow {
    RunRow {
        run_id: id.into(),
        workflow_id: "basic-agent@0.0.5".into(),
        status: status.into(),
        updated_at: "2026-10-04T02:32:08.829559+00:00".into(),
        current_node: "llm-1".into(),
        session_id: format!("sess-{id}"),
        actor_id: "gateway".into(),
        created_at: "2026-10-04T02:32:01+00:00".into(),
        ..Default::default()
    }
}

/// On Runtimes, the default plane chosen, `rows` loaded.
fn on_runs(h: &mut Harness, rows: Vec<RunRow>) -> String {
    h.connect(true);
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_RUNTIMES);
    h.store
        .runtimes
        .set(Loadable::Ready(runtimes_from_payload(&runtimes_fixture())));
    h.turns(3);
    h.key(b"\r"); // choose the highlighted (default) runtime
    h.store.runs.set(Loadable::Ready(RunsData {
        status: String::new(),
        query: String::new(),
        root_only: true,
        offset: 0,
        has_more: false,
        scope: RunScope::Own,
        rows,
    }));
    let s = h.turns(3);
    h.drain();
    s
}

#[test]
fn runs_table_has_the_web_columns_and_enter_opens_inspect_rows() {
    let mut h = harness_sized(Size::new(150, 50));
    let s = on_runs(&mut h, vec![run("r-1", "running")]);
    for col in ["Run", "Workflow", "Status", "Node", "Session", "Updated"] {
        assert!(s.contains(col), "column {col}:\n{s}");
    }
    assert!(
        s.contains("llm-1") && s.contains("sess-r-1"),
        "node + session cells:\n{s}"
    );
    // Focus the runs table (Tab past the toolbar), then Enter.
    let y = s.lines().position(|l| l.contains("sess-r-1")).unwrap();
    let col = s.lines().nth(y).unwrap().find("r-1").unwrap();
    h.key(
        format!(
            "\x1b[<0;{};{}M\x1b[<0;{};{}m",
            col + 1,
            y + 1,
            col + 1,
            y + 1
        )
        .as_bytes(),
    );
    let s = h.key(b"\r");
    assert!(
        s.contains("Actor    gateway"),
        "Inspect rows in place:\n{s}"
    );
    assert!(s.contains("Created  2026-10-04T02:32:01"), "{s}");
}

#[test]
fn root_runs_only_switch_rereads_with_children() {
    let mut h = harness_sized(Size::new(150, 50));
    let s = on_runs(&mut h, vec![run("r-1", "running")]);
    assert!(s.contains("[x] Root runs only (t)"), "{s}");
    h.key(b"t");
    let sent = h.drain();
    assert!(
        sent.iter().any(|c| matches!(
            c,
            Cmd::LoadRuns {
                root_only: false,
                ..
            }
        )),
        "t re-reads with root_only=false: {sent:?}"
    );
    h.store.runs.set(Loadable::Ready(RunsData {
        status: String::new(),
        query: String::new(),
        root_only: false,
        offset: 0,
        has_more: false,
        scope: RunScope::Own,
        rows: vec![run("r-1", "running")],
    }));
    let s = h.turns(3);
    assert!(s.contains("[ ] Root runs only (t)"), "{s}");
}

#[test]
fn cancel_is_an_inline_confirm_in_the_web_words() {
    let mut h = harness_sized(Size::new(150, 50));
    on_runs(&mut h, vec![run("r-1", "running")]);
    let s = h.key(b"c");
    let f = flat(&s);
    assert!(
        f.contains("Cancel run r-1? Any in-flight work stops at the next tick."),
        "the web's confirm sentence:\n{s}"
    );
    assert!(s.contains("[y] Cancel run"), "{s}");
    assert!(
        !h.drain().iter().any(|c| matches!(c, Cmd::CancelRun { .. })),
        "nothing sent yet"
    );
    h.key(b"n");
    assert!(
        !h.drain().iter().any(|c| matches!(c, Cmd::CancelRun { .. })),
        "n keeps"
    );
    h.key(b"c");
    h.key(b"y");
    assert!(
        h.drain()
            .iter()
            .any(|c| matches!(c, Cmd::CancelRun { run_id } if run_id == "r-1")),
        "y sends the durable cancel"
    );
    // A finished run has no Cancel (the web shows none): refused with why.
    let mut h = harness_sized(Size::new(150, 50));
    on_runs(&mut h, vec![run("r-2", "completed")]);
    h.key(b"c");
    assert!(h.notice().contains("already completed"), "{}", h.notice());
}

#[test]
fn runs_empty_sentences_are_the_webs() {
    use abstractgateway_console::ui::runtimes::runs_empty_text;
    assert_eq!(runs_empty_text(&RunScope::Own, "", ""), "No runs yet.");
    assert_eq!(
        runs_empty_text(&RunScope::Own, "failed", ""),
        "No failed runs."
    );
    assert_eq!(
        runs_empty_text(&RunScope::Own, "", "abc"),
        "No runs match \"abc\"."
    );
    assert_eq!(
        runs_empty_text(&RunScope::Own, "failed", "abc"),
        "No failed runs match \"abc\"."
    );
    let plane = RunScope::Plane {
        kind: "user".into(),
        tenant_id: "default".into(),
        runtime_id: "alice".into(),
        label: "default/alice".into(),
    };
    assert_eq!(
        runs_empty_text(&plane, "", ""),
        "No runs on this runtime yet."
    );
    let mut h = harness_sized(Size::new(150, 50));
    let s = on_runs(&mut h, vec![]);
    assert!(s.contains("No runs yet."), "{s}");
}

#[test]
fn run_detail_rows_use_the_web_labels() {
    use abstractgateway_console::ui::runtimes::run_detail_rows;
    let mut r = run("r-9", "failed");
    r.error = "boom".into();
    let labels: Vec<&str> = run_detail_rows(&r).iter().map(|(k, _)| *k).collect();
    assert_eq!(
        labels,
        ["Run", "Workflow", "Status", "Node", "Session", "Actor", "Error", "Created", "Updated"]
    );
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
             "pid": 4242, "app_path": "/apps/flow/", "actions": ["open", "stop", "update", "logs"], "install_parts": ["web"],
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
        "apps_host": "127.0.0.1",
        "apps_path_prefix": "/apps/"
    })
}
