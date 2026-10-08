//! The r2shots harness plus the root (Scope, Ctx), so a test can open a
//! screen's entry point directly (the Accounts screen's other-user email).
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

use abstractgateway_console::store::{ConnPhase, Identity, Store};
use abstractgateway_console::ui::{self, Ctx, UiState};
use abstractgateway_console::worker::Cmd;

pub const SIZES: [(i32, i32); 2] = [(120, 40), (60, 30)];

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

pub struct Harness {
    pub root: Rc<RefCell<Option<(Scope, Ctx)>>>,
    pub app: App,
    pub term: CaptureTerm,
    pub driver: Driver,
    pub store: Store,
    pub ui: UiState,
    _rx: mpsc::Receiver<Cmd>,
}

pub fn harness(size: Size) -> Harness {
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
    let slot: Rc<RefCell<Option<(Store, UiState)>>> = Rc::new(RefCell::new(None));
    let root: Rc<RefCell<Option<(Scope, Ctx)>>> = Rc::new(RefCell::new(None));
    let root_out = root.clone();
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
        *root_out.borrow_mut() = Some((cx, ctx.clone()));
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
        root,
        app,
        term,
        driver,
        store,
        ui,
        _rx: rx,
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
        self.term.push_input(bytes);
        self.turns(3)
    }
    pub fn admin(&mut self) {
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
    pub fn wheel_down(&mut self, n: usize) {
        let size = self.term.screen().size();
        for _ in 0..n {
            let ev = format!("\x1b[<65;{};{}M", size.w / 2, size.h / 2);
            self.term.push_input(ev.as_bytes());
        }
        self.turns(3);
    }
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
    pub fn shoot(&mut self, name: &str) {
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

impl Harness {
    /// Commands the UI sent to the worker since the last drain.
    pub fn sent(&mut self) -> Vec<Cmd> {
        self._rx.try_iter().collect()
    }
}

/// R15: a form modal is a centred box over the page — the text inside
/// the outermost box when one is open (the page shows beside it).
pub fn inside_modal(s: &str) -> String {
    let lines: Vec<Vec<char>> = s.lines().map(|l| l.chars().collect()).collect();
    let mut best: Option<(usize, usize, usize)> = None;
    for (y, l) in lines.iter().enumerate() {
        if let (Some(x0), Some(x1)) = (
            l.iter().position(|c| *c == '╭'),
            l.iter().rposition(|c| *c == '╮'),
        ) {
            let w = x1.saturating_sub(x0);
            if w > 20
                && best.map(|(_, a, b)| w > b - a).unwrap_or(true)
                && l.get(x0 + 1) == Some(&'─')
            {
                best = Some((y, x0, x1));
            }
        }
    }
    let Some((top, x0, x1)) = best else {
        return s.to_string();
    };
    let mut out = Vec::new();
    for l in lines.iter().skip(top + 1) {
        if l.get(x0) == Some(&'╰') {
            break;
        }
        out.push(l.iter().skip(x0 + 1).take(x1 - x0 - 1).collect::<String>());
    }
    out.join("\n")
}
