//! Headless UI tests for the parity fix-ui pass (2026-09-27, REVIEW-1
//! M1/M2/M3/M6 and the UI minors). Own file with a minimal harness so
//! parallel parity branches never collide in headless_ui.rs. No test
//! touches the network: the worker is a dummy channel and gateway
//! results are applied to the store directly.

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
    AvailabilityData, ConnPhase, Identity, Loadable, ProvidersData, RoutesData, Store,
};
use abstractgateway_console::ui::{self, Ctx, UiState};
use abstractgateway_console::worker::Cmd;

struct NoTransport;

impl ConsoleTransport for NoTransport {
    fn host_profile(&self) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn engines_status(&self, _probe: bool) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn models_catalog(
        &self,
        _q: &str,
        _e: Option<&str>,
        _f: bool,
    ) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn models_installed(&self, _p: Option<&str>) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn start_download(
        &self,
        _p: &str,
        _a: &str,
        _expected_bytes: Option<u64>,
    ) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn delete_model(&self, _p: &str, _a: &str, _f: bool) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn engine_install(&self, _id: &str, _d: bool) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn job(&self, _id: &str) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
    fn cancel_job(&self, _id: &str) -> Result<Value, TransportError> {
        Err(TransportError::unavailable("test"))
    }
}

struct H {
    app: App,
    term: CaptureTerm,
    driver: Driver,
    store: Store,
    ui: UiState,
    rx: mpsc::Receiver<Cmd>,
}

