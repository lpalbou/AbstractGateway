//! R8-W4 harness (gateway TUI parity, round 8; copied from the R7-W1 harness): mounts ONE page — or the whole
//! root — over a capture terminal; hermetic by default (a channel nobody
//! drains), or wired to the REAL worker against a live scratch gateway
//! (`R8W4_URL` + `R8W4_TOKEN`; never :8080/:8081). Captures land in
//! `R8W4_SHOTS_DIR` when set.
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

pub const SIZES: [(i32, i32); 2] = [(80, 24), (120, 40)];

pub struct NoTransport;

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

/// What the harness mounts.
#[derive(Clone, Copy)]
pub enum Mount {
    /// The whole console (sidebar, footer) — `ui::root`.
    Root,
    /// One page alone (its own keys, overlays and confirms).
    Page(fn(&Ctx, Scope) -> View),
}

pub struct Harness {
    pub app: App,
    pub term: CaptureTerm,
    pub driver: Driver,
    pub store: Store,
    pub ui: UiState,
    pub tx: mpsc::Sender<Cmd>,
    pub rx: Option<mpsc::Receiver<Cmd>>,
    _worker: Option<std::thread::JoinHandle<()>>,
}

pub fn harness(size: (i32, i32), mount: Mount) -> Harness {
    let size = Size::new(size.0, size.1);
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
        // R15: the shell installs these; a page mounted alone needs them for
        // tooltips, the focused-control line and refused presses.
        abstractgateway_console::ui::w::tip::install(ui_state.focus_line, overlays.clone());
        abstractgateway_console::ui::w::tip::install_notice(store.notice);
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
        match mount {
            Mount::Root => ui::root(cx, ctx),
            Mount::Page(page) => {
                let notice = store.notice;
                Element::new()
                    .style(LayoutStyle::column().grow(1.0))
                    .child(page(&ctx, cx))
                    .child(dyn_view(LayoutStyle::line(1).shrink(0.0), move || {
                        let t = use_theme(cx).get().tokens;
                        let n = notice.get().unwrap_or_default();
                        Element::new()
                            .style(LayoutStyle::line(1))
                            .draw(move |canvas, rect| {
                                canvas.print(
                                    abstracttui::base::Point::new(rect.x, rect.y),
                                    &n,
                                    t.text_muted,
                                    abstracttui::base::Rgba::TRANSPARENT,
                                );
                            })
                            .build()
                    }))
                    .build()
            }
        }
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
        _worker: None,
    }
}

impl Harness {
    pub fn turns(&mut self, n: usize) -> String {
        let mut last = String::new();
        for _ in 0..n {
            self.driver
                .turn(&mut self.app, &mut self.term)
                .expect("turn");
            last = self.term.screen().to_text();
        }
        last
    }
    pub fn key(&mut self, bytes: &[u8]) -> String {
        if bytes == b"\x1b" {
            return self.esc();
        }
        self.term.push_input(bytes);
        self.turns(3)
    }
    /// A bare Esc (the reader resolves it after its 30 ms deadline).
    pub fn esc(&mut self) -> String {
        self.term.push_input(&[0x1b]);
        self.turns(1);
        std::thread::sleep(Duration::from_millis(45));
        self.turns(3)
    }
    /// Wheel down over the middle of the screen (scrolls an overlay body).
    pub fn wheel_down(&mut self, n: usize) -> String {
        let size = self.term.screen().size();
        for _ in 0..n {
            let ev = format!("\x1b[<65;{};{}M", size.w / 2, size.h / 2);
            self.term.push_input(ev.as_bytes());
        }
        self.turns(3)
    }
    /// Click the first occurrence of `text` on screen.
    pub fn click_text(&mut self, text: &str) -> String {
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
    /// Click `dx` cells right of the first occurrence of `text` (a field
    /// beside its label).
    pub fn click_right_of(&mut self, text: &str, dx: usize) -> String {
        let screen = self.turns(1);
        let (row, col) = screen
            .lines()
            .enumerate()
            .find_map(|(i, l)| l.find(text).map(|c| (i, l[..c].chars().count())))
            .unwrap_or_else(|| panic!("{text:?} not on screen:\n{screen}"));
        let x = col + dx + 1;
        let click = format!("\x1b[<0;{};{}M\x1b[<0;{};{}m", x, row + 1, x, row + 1);
        self.key(click.as_bytes())
    }
    /// Click `needle` on the first line at or below the first line that
    /// contains `anchor` (a row's control under its label: a path, then its
    /// "Permission:" segments).
    pub fn click_after(&mut self, anchor: &str, needle: &str) -> String {
        let screen = self.turns(1);
        let lines: Vec<&str> = screen.lines().collect();
        let start = lines
            .iter()
            .position(|l| l.contains(anchor))
            .unwrap_or_else(|| panic!("{anchor:?} not on screen:\n{screen}"));
        let (row, col) = lines
            .iter()
            .enumerate()
            .skip(start)
            .find_map(|(i, l)| l.find(needle).map(|c| (i, l[..c].chars().count())))
            .unwrap_or_else(|| panic!("{needle:?} not at/below {anchor:?}:\n{screen}"));
        let click = format!(
            "\x1b[<0;{};{}M\x1b[<0;{};{}m",
            col + 2,
            row + 1,
            col + 2,
            row + 1
        );
        self.key(click.as_bytes())
    }
    /// Type text (each char a key press).
    pub fn type_text(&mut self, text: &str) -> String {
        self.term.push_input(text.as_bytes());
        self.turns(3)
    }
    pub fn text(&mut self) -> String {
        self.turns(2)
    }
    /// Signed in as an admin (hermetic: no probe).
    pub fn admin(&mut self) {
        self.identity("admin", true);
    }
    pub fn identity(&mut self, user: &str, admin: bool) {
        let roles = if admin {
            json!(["admin", "user"])
        } else {
            json!(["user"])
        };
        let id = Identity::from_me(&json!({
            "principal": {"user_id": user, "tenant_id": "default", "roles": roles, "admin": admin},
            "auth": {"mode": "users"}, "routing": {"mode": "per-principal"}
        }))
        .unwrap();
        self.store.conn.set(ConnPhase::Connected(id));
        self.turns(1);
        self.ui.wizard.set(false);
    }
    /// Commands the UI sent (hermetic harness only).
    pub fn sent(&mut self) -> Vec<Cmd> {
        self.rx
            .as_ref()
            .map(|r| r.try_iter().collect())
            .unwrap_or_default()
    }
    /// Turn frames until `pred` holds (the worker answers on its thread).
    pub fn until(
        &mut self,
        what: &str,
        mut pred: impl FnMut(&mut Harness, &str) -> bool,
    ) -> String {
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
            std::thread::sleep(Duration::from_millis(25));
        }
    }
    pub fn until_text(&mut self, needle: &str) -> String {
        let n = needle.to_string();
        self.until(needle, move |_, s| s.contains(&n))
    }
    /// Write the capture (`<dir>/<name>-WxH.txt`) when `R8W4_SHOTS_DIR` is set.
    pub fn shoot(&mut self, name: &str) -> String {
        let s = self.turns(2);
        if let Ok(dir) = std::env::var("R8W4_SHOTS_DIR") {
            let size = self.term.screen().size();
            std::fs::create_dir_all(&dir).expect("shots dir");
            std::fs::write(format!("{dir}/{name}-{}x{}.txt", size.w, size.h), &s).expect("write");
        }
        s
    }
    /// Every line fits the terminal (no overflow / garbling).
    pub fn assert_fits(&mut self) {
        let s = self.turns(1);
        let w = self.term.screen().size().w;
        for l in s.lines() {
            assert!(
                abstracttui::text::width(l) <= w,
                "line wider than {w}: {l:?}\n{s}"
            );
        }
    }
}

