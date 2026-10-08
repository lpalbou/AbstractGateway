//! Admin guards (web parity, review 1 finding M4): every gateway-side verb
//! the gateway authorizes only for admins is refused BEFORE anything is
//! sent when the principal is known not to be an admin — with the reason,
//! never a 403 surprise — and the screens / footer hide what the web hides
//! (console.py renderAccount, applyEntityAdminGating, the per-row admin
//! checks). The live twin (tests/live_admin_guards.rs) proves the same
//! verb set earns a 403 from a real gateway for a non-admin token.
//!
//! The real interface through AbstractTUI's capture harness; the worker is
//! a channel the test drains.

mod accounts_fixture;

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::{mpsc, Arc};

use abstracttui::app::Driver;
use abstracttui::prelude::*;
use abstracttui::testing::CaptureTerm;
use serde_json::{json, Value};

use abstractcore_console::screens::{ScreensCtx, ScreensOptions};
use abstractcore_console::{ConsoleTransport, TransportError};

use abstractgateway_console::store::accounts::accounts_from_payload;
use abstractgateway_console::store::{
    entities_from_payload, host_state_from_payload, users_from_payload, workflows_from_payload,
    ConnPhase, Identity, Loadable, RoutesData, Store,
};
use abstractgateway_console::ui::{self, Ctx, UiState};
use abstractgateway_console::worker::Cmd;

