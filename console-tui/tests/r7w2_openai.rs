//! OpenAI API page (R7.2, round 7): every screen state rendered from the
//! gateway's own payloads (`gateway_openai_api_v1` fixtures captured from a
//! scratch gateway), and every action asserted at the route it sends — the
//! same routes and bodies as the web page (console_ui.py `oai*`).
//!
//! No network: the worker is a recording channel; tests apply the JSON
//! lane's slots/writes exactly as the worker's posted closures would.

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::{mpsc, Arc};

use abstracttui::app::Driver;
use abstracttui::prelude::*;
use abstracttui::testing::CaptureTerm;
use serde_json::{json, Value};

use abstractcore_console::screens::{ScreensCtx, ScreensOptions};
use abstractcore_console::{ConsoleTransport, TransportError};

use abstractgateway_console::api::{ApiError, ApiErrorKind};
use abstractgateway_console::store::json::WriteState;
use abstractgateway_console::store::{ConnPhase, Identity, Loadable, Store};
use abstractgateway_console::ui::openai_api::{
    self, sha256_hex, KEY_CHANGE, KEY_CHECK, KEY_LOGS, KEY_NEW_KEY, KEY_PAGE, KEY_RESTART, MASK,
};
use abstractgateway_console::ui::{self, Ctx, UiState};
use abstractgateway_console::worker::json::JsonCmd;
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

const ADMIN_TOKEN: &str = "agw_test-admin-token-0001";

struct H {
    app: App,
    term: CaptureTerm,
    driver: Driver,
    store: Store,
    ui: UiState,
    rx: mpsc::Receiver<Cmd>,
}

