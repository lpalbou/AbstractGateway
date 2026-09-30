//! Screen captures of every screen the settings / email / sign-in design
//! changed (DESIGN 2026-09-30), in every state: text dumps plus SVG renders
//! at 120x40 and 60x30, written only when `STATE_TOGGLE_SHOTS_DIR` is set
//! (`cargo test --test state_toggle_shots -- --ignored`). Hermetic: no
//! gateway, no network — the store is seeded with shape-faithful fixtures
//! and the worker is a channel nobody drains.

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::{mpsc, Arc};

use abstracttui::app::Driver;
use abstracttui::prelude::*;
use abstracttui::testing::CaptureTerm;
use serde_json::{json, Value};

use abstractcore_console::screens::{ScreensCtx, ScreensOptions};
use abstractcore_console::{ConsoleTransport, TransportError};

use abstractgateway_console::store::email::{
    Discovery, EmailCaps, MyEmail, MyNotifications, RecoveryAnswer, RecoveryStep,
};
use abstractgateway_console::store::{users_from_payload, ConnPhase, Identity, Loadable, Store};
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
    _rx: mpsc::Receiver<Cmd>,
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
        _rx: rx,
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

const SIZES: [(i32, i32); 2] = [(120, 40), (60, 30)];

fn users_payload() -> Value {
    json!({"users": [
        {"user_id": "admin", "tenant_id": "default", "email": "admin@example.test", "roles": ["admin", "user"],
         "enabled": true, "runtime_id": "default", "created_at": "2026-05-30T05:17:35Z",
         "email_account": {"configured": true, "address": "admin@example.test", "state": "connected",
                           "admin_enabled": true, "agent_tools_available": true}},
        {"user_id": "alice", "tenant_id": "default", "email": "alice@example.test", "roles": ["user"],
         "enabled": true, "runtime_id": "alice", "created_at": "2026-07-01T00:00:00Z",
         "email_account": {"configured": false, "address": "", "state": "not connected",
                           "admin_enabled": true, "agent_tools_available": false}},
        {"user_id": "bob", "tenant_id": "default", "email": "", "roles": ["user"],
         "enabled": false, "runtime_id": "bob", "created_at": "2026-07-02T00:00:00Z",
         "email_account": {"configured": true, "address": "bob@example.test",
                           "state": "turned off by an administrator",
                           "admin_enabled": false, "agent_tools_available": false,
                           "capabilities": {"email": {"value": false, "source": "user"},
                                            "email_agent_tools": {"value": true, "source": "gateway"}}}}
    ]})
}

fn my_email_connected() -> Value {
    json!({
        "schema": "email_settings_v1", "configured": true, "enabled": true,
        "admin_enabled": true, "effective_enabled": true, "email_available": true,
        "address": "admin@example.test", "username": "admin@example.test", "auth_kind": "password",
        "registered_address": "admin@example.test",
        "imap": {"host": "imap.fastmail.com", "port": 993, "security": "ssl", "folder": "INBOX"},
        "smtp": {"host": "smtp.fastmail.com", "port": 465, "security": "ssl"},
        "secret_storage": "os-keychain",
        "policy": {"mode": "allowlist", "entries": ["admin@example.test", "example.org"], "default": false},
        "limits": {"per_hour": 20, "per_day": 100, "used_last_hour": 0, "used_last_day": 2},
        "status": {"last_test": "2026-09-30T18:00:00+00:00", "last_ok": "2026-09-30T18:00:00+00:00",
                   "legs": {"imap": {"ok": true}, "smtp": {"ok": true}}},
        "watcher": {"state": "watching", "last_poll": "2026-09-30T18:01:00+00:00"},
        "agent_tools": {"on": false, "enabled": false, "available": true, "active": false,
                        "unavailable_reason": null},
        "notifications": {"job_failed": true, "approval_needed": true},
        "oauth_providers": [{"id": "google", "available": true, "reason": null},
                            {"id": "microsoft", "available": true, "reason": null}]
    })
}