fn harness(size: Size) -> H {
    abstracttui::app::set_theme_by_id("abstract-dark");
    let mut app = App::new(size);
    let overlays = app.overlays();
    let quitter = app.quitter();
    let (tx, rx) = mpsc::channel::<Cmd>();
    let store_slot: Rc<RefCell<Option<Store>>> = Rc::new(RefCell::new(None));
    let store_out = store_slot.clone();
    let ui_slot: Rc<RefCell<Option<UiState>>> = Rc::new(RefCell::new(None));
    let ui_out = ui_slot.clone();
    app.mount(move |cx| {
        let store = Store::create(cx);
        *store_out.borrow_mut() = Some(store);
        let ui_state = UiState::create(cx, "http://127.0.0.1:8080".to_string(), String::new());
        *ui_out.borrow_mut() = Some(ui_state);
        let transport: Arc<dyn ConsoleTransport> = Arc::new(NoTransport);
        let screens = ScreensCtx::new(
            cx,
            transport.clone(),
            overlays.clone(),
            // The same Access wiring as lib.rs (follows the connection).
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
    let store = store_slot.borrow().expect("store");
    let ui = ui_slot.borrow().expect("ui");
    H {
        app,
        term,
        driver,
        store,
        ui,
        rx,
    }
}

impl H {
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
    fn type_text(&mut self, s: &str) {
        self.term.push_input(s.as_bytes());
    }
    fn key(&mut self, bytes: &[u8]) {
        self.term.push_input(bytes);
    }
    fn press_escape(&mut self) {
        // Bare-ESC disambiguation: byte arrives, 30ms deadline, resolve.
        self.term.push_input(&[0x1b]);
        self.turns(1);
        std::thread::sleep(std::time::Duration::from_millis(45));
        self.turns(2);
    }
    fn click(&mut self, x: usize, y: usize) {
        self.key(format!("\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m").as_bytes());
        self.turns(2);
    }
    /// Click the field column of the row whose text contains `label`.
    fn click_field(&mut self, label: &str) {
        let s = self.turns(1);
        let row = find_row(&s, label);
        self.click(25, row);
    }
    fn drain(&mut self) -> Vec<Cmd> {
        let mut out = Vec::new();
        while let Ok(c) = self.rx.try_recv() {
            out.push(c);
        }
        out
    }
    fn find_cmd(&mut self, mut pred: impl FnMut(&Cmd) -> bool) -> Option<Cmd> {
        while let Ok(c) = self.rx.try_recv() {
            if pred(&c) {
                return Some(c);
            }
        }
        None
    }
    fn connect(&mut self) {
        let id = Identity::from_me(&json!({
            "ok": true,
            "principal": {"user_id": "admin", "tenant_id": "default", "roles": ["admin"], "admin": true},
            "auth": {"mode": "users"}, "routing": {"mode": "per-principal"}
        }))
        .expect("identity");
        self.store.conn.set(ConnPhase::Connected(id));
        self.turns(1);
    }
}

fn find_row(screen: &str, needle: &str) -> usize {
    screen
        .lines()
        .position(|l| l.contains(needle))
        .unwrap_or_else(|| panic!("'{needle}' not on screen:\n{screen}"))
        + 1
}

fn fixture(name: &str) -> Value {
    let path = format!("{}/tests/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
    serde_json::from_str(&std::fs::read_to_string(&path).expect("fixture")).expect("json")
}

fn network() -> Value {
    json!({
        "schema": "gateway_network_v1", "writable": true,
        "configured": {"mode": "localhost", "label": "Localhost only", "port": 8080, "bind_host": "127.0.0.1",
            "source": "stored", "port_source": "stored", "internet_acknowledged": null},
        "effective": {"mode": "localhost", "label": "Localhost only", "bind_host": "127.0.0.1", "port": 8080,
            "overridden_by_cli": false, "host_source": "setting", "port_source": "setting", "running": true},
        "restart_required": false,
        "restart": {"available": true, "applies": true, "needed": false, "how": "POST", "reason": null},
        "auth": {"user_auth": true, "token_auth": false, "ok_for_mode": true, "will_enable_user_auth": false},
        "modes": [
            {"id": "localhost", "label": "Localhost only", "selected": true, "allowed": true, "requires_acknowledgement": false},
            {"id": "lan", "label": "Local network", "selected": false, "allowed": true, "requires_acknowledgement": false},
            {"id": "internet", "label": "Internet", "selected": false, "allowed": true, "requires_acknowledgement": true}
        ],
        "addresses": [
            {"kind": "loopback", "url": "http://127.0.0.1:8080", "host": "127.0.0.1", "port": 8080, "reachable": true, "note": "this machine only"},
            {"kind": "lan", "url": "http://192.168.1.23:8080", "host": "192.168.1.23", "port": 8080, "interface": "en0", "interface_label": "Wi-Fi", "reachable": false}
        ],
        "copy_hint": "http://127.0.0.1:8080", "warnings": []
    })
}

fn finish_in_the_guide(h: &mut H) {
    h.ui.wizard.set(true);
    h.ui.screen.set(ui::SCREEN_REVIEW);
    let s = h.turns(3);
    let row = find_row(&s, "Skip setup");
    let col = s
        .lines()
        .nth(row - 1)
        .unwrap()
        .find(" Finish ")
        .expect("Finish")
        + 2;
    h.drain();
    h.click(col, row);
    let fid = match h.find_cmd(|c| matches!(c, Cmd::CompleteFirstRun { .. })) {
        Some(Cmd::CompleteFirstRun { form_id, .. }) => form_id.expect("routed back"),
        other => panic!("expected CompleteFirstRun, got {other:?}"),
    };
    h.ui.write_done
        .set(Some((fid, Ok("GET /host/first-run: completed".into()))));
    h.turns(3);
    assert!(!h.ui.wizard.get_untracked(), "verified → browse");
}

// ---- M1: the Review prompt never takes the caret by itself -------------

/// Finish flips the guide to browse (the Review page re-mounts): `3`
/// must jump to Routes, not type "3" into the sandbox prompt.
#[test]
fn m1_after_finish_screen_keys_jump_instead_of_typing() {
    let mut h = harness(Size::new(110, 34));
    h.connect();
    let prompt = h.ui.sb_prompt.get_untracked();
    finish_in_the_guide(&mut h);
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_REVIEW);
    h.type_text("3");
    h.turns(3);
    assert_eq!(
        h.ui.screen.get_untracked(),
        ui::SCREEN_ROUTES,
        "3 jumps to Routes"
    );
    assert_eq!(
        h.ui.sb_prompt.get_untracked(),
        prompt,
        "nothing typed into the prompt"
    );
}

/// Arriving on Review in the guide, Enter must NOT start a real
/// generation (it did while the prompt was autofocused).
#[test]
fn m1_enter_on_arrival_at_review_generates_nothing() {
    let mut h = harness(Size::new(110, 34));
    h.connect();
    // A runnable pair: were the prompt focused, Enter WOULD generate.
    h.store
        .providers
        .set(Loadable::Ready(ProvidersData::from_value(&json!({
            "items": [{"name": "lmstudio", "display_name": "LMStudio", "status": "available",
                       "local_provider": true, "authentication_required": false, "models": []}]
        }))));
    h.store.models.update(|m| {
        m.insert(
            "lmstudio".into(),
            Loadable::Ready(vec!["test-model-a".into()]),
        );
    });
    h.ui.sb_provider.set("lmstudio".into());
    h.ui.sb_model.set("test-model-a".into());
    h.ui.wizard.set(true);
    h.ui.screen.set(ui::SCREEN_REVIEW);
    h.turns(3);
    h.drain();
    h.type_text("\r");
    h.turns(2);
    let cmds = h.drain();
    assert!(
        !cmds
            .iter()
            .any(|c| matches!(c, Cmd::SandboxTest { .. } | Cmd::SandboxMedia { .. })),
        "no generation on a bare Enter: {cmds:?}"
    );
}

// ---- M2: URL autofocus only while not connected; Esc releases ----------

/// Boot (not connected): the caret is in the URL field. Once connected
/// (a forced browse session, no remount of the screen), `5` jumps.
#[test]
fn m2_url_field_holds_the_caret_only_until_connected() {
    let mut h = harness(Size::new(110, 34));
    h.ui.wizard.set(false);
    h.ui.conn_url.set(String::new());
    h.turns(3);
    h.type_text("x");
    h.turns(2);
    assert_eq!(
        h.ui.conn_url.get_untracked(),
        "x",
        "not connected: typing goes to the URL"
    );
    h.connect();
    h.turns(2);
    h.type_text("5");
    h.turns(3);
    assert_eq!(
        h.ui.screen.get_untracked(),
        4,
        "connected: 5 jumps to Runtimes"
    );
    assert_eq!(h.ui.conn_url.get_untracked(), "x", "the URL kept its value");
}

/// Esc in the URL field hands the keyboard back (and says so); the
/// next digit jumps; a second Esc is the screen's own Esc again.
#[test]
fn m2_esc_releases_the_url_field() {
    let mut h = harness(Size::new(110, 34));
    h.ui.wizard.set(false);
    h.ui.conn_url.set(String::new());
    h.turns(3);
    h.type_text("ab");
    h.turns(2);
    assert_eq!(h.ui.conn_url.get_untracked(), "ab");
    h.press_escape();
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some(ui::util::FOCUS_RELEASED),
        "the release is acknowledged"
    );
    assert_eq!(
        h.ui.screen.get_untracked(),
        0,
        "the first Esc does not navigate"
    );
    h.press_escape();
    assert_eq!(
        h.store.notice.get_untracked().as_deref(),
        Some("already on the first screen"),
        "the second Esc is the screen's Esc"
    );
    h.type_text("2");
    h.turns(3);
    assert_eq!(h.ui.screen.get_untracked(), 1, "2 jumps to Providers");
    assert_eq!(
        h.ui.conn_url.get_untracked(),
        "ab",
        "nothing typed after the release"
    );
}

/// The sandbox prompt: focused by hand, Esc releases, `3` jumps.
#[test]
fn m2_esc_releases_the_sandbox_prompt() {
    let mut h = harness(Size::new(110, 34));
    h.connect();
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_REVIEW);
    h.turns(3);
    h.click_field("│prompt ");
    h.type_text("z");
    h.turns(2);
    assert!(
        h.ui.sb_prompt.get_untracked().contains('z'),
        "the click put the caret there"
    );
    h.press_escape();
    h.type_text("3");
    h.turns(3);
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_ROUTES);
}

