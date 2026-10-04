//! Shared harness for the round-7 R7-W2 system-page suites
//! (tests/r7w2_system.rs headless, tests/live_r7w2_system.rs live): the REAL
//! interface through AbstractTUI's capture terminal; the worker is either a
//! command recorder (headless) or the real worker against a hermetic
//! scratch gateway (live, `--ignored`).
#![allow(dead_code)]

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

pub struct NoTransport;

impl ConsoleTransport for NoTransport {
    fn host_profile(&self) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("not under test"))
    }
    fn engines_status(&self, _probe: bool) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("not under test"))
    }
    fn models_catalog(&self, _q: &str, _e: Option<&str>, _f: bool) -> Result<Value, TransportError> {
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

pub struct Harness {
    pub app: App,
    pub term: CaptureTerm,
    pub driver: Driver,
    pub store: Store,
    pub ui: UiState,
    pub tx: mpsc::Sender<Cmd>,
    pub rx: Option<mpsc::Receiver<Cmd>>,
}

pub fn harness(size: Size) -> Harness {
    abstracttui::app::set_theme_by_id("abstract-dark");
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

pub fn admin_identity() -> Identity {
    Identity::from_me(&json!({
        "principal": {"user_id": "admin", "tenant_id": "default", "roles": ["admin", "user"], "admin": true},
        "auth": {"mode": "users"}, "routing": {"mode": "per-principal"}
    }))
    .unwrap()
}

impl Harness {
    pub fn turns(&mut self, n: usize) -> String {
        let mut last = String::new();
        for _ in 0..n {
            self.driver.turn(&mut self.app, &mut self.term).expect("turn");
            last = self.term.screen().to_text();
        }
        last
    }
    pub fn key(&mut self, bytes: &[u8]) -> String {
        self.term.push_input(bytes);
        self.turns(3)
    }
    pub fn esc(&mut self) -> String {
        self.term.push_input(&[0x1b]);
        self.turns(1);
        std::thread::sleep(Duration::from_millis(45));
        self.turns(3)
    }
    /// Headless: connected as admin, browse mode, on `screen`.
    pub fn admin_on(&mut self, screen: usize) -> String {
        self.store.conn.set(ConnPhase::Connected(admin_identity()));
        self.turns(1);
        self.ui.wizard.set(false);
        self.ui.screen.set(screen);
        self.turns(3)
    }
    pub fn drain(&mut self) -> Vec<Cmd> {
        let mut out = Vec::new();
        if let Some(rx) = &self.rx {
            while let Ok(c) = rx.try_recv() {
                out.push(c);
            }
        }
        out
    }
    pub fn until(&mut self, what: &str, mut pred: impl FnMut(&mut Harness, &str) -> bool) -> String {
        let deadline = Instant::now() + Duration::from_secs(15);
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
    /// Write the screen text to `$R7W2_SHOTS_DIR/<name>-<w>x<h>.txt`.
    pub fn shoot(&mut self, name: &str) {
        let s = self.turns(2);
        let Ok(dir) = std::env::var("R7W2_SHOTS_DIR") else {
            return;
        };
        let size = self.term.screen().size();
        std::fs::create_dir_all(&dir).expect("shots dir");
        std::fs::write(format!("{dir}/{name}-{}x{}.txt", size.w, size.h), &s).expect("write");
    }
}

/// Live: the real worker, connected to `$ABSTRACTGATEWAY_URL` with
/// `$ABSTRACTGATEWAY_AUTH_TOKEN` (hermetic ports only).
pub fn live(size: Size) -> (Harness, String, String) {
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
        url: url.clone(),
        token: token.clone().into(),
    })
    .unwrap();
    h.until("connected", |h, _| h.store.conn.with_untracked(ConnPhase::is_connected));
    (h, url, token)
}

/// Direct HTTP to the scratch gateway (state assertions, out of band).
pub fn http(url: &str, token: &str, method: &str, path: &str, body: Option<Value>) -> (u16, Value) {
    let agent = ureq::AgentBuilder::new().timeout(Duration::from_secs(60)).build();
    let req = agent
        .request(method, &format!("{url}/api/gateway{path}"))
        .set("Authorization", &format!("Bearer {token}"));
    let resp = match body {
        Some(b) => req.set("Content-Type", "application/json").send_string(&b.to_string()),
        None => req.call(),
    };
    match resp {
        Ok(r) => {
            let code = r.status();
            (code, serde_json::from_str(&r.into_string().unwrap_or_default()).unwrap_or(Value::Null))
        }
        Err(ureq::Error::Status(code, r)) => (
            code,
            serde_json::from_str(&r.into_string().unwrap_or_default()).unwrap_or(Value::Null),
        ),
        Err(e) => panic!("{method} {path}: {e}"),
    }
}
