//! R7-W2 fork E — Runtimes and Apps driven through the REAL worker against
//! a LIVE hermetic scratch gateway (never :8080); after each action the
//! gateway's state is read back over HTTP. Ignored by default:
//!
//!   ABSTRACTGATEWAY_URL=http://127.0.0.1:18821 ABSTRACTGATEWAY_AUTH_TOKEN=... \
//!   cargo test --test live_r7w2_work -- --ignored --test-threads 1
//!
//! Seed: at least one running run of the admin (POST /runs/start with the
//! runner off keeps it running). Nothing is installed: apps actions stop
//! at reading and at the settings write, which is restored.

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::{mpsc, Arc};
use std::time::{Duration, Instant};

use abstracttui::app::Driver;
use abstracttui::prelude::*;
use abstracttui::testing::CaptureTerm;
use serde_json::{json, Value};

use abstractcore_console::screens::{ScreensCtx, ScreensOptions};
use abstractcore_console::{ConsoleTransport, TransportError};

use abstractgateway_console::store::{ConnPhase, Identity, Store};
use abstractgateway_console::ui::{self, Ctx, UiState};
use abstractgateway_console::worker::Cmd;

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
    fn start_download(&self, _p: &str, _a: &str, _b: Option<u64>) -> Result<Value, TransportError> {
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
    tx: mpsc::Sender<Cmd>,
    rx: Option<mpsc::Receiver<Cmd>>,
}

