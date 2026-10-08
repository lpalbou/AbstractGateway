//! Round 7 (R7.2) shell parity, headless: the web sidebar's order and
//! groups, the About page/overlay (the shared kit card, no package list),
//! and the Setup page's recommended-defaults flow — snapshots per state at
//! 80×24 and 120×40 plus the command each action sends. The live drive of
//! the same actions is `tests/live_r7w2_shell.rs`.

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::{mpsc, Arc};

use abstracttui::app::Driver;
use abstracttui::prelude::*;
use abstracttui::testing::CaptureTerm;
use serde_json::{json, Value};

use abstractcore_console::screens::{ScreensCtx, ScreensOptions};
use abstractcore_console::{ConsoleTransport, TransportError};

use abstractgateway_console::store::{
    AvailabilityData, ConnPhase, Identity, Loadable, RoutesData, Store,
};
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
    rx: mpsc::Receiver<Cmd>,
}

fn harness(size: Size) -> Harness {
    abstracttui::app::set_theme_by_id("abstract-dark");
    let mut app = App::new(size);
    let overlays = app.overlays();
    let quitter = app.quitter();
    let (tx, rx) = mpsc::channel::<Cmd>();
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
    fn esc(&mut self) -> String {
        // Bare-ESC disambiguation: byte arrives, 30ms deadline, resolve.
        self.term.push_input(&[0x1b]);
        self.turns(1);
        std::thread::sleep(std::time::Duration::from_millis(45));
        self.turns(3)
    }
    fn drain(&mut self) -> Vec<Cmd> {
        let mut out = Vec::new();
        while let Ok(c) = self.rx.try_recv() {
            out.push(c);
        }
        out
    }
    fn admin(&mut self) {
        let id = Identity::from_me(&json!({
            "principal": {"user_id": "admin", "tenant_id": "default", "roles": ["admin", "user"], "admin": true},
            "auth": {"mode": "users"}, "routing": {"mode": "per-principal"}
        }))
        .unwrap();
        self.store.conn.set(ConnPhase::Connected(id));
        self.ui.wizard.set(false);
        self.turns(2);
    }
    fn user(&mut self) {
        let id = Identity::from_me(&json!({
            "principal": {"user_id": "alice", "tenant_id": "default", "roles": ["user"], "admin": false},
            "auth": {"mode": "users"}, "routing": {"mode": "per-principal"}
        }))
        .unwrap();
        self.store.conn.set(ConnPhase::Connected(id));
        self.ui.wizard.set(false);
        self.turns(2);
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

const SIZES: [(i32, i32); 2] = [(80, 24), (120, 40)];

/// Every line of a screen fits its width (no garbling/overflow).
fn assert_fits(s: &str, w: i32) {
    for l in s.lines() {
        assert!(
            abstracttui::text::width(l) <= w,
            "line wider than {w}: {l:?}"
        );
    }
}

// ---------------------------------------------------------------------
// Navigation: the web sidebar (console.py shell_nav)
// ---------------------------------------------------------------------

#[test]
fn group_line_and_tabs_follow_the_web_sidebar() {
    // R15 shell: below 120x32 a one-row nav strip (the active screen's group
    // caption + titles windowed around it); from 120x32 (the harness adds
    // the rail's 21 cells to wide sizes) the web sidebar as a left rail.
    for (w, hgt) in SIZES {
        let mut h = harness(Size::new(w, hgt));
        h.admin();
        let s = h.goto(ui::SCREEN_USERS);
        assert_fits(&s, w);
        let wide = w >= 120 && hgt >= 32;
        if wide {
            for caption in [" ACCOUNTS", " WORK", " MODELS", " SYSTEM"] {
                assert!(
                    s.lines().any(|l| l.starts_with(caption)),
                    "rail caption {caption}:\n{s}"
                );
            }
            let pos = |name: &str| {
                s.lines()
                    .position(|l| l.chars().take(20).collect::<String>().contains(name))
            };
            // MODELS: Providers, OpenAI API, Models, Multimodal — in order;
            // SYSTEM: Resources, Sandbox, Network; then Connection, Setup, About.
            for (a, b) in [
                ("Providers", "OpenAI API"),
                ("OpenAI API", "Models"),
                ("Resources", "Sandbox"),
                ("Sandbox", "Network"),
                ("Network", "Setup"),
                ("Setup", "About"),
            ] {
                assert!(pos(a) < pos(b), "{a} above {b}:\n{s}");
            }
        } else {
            let strip = s.lines().nth(1).unwrap_or_default();
            assert!(strip.starts_with(" ACCOUNTS"), "strip caption:\n{strip}");
            let s = h.goto(ui::SCREEN_OPENAI);
            let strip = s.lines().nth(1).unwrap_or_default();
            assert!(strip.starts_with(" MODELS"), "strip caption:\n{strip}");
            let p = strip.find("Providers");
            let o = strip.find("OpenAI API");
            assert!(
                p.is_some() && o.is_some() && p < o,
                "MODELS order:\n{strip}"
            );
        }
        h.shoot("nav-openai");
        let s = h.goto(ui::SCREEN_ABOUT);
        // No Engines tab anywhere.
        assert!(
            !s.contains("Engines"),
            "Engines is merged into Providers:\n{s}"
        );
    }
}

#[test]
fn letter_keys_jump_to_the_system_pages_and_about() {
    let mut h = harness(Size::new(120, 40));
    h.admin();
    h.goto(ui::SCREEN_USERS);
    for (key, screen) in [
        (b"H", ui::SCREEN_MODELS),
        (b"T", ui::SCREEN_REVIEW),
        (b"N", ui::SCREEN_NETWORK),
        (b"S", ui::SCREEN_WELCOME),
        (b"I", ui::SCREEN_ABOUT),
        (b"8", ui::SCREEN_OPENAI),
    ] {
        h.ui.screen.set(ui::SCREEN_USERS);
        h.turns(2);
        h.key(key);
        assert_eq!(h.ui.screen.get_untracked(), screen, "key {:?}", key);
    }
}

#[test]
fn the_key_hint_bar_wraps_instead_of_cutting_the_screen_verbs() {
    // R15 §2.7: ONE status row; the screen's verbs lead, `? keys` follows
    // them, the universal keys truncate first; the panel lists them all.
    let mut h = harness(Size::new(80, 24));
    h.admin();
    let s = h.goto(ui::SCREEN_WELCOME);
    let rows: Vec<&str> = s.lines().collect();
    let last = rows[rows.len() - 1];
    assert!(
        // D Download all joins once the recommended set has an absent model.
        last.starts_with("a Use recommended defaults · r refresh"),
        "{s}"
    );
    assert!(last.contains("? keys"), "the keys panel is named:\n{s}");
}

// ---------------------------------------------------------------------
// About: the shared kit card (AfAbout), page and overlay
// ---------------------------------------------------------------------

fn about_payload() -> Value {
    json!({"abstractframework": "0.10.0", "abstractgateway": "0.13.0",
           "packages": {"abstractcore": "2.25.0", "abstractruntime": "0.9.0"}})
}

#[test]
fn about_page_is_the_kit_card_without_a_package_list() {
    for (w, hgt) in SIZES {
        let mut h = harness(Size::new(w, hgt));
        h.admin();
        let s = h.goto(ui::SCREEN_ABOUT);
        assert!(
            h.drain().iter().any(|c| matches!(c, Cmd::LoadAbout)),
            "the page reads GET /about"
        );
        assert!(s.contains("reading GET /api/gateway/about"), "{s}");
        h.store.about.set(Loadable::Ready(about_payload()));
        let s = h.turns(3);
        assert_fits(&s, w);
        for needle in [
            "AbstractGateway console",
            "AbstractFramework 0.10.0",
            "AbstractGateway   0.13.0",
            "Website",
            "Source",
            "Docs",
            "Issues",
            "Feedback",
            "Contact           contact@abstractframework.ai",
            "Released under the MIT License.",
        ] {
            assert!(s.contains(needle), "{needle:?} at {w}x{hgt}:\n{s}");
        }
        for absent in ["abstractcore", "abstractruntime", "2.25.0"] {
            assert!(!s.contains(absent), "no package list ({absent}):\n{s}");
        }
        // The feedback link is never cut: its tail is on screen.
        assert!(s.contains("feedback"), "the long link wraps:\n{s}");
        h.shoot("about");
        // `r` re-reads it.
        h.key(b"r");
        assert!(h.drain().iter().any(|c| matches!(c, Cmd::LoadAbout)));
    }
}

#[test]
fn about_overlay_opens_anywhere_and_esc_closes_it() {
    let mut h = harness(Size::new(80, 24));
    h.admin();
    h.goto(ui::SCREEN_USERS);
    h.key(b"\x1bOP");
    assert!(
        h.drain().iter().any(|c| matches!(c, Cmd::LoadAbout)),
        "F1 re-reads GET /about"
    );
    h.store.about.set(Loadable::Ready(about_payload()));
    let s = h.turns(3);
    assert!(s.contains("AbstractGateway console"), "the name row:\n{s}");
    assert!(
        s.contains("Close (Esc)") && s.contains("AbstractGateway   0.13.0"),
        "{s}"
    );
    h.shoot("about-overlay");
    let s = h.esc();
    assert!(!s.contains("Close (Esc)"), "Esc closes:\n{s}");
}

// ---------------------------------------------------------------------
// Setup: the recommended-defaults flow (the web guide's model step)
// ---------------------------------------------------------------------

fn plan() -> AvailabilityData {
    AvailabilityData::from_value(&json!({
        "routes": [],
        "recommended": {
            "recommended": [
                {"route": "input.text", "provider": "mlx", "artifact": "mlx-community/Qwen3-8B-4bit",
                 "route_provider": "mlx", "route_model": "mlx-community/Qwen3-8B-4bit",
                 "status": "installed"},
                {"route": "output.voice", "provider": "supertonic", "artifact": "supertonic-3",
                 "route_provider": "supertonic", "route_model": "supertonic-3", "status": "absent",
                 "warning": "Tight: it runs with a small context by default."},
                {"route": "input.voice", "provider": "huggingface", "artifact": "Systran/faster-whisper-large-v3",
                 "route_provider": "faster-whisper", "route_model": "large-v3", "status": "absent"}
            ],
            "total": 3, "installed": 1, "absent": 2, "unknown": 0, "gaps": []
        }
    }))
}

fn routes() -> RoutesData {
    RoutesData::from_value(&json!({"ok": true, "writable": true, "routes": [
        {"key": "output.text", "kind": "output", "modality": "text", "configured": true,
         "provider": "mlx", "model": "mlx-community/Qwen3-8B-4bit", "source": "stored"}
    ]}))
}

#[test]
fn setup_shows_the_recommended_set_in_the_guides_words() {
    for (w, hgt) in SIZES {
        let mut h = harness(Size::new(w, hgt));
        h.admin();
        h.goto(ui::SCREEN_WELCOME);
        let cmds = h.drain();
        assert!(
            cmds.iter().any(|c| matches!(c, Cmd::LoadAvailability)),
            "{cmds:?}"
        );
        assert!(
            cmds.iter().any(|c| matches!(c, Cmd::LoadRoutes)),
            "{cmds:?}"
        );
        h.store.availability.set(Loadable::Ready(plan()));
        h.store.routes.set(Loadable::Ready(routes()));
        // Scroll the page body to the recommended block at 80×24.
        let mut s = h.turns(3);
        for _ in 0..4 {
            if s.contains("Transcription · Not downloaded") {
                break;
            }
            s = h.key(b"\x1b[6~");
        }
        let all = s.clone();
        assert_fits(&all, w);
        if hgt >= 40 {
            for needle in [
                "Recommended for this computer",
                "Text model now: mlx · mlx-community/Qwen3-8B-4bit",
                "Chat and text · Installed",
                "Voice · Not downloaded",
                "supertonic · supertonic-3",
                "Reads answers aloud",
                "Tight: it runs with a small context by default.",
                "Transcription · Not downloaded",
                "faster-whisper · large-v3",
                "a Use recommended defaults · D Download all",
            ] {
                assert!(all.contains(needle), "{needle:?}:\n{all}");
            }
            assert!(all.contains("Choices you already made are kept."), "{all}");
            h.shoot("setup-recommended");
        }
    }
}

#[test]
fn setup_empty_and_unset_states_use_the_web_sentences() {
    let mut h = harness(Size::new(120, 40));
    h.admin();
    h.goto(ui::SCREEN_WELCOME);
    let s = h.turns(2);
    assert!(
        s.contains("Checking the recommended starter models..."),
        "{s}"
    );
    h.store
        .availability
        .set(Loadable::Ready(AvailabilityData::from_value(
            &json!({"routes": [], "recommended": {"recommended": []}}),
        )));
    h.store.routes.set(Loadable::Ready(RoutesData::from_value(
        &json!({"ok": true, "routes": []}),
    )));
    let s = h.turns(3);
    assert!(
        s.contains("This gateway reported no recommended downloads."),
        "{s}"
    );
    assert!(s.contains("No text model is set yet."), "{s}");
}

#[test]
fn setup_a_applies_the_recommended_defaults_and_offers_the_second_pass_inline() {
    let mut h = harness(Size::new(120, 40));
    h.admin();
    h.goto(ui::SCREEN_WELCOME);
    h.store.availability.set(Loadable::Ready(plan()));
    h.turns(2);
    h.drain();
    h.key(b"a");
    let cmds = h.drain();
    assert!(
        cmds.iter()
            .any(|c| matches!(c, Cmd::ApplyRecommendedRoutes { force: false })),
        "a applies without overwriting your routes: {cmds:?}"
    );
    // The worker offers the forced pass: the web's button under the
    // outcome sentence ("♻ Replace mine too"), never a dialog.
    h.store.apply_followup.set(Some("Replace mine too".into()));
    let s = h.turns(3);
    assert!(
        s.contains("routes you configured were kept") && s.contains("♻ Replace mine too"),
        "{s}"
    );
    h.shoot("setup-replace-mine-too");
    let (y, line) = s
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains("♻ Replace mine too"))
        .unwrap();
    let x = line[..line.find("♻").unwrap()].chars().count() + 1;
    h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    h.turns(2);
    let cmds = h.drain();
    assert!(
        cmds.iter()
            .any(|c| matches!(c, Cmd::ApplyRecommendedRoutes { force: true })),
        "the button runs the forced pass: {cmds:?}"
    );
}

#[test]
fn setup_d_confirms_inline_then_downloads_all() {
    // R15: the web's Download all starts at once (no question).
    let mut h = harness(Size::new(120, 40));
    h.admin();
    h.goto(ui::SCREEN_WELCOME);
    h.store.availability.set(Loadable::Ready(plan()));
    h.turns(2);
    h.drain();
    h.key(b"D");
    h.turns(2);
    assert!(h
        .drain()
        .iter()
        .any(|c| matches!(c, Cmd::DownloadRecommended)));
}

#[test]
fn setup_actions_are_admin_only() {
    let mut h = harness(Size::new(120, 40));
    h.user();
    h.goto(ui::SCREEN_WELCOME);
    h.store.availability.set(Loadable::Ready(plan()));
    let s = h.turns(3);
    assert!(
        !s.contains("a Use recommended defaults · D"),
        "no admin verbs for a user:\n{s}"
    );
    h.drain();
    h.key(b"a");
    h.key(b"D");
    let cmds = h.drain();
    assert!(
        !cmds.iter().any(|c| matches!(
            c,
            Cmd::ApplyRecommendedRoutes { .. } | Cmd::DownloadRecommended
        )),
        "{cmds:?}"
    );
}