fn my_email_not_connected() -> Value {
    json!({
        "schema": "email_settings_v1", "configured": false, "enabled": true,
        "admin_enabled": true, "effective_enabled": false, "email_available": true,
        "address": "", "registered_address": "alice@fastmail.test",
        "policy": {"mode": "allowlist", "entries": [], "default": true},
        "limits": {"per_hour": 20, "per_day": 100, "used_last_hour": 0, "used_last_day": 0},
        "status": {}, "watcher": {"state": "idle"},
        "agent_tools": {"on": false, "enabled": false, "available": false, "active": false,
                        "unavailable_reason": "Connect a mailbox first."},
        "notifications": {"job_failed": true, "approval_needed": true},
        "notifications_unavailable_reason": "Connect a mailbox first.",
        "oauth_providers": [{"id": "google", "available": true, "reason": null},
                            {"id": "microsoft", "available": false,
                             "reason": "No Microsoft sign-in client on this gateway: add one under Advanced, or ask your admin."}]
    })
}

fn my_email_mailboxes_off() -> Value {
    let mut v = my_email_not_connected();
    v["admin_enabled"] = json!(false);
    v["email_available"] = json!(false);
    v["agent_tools"]["unavailable_reason"] = json!("Your admin turned mailboxes off.");
    v["notifications_unavailable_reason"] = json!("Your admin turned mailboxes off.");
    v
}

fn caps() -> EmailCaps {
    EmailCaps::from_value(&json!({"capabilities": [
        {"id": "email", "default": true}, {"id": "email_agent_tools", "default": true},
        {"id": "email_recovery", "default": true}
    ]}))
}

fn users_screen(size: (i32, i32)) -> Harness {
    let mut h = harness(Size::new(size.0, size.1));
    h.admin();
    h.ui.screen.set(ui::SCREEN_USERS);
    h.turns(2);
    h.store
        .users
        .set(Loadable::Ready(users_from_payload(&users_payload())));
    h.store.op.email_caps.set(Loadable::Ready(caps()));
    h.turns(3);
    h
}

fn email_page(size: (i32, i32), v: &Value) -> Harness {
    let mut h = users_screen(size);
    h.key(b"@");
    h.store
        .op
        .my_email
        .set(Loadable::Ready(MyEmail::from_value(v)));
    h.store
        .op
        .my_notifications
        .set(Loadable::Ready(MyNotifications::from_value(
            &json!({"events": [], "channels": {"email": {"available": false}}, "outbox": {}}),
        )));
    h.turns(4);
    h
}

#[test]
#[ignore = "writes screen captures; run with STATE_TOGGLE_SHOTS_DIR set"]
fn capture_users_screen() {
    for size in SIZES {
        let mut h = users_screen(size);
        h.shoot("users");
        h.click_text("Advanced ▸");
        h.shoot("users-advanced");
        // Space on alice: the Active switch asks before deactivating.
        let mut h = users_screen(size);
        h.key(b"\x1b[B");
        h.key(b" ");
        h.shoot("users-deactivate-confirm");
        // Own row: unavailable, the reason in the status line.
        let mut h = users_screen(size);
        h.key(b" ");
        h.shoot("users-own-row-unavailable");
    }
}

#[test]
#[ignore = "writes screen captures; run with STATE_TOGGLE_SHOTS_DIR set"]
fn capture_create_user_form() {
    for size in SIZES {
        let mut h = users_screen(size);
        h.key(b"a");
        h.shoot("create-user");
        h.click_text("Advanced ▸  runtime");
        h.shoot("create-user-advanced");
    }
}