// ---- M3: the connected Connection screen at ≤ 32 rows ------------------

fn connection_connected(size: Size) -> String {
    let mut h = harness(size);
    h.connect();
    h.ui.wizard.set(false);
    h.ui.screen.set(0);
    h.turns(3);
    h.store.network.set(Loadable::Ready(
        abstractgateway_console::store::NetworkData::from_value(&network()),
    ));
    h.turns(3)
}

#[test]
fn m3_connection_keeps_its_top_rows_at_every_height() {
    for (w, hh) in [(80, 24), (80, 28), (80, 30), (110, 32), (200, 60)] {
        let s = connection_connected(Size::new(w, hh));
        for needle in [
            "Gateway URL",
            "Admin token",
            "Re-probe (connected ✓)",
            "● connected",
            // The Connection screen keeps ONE network line; N opens the screen.
            "Network: Localhost only · running Localhost only 127.0.0.1:8080",
        ] {
            assert!(s.contains(needle), "[{w}x{hh}] {needle:?} on screen:\n{s}");
        }
        // The block's first content row is never blank (the collapse
        // painted empty rows where the URL/token rows should be).
        let top = find_row(&s, "╭ Connection");
        let first = s.lines().nth(top).unwrap();
        assert!(
            !first
                .trim_matches(|c: char| c == '│' || c.is_whitespace())
                .is_empty(),
            "[{w}x{hh}] first row inside the block is blank:\n{s}"
        );
    }
}