fn harness(size: Size, token: &str) -> H {
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
    let out = slot.clone();
    let token = token.to_string();
    app.mount(move |cx| {
        let store = Store::create(cx);
        let ui_state = UiState::create(cx, "http://127.0.0.1:18781".to_string(), token.clone());
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
    fn key(&mut self, bytes: &[u8]) -> String {
        self.term.push_input(bytes);
        self.turns(3)
    }
    /// Wheel down over the middle of the screen (scrolls an overlay body).
    fn wheel_down(&mut self, n: usize) -> String {
        let size = self.term.screen().size();
        for _ in 0..n {
            let ev = format!("\x1b[<65;{};{}M", size.w / 2, size.h / 2);
            self.term.push_input(ev.as_bytes());
        }
        self.turns(3)
    }
    fn connect(&mut self, admin: bool) {
        let (uid, roles) = if admin {
            ("admin", json!(["admin", "user"]))
        } else {
            ("alice", json!(["user"]))
        };
        let id = Identity::from_me(&json!({
            "principal": {"user_id": uid, "tenant_id": "default", "roles": roles, "admin": admin},
            "auth": {"mode": "users"}, "routing": {"mode": "per-principal"}
        }))
        .unwrap();
        self.store.conn.set(ConnPhase::Connected(id));
        self.ui.wizard.set(false);
        self.turns(2);
    }
    fn open_page(&mut self) -> String {
        self.ui.screen.set(ui::SCREEN_OPENAI);
        self.turns(3)
    }
    fn cmds(&mut self) -> Vec<Cmd> {
        self.rx.try_iter().collect()
    }
    fn json_cmds(&mut self) -> Vec<JsonCmd> {
        self.cmds()
            .into_iter()
            .filter_map(|c| match c {
                Cmd::Json(j) => Some(j),
                _ => None,
            })
            .collect()
    }
    fn seed(&mut self, page: Value, logs: Value) -> String {
        self.store.json.set(KEY_PAGE, Loadable::Ready(page));
        self.store.json.set(KEY_LOGS, Loadable::Ready(logs));
        self.turns(3)
    }
}

/// The admin page as a scratch gateway answered it, fingerprint = the
/// test token's.
fn admin_page() -> Value {
    json!({
        "schema": "gateway_openai_api_v1", "role": "admin", "writable": true,
        "enabled": true, "running": true, "base_url": "http://127.0.0.1:18781/v1",
        "key": {"own_token": true, "user_id": "admin", "named_keys": true, "fingerprint": &sha256_hex(ADMIN_TOKEN.as_bytes())[..12], "allowed": true},
        "docs": {"openai_api": "https://github.com/lpalbou/abstractgateway/blob/main/docs/openai-api.md",
                 "abstractcore": "https://github.com/lpalbou/AbstractCore/blob/main/docs/server.md"},
        "support": {"tested": ["GET /v1/models"], "served": ["POST /v1/responses"], "not_yet": ["n > 1"]},
        "example_model": "lmstudio/fake-tool-model", "access": "token", "reach": "machine",
        "reach_options": [
            {"id": "machine", "label": "This machine only", "selected": true, "available": true},
            {"id": "network", "label": "Devices on my network", "selected": false, "available": true},
            {"id": "tailnet", "label": "Tailnet", "selected": false, "available": true, "shown": true},
            {"id": "anywhere", "label": "Anywhere", "selected": false, "available": false,
             "reason": "Anywhere needs Internet on the Network page first (it asks you to confirm the risks)."}
        ],
        "open_account": "guest",
        "open_account_options": [
            {"id": "guest", "label": "Guest (models only)", "available": true, "selected": true},
            {"id": "admin", "label": "admin", "available": false, "selected": false,
             "reason": "Requests without a key never run as an admin: choose Guest or a user account."},
            {"id": "alice", "label": "alice", "available": true, "selected": false}
        ],
        "warnings": [], "listener": {"mode": "localhost", "label": "Localhost only"},
        "tailscale": {"dns_name": "scratch-mac.tail1234.ts.net", "ips": ["100.101.102.103"]},
        "open_requests": 0
    })
}

fn logs(scope: &str) -> Value {
    json!({"schema": "gateway_openai_api_v1", "scope": scope, "rows": [
        {"request_id": "rid-structured", "ts": "2026-10-04T02:24:03+00:00", "client": "alice",
         "user_id": "alice", "ip": "127.0.0.1", "method": "POST", "path": "/v1/chat/completions",
         "model": "lmstudio/fake-tool-model", "prompt_tokens": 12, "completion_tokens": 4,
         "duration_ms": 10, "status": 200, "run_id": "run-1", "observer_path": "/observer/runs/run-1", "recorded": true},
        {"request_id": "rid-refused", "ts": "2026-10-04T02:24:04+00:00", "client": "bob",
         "user_id": "bob", "ip": "127.0.0.1", "method": "GET", "path": "/v1/models",
         "model": null, "prompt_tokens": null, "completion_tokens": null,
         "duration_ms": 57, "status": 403, "run_id": null, "observer_path": null, "recorded": true}
    ]})
}

fn detail() -> Value {
    json!({"schema": "gateway_openai_api_v1", "row": {
        "request_id": "rid-structured", "client": "alice", "ip": "127.0.0.1", "method": "POST",
        "path": "/v1/chat/completions", "user_agent": "OpenAI/Python 1.109.1",
        "observer_path": "/observer/runs/run-1",
        "request": {"body": {"model": "lmstudio/fake-tool-model", "messages": [{"role": "user", "content": "Say hello"}]}, "bytes": 88},
        "response": {"body": {"choices": [{"message": {"content": "Hello there."}}]}, "bytes": 120}
    }})
}

fn sends(cmds: &[JsonCmd]) -> Vec<(String, String, Value)> {
    cmds.iter()
        .filter_map(|c| match c {
            JsonCmd::Send {
                method, path, body, ..
            } => Some((method.clone(), path.clone(), body.clone())),
            _ => None,
        })
        .collect()
}

#[test]
fn first_look_reads_the_page_and_the_log() {
    let mut h = harness(Size::new(120, 40), ADMIN_TOKEN);
    h.connect(true);
    let s = h.open_page();
    let gets: Vec<String> = h
        .json_cmds()
        .into_iter()
        .filter_map(|c| match c {
            JsonCmd::Get { path, .. } => Some(path),
            _ => None,
        })
        .collect();
    assert!(gets.contains(&"/openai-api".to_string()), "{gets:?}");
    assert!(
        gets.contains(&"/openai-api/logs?limit=25".to_string()),
        "{gets:?}"
    );
    assert!(s.contains("Reading the OpenAI API settings..."), "{s}");
    assert!(s.contains("Reading the log..."), "{s}");
}

#[test]
fn admin_overview_has_every_card_and_masks_the_key() {
    for size in [Size::new(80, 24), Size::new(120, 40)] {
        let mut h = harness(size, ADMIN_TOKEN);
        h.connect(true);
        h.open_page();
        let s = h.seed(admin_page(), logs("all"));
        // R15: the Status card — its pill, the address with Copy, the
        // Endpoint toggle beside Restart / Check setup.
        assert!(s.contains("Status") && s.contains("● Running"), "{s}");
        assert!(s.contains("Base URL   http://127.0.0.1:18781/v1"), "{s}");
        assert!(s.contains("━● Endpoint"), "{s}");
        assert!(s.contains("Recent requests"), "{s}");
        assert!(s.contains("Every account's requests, newest first."), "{s}");
        assert!(!s.contains(ADMIN_TOKEN), "key masked:\n{s}");
    }
    // The tall terminal shows Connect + Access without scrolling.
    let mut h = harness(Size::new(120, 60), ADMIN_TOKEN);
    h.connect(true);
    h.open_page();
    let s = h.seed(admin_page(), logs("all"));
    for needle in [
        "Connect your app",
        "Paste these two values into any OpenAI SDK or app.",
        "API keys",
        openai_api::KEYS_NOTE,
        "Access",
        // R15: Authentication = two segments, the chosen one's sentence under.
        "Protected (API key)",
        "Open (no key)",
        "Apps send an API key made on this page.",
        // Who can connect = a picker showing the chosen option + its sentence
        // (the other options are in its popup: who_can_connect_… below).
        "This machine only",
        "Apps on this computer.",
    ] {
        assert!(s.contains(needle), "missing {needle:?}:\n{s}");
    }
}

#[test]
fn the_gateway_token_is_never_offered_as_the_key() {
    // Round 16: no Show / Copy of the console's token; `v` does nothing.
    let mut h = harness(Size::new(120, 60), ADMIN_TOKEN);
    h.connect(true);
    h.open_page();
    let s = h.seed(admin_page(), logs("all"));
    assert!(!s.contains(MASK) && !s.contains(ADMIN_TOKEN), "{s}");
    let s = h.key(b"v");
    assert!(!s.contains(ADMIN_TOKEN), "{s}");
}

#[test]
fn another_consoles_token_is_never_shown_either() {
    let mut h = harness(Size::new(120, 60), "agw_some-other-token");
    h.connect(true);
    h.open_page();
    let s = h.seed(admin_page(), logs("all"));
    assert!(!s.contains("agw_some-other-token"), "{s}");
    assert!(s.contains("API keys"), "{s}");
}

#[test]
fn user_view_has_no_access_card_and_own_requests() {
    let mut h = harness(Size::new(120, 60), "r7w2-alice-token-0001");
    h.connect(false);
    h.open_page();
    let mut page = admin_page();
    let o = page.as_object_mut().unwrap();
    o.insert("role".into(), json!("user"));
    o.insert("writable".into(), json!(false));
    for k in [
        "reach_options",
        "open_account_options",
        "warnings",
        "access",
        "reach",
    ] {
        o.remove(k);
    }
    o.insert(
        "key".into(),
        json!({"own_token": true, "user_id": "alice", "fingerprint": &sha256_hex(b"r7w2-alice-token-0001")[..12], "allowed": false}),
    );
    let s = h.seed(page, logs("own"));
    assert!(
        s.contains("Apps can connect now. Only an admin can start or stop it."),
        "{s}"
    );
    assert!(
        s.contains("The OpenAI API is off for your account. An admin can turn it on in Accounts."),
        "{s}"
    );
    assert!(s.contains("Your requests, newest first."), "{s}");
    assert!(!s.contains("Endpoint: answers apps"), "{s}");
    assert!(!s.contains("Who can connect"), "{s}");
    // Admin verbs answer with the reason and send nothing.
    h.cmds();
    let s = h.key(b"e");
    assert!(s.contains("Only an admin can start or stop it."), "{s}");
    assert!(sends(&h.json_cmds()).is_empty());
}

#[test]
fn endpoint_switch_posts_core_endpoint_and_says_the_web_sentence() {
    let mut h = harness(Size::new(120, 40), ADMIN_TOKEN);
    h.connect(true);
    h.open_page();
    h.seed(admin_page(), logs("all"));
    h.cmds();
    let s = h.key(b"e");
    let sent = sends(&h.json_cmds());
    assert_eq!(
        sent,
        vec![(
            "POST".into(),
            "/admin/core-endpoint".into(),
            json!({"enabled": false})
        )]
    );
    assert!(s.contains("Saving..."), "{s}");
    let mut after = admin_page();
    after["enabled"] = json!(false);
    after["running"] = json!(false);
    h.store
        .json
        .set_write(KEY_CHANGE, Some(WriteState::Done(after.clone())));
    h.store.json.set(KEY_PAGE, Loadable::Ready(after));
    let s = h.turns(3);
    assert!(s.contains("●─ Endpoint"), "{s}");
    assert!(s.contains("○ Stopped"), "{s}");
}

#[test]
fn authentication_toggle_and_refusal_sentence() {
    let mut h = harness(Size::new(120, 60), ADMIN_TOKEN);
    h.connect(true);
    h.open_page();
    h.seed(admin_page(), logs("all"));
    h.cmds();
    h.key(b"a");
    assert_eq!(
        sends(&h.json_cmds()),
        vec![(
            "POST".into(),
            "/admin/core-endpoint".into(),
            json!({"access": "open"})
        )]
    );
    let mut after = admin_page();
    after["access"] = json!("open");
    h.store
        .json
        .set_write(KEY_CHANGE, Some(WriteState::Done(after.clone())));
    h.store.json.set(KEY_PAGE, Loadable::Ready(after));
    let s = h.turns(3);
    assert!(s.contains("Saved: Open (no key). Applies now."), "{s}");
    assert!(
        s.contains("Requests without a key run as") && s.contains("Guest (models only)"),
        "{s}"
    );
    assert!(s.contains("Open mode: SDKs still ask for a key"), "{s}");
    // A refusal keeps the gateway's words.
    h.key(b"a");
    h.store.json.set_write(
        KEY_CHANGE,
        Some(WriteState::Failed(ApiError::new(
            ApiErrorKind::Forbidden,
            "Open mode needs the endpoint on this machine only.",
        ))),
    );
    let s = h.turns(3);
    assert!(
        s.contains("Not saved: Open mode needs the endpoint on this machine only."),
        "{s}"
    );
}

#[test]
fn restart_check_and_new_key_use_the_web_routes() {
    let mut h = harness(Size::new(120, 60), ADMIN_TOKEN);
    h.connect(true);
    h.open_page();
    h.seed(admin_page(), logs("all"));
    h.cmds();
    h.key(b"x");
    h.store.json.set_write(
        KEY_RESTART,
        Some(WriteState::Done(json!({"ended_requests": 2}))),
    );
    let s = h.turns(3);
    assert!(s.contains("Restarted: 2 open requests ended."), "{s}");
    h.key(b"h");
    h.store.json.set_write(
        KEY_CHECK,
        Some(WriteState::Done(json!({"checks": [
            {"id": "core", "ok": true, "text": "AbstractCore answers."},
            {"id": "models", "ok": false, "text": "No model is configured."}
        ]}))),
    );
    let s = h.turns(3);
    assert!(s.contains("OK   AbstractCore answers."), "{s}");
    assert!(s.contains("Fix  No model is configured."), "{s}");
    // New key: a Name form (the web's inline form); Enter makes the key.
    let s = h.key(b"n");
    assert!(
        s.contains("Make key") && s.contains("Name it after the app"),
        "{s}"
    );
    let mut sent: Vec<_> = sends(&h.json_cmds());
    h.key(b"laptop Cursor");
    h.key(b"\r");
    sent.extend(sends(&h.json_cmds()));
    assert!(
        sent.contains(&(
            "POST".into(),
            "/me/openai-keys".into(),
            json!({"label": "laptop Cursor"})
        )),
        "{sent:?}"
    );
    assert!(!sent.iter().any(|(_, p, _)| p == "/me/token/rotate"));
    assert!(sent
        .iter()
        .any(|(_, p, _)| p == "/admin/core-endpoint/restart"));
    assert!(sent
        .iter()
        .any(|(_, p, _)| p == "/admin/core-endpoint/check"));
    // The key is shown once; the console keeps its own token.
    h.store.json.set_write(
        KEY_NEW_KEY,
        Some(WriteState::Done(json!({"key": "sk-agw-fresh-named", "item": {"label": "laptop Cursor", "fingerprint": "aaaaaaaaaaaa"}}))),
    );
    let s = h.turns(3);
    assert!(s.contains("sk-agw-fresh-named"), "{s}");
    assert!(s.contains("Key “laptop Cursor” made."), "{s}");
    assert_eq!(h.ui.conn_token.get_untracked(), ADMIN_TOKEN);
    assert!(
        !h.cmds().iter().any(|c| matches!(c, Cmd::Connect { .. })),
        "a named key never replaces the console's sign-in"
    );
}

#[test]
fn who_can_connect_offers_the_gateways_options() {
    let mut h = harness(Size::new(120, 40), ADMIN_TOKEN);
    h.connect(true);
    h.open_page();
    h.seed(admin_page(), logs("all"));
    let s = h.key(b"w");
    for label in [
        "This machine only",
        "Devices on my network",
        "Tailnet",
        "Anywhere",
    ] {
        assert!(s.contains(label), "missing {label}:\n{s}");
    }
    // Down to "Devices on my network", Enter → POST reach.
    h.cmds();
    h.key(b"\x1b[B");
    h.key(b"\r");
    assert_eq!(
        sends(&h.json_cmds()),
        vec![(
            "POST".into(),
            "/admin/core-endpoint".into(),
            json!({"reach": "network"})
        )]
    );
}

#[test]
fn a_log_row_opens_to_the_recorded_request_and_response() {
    let mut h = harness(Size::new(120, 40), ADMIN_TOKEN);
    h.connect(true);
    h.open_page();
    let s = h.seed(admin_page(), logs("all"));
    assert!(s.contains("lmstudio/fake-tool-model"), "{s}");
    assert!(s.contains("12 in · 4 out"), "{s}");
    assert!(s.contains("403"), "{s}");
    h.cmds();
    // R15: Enter on the request table opens the record (a modal).
    let s = h.key(b"\r");
    let gets: Vec<String> = h
        .json_cmds()
        .into_iter()
        .filter_map(|c| match c {
            JsonCmd::Get { path, .. } => Some(path),
            _ => None,
        })
        .collect();
    assert!(
        gets.contains(&"/openai-api/logs/rid-structured".to_string()),
        "{gets:?}"
    );
    assert!(s.contains("Reading the request..."), "{s}");
    h.store
        .json
        .set("openai.log.rid-structured", Loadable::Ready(detail()));
    let s = h.turns(3);
    assert!(s.contains("Keys and tokens were removed"), "{s}");
    assert!(s.contains("Request · 88 bytes"), "{s}");
    // The JSON keeps its indentation (no-break spaces survive the wrap).
    assert!(
        s.contains("\u{a0}\u{a0}\u{a0}\u{a0}\u{a0}\u{a0}\"content\": \"Say hello\""),
        "{s}"
    );
    assert!(
        s.contains("Open in Observer  http://127.0.0.1:18781/observer/runs/run-1"),
        "{s}"
    );
    assert!(s.contains("Request 0"), "{s}");
    // The response is in the same record (it scrolls).
    let s = h.key(b"\x1b[F");
    let s = if s.contains("Hello there.") {
        s
    } else {
        h.wheel_down(10)
    };
    assert!(s.contains("\"content\": \"Hello there.\""), "{s}");
    h.term.push_input(&[0x1b]);
    h.turns(1);
    std::thread::sleep(std::time::Duration::from_millis(45));
    let s = h.turns(3);
    assert!(!s.contains("Copy response"), "Esc closes the record:\n{s}");
    // `f` opens it again.
    let s = h.key(b"f");
    assert!(
        s.contains("Request 0") && s.contains("Copy response"),
        "{s}"
    );
}

#[test]
fn empty_and_failed_states_use_the_web_sentences() {
    let mut h = harness(Size::new(120, 40), ADMIN_TOKEN);
    h.connect(true);
    h.open_page();
    h.store.json.set(
        KEY_PAGE,
        Loadable::Failed(ApiError::new(ApiErrorKind::Unreachable, "gateway down")),
    );
    h.store.json.set(
        KEY_LOGS,
        Loadable::Ready(json!({"rows": [], "scope": "all"})),
    );
    let s = h.turns(3);
    assert!(s.contains("Could not read the OpenAI API settings."), "{s}");
    assert!(s.contains("No requests yet."), "{s}");
}

#[test]
fn hints_hide_the_admin_verbs_for_a_user() {
    let user: Vec<&str> = openai_api::hints(true).iter().map(|(k, _)| *k).collect();
    let admin: Vec<&str> = openai_api::hints(false).iter().map(|(k, _)| *k).collect();
    for k in ["e", "x", "h", "a", "w", "u"] {
        assert!(!user.contains(&k) && admin.contains(&k), "{k}");
    }
}