#[test]
#[ignore = "writes screen captures; run with STATE_TOGGLE_SHOTS_DIR set"]
fn capture_email_panel() {
    for size in SIZES {
        // Connected: status line + Test + Disconnect, switches, Advanced.
        let mut h = email_page(size, &my_email_connected());
        h.shoot("email-connected");
        h.click_text("Disconnect");
        h.shoot("email-connected-disconnect-confirm");
        let mut h = email_page(size, &my_email_connected());
        h.wheel_down(20);
        h.click_text("Advanced ▸");
        h.wheel_down(20);
        h.shoot("email-connected-advanced");
        // Not connected: the Google tab (default), Microsoft unavailable.
        let mut h = email_page(size, &my_email_not_connected());
        h.shoot("email-not-connected");
        h.click_text("Microsoft");
        h.shoot("email-not-connected-microsoft-unavailable");
        // Other: address + password, discovery found / not found.
        let mut h = email_page(size, &my_email_not_connected());
        h.click_text("Other");
        h.shoot("email-discovery-looking");
        h.store.op.email_discovery.set(Some((
            "alice@fastmail.test".into(),
            Loadable::Ready(Discovery::from_value(&json!({
                "address": "alice@fastmail.test", "domain": "fastmail.test", "found": true,
                "source": "known",
                "imap": {"host": "imap.fastmail.com", "port": 993, "security": "ssl"},
                "smtp": {"host": "smtp.fastmail.com", "port": 465, "security": "ssl"},
                "username": "alice@fastmail.test"
            }))),
        )));
        h.turns(3);
        h.shoot("email-discovery-found");
        h.store.op.email_discovery.set(Some((
            "alice@fastmail.test".into(),
            Loadable::Ready(Discovery::from_value(&json!({
                "address": "alice@fastmail.test", "domain": "fastmail.test", "found": false
            }))),
        )));
        h.turns(3);
        h.shoot("email-discovery-failed");
        // Mailboxes off by the admin: the reasons on every switch.
        let mut h = email_page(size, &my_email_mailboxes_off());
        h.shoot("email-unavailable");
    }
}

fn sign_in(size: (i32, i32)) -> Harness {
    let mut h = harness(Size::new(size.0, size.1));
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_CONNECTION);
    h.store.conn.set(ConnPhase::Unauthorized(
        "Missing or invalid gateway token".into(),
    ));
    h.turns(3);
    h
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[test]
#[ignore = "writes screen captures; run with STATE_TOGGLE_SHOTS_DIR set"]
fn capture_sign_in() {
    for size in SIZES {
        let mut h = sign_in(size);
        h.store.op.recovery.update(|r| r.available = Some(false));
        h.turns(2);
        h.shoot("sign-in-not-offered");
        h.store.op.recovery.update(|r| r.available = Some(true));
        h.turns(2);
        h.shoot("sign-in-idle");
        h.store
            .op
            .recovery
            .update(|r| r.step = RecoveryStep::Sending);
        h.turns(2);
        h.shoot("sign-in-sending");
        let sent = RecoveryAnswer::from_value(
            "admin",
            &json!({"sent": true, "to": "a•••@•••", "expires_in_s": 600,
                    "message": "A sign-in code is on its way to a•••@•••. It expires in 10 minutes."}),
            now_ms(),
        );
        h.store
            .op
            .recovery
            .update(|r| r.step = RecoveryStep::Code(sent.clone()));
        h.turns(3);
        h.shoot("sign-in-code-step");
        h.store.op.recovery.update(|r| {
            r.error = Some("That code is wrong, expired or already used. Send a new one.".into())
        });
        h.turns(2);
        h.shoot("sign-in-error");
        h.store.op.recovery.update(|r| {
            r.error = None;
            r.step = RecoveryStep::Code(RecoveryAnswer::from_value(
                "bob",
                &json!({"sent": false, "reason_code": "no_email_address",
                        "message": "This account has no email address, so a code can't be sent. Ask your gateway admin for a token."}),
                now_ms(),
            ));
        });
        h.turns(2);
        h.shoot("sign-in-no-email-address");
        // The code worked: signed in with a new token, shown once.
        h.store.op.recovery.update(|r| {
            r.step = RecoveryStep::Idle;
            r.new_token = Some("agw_example_new_token_0123456789".into());
            r.signed_in_user = "admin".into();
        });
        h.store.notice.set(Some(
            abstractgateway_console::store::email::SIGNED_IN_NEW_TOKEN.into(),
        ));
        h.admin();
        h.ui.screen.set(ui::SCREEN_CONNECTION);
        h.turns(3);
        h.shoot("sign-in-signed-in-new-token");
    }
}