/// The live 80x24 shape (a `--port` gateway: the "not applied" banner,
/// five addresses) plus a reverse proxy, on the Network screen: taller
/// than any 24-row screen. Whatever does not fit is cut — nothing paints
/// over the bottom border.
#[test]
fn m3_a_tall_network_panel_never_paints_over_the_border() {
    let mut v = network();
    v["restart_required"] = json!(true);
    v["restart"] = json!({"available": true, "applies": false, "needed": true, "how": "POST",
        "reason": "this gateway was started with --host/--port on its command line"});
    v["effective"]["overridden_by_cli"] = json!(true);
    v["addresses"] = json!((0..5)
        .map(
            |i| json!({"kind": "lan", "url": format!("http://10.0.0.{i}:18872"),
        "host": format!("10.0.0.{i}"), "port": 18872, "reachable": false})
        )
        .collect::<Vec<_>>());
    v["reverse_proxy"] = json!({
        "allowed_origins": {"value": ["https://gw.example.com"], "source": "setting",
            "overridden_by_env": false, "effective": ["https://gw.example.com"],
            "builtin": [], "self_origins": [], "applies": "live", "warnings": []},
        "trust_proxy": {"value": true, "source": "setting", "overridden_by_env": false,
            "effective": true, "applies": "live"}
    });
    let mut h = harness(Size::new(80, 24));
    h.connect();
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_NETWORK);
    h.turns(3);
    h.store.network.set(Loadable::Ready(
        abstractgateway_console::store::NetworkData::from_value(&v),
    ));
    let s = h.turns(3);
    for l in s.lines().filter(|l| l.trim_start().starts_with('╰')) {
        assert!(
            !l.chars().any(|c| c.is_ascii_alphanumeric()),
            "content fused into the border: {l:?}\n{s}"
        );
    }
    assert!(s.contains("Saved:") && s.contains("Running now:"), "{s}");
}

/// At 80x24 the not-connected 401 (the tallest status block) keeps the
/// URL field AND its fix on screen.
#[test]
fn m3_signin_needed_fits_at_80x24() {
    let mut h = harness(Size::new(80, 24));
    h.ui.wizard.set(true);
    h.ui.token_source
        .set(Some(ui::connection::NO_TOKEN_SENT.into()));
    h.store
        .conn
        .set(ConnPhase::Unauthorized("Missing bearer token".into()));
    let s = h.turns(3);
    for needle in [
        "Gateway URL",
        "sign-in needed (401)",
        "`abstractgateway serve` prints it",
    ] {
        assert!(s.contains(needle), "{needle:?}:\n{s}");
    }
}

// ---- M6: two different 401s ---------------------------------------------

#[test]
fn m6_401_says_whether_a_token_was_sent_and_where_the_admin_token_is() {
    let mut h = harness(Size::new(160, 40));
    h.ui.wizard.set(false);
    h.ui.token_source
        .set(Some(ui::connection::NO_TOKEN_SENT.into()));
    h.store
        .conn
        .set(ConnPhase::Unauthorized("Missing bearer token".into()));
    let s = h.turns(3);
    assert!(
        s.contains("sign-in needed (401) — no token was sent"),
        "{s}"
    );
    assert!(
        s.contains("admin token: `abstractgateway serve` prints it when it starts"),
        "{s}"
    );
    assert!(s.contains("launch with --token <token>"), "{s}");
    assert!(!s.contains("bootstrap-admin-token"), "{s}");
    assert!(!s.contains("rejected"), "nothing was rejected:\n{s}");

    h.ui.token_source.set(Some("the field (12 chars)".into()));
    h.store
        .conn
        .set(ConnPhase::Unauthorized("Invalid token".into()));
    let s = h.turns(3);
    assert!(
        s.contains("unauthorized (401) — the gateway rejected the token sent"),
        "{s}"
    );
    assert!(s.contains("token sent: the field (12 chars)"), "{s}");
    assert!(
        s.contains("`abstractgateway serve` prints it when it starts"),
        "{s}"
    );
    assert!(!s.contains("no token was sent"), "{s}");
}