fn harness(size: Size) -> Harness {
    abstracttui::app::set_theme_by_id("abstract-dark");
    // R15 rail: from 120x32 the console shows a 21-cell nav rail; these
    // suites pin PAGE layouts, so a wide size keeps its page width.
    let size = if size.w >= 120 && size.h >= 32 {
        Size::new(size.w + 21, size.h)
    } else {
        size
    };
    let mut app = App::new(size);
    let overlays = app.overlays();
    let quitter = app.quitter();
    let (tx, rx) = mpsc::channel::<Cmd>();
    let tx_keep = tx.clone();
    let slot: Rc<RefCell<Option<(Store, UiState)>>> = Rc::new(RefCell::new(None));
    let out = slot.clone();
    app.mount(move |cx| {
        let store = Store::create(cx);
        let ui_state = UiState::create(cx, "http://127.0.0.1:18999".to_string(), String::new());
        *out.borrow_mut() = Some((store, ui_state));
        let transport: Arc<dyn ConsoleTransport> = Arc::new(NoTransport);
        let screens = ScreensCtx::new(
            cx,
            transport.clone(),
            overlays.clone(),
            ui::screens_access_signal(cx, store),
            ScreensOptions {
                notice: Some(store.notice),
                opener: Some(Rc::new(|_url: &str| Ok(()))),
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
            no_display: None,
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
        tx: tx_keep,
        rx: Some(rx),
    }
}

#[allow(dead_code)]
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
    fn admin(&mut self) {
        let id = Identity::from_me(&json!({
            "principal": {"user_id": "admin", "tenant_id": "default", "roles": ["admin", "user"], "admin": true},
            "auth": {"mode": "users"}, "routing": {"mode": "per-principal"}
        }))
        .unwrap();
        self.store.conn.set(ConnPhase::Connected(id));
        self.turns(1);
        self.ui.wizard.set(false);
    }
    /// Wheel down over the middle of the screen (scrolls a modal's body).
    fn wheel_down(&mut self, n: usize) {
        let size = self.term.screen().size();
        for _ in 0..n {
            let ev = format!("\x1b[<65;{};{}M", size.w / 2, size.h / 2);
            self.term.push_input(ev.as_bytes());
        }
        self.turns(3);
    }
    fn click_text(&mut self, text: &str) -> String {
        let screen = self.turns(1);
        let (row, col) = screen
            .lines()
            .enumerate()
            .find_map(|(i, l)| l.find(text).map(|c| (i, l[..c].chars().count())))
            .unwrap_or_else(|| panic!("{text:?} not on screen:\n{screen}"));
        let click = format!(
            "\x1b[<0;{};{}M\x1b[<0;{};{}m",
            col + 2,
            row + 1,
            col + 2,
            row + 1
        );
        self.key(click.as_bytes())
    }
    fn shoot(&mut self, name: &str) {
        let s = self.turns(2);
        let Ok(dir) = std::env::var("R7W2_SHOTS_DIR") else {
            return;
        };
        let size = self.term.screen().size();
        let base = format!("{dir}/{name}-{}x{}", size.w, size.h);
        std::fs::create_dir_all(&dir).expect("shots dir");
        std::fs::write(format!("{base}.txt"), &s).expect("write text");
        std::fs::write(
            format!("{base}.svg"),
            self.term.screen().screenshot().to_svg(),
        )
        .expect("write svg");
    }
}

#[allow(dead_code)]
impl Harness {
    /// Turn frames until `pred` holds (the worker answers on its thread).
    fn until(&mut self, what: &str, mut pred: impl FnMut(&mut Harness, &str) -> bool) -> String {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            let s = self.turns(1);
            if pred(self, &s) {
                return s;
            }
            if Instant::now() > deadline {
                panic!(
                    "timed out waiting for {what} (notice {:?}):\n{s}",
                    self.store.notice.get_untracked()
                );
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}

fn live(size: Size) -> Harness {
    let url = std::env::var("ABSTRACTGATEWAY_URL").expect("ABSTRACTGATEWAY_URL");
    assert!(
        !url.ends_with(":8080") && !url.ends_with(":8081"),
        "hermetic gateways only"
    );
    let token = std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN").expect("ABSTRACTGATEWAY_AUTH_TOKEN");
    let mut h = harness(size);
    let wake = abstracttui::reactive::wake_handle();
    let ui_state = h.ui;
    let done_sink = {
        let wake = wake.clone();
        move |fid: u64, out: Result<String, String>| {
            wake.post(move || ui_state.write_done.set(Some((fid, out.clone()))))
        }
    };
    let rx = h.rx.take().expect("rx");
    let _worker = abstractgateway_console::worker::spawn(
        h.store,
        wake,
        rx,
        h.tx.clone(),
        |_u: String, _t: String| {},
        done_sink,
    );
    h.ui.wizard.set(false);
    h.ui.conn_url.set(url.clone());
    h.ui.conn_token.set(token.clone());
    h.tx.send(Cmd::Connect {
        url,
        token: token.into(),
    })
    .unwrap();
    h.until("connected", |h, _| {
        h.store.conn.with_untracked(ConnPhase::is_connected)
    });
    h
}

fn http(method: &str, path: &str, body: Option<Value>) -> Value {
    let url = std::env::var("ABSTRACTGATEWAY_URL").unwrap();
    let token = std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN").unwrap();
    let req = ureq::request(method, &format!("{url}/api/gateway{path}"))
        .set("Authorization", &format!("Bearer {token}"));
    let resp = match body {
        Some(b) => req
            .set("Content-Type", "application/json")
            .send_string(&b.to_string()),
        None => req.call(),
    }
    .expect("gateway answers");
    serde_json::from_str(&resp.into_string().expect("body")).expect("json")
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn runtimes_runs_root_switch_and_cancel_live() {
    // A fresh running run to act on.
    let started = http(
        "POST",
        "/runs/start",
        Some(json!({"bundle_id": "basic-agent", "input_data": {"prompt": "r7w2 live"}})),
    );
    let rid = started["run_id"].as_str().unwrap().to_string();
    let mut h = live(Size::new(120, 40));
    h.ui.screen.set(ui::SCREEN_RUNTIMES);
    h.until("the inventory", |_, s| s.contains("default/default"));
    h.key(b"\r"); // choose the default runtime
    let s = h.until("the runs", |h, _| {
        h.store.runs.with_untracked(|r| {
            r.ready()
                .is_some_and(|d| d.rows.iter().any(|r| r.run_id == rid))
        })
    });
    assert!(s.contains("[x] Root runs only (t)"), "{s}");
    // The table shows what GET /runs?root_only=true lists.
    let listed = http(
        "GET",
        "/runs?limit=100&root_only=true&include_ledger_len=false",
        None,
    );
    let n = listed["items"].as_array().unwrap().len();
    assert_eq!(
        h.store
            .runs
            .with_untracked(|r| r.ready().map(|d| d.rows.len()).unwrap()),
        n,
        "same rows as the web's read"
    );
    h.shoot("runtimes-runs");
    // Root runs only off: the read asks root_only=false.
    h.key(b"t");
    h.until("children included", |h, _| {
        h.store
            .runs
            .with_untracked(|r| r.ready().is_some_and(|d| !d.root_only))
    });
    h.key(b"t");
    h.until("root only again", |h, _| {
        h.store
            .runs
            .with_untracked(|r| r.ready().is_some_and(|d| d.root_only))
    });
    // Cancel the fresh run: inline confirm, y, the gateway accepts the
    // durable command (with the runner off the run stays `running` until
    // a runner ticks it — the command is what this verifies).
    let idx = h
        .store
        .runs
        .with_untracked(|r| r.ready().unwrap().rows.iter().position(|r| r.run_id == rid))
        .unwrap();
    h.ui.run_sel.set(idx);
    h.turns(3);
    let s = h.key(b"c");
    assert!(
        s.contains("Any in-flight work stops at the next tick."),
        "the web's confirm:\n{s}"
    );
    h.key(b"y");
    h.until("the cancel journaled", |h, _| {
        h.store.journal.with_untracked(|j| {
            j.iter()
                .any(|e| e.action.starts_with("cancel run") && e.outcome.is_ok())
        })
    });
    let run = http("GET", &format!("/runs/{rid}"), None);
    assert!(
        matches!(run["status"].as_str(), Some("running" | "cancelled")),
        "the run is still readable after the cancel: {run}"
    );
}

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn apps_page_and_settings_write_live() {
    let mut h = live(Size::new(120, 40));
    h.ui.screen.set(ui::SCREEN_APPS);
    let s = h.until("the apps", |_, s| {
        s.contains("Apps open in your browser at")
    });
    // Same list as GET /apps.
    let apps = http("GET", "/apps?latest=false", None);
    for a in apps["apps"].as_array().unwrap() {
        let name = a["name"].as_str().unwrap();
        assert!(s.contains(name), "{name} listed:\n{s}");
    }
    let s = h.until("the settings block", |_, s| {
        s.contains("Advanced: apps settings")
    });
    h.shoot("apps");
    let before = http("GET", "/admin/runtime-config", None);
    let node_before = before["apps"]["node"].clone();
    // The form lists the registry's fields in the payload's order: Tab
    // reaches field i after i+1 presses, Save after the last.
    let keys: Vec<String> = before["apps"]
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect();
    let i = keys
        .iter()
        .position(|k| k == "node")
        .expect("apps.node in the registry");
    let to_save = keys.len() - i;
    assert!(s.contains("Node.js for apps:"), "{s}");
    // a → the form; Tab to "Node.js for apps", type, Tab to Save, Enter.
    let s = h.key(b"a");
    assert!(s.contains("Browser apps settings"), "{s}");
    for _ in 0..=i {
        h.key(b"\t");
    }
    h.key(b"\x1b[F"); // End: the caret after the saved value
    for _ in 0..40 {
        h.key(b"\x7f");
    }
    h.term.push_input(b"managed");
    h.turns(3);
    for _ in 0..to_save {
        h.key(b"\t");
    }
    h.key(b"\r");
    h.until("the write verified", |_, _| {
        http("GET", "/admin/runtime-config", None)["apps"]["node"]["value"] == json!("managed")
    });
    let after = http("GET", "/admin/runtime-config", None);
    assert_eq!(after["apps"]["node"]["source"], json!("stored"));
    let s = h.until("the page shows it", |_, s| {
        s.contains("Node.js for apps: managed · Saved setting")
    });
    h.shoot("apps-settings-saved");
    let _ = s;
    // Restore: clear the field (= back to env/default).
    h.key(b"a");
    for _ in 0..=i {
        h.key(b"\t");
    }
    h.key(b"\x1b[F"); // End: the caret after the saved value
    for _ in 0..40 {
        h.key(b"\x7f");
    }
    for _ in 0..to_save {
        h.key(b"\t");
    }
    h.key(b"\r");
    h.until("restored", |_, _| {
        http("GET", "/admin/runtime-config", None)["apps"]["node"]["source"] != json!("stored")
    });
    let restored = http("GET", "/admin/runtime-config", None);
    assert_eq!(restored["apps"]["node"]["value"], node_before["value"]);
}