/// The live scratch gateway (`R8W4_URL`, `R8W4_TOKEN`), or None (the test
/// then skips with a line saying so — `cargo test` stays hermetic).
pub fn live_env() -> Option<(String, String)> {
    let url = std::env::var("R8W4_URL").ok()?;
    let token = std::env::var("R8W4_TOKEN").ok()?;
    assert!(
        !url.ends_with(":8080") && !url.ends_with(":8081"),
        "hermetic gateways only"
    );
    Some((url, token))
}

/// A harness wired to the real worker, connected to the scratch gateway.
pub fn live(size: (i32, i32), mount: Mount, url: &str, token: &str) -> Harness {
    let mut h = harness(size, mount);
    let wake = abstracttui::reactive::wake_handle();
    let ui_state = h.ui;
    let done_sink = {
        let wake = wake.clone();
        move |fid: u64, out: Result<String, String>| {
            wake.post(move || ui_state.write_done.set(Some((fid, out.clone()))))
        }
    };
    let rx = h.rx.take().expect("rx");
    // Issued tokens go to the token queue, as in production (lib.rs).
    let token_sink = {
        let wake = wake.clone();
        move |u: String, t: String| {
            wake.post(move || {
                ui_state
                    .token_queue
                    .update(|q| q.push((u.clone(), t.clone())))
            })
        }
    };
    let _ = abstractgateway_console::worker::spawn(
        h.store,
        wake,
        rx,
        h.tx.clone(),
        token_sink,
        done_sink,
    );
    h.ui.wizard.set(false);
    h.ui.conn_url.set(url.to_string());
    h.ui.conn_token.set(token.to_string());
    h.tx.send(Cmd::Connect {
        url: url.to_string(),
        token: token.to_string().into(),
    })
    .unwrap();
    h.until("connected", |h, _| {
        h.store.conn.with_untracked(ConnPhase::is_connected)
    });
    h
}

/// A direct HTTP call to the scratch gateway (test assertions read the
/// gateway's truth, not the console's).
pub fn gw(method: &str, url: &str, token: &str, path: &str, body: Option<Value>) -> Value {
    let full = format!("{url}/api/gateway{path}");
    let req = ureq::request(method, &full).set("Authorization", &format!("Bearer {token}"));
    let resp = match body {
        Some(b) => req
            .set("Content-Type", "application/json")
            .send_string(&b.to_string()),
        None => req.call(),
    };
    let text = match resp {
        Ok(r) => r.into_string().unwrap_or_default(),
        Err(ureq::Error::Status(_, r)) => r.into_string().unwrap_or_default(),
        Err(e) => panic!("{full}: {e}"),
    };
    serde_json::from_str(&text).unwrap_or(Value::Null)
}