// ---- UI minors ----------------------------------------------------------

/// `r` while not connected re-probes (the gateway came back) instead of
/// sending the operator to the Connection screen.
#[test]
fn r_reconnects_when_not_connected() {
    let mut h = harness(Size::new(110, 34));
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_ROUTES);
    h.store
        .conn
        .set(ConnPhase::Unreachable("connection refused".into()));
    h.turns(3);
    h.drain();
    h.type_text("r");
    h.turns(2);
    match h.find_cmd(|c| matches!(c, Cmd::Connect { .. })) {
        Some(Cmd::Connect { url, .. }) => assert_eq!(url, "http://127.0.0.1:8080"),
        other => panic!("expected a re-probe, got {other:?}"),
    }
    let n = h.store.notice.get_untracked().unwrap_or_default();
    assert!(n.contains("probing http://127.0.0.1:8080 again"), "{n}");
}

/// Notices live in the footer only: nothing draws over the header's
/// connection status (the engine toast rested on row 1, slid over row 0),
/// and a busy op no longer hides the notice.
#[test]
fn notices_never_cover_the_status_line() {
    let mut h = harness(Size::new(110, 34));
    h.connect();
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_ROUTES);
    h.turns(3);
    let note = "NOTICE-XYZ something the operator just did";
    h.store.notice.set(Some(note.into()));
    for _ in 0..6 {
        let s = h.turns(1);
        let head: Vec<&str> = s.lines().take(2).collect();
        assert!(head[0].contains("admin@default"), "header intact:\n{s}");
        assert!(
            !head.iter().any(|l| l.contains("NOTICE-XYZ")),
            "no overlay on the chrome:\n{s}"
        );
    }
    let s = h.turns(1);
    let footer = s.lines().rev().nth(1).unwrap_or_default();
    assert!(footer.contains("NOTICE-XYZ"), "the footer shows it:\n{s}");
    // Busy: the notice leads, the op follows.
    h.store.busy.update(|b| {
        b.push(abstractgateway_console::store::BusyOp {
            id: 1,
            label: "reading routes".into(),
            started: std::time::Instant::now(),
        })
    });
    let s = h.turns(2);
    let footer = s.lines().rev().nth(1).unwrap_or_default();
    assert!(
        footer.contains("NOTICE-XYZ") && footer.contains("reading routes"),
        "{footer}\n{s}"
    );
}

/// The Setup lede wraps inside the padded block (it wrapped at the
/// border and lost its last word to an ellipsis).
#[test]
fn welcome_lede_wraps_inside_the_padding() {
    for w in [80, 110] {
        let mut h = harness(Size::new(w, 30));
        h.connect();
        h.ui.wizard.set(true);
        h.ui.screen.set(ui::SCREEN_WELCOME);
        let s = h.turns(3);
        let text: String = s
            .lines()
            .map(|l| l.trim_matches(|c: char| c == '│' || c.is_whitespace()))
            .collect::<Vec<_>>()
            .join(" ");
        assert!(
            text.contains("The next steps get you to a working model. Every step is optional."),
            "[{w}] the whole lede, no word lost to an ellipsis:\n{s}"
        );
    }
}

/// Finish row: the "done" facts show at 80x24 (they vanished under 30
/// rows), and the web's command-line block shows on a taller terminal.
#[test]
fn finish_row_shows_the_facts_under_30_rows_and_the_cli_above() {
    let mut h = harness(Size::new(80, 24));
    h.connect();
    h.ui.wizard.set(true);
    h.ui.screen.set(ui::SCREEN_REVIEW);
    let s = h.turns(3);
    let row = s.lines().nth(find_row(&s, "Skip setup") - 1).unwrap();
    assert!(row.contains("text model not set yet"), "{s}");
    let mut h = harness(Size::new(160, 40));
    h.connect();
    h.ui.wizard.set(true);
    h.ui.screen.set(ui::SCREEN_REVIEW);
    let s = h.turns(3);
    assert!(s.contains("Console http://127.0.0.1:8080/console"), "{s}");
    assert!(
        s.contains("CLI abstractgateway claim --open (sign in again) · abstractgateway service install (start at login) · abstractgateway-config status"),
        "{s}"
    );
}

