//! Round 7 (R7.2) shell parity, LIVE: the real worker against a hermetic
//! scratch gateway (untracked/round4/r7/w2/run_scratch_gateway.sh). Each
//! action's effect is read back from the gateway over HTTP (the same
//! routes the web console calls). Ignored by default:
//!
//!   ABSTRACTGATEWAY_URL=http://127.0.0.1:18771 ABSTRACTGATEWAY_AUTH_TOKEN=... \
//!   R7W2_SHOTS_DIR=<dir> cargo test --test live_r7w2_shell -- --ignored --test-threads 1

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::{mpsc, Arc};
use std::time::{Duration, Instant};

use abstracttui::app::Driver;
use abstracttui::prelude::*;
use abstracttui::testing::CaptureTerm;
use serde_json::Value;

use abstractcore_console::screens::{ScreensCtx, ScreensOptions};
use abstractcore_console::{ConsoleTransport, TransportError};

use abstractgateway_console::api::GatewayClient;
use abstractgateway_console::store::{ConnPhase, Loadable, Store};
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
    rx: Option<mpsc::Receiver<Cmd>>,
    tx: mpsc::Sender<Cmd>,
}

fn harness(size: Size) -> Harness {
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
        rx: Some(rx),
        tx: tx_keep,
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
    fn esc(&mut self) -> String {
        // Bare-ESC disambiguation: byte arrives, 30ms deadline, resolve.
        self.term.push_input(&[0x1b]);
        self.turns(1);
        std::thread::sleep(std::time::Duration::from_millis(45));
        self.turns(3)
    }
    fn goto(&mut self, screen: usize) -> String {
        self.ui.screen.set(screen);
        self.turns(3)
    }
    /// Text captures for the reviewer: R7W2_SHOTS_DIR=<dir>.
    fn shoot(&mut self, name: &str) {
        let s = self.turns(2);
        let Ok(dir) = std::env::var("R7W2_SHOTS_DIR") else {
            return;
        };
        let size = self.term.screen().size();
        std::fs::create_dir_all(&dir).expect("shots dir");
        std::fs::write(format!("{dir}/{name}-{}x{}.txt", size.w, size.h), s).expect("write");
    }
}

impl Harness {
    fn until(&mut self, what: &str, mut pred: impl FnMut(&mut Harness, &str) -> bool) -> String {
        let deadline = Instant::now() + Duration::from_secs(20);
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

fn env() -> (String, String) {
    let url = std::env::var("ABSTRACTGATEWAY_URL").expect("ABSTRACTGATEWAY_URL");
    assert!(
        !url.ends_with(":8080") && !url.ends_with(":8081"),
        "hermetic gateways only"
    );
    let token = std::env::var("ABSTRACTGATEWAY_AUTH_TOKEN").expect("ABSTRACTGATEWAY_AUTH_TOKEN");
    (url, token)
}

fn live(size: Size) -> Harness {
    let (url, token) = env();
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
    h.ui.wizard.set(false);
    h
}

fn gateway() -> GatewayClient {
    let (url, token) = env();
    GatewayClient::new(&url, Some(&token))
}

/// About: the page shows the versions the gateway's `GET /about` reports
/// (the web console's About reads the same route) and no package.
#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn about_page_shows_the_gateways_versions_live() {
    let about = gateway().json_get("/about", false).expect("GET /about");
    let gw = about["abstractgateway"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    let fw = about["abstractframework"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    assert!(
        !gw.is_empty(),
        "the scratch gateway reports its version: {about}"
    );
    for size in [Size::new(80, 24), Size::new(120, 40)] {
        let mut h = live(size);
        h.ui.screen.set(ui::SCREEN_ABOUT);
        let s = h.until("the About card", |h, _| {
            h.store
                .about
                .with_untracked(|a| matches!(a, Loadable::Ready(_)))
        });
        let s = if s.contains(&format!("AbstractGateway   {gw}")) {
            s
        } else {
            h.turns(3)
        };
        assert!(s.contains(&format!("AbstractGateway   {gw}")), "{s}");
        if !fw.is_empty() {
            assert!(s.contains(&format!("AbstractFramework {fw}")), "{s}");
        }
        if let Some(pkgs) = about["packages"].as_object() {
            for (name, _) in pkgs {
                if name != "abstractgateway" && name != "abstractframework" {
                    assert!(!s.contains(name.as_str()), "no package list ({name}):\n{s}");
                }
            }
        }
        h.shoot("live-about");
    }
}

/// Setup: `a` (Use recommended defaults) runs the web's non-forced
/// `POST /config/capability-defaults/apply-recommended`; the routes the
/// gateway then reports are what the page shows as the text model.
#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn setup_use_recommended_defaults_live() {
    let mut h = live(Size::new(120, 40));
    h.ui.screen.set(ui::SCREEN_WELCOME);
    h.until("the recommended set", |_, s| {
        s.contains("Recommended for this computer")
            && !s.contains("Checking the recommended starter models")
    });
    h.shoot("live-setup");
    let journal_before = h.store.journal.with_untracked(Vec::len);
    h.key(b"a");
    h.until("the apply journal entry", |h, _| {
        h.store.journal.with_untracked(|j| {
            j.len() > journal_before
                && j.last()
                    .is_some_and(|e| e.action.contains("apply-recommended"))
        })
    });
    let entry = h
        .store
        .journal
        .with_untracked(|j| j.last().cloned())
        .unwrap();
    assert!(entry.outcome.is_ok(), "{entry:?}");
    // The gateway's own state: every recommended route that this host can
    // run is configured; the page's text-model line is the stored route.
    let defaults = gateway()
        .json_get("/config/capability-defaults", false)
        .expect("GET capability-defaults");
    let text = defaults["routes"]
        .as_array()
        .and_then(|r| {
            r.iter()
                .find(|x| x["key"] == "output.text")
                .or_else(|| r.iter().find(|x| x["key"] == "input.text"))
        })
        .cloned()
        .unwrap_or(Value::Null);
    let (p, m) = (
        text["provider"].as_str().unwrap_or_default().to_string(),
        text["model"].as_str().unwrap_or_default().to_string(),
    );
    assert!(
        !p.is_empty() && !m.is_empty(),
        "a text route is set: {text}"
    );
    h.tx.send(Cmd::LoadRoutes).unwrap();
    let s = h.until("the text model line", |_, s| {
        s.contains(&format!("Text model now: {p} · "))
    });
    assert!(s.contains("Text model now:"), "{s}");
    h.shoot("live-setup-applied");
}

/// The screen keys jump where the web sidebar lists the pages.
#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn sidebar_keys_live() {
    let mut h = live(Size::new(80, 24));
    for (key, screen, title) in [
        (b"8", ui::SCREEN_OPENAI, "OpenAI API"),
        (b"H", ui::SCREEN_MODELS, "Resources"),
        (b"T", ui::SCREEN_REVIEW, "Sandbox"),
        (b"N", ui::SCREEN_NETWORK, "Network"),
        (b"S", ui::SCREEN_WELCOME, "Setup"),
        (b"I", ui::SCREEN_ABOUT, "About"),
    ] {
        h.ui.screen.set(ui::SCREEN_USERS);
        h.turns(3);
        let s = h.key(key);
        assert_eq!(h.ui.screen.get_untracked(), screen, "{title}");
        assert!(s.contains(title), "{title}:\n{s}");
    }
}