/// The shared Models/Engines screens are not under test here.
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
    fn start_download(
        &self,
        _p: &str,
        _a: &str,
        _expected_bytes: Option<u64>,
    ) -> Result<Value, TransportError> {
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

fn harness() -> Harness {
    // Wide enough that the footer never truncates the verbs under test.
    let size = Size::new(320, 44);
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
    app.mount(move |cx| {
        let store = Store::create(cx);
        // DESIGN-v2 §2: the Accounts table reads `/admin/accounts`. These
        // suites seed the users registry and the entity roster; derive
        // the §6 accounts reply from them (tests/accounts_fixture).
        accounts_fixture::mirror(cx, store);
        let ui_state = UiState::create(cx, "http://127.0.0.1:8080".to_string(), String::new());
        *out.borrow_mut() = Some((store, ui_state));
        let transport: Arc<dyn ConsoleTransport> = Arc::new(NoTransport);
        let screens = ScreensCtx::new(
            cx,
            transport.clone(),
            overlays.clone(),
            // The production wiring: Models/Engines access follows the
            // connection (ui::screens_access_signal, used by lib.rs).
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
    fn drain(&mut self) -> Vec<Cmd> {
        let mut out = Vec::new();
        while let Ok(c) = self.rx.try_recv() {
            out.push(c);
        }
        out
    }
    fn connect(&mut self, admin: bool) {
        let id = Identity::from_me(&json!({
            "principal": {"user_id": if admin {"admin"} else {"ana"}, "tenant_id": "default",
                          "roles": if admin {json!(["admin"])} else {json!(["user"])}, "admin": admin},
            "auth": {"mode": "users"}, "routing": {"mode": "per-principal"}
        }))
        .unwrap();
        self.store.conn.set(ConnPhase::Connected(id));
        self.turns(1);
    }
    /// Browse mode on `screen` with its data loaded (a screen's keys live
    /// on its focusable content); the entry loads are drained.
    fn on(&mut self, screen: usize, admin: bool) -> String {
        self.connect(admin);
        self.ui.wizard.set(false);
        self.ui.screen.set(screen);
        self.turns(2);
        self.seed();
        let s = self.turns(3);
        self.drain();
        s
    }
    fn seed(&mut self) {
        self.store
            .routes
            .set(Loadable::Ready(RoutesData::from_value(&json!({
                "ok": true, "writable": true, "errors": [],
                "routes": [{"key": "input.text", "kind": "input", "modality": "text",
                            "label": "Text Input", "provider": "lmstudio", "model": "m",
                            "source": "abstractcore.gateway_runtime", "configured": true}]
            }))));
        self.store
            .entities
            .set(Loadable::Ready(entities_from_payload(&entities())));
        // (A non-admin never reads the registry; seeding it anyway proves
        // the screen still refuses on the principal, not on missing data.)
        self.store
            .users
            .set(Loadable::Ready(users_from_payload(&json!({"users": [
                {"user_id": "bob", "tenant_id": "default", "roles": ["user"], "enabled": true,
                 "runtime_id": "bob"}
            ]}))));
        self.store
            .workflows
            .set(Loadable::Ready(workflows_from_payload(&json!({
                "items": [{"bundle_id": "demo", "bundle_version": "1.0.0",
                           "entrypoints": [{"flow_id": "main"}]}]
            }))));
        self.store
            .host_state
            .set(Loadable::Ready(host_state_from_payload(&json!({
                "ok": true,
                "models": [{"task": "text_generation", "provider": "mlx", "model": "q",
                            "resident": true, "locked": false, "lockable": true}],
                "session_caches": [{"provider": "mlx", "model": "q", "session_id": "s1"}]
            }))));
    }
    fn notice(&self) -> String {
        self.store.notice.get_untracked().unwrap_or_default()
    }
}

/// Commands a refused verb may still leave behind: the screen's own reads
/// and polls (entering a screen loads it). Anything else is a write.
fn only_reads(cmds: &[Cmd]) -> bool {
    cmds.iter().all(|c| {
        let d = format!("{c:?}");
        d.starts_with("Load") || d.starts_with("Poll")
    })
}

/// Each screen's admin verbs, as published by the screen itself (the same
/// lists the footer hides).
fn gated() -> Vec<(usize, &'static str, &'static [&'static str])> {
    vec![
        (ui::SCREEN_ROUTES, "Routes", ui::routes::ADMIN_KEYS),
        (ui::SCREEN_USERS, "Users", ui::users::ADMIN_KEYS),
        (ui::SCREEN_WORKFLOWS, "Workflows", ui::workflows::ADMIN_KEYS),
        (ui::SCREEN_MODELS, "Resources", ui::models::ADMIN_KEYS),
    ]
}

/// A non-admin pressing any admin verb gets the reason and nothing is
/// sent — no prompt, no form, no write.
#[test]
fn non_admin_verbs_are_refused_with_the_reason() {
    for (screen, name, keys) in gated() {
        assert!(!keys.is_empty(), "{name} publishes its admin verbs");
        for key in keys {
            let mut h = harness();
            h.on(screen, false);
            h.store.notice.set(None);
            // Footer labels name keys in words ("space"); press the key.
            let bytes: &str = if *key == "space" { " " } else { key };
            let s = h.key(bytes.as_bytes());
            let notice = h.notice();
            assert!(
                notice.contains("is admin-only on the gateway") && notice.contains("ana"),
                "{name} `{key}`: expected the admin reason, got {notice:?}\n{s}"
            );
            let cmds = h.drain();
            assert!(only_reads(&cmds), "{name} `{key}` sent a write: {cmds:?}");
        }
    }
}

/// The gate lets an admin through: the same keys open their prompt/form.
#[test]
fn admin_reaches_the_gated_verbs() {
    let mut h = harness();
    h.on(ui::SCREEN_ROUTES, true);
    let s = h.key(b"a");
    assert!(
        s.contains("Apply the framework's recommended routes"),
        "{s}"
    );
    let mut h = harness();
    h.on(ui::SCREEN_MODELS, true);
    let s = h.key(b"w");
    assert!(
        s.contains("Load (warm up) this model on the host now"),
        "{s}"
    );
    let mut h = harness();
    h.on(ui::SCREEN_USERS, true);
    let s = h.key(b"a");
    assert!(!h.notice().contains("admin-only"), "{}", h.notice());
    assert!(s.contains("Create user"), "the add-user form opens:\n{s}");
}

/// The footer hides the admin verbs from a non-admin (the web hides the
/// controls) and keeps the verbs every principal may use.
#[test]
fn footer_hides_admin_verbs_from_a_non_admin() {
    let mut h = harness();
    let s = h.on(ui::SCREEN_ROUTES, true);
    // R15: the Multimodal hints name the web's buttons.
    assert!(
        s.contains("Apply recommended") && s.contains("Download all"),
        "{s}"
    );
    assert!(s.contains("Ctrl+G setup guide"), "{s}");
    let mut h = harness();
    let s = h.on(ui::SCREEN_ROUTES, false);
    assert!(s.contains("Enter Configure"), "{s}");
    for hidden in [
        "Apply recommended",
        "Download missing",
        "Download all",
        "Ctrl+G",
    ] {
        assert!(
            !s.contains(hidden),
            "non-admin footer shows {hidden:?}:\n{s}"
        );
    }
    assert!(
        s.contains("a/m/D admin only"),
        "disabled with the reason:\n{s}"
    );
    let s = h.on(ui::SCREEN_MODELS, false);
    // R15: the Resources hints name the web's buttons.
    assert!(s.contains("e Estimate"), "{s}");
    assert!(
        !s.contains("Load model") && !s.contains("Clear session caches"),
        "{s}"
    );
    assert!(s.contains("u/k/w/c admin only"), "{s}");
}

/// Runtimes is admin-only end to end (every route is /admin/*; the web
/// hides the tab): a non-admin gets the reason and nothing loads.
#[test]
fn runtimes_screen_is_admin_only() {
    let mut h = harness();
    h.connect(false);
    h.ui.wizard.set(false);
    h.ui.screen.set(4);
    let s = h.turns(3);
    assert!(
        s.contains("the Runtimes screen is admin-only on the gateway"),
        "{s}"
    );
    let cmds = h.drain();
    assert!(
        !cmds.iter().any(|c| matches!(c, Cmd::LoadRuntimes)),
        "no /admin/runtimes read for a non-admin: {cmds:?}"
    );
    // `r` (the root refresh) does not read it either.
    h.key(b"r");
    let cmds = h.drain();
    assert!(
        !cmds.iter().any(|c| matches!(c, Cmd::LoadRuntimes)),
        "r: {cmds:?}"
    );
    let mut h = harness();
    h.connect(true);
    h.ui.wizard.set(false);
    h.ui.screen.set(4);
    let s = h.turns(3);
    assert!(!s.contains("admin-only"), "{s}");
    assert!(h.drain().iter().any(|c| matches!(c, Cmd::LoadRuntimes)));
}

/// A non-admin's Accounts screen (entity RBAC, operator ruling
/// 2026-10-01): the users registry and `/admin/accounts` are never read
/// (they would 403); the table is `/me/accounts` — themself + the
/// entities they created — with one line saying whose view it is.
#[test]
fn users_registry_is_not_read_for_a_non_admin() {
    let mut h = harness();
    h.connect(false);
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_USERS);
    h.turns(3);
    let cmds = h.drain();
    assert!(
        !cmds
            .iter()
            .any(|c| matches!(c, Cmd::LoadUsers | Cmd::LoadAccounts)),
        "{cmds:?}"
    );
    assert!(
        cmds.iter().any(|c| matches!(c, Cmd::LoadMyAccounts)),
        "a non-admin's table is /me/accounts: {cmds:?}"
    );
    assert!(
        cmds.iter().any(|c| matches!(c, Cmd::LoadEntities)),
        "{cmds:?}"
    );
    // The rows land: own account + own entity, the scope line above.
    h.store
        .entities
        .set(Loadable::Ready(entities_from_payload(&entities())));
    h.store.accounts.set(Loadable::Ready(
        accounts_from_payload(&my_accounts()).unwrap(),
    ));
    let s = h.turns(4);
    assert!(
        s.contains("Signed in as ana, not an admin: you see your own account and the entities you created."),
        "{s}"
    );
    assert!(s.contains("testor"), "own entity row: {s}");
    assert!(!s.contains("bob"), "never another user: {s}");
    let cmds = h.drain();
    assert!(
        !cmds
            .iter()
            .any(|c| matches!(c, Cmd::LoadEntities | Cmd::LoadUsers | Cmd::LoadMyAccounts)),
        "the screen settled, nothing re-sent: {cmds:?}"
    );
    assert!(matches!(
        h.store.entities.get_untracked(),
        Loadable::Ready(_)
    ));
    // Re-entering the screen loads nothing: the registry a non-admin never
    // reads does not count as "never asked".
    h.ui.screen.set(ui::SCREEN_ROUTES);
    h.turns(2);
    h.drain();
    h.ui.screen.set(ui::SCREEN_USERS);
    h.turns(3);
    let cmds = h.drain();
    assert!(
        !cmds
            .iter()
            .any(|c| matches!(c, Cmd::LoadEntities | Cmd::LoadUsers)),
        "re-entry: {cmds:?}"
    );
    h.key(b"r");
    let cmds = h.drain();
    assert!(
        !cmds
            .iter()
            .any(|c| matches!(c, Cmd::LoadUsers | Cmd::LoadAccounts)),
        "r: {cmds:?}"
    );
    assert!(
        cmds.iter().any(|c| matches!(c, Cmd::LoadEntities))
            && cmds.iter().any(|c| matches!(c, Cmd::LoadMyAccounts)),
        "r reloads the roster and your accounts: {cmds:?}"
    );
}

/// `GET /me/accounts` for `ana` (the gateway's shape: own row + the entity
/// ana created; non-admin actions unavailable with the reason).
fn my_accounts() -> Value {
    let act = |ok: bool, why: &str| {
        if ok {
            json!({"available": true, "reason": null})
        } else {
            json!({"available": false, "reason": why})
        }
    };
    json!({"scope": "own", "accounts": [
        {"id": "ana", "tenant_id": "default", "kind": "user", "role": "user", "own": true,
         "email_address": null, "mailbox": {"state": "not_connected", "address": null, "provider": null, "reason": null},
         "runtime_id": "ana", "active": true, "entity_state": null, "archived": false,
         "actions": {"email": act(true, ""), "logs": act(true, ""),
                     "workspace": act(true, ""),
                     "rotate": act(false, "Only an admin can rotate your token."),
                     "manage": act(false, "Only entities have a management page."),
                     "archive": act(false, "You can't archive your own account."),
                     "unarchive": act(false, "This account isn't archived."),
                     "suspend": act(false, "You can't deactivate your own account.")}},
        {"id": "testor", "tenant_id": "default", "kind": "entity", "role": "entity", "own": false,
         "email_address": null, "mailbox": {"state": "not_connected", "address": null, "provider": null, "reason": null},
         "runtime_id": "testor", "active": true, "entity_state": "asleep", "archived": false,
         "created_by": {"tenant_id": "default", "user_id": "ana"},
         "actions": {"email": act(true, ""),
                     "logs": act(true, ""), "workspace": act(true, ""),
                     "rotate": act(false, "An entity has no token to rotate: its credential is discarded when it is created and no one holds it."),
                     "manage": act(true, ""),
                     "archive": act(true, ""),
                     "unarchive": act(false, "Only an admin can unarchive an account."),
                     "suspend": act(false, "Only an admin can suspend an entity.")}}
    ]})
}

/// `l` on a non-admin's own entity row reads `/me/accounts/{id}/activity`
/// (mine = true), never the admin route.
#[test]
fn a_non_admins_entity_logs_go_through_me_accounts() {
    let mut h = harness();
    h.on(ui::SCREEN_USERS, false);
    h.store.accounts.set(Loadable::Ready(
        accounts_from_payload(&my_accounts()).unwrap(),
    ));
    h.turns(2);
    h.ui.account_sel.set(1);
    h.turns(2);
    h.drain();
    h.key(b"l");
    let cmds = h.drain();
    assert!(
        cmds.iter().any(|c| matches!(c,
            Cmd::LoadActivity { target: Some((id, _)), mine: true, .. } if id == "testor")),
        "{cmds:?}"
    );
}

/// Ctrl+G: the setup guide is an admin surface (the web hides its button).
#[test]
fn ctrl_g_is_refused_for_a_non_admin() {
    let mut h = harness();
    h.on(ui::SCREEN_ROUTES, false);
    h.key(b"\x07");
    assert!(!h.ui.wizard.get_untracked(), "the guide stays closed");
    assert!(
        h.notice().contains("the setup guide is admin-only"),
        "{}",
        h.notice()
    );
    let mut h = harness();
    h.on(ui::SCREEN_ROUTES, true);
    h.key(b"\x07");
    assert!(h.ui.wizard.get_untracked(), "an admin opens it");
}

fn entities() -> Value {
    json!({"entities": [
        {"format_version": 1, "entity_id": "entity:testor", "name": "Testor", "slug": "testor",
         "home_id": "home-00000000", "handle": "testor@127.0.0.1",
         "state": {"state": "asleep", "written_by": "operator", "liveness": "alive"}}
    ]})
}

/// Entity manage: the menu tells a non-admin which parts are view-only,
/// and the pure admin acts (state, re-embed) are refused at the pick.
#[test]
fn entity_manage_refuses_admin_acts_for_a_non_admin() {
    let mut h = harness();
    h.on(ui::SCREEN_USERS, false);
    // The table is /me/accounts; the own entity row is selected.
    h.store.accounts.set(Loadable::Ready(
        accounts_from_payload(&my_accounts()).unwrap(),
    ));
    h.turns(2);
    h.ui.account_sel.set(1);
    h.turns(2);
    h.drain();
    let s = h.key(b"m");
    assert!(s.contains("You are not an admin"), "{s}");
    // R15-B: Manage is one screen; its Lifecycle tab (a click on the tab
    // bar) shows the state read-only — the segments are off and the card
    // says why — and nothing is written.
    h.drain();
    let (y, line) = s
        .lines()
        .enumerate()
        .find(|(_, l)| l.contains(" Overview ") && l.contains(" Lifecycle "))
        .expect("Manage's tab bar");
    let x = line[..line.find(" Lifecycle ").unwrap()].chars().count() + 2;
    let s = h.key(format!("\x1b[<0;{x};{}M\x1b[<0;{x};{}m", y + 1, y + 1).as_bytes());
    assert!(
        s.contains("Awake or asleep") && s.contains("admin-only"),
        "the state card says why:\n{s}"
    );
    let s = h.turns(2);
    assert!(!s.contains("Apply"), "no state modal opened:\n{s}");
    assert!(only_reads(&h.drain()));
}

/// The shared Engines screen still takes the admin concept from the
/// connection; the Models page (9, the web catalog in the terminal, round
/// 7) labels its admin verbs in the footer and refuses `w` with the web
/// page's words.
#[test]
fn core_screens_follow_the_connection_principal() {
    use abstractcore_console::screens::Access;
    let id = |admin: bool| {
        Identity::from_me(
            &json!({"principal": {"user_id": "ana", "tenant_id": "default", "admin": admin}}),
        )
        .unwrap()
    };
    assert_eq!(
        ui::screens_access(&ConnPhase::Connected(id(true))),
        Access::Admin
    );
    assert_eq!(
        ui::screens_access(&ConnPhase::Verifying(id(true))),
        Access::Admin
    );
    assert_eq!(
        ui::screens_access(&ConnPhase::Connected(id(false))),
        Access::ReadOnly("signed in as ana, not an admin".into())
    );
    assert!(!ui::screens_access(&ConnPhase::NotConnected).is_admin());

    let mut h = harness();
    let s = h.on(ui::SCREEN_CATALOG, false);
    assert!(s.contains("w/d/u/c admin only"), "footer labels it:\n{s}");
    let mut h = harness();
    let s = h.on(ui::SCREEN_CATALOG, true);
    assert!(!s.contains("admin only"), "an admin sees the verbs:\n{s}");
    assert!(s.contains("w download"), "{s}");
}

/// A non-admin on a gateway with NO entities yet: no table takes the
/// keyboard, and the Users screen's keys still answer (summon included).
#[test]
fn users_screen_keys_live_without_any_table() {
    let mut h = harness();
    h.connect(false);
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_USERS);
    h.turns(2);
    h.store.entities.set(Loadable::Ready(entities_from_payload(
        &json!({"entities": []}),
    )));
    h.turns(3);
    h.drain();
    h.store.notice.set(None);
    h.key(b"a");
    assert!(
        h.notice().contains("creating a user is admin-only"),
        "{:?}",
        h.notice()
    );
    let s = h.key(b"n");
    assert!(s.contains("Summon"), "n opens the summon form:\n{s}");
}

/// The connected line tells a non-admin what the guards do (refuse with
/// the reason) — never the old "those screens will show 403" promise.
#[test]
fn connection_line_describes_the_guards_for_a_non_admin() {
    let mut h = harness();
    h.connect(false);
    let s = h.on(ui::SCREEN_CONNECTION, false);
    assert!(
        s.contains(
            "not an admin: admin-only actions are refused with the reason; reads still work"
        ),
        "connected line names the guard behaviour:\n{s}"
    );
    assert!(!s.contains("will show 403"), "no stale 403 promise:\n{s}");
}

/// After a network failure the health authority retries the failed
/// Accounts read ONCE — with the same route the screen chose: a non-admin
/// is retried on `/me/accounts`, never sent to the admin-only
/// `/admin/accounts` (which would 403); an admin keeps `/admin/accounts`.
#[test]
fn the_accounts_retry_after_a_network_failure_follows_the_role() {
    for admin in [false, true] {
        let mut h = harness();
        // No users/entities seeded: the accounts fixture mirror (which
        // derives the table from them) stays out of the way.
        h.connect(admin);
        h.ui.wizard.set(false);
        h.ui.screen.set(ui::SCREEN_ROUTES);
        h.turns(2);
        h.drain();
        h.store
            .accounts
            .set(Loadable::Failed(abstractgateway_console::api::ApiError {
                kind: abstractgateway_console::api::ApiErrorKind::Unreachable,
                message: "GET /accounts: Network Error: Connection reset by peer".into(),
                body: None,
                timed_out: false,
            }));
        h.turns(3);
        assert!(
            matches!(h.store.conn.get_untracked(), ConnPhase::Verifying(_)),
            "the transport failure is verified first"
        );
        let (tx, retry_rx) = mpsc::channel::<Cmd>();
        let gen = h.store.probe_gen.get_untracked();
        abstractgateway_console::health::settle(h.store, &tx, gen, Ok(()));
        h.turns(2);
        let retried: Vec<Cmd> = retry_rx.try_iter().collect();
        if admin {
            assert!(
                retried.iter().any(|c| matches!(c, Cmd::LoadAccounts))
                    && !retried.iter().any(|c| matches!(c, Cmd::LoadMyAccounts)),
                "admin retry: {retried:?}"
            );
        } else {
            assert!(
                retried.iter().any(|c| matches!(c, Cmd::LoadMyAccounts))
                    && !retried.iter().any(|c| matches!(c, Cmd::LoadAccounts)),
                "non-admin retry reads /me/accounts: {retried:?}"
            );
        }
    }
}