#[test]
fn done_cli_hints_follow_the_login_service() {
    use abstractgateway_console::ui::welcome::done_cli_hints;
    assert!(done_cli_hints(None).contains("abstractgateway service install"));
    let installed = done_cli_hints(Some((Some(true), Some("launchd-agent".into()))));
    assert!(
        installed
            .contains("starts at login (launchd-agent; remove: abstractgateway service uninstall)"),
        "{installed}"
    );
}

// ---- route_unavailable (REVIEW-1 contract, core-video 6c64508 payloads) --

const FLUX_REASON: &str =
    "MLX-Gen image generation needs MLX, and MLX runs only on Apple Silicon Macs";

#[test]
fn routes_flag_a_configured_route_this_host_cannot_run() {
    let d = RoutesData::from_value(&fixture("route_unavailable_rows.json"));
    let image = d.rows.iter().find(|r| r.key == "output.image").unwrap();
    let u = image.route_unavailable.as_ref().expect("parsed");
    assert_eq!(
        (u.provider.as_str(), u.model.as_str()),
        ("mlx-gen", "AbstractFramework/flux.2-klein-4b-8bit")
    );
    assert!(u.reason.starts_with(FLUX_REASON));
    assert_eq!(image.state_label(), "cannot run here");
    let text = d.rows.iter().find(|r| r.key == "input.text").unwrap();
    assert!(
        text.route_unavailable.is_none(),
        "absent = the old behaviour"
    );
    assert_eq!(text.state_label(), "configured");

    let mut h = harness(Size::new(200, 50));
    h.connect();
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_ROUTES);
    h.turns(2);
    h.store.routes.set(Loadable::Ready(d.clone()));
    h.store
        .availability
        .set(Loadable::Ready(AvailabilityData::from_value(&json!({
            "routes": [],
            "recommended": {"recommended": [
                {"route": "output.video", "provider": "mlx-gen",
                 "artifact": "AbstractFramework/wan2.2-ti2v-5b-diffusers-8bit", "status": "absent"}
            ], "total": 1, "installed": 0, "absent": 1, "unknown": 0, "gaps": []}
        }))));
    let idx = d.rows.iter().position(|r| r.key == "output.image").unwrap();
    h.ui.route_sel.set(idx);
    let s = h.turns(3);
    assert!(s.contains("cannot run here"), "state column:\n{s}");
    assert!(
        s.contains(&format!(
            "configured but cannot run on this computer: {FLUX_REASON}"
        )),
        "selected-row warning:\n{s}"
    );
    h.type_text("p");
    let s = h.turns(3);
    assert!(
        s.contains("Configured but cannot run on this computer"),
        "plan section:\n{s}"
    );
    assert!(
        s.contains("output.image: mlx-gen AbstractFramework/flux.2-klein-4b-8bit"),
        "{s}"
    );
    // The fix named is the one that exists: `a` → Replace mine too for a
    // recommended key (review 2 minor c).
    assert!(
        s.contains("fix: a, then Replace mine too, swaps in what runs here"),
        "{s}"
    );
    // The plan lists the video row under its own title.
    assert!(s.contains("Video  Not downloaded"), "video plan row:\n{s}");
}

#[test]
fn apply_report_names_broken_routes_kept_and_cleared() {
    use abstractgateway_console::worker::applied_recommended_summary;
    // The core console's words (parity/engines 843ca55 writes.rs).
    let kept = applied_recommended_summary(&fixture("route_unavailable_apply_noforce.json"));
    assert!(
        kept.starts_with("1 changed · 1 already · 2 unavailable · "),
        "totals first: {kept}"
    );
    assert!(
        kept.contains(&format!(
            "output.image: nothing recommended runs on this computer — {FLUX_REASON}"
        )) && kept.contains(&format!(
            "; left as mlx-gen/AbstractFramework/flux.2-klein-4b-8bit — yours cannot run on this computer: {FLUX_REASON}"
        )),
        "{kept}"
    );
    assert!(
        !kept.contains("left unset: output.image"),
        "a configured route is not unset: {kept}"
    );
    assert!(
        kept.contains("1 not available on this host, left unset: output.video"),
        "{kept}"
    );

    let forced = applied_recommended_summary(&fixture("route_unavailable_apply_force.json"));
    assert!(
        forced.starts_with("2 changed · 1 cleared · 1 already · 1 unavailable · "),
        "{forced}"
    );
    assert!(
        forced.contains(&format!(
            "output.image: removed mlx-gen/AbstractFramework/flux.2-klein-4b-8bit — yours cannot run on this computer: {FLUX_REASON}"
        )) && forced.contains("; nothing recommended runs here either — MLX-Gen image generation needs MLX"),
        "{forced}"
    );
    assert!(!forced.contains("left as mlx-gen"), "{forced}");
}

