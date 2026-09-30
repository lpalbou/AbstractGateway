//! Sign-in by email end to end through the REAL worker: the Connection
//! screen, `abstractgateway_console::worker::spawn`, and a local fake
//! gateway (std TcpListener on a free loopback port — never a real
//! gateway, never :8080). Pins what a seeded snapshot cannot: after
//! "Use code" the worker redeems, the console signs in with the new
//! token, and the status line says the old token no longer works.

use std::cell::RefCell;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::rc::Rc;
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant};

use abstracttui::app::Driver;
use abstracttui::prelude::*;
use abstracttui::testing::CaptureTerm;
use serde_json::{json, Value};

use abstractcore_console::screens::{ScreensCtx, ScreensOptions};
use abstractcore_console::{ConsoleTransport, TransportError};

use abstractgateway_console::store::email::SIGNED_IN_NEW_TOKEN;
use abstractgateway_console::store::{ConnPhase, Identity, Store};
use abstractgateway_console::ui::{self, Ctx, UiState};
use abstractgateway_console::worker::Cmd;

/// What the fake gateway saw: (method, path, Authorization, JSON body).
type Seen = Arc<Mutex<Vec<(String, String, Option<String>, Option<Value>)>>>;

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
        let Ok(dir) = std::env::var("STATE_TOGGLE_SHOTS_DIR") else {
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

/// The fake gateway: the recovery routes, then /ping + /me for the token
/// the redeem returned; everything else 404. Records every request.
fn fake_gateway() -> (String, Seen) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind a free port");
    let url = format!("http://{}", listener.local_addr().unwrap());
    let seen = Arc::new(Mutex::new(Vec::new()));
    let seen_srv = seen.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(stream) = stream else { continue };
            let seen = seen_srv.clone();
            std::thread::spawn(move || serve(stream, &seen));
        }
    });
    (url, seen)
}

fn serve(mut stream: TcpStream, seen: &Seen) {
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let mut request_line = String::new();
    if reader.read_line(&mut request_line).is_err() {
        return;
    }
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let path = parts.next().unwrap_or("").to_string();
    let (mut len, mut auth) = (0usize, None);
    loop {
        let mut h = String::new();
        if reader.read_line(&mut h).is_err() || h == "\r\n" || h.is_empty() {
            break;
        }
        let (k, v) = h.split_once(':').unwrap_or((&h, ""));
        match k.to_ascii_lowercase().as_str() {
            "content-length" => len = v.trim().parse().unwrap_or(0),
            "authorization" => auth = Some(v.trim().to_string()),
            _ => {}
        }
    }
    let mut buf = vec![0u8; len];
    let _ = reader.read_exact(&mut buf);
    let body = serde_json::from_slice::<Value>(&buf).ok();
    seen.lock()
        .unwrap()
        .push((method.clone(), path.clone(), auth.clone(), body));
    let p = path.trim_start_matches("/api/gateway");
    let good = auth.as_deref() == Some("Bearer tok-NEW");
    let (status, text) = match (method.as_str(), p) {
        ("GET", "/session/recovery") => (
            200,
            json!({"available": true, "purposes": ["sign_in", "reset_token"]}),
        ),
        ("POST", "/session/recovery/request") => (
            200,
            json!({"ok": true, "sent": true, "to": "a•••@•••", "expires_in_s": 600,
            "message": "A sign-in code is on its way to a•••@•••. It expires in 10 minutes."}),
        ),
        ("POST", "/session/recovery/redeem") => (
            200,
            json!({"ok": true, "principal": {"user_id": "admin"},
            "token": "tok-NEW", "token_note": "Your new gateway token. It is shown once; your old token no longer works."}),
        ),
        ("GET", "/ping") if good => (200, json!({"ok": true})),
        ("GET", "/me") if good => (
            200,
            json!({"ok": true,
            "principal": {"user_id": "admin", "tenant_id": "default", "roles": ["admin", "user"], "admin": true},
            "auth": {"mode": "users"}, "routing": {"mode": "per-principal"}}),
        ),
        ("GET", "/host/first-run") if good => (
            200,
            json!({"completed": true, "outcome": "finished",
            "completed_at": "2026-09-30T00:00:00Z", "completed_by": "default/admin"}),
        ),
        (_, "/ping") | (_, "/me") => (401, json!({"detail": "Missing or invalid gateway token"})),
        _ => (404, json!({"detail": "not in this fake"})),
    };
    let text = text.to_string();
    let reason = match status {
        200 => "OK",
        401 => "Unauthorized",
        _ => "Not Found",
    };
    let resp = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{text}",
        text.len()
    );
    let _ = stream.write_all(resp.as_bytes());
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

#[test]
fn a_redeemed_code_signs_in_with_a_new_token_and_says_the_old_one_stopped_working() {
    let (url, seen) = fake_gateway();
    let mut h = harness(Size::new(120, 40));
    // The real worker, on the harness's command channel.
    let wake = abstracttui::reactive::wake_handle();
    let (store, ui_state) = (h.store, h.ui);
    let token_sink = |_u: String, _t: String| {};
    let done_sink = {
        let wake = wake.clone();
        move |fid: u64, out: Result<String, String>| {
            wake.post(move || ui_state.write_done.set(Some((fid, out.clone()))))
        }
    };
    let rx = h.rx.take().expect("rx");
    let _worker = abstractgateway_console::worker::spawn(
        store,
        wake,
        rx,
        h.tx.clone(),
        token_sink,
        done_sink,
    );
    h.ui.wizard.set(false);
    h.ui.conn_url.set(url.clone());
    h.ui.screen.set(ui::SCREEN_CONNECTION);
    h.store.conn.set(ConnPhase::Unauthorized(
        "Missing or invalid gateway token".into(),
    ));
    let s = h.until("the link", |_, s| {
        s.contains("Forgot your token? Email me a sign-in code")
    });
    h.click_text("Forgot your token?");
    let _ = s;
    h.until("the code step", |_, s| s.contains("Code from the email"));
    h.key(b"12345678\r");
    let s = h.until("the signed-in status line", |h, _| {
        h.store.notice.get_untracked().as_deref() == Some(SIGNED_IN_NEW_TOKEN)
    });
    assert!(
        h.store
            .conn
            .with_untracked(|c| matches!(c, ConnPhase::Connected(_))),
        "signed in with the new token"
    );
    assert_eq!(h.ui.conn_token.get_untracked(), "tok-NEW");
    assert!(s.contains("tok-NEW"), "the token is shown once:\n{s}");
    let log = seen.lock().unwrap().clone();
    let redeem = log
        .iter()
        .find(|(m, p, _, _)| m == "POST" && p.ends_with("/session/recovery/redeem"))
        .expect("redeem sent");
    assert_eq!(redeem.3.as_ref().unwrap()["code"], json!("12345678"));
    assert_eq!(redeem.3.as_ref().unwrap()["purpose"], json!("reset_token"));
    assert!(
        log.iter()
            .any(|(_, p, a, _)| p.ends_with("/me") && a.as_deref() == Some("Bearer tok-NEW")),
        "the probe used the new token: {log:?}"
    );
    let _ = Store::create; // keep the import honest
}