#[test]
#[ignore = "writes screen captures; run with STATE_TOGGLE_SHOTS_DIR set"]
fn capture_runtimes() {
    for size in SIZES {
        let mut h = harness(Size::new(size.0, size.1));
        h.admin();
        h.ui.screen.set(4);
        h.turns(2);
        h.store.runtimes.set(Loadable::Ready(vec![]));
        h.turns(3);
        h.shoot("runtimes");
    }
}
#[test]
#[ignore = "writes screen captures; run with STATE_TOGGLE_SHOTS_DIR set"]
fn capture_host_panel() {
    use abstractgateway_console::store::operator::{HostRunner, StartAtLogin};
    for size in SIZES {
        let mut h = harness(Size::new(size.0, size.1));
        h.admin();
        h.ui.screen.set(ui::SCREEN_WELCOME);
        h.turns(2);
        h.store.op.runner.set(Loadable::Ready(HostRunner::from_value(&json!({
            "paused": true, "paused_at": "2026-09-30T10:00:00+00:00", "paused_by": "default/admin",
            "inflight_ticks": 0, "runner_in_process": true,
            "capabilities": {"restart": true, "shutdown": true, "reason": null}
        }))));
        h.store
            .op
            .start_at_login
            .set(Loadable::Ready(StartAtLogin::from_value(&json!({
                "schema": "gateway_start_at_login_v1", "enabled": false, "state": "off",
                "mechanism": "launchd", "mechanism_label": "a login item", "can_change": true,
                "summary": "Off — nothing starts the gateway at login"
            }))));
        h.turns(2);
        h.key(b"\x1bOR"); // F3
        h.store.op.runner.set(Loadable::Ready(HostRunner::from_value(&json!({
            "paused": true, "paused_at": "2026-09-30T10:00:00+00:00", "paused_by": "default/admin",
            "inflight_ticks": 0, "runner_in_process": true,
            "capabilities": {"restart": true, "shutdown": true, "reason": null}
        }))));
        h.store
            .op
            .start_at_login
            .set(Loadable::Ready(StartAtLogin::from_value(&json!({
                "schema": "gateway_start_at_login_v1", "enabled": false, "state": "off",
                "mechanism": "launchd", "mechanism_label": "a login item", "can_change": true,
                "summary": "Off — nothing starts the gateway at login"
            }))));
        h.shoot("host-panel");
    }
}

#[test]
#[ignore = "writes screen captures; run with STATE_TOGGLE_SHOTS_DIR set"]
fn capture_network_reverse_proxy() {
    for size in SIZES {
        let mut h = harness(Size::new(size.0, size.1));
        h.admin();
        h.ui.screen.set(ui::SCREEN_NETWORK);
        h.turns(2);
        let v = json!({
            "schema": "gateway_network_v1", "writable": true,
            "configured": {"mode": "localhost", "label": "Localhost only", "port": 8080,
                "bind_host": "127.0.0.1", "source": "stored", "port_source": "stored"},
            "effective": {"mode": "localhost", "label": "Localhost only", "bind_host": "127.0.0.1",
                "port": 8080, "overridden_by_cli": false, "host_source": "setting",
                "port_source": "setting", "running": true},
            "restart_required": false,
            "restart": {"available": true, "applies": true, "needed": false},
            "auth": {"user_auth": true, "token_auth": false, "ok_for_mode": true},
            "modes": [
                {"id": "localhost", "label": "Localhost only", "selected": true, "allowed": true},
                {"id": "lan", "label": "Local network", "selected": false, "allowed": true},
                {"id": "internet", "label": "Internet", "selected": false, "allowed": true, "requires_acknowledgement": true}
            ],
            "addresses": [{"kind": "loopback", "url": "http://127.0.0.1:8080", "host": "127.0.0.1",
                "port": 8080, "reachable": true, "note": "this machine only"}],
            "warnings": [],
            "reverse_proxy": {
                "allowed_origins": {"value": [], "source": "default", "overridden_by_env": false, "applies": "live"},
                "trust_proxy": {"value": true, "source": "setting", "overridden_by_env": false,
                    "effective": true, "applies": "live"}
            }
        });
        h.store.network.set(Loadable::Ready(
            abstractgateway_console::store::NetworkData::from_value(&v),
        ));
        h.turns(3);
        h.shoot("network-reverse-proxy");
    }
}