/// The web's reading of an apply report (review 2 minor d): broken or
/// unavailable rows need attention (never a plain success), and the
/// forced second pass is offered under the web's label.
#[test]
fn apply_report_needs_attention_and_offers_the_web_second_pass() {
    use abstractgateway_console::worker::applied_recommended_followup;
    let noforce = fixture("route_unavailable_apply_noforce.json");
    let (attention, followup) = applied_recommended_followup(&noforce, false);
    let attention = attention.expect("broken + unavailable rows need attention");
    assert!(
        attention.contains("1 configured route cannot run on this computer"),
        "{attention}"
    );
    assert!(
        attention.contains("2 routes have no recommendation"),
        "{attention}"
    );
    assert_eq!(
        followup,
        Some("Clear what cannot run here"),
        "nothing kept, one broken"
    );
    let (_, again) = applied_recommended_followup(&noforce, true);
    assert_eq!(again, None, "no second pass after a forced one");
    let kept = serde_json::json!({"applied_recommended": {"routes": [
        {"key": "input.text", "action": "kept", "changed": false}
    ]}});
    assert_eq!(
        applied_recommended_followup(&kept, false),
        (None, Some("Replace mine too"))
    );
    let clean = serde_json::json!({"applied_recommended": {"routes": [
        {"key": "input.text", "action": "apply", "changed": true}
    ]}});
    assert_eq!(applied_recommended_followup(&clean, false), (None, None));
}

/// The broken-route copy names the fix that exists (review 2 minor c):
/// `a` only for a recommended key and an admin; task rows need an edit.
#[test]
fn broken_route_fix_says_the_truth() {
    use abstractgateway_console::ui::routes::broken_route_fix;
    assert!(broken_route_fix("output.image", true).contains("Replace mine too"));
    assert!(broken_route_fix("output.image.text_to_image", true).contains("Enter edits it"));
    let na = broken_route_fix("output.image", false);
    assert!(!na.contains("a,") && na.contains("an admin"), "{na}");
}

/// A derived row (output.text ← input.text) carries its source's
/// route_unavailable flag: the plan's broken list names the source once,
/// and a task row's fix is an edit, not `a` (review 2 minor c).
#[test]
fn plan_broken_list_skips_derived_rows_and_names_task_row_fixes() {
    let why = "MLX needs Apple silicon";
    let flag = json!({"provider": "mlx", "model": "m", "reason": why});
    let d = RoutesData::from_value(&json!({"ok": true, "writable": true, "routes": [
        {"key": "input.text", "kind": "input", "modality": "text", "provider": "mlx",
         "model": "m", "configured": true, "route_unavailable": flag},
        {"key": "output.text", "kind": "output", "modality": "text", "provider": "mlx",
         "model": "m", "configured": true, "derived_from": "input.text", "route_unavailable": flag},
        {"key": "output.image.text_to_image", "kind": "output", "modality": "image",
         "provider": "mlx", "model": "m", "configured": true, "broad_key": "output.image",
         "route_unavailable": flag}
    ]}));
    let mut h = harness(Size::new(200, 50));
    h.connect();
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_ROUTES);
    h.turns(2);
    h.store.routes.set(Loadable::Ready(d));
    h.store
        .availability
        .set(Loadable::Ready(AvailabilityData::from_value(&json!({
            "routes": [], "recommended": {"recommended": [], "total": 0, "installed": 0,
                                           "absent": 0, "unknown": 0, "gaps": []}
        }))));
    h.turns(2);
    h.type_text("p");
    let s = h.turns(3);
    assert!(s.contains("input.text: mlx m"), "{s}");
    assert!(
        !s.contains("output.text: mlx m"),
        "the derived row is not listed twice:\n{s}"
    );
    assert!(
        s.contains("fix: not part of the recommendation: Enter edits it"),
        "a task row's fix is an edit:\n{s}"
    );
}
