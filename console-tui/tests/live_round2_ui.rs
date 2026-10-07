//! Round-2 screens through the REAL worker against a LIVE hermetic gateway
//! (DESIGN-v2 §2, §4): connect, open Accounts, switch a user's Active off
//! and on, open the activity, open Workflows. Ignored by default:
//!
//!   ABSTRACTGATEWAY_URL=http://127.0.0.1:18541 ABSTRACTGATEWAY_AUTH_TOKEN=... \
//!   ROUND2_SHOTS_DIR=<dir> cargo test --test live_round2_ui -- --ignored --test-threads 1

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
        let Ok(dir) = std::env::var("ROUND2_SHOTS_DIR") else {
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

#[test]
#[ignore = "talks to a live gateway; run with --ignored"]
fn accounts_and_workflows_live() {
    for size in [Size::new(120, 40), Size::new(60, 30)] {
        let mut h = live(size);
        h.ui.screen.set(ui::SCREEN_USERS);
        let s = h.until("the accounts table", |_, s| s.contains("r2alice"));
        assert!(
            s.contains("[-] You can't deac") || s.contains("[-] Y"),
            "own row:\n{s}"
        );
        h.shoot("live-accounts");
        // Select r2alice and switch Active off (confirm), then on.
        let idx = h
            .store
            .accounts
            .with_untracked(|d| {
                d.ready()
                    .and_then(|r| r.iter().position(|a| a.id == "r2alice"))
            })
            .expect("r2alice row");
        h.ui.account_sel.set(idx);
        h.turns(3);
        h.key(b" ");
        h.key(b"\x1b[A");
        h.key(b"\r");
        h.until("r2alice inactive", |h, _| {
            h.store.accounts.with_untracked(|d| {
                d.ready()
                    .and_then(|r| r.iter().find(|a| a.id == "r2alice").map(|a| !a.active))
                    .unwrap_or(false)
            })
        });
        h.shoot("live-accounts-r2alice-inactive");
        h.key(b" ");
        h.until("r2alice active again", |h, _| {
            h.store.accounts.with_untracked(|d| {
                d.ready()
                    .and_then(|r| r.iter().find(|a| a.id == "r2alice").map(|a| a.active))
                    .unwrap_or(false)
            })
        });
        // The activity of r2alice lists the account changes.
        h.key(b"l");
        let s = h.until("the activity list", |_, s| s.contains("Account changed"));
        assert!(s.contains("Activity — r2alice"), "{s}");
        h.shoot("live-accounts-activity");
        h.key(b"\x1b");
        h.turns(3);
        // Workflows: plain names, source, and the per-app defaults.
        h.ui.screen.set(ui::SCREEN_WORKFLOWS);
        let s = h.until("the workflows", |_, s| s.contains("Prompt structured"));
        assert!(s.contains("Imported"), "{s}");
        h.shoot("live-workflows");
    }
    let _ = (json!({}), Value::Null, Identity::from_me, Store::create);
}
