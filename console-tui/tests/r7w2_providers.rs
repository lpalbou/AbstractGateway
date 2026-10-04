//! Providers (round 7, R7.2): Local providers (the engines, merged from
//! the old Engines tab), Remote providers (presets) and Available
//! Providers — headless, through AbstractTUI's capture harness. The
//! engines section reads the web's routes over the JSON lane, so the
//! fixtures land in `store.json` exactly as the worker posts them; every
//! action is asserted as the `Cmd::Json` request (route + method + body)
//! the web console sends.

use std::cell::RefCell;
use std::rc::Rc;
use std::sync::{mpsc, Arc};
use std::time::Duration;

use abstracttui::app::Driver;
use abstracttui::prelude::*;
use abstracttui::testing::CaptureTerm;
use serde_json::{json, Value};

use abstractcore_console::screens::{ScreensCtx, ScreensOptions, ScreensStore};
use abstractcore_console::{ConsoleTransport, TransportError};

use abstractgateway_console::store::{ConnPhase, Identity, Loadable, ProfilesData, Store};
use abstractgateway_console::ui::{self, Ctx, UiState};
use abstractgateway_console::worker::json::JsonCmd;
use abstractgateway_console::worker::Cmd;

struct Mock;

impl ConsoleTransport for Mock {
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
    fn host_label(&self) -> String {
        "gateway host studio (10.0.0.5:8080)".into()
    }
}

struct Harness {
    app: App,
    term: CaptureTerm,
    driver: Driver,
    store: Store,
    ui: UiState,
    rx: mpsc::Receiver<Cmd>,
    screens: ScreensStore,
}

fn harness_sized(size: Size) -> Harness {
    abstracttui::app::set_theme_by_id("abstract-dark");
    let mut app = App::new(size);
    let overlays = app.overlays();
    let quitter = app.quitter();
    let (tx, rx) = mpsc::channel::<Cmd>();
    type Slots = (Store, UiState, ScreensStore);
    let slot: Rc<RefCell<Option<Slots>>> = Rc::new(RefCell::new(None));
    let out = slot.clone();
    app.mount(move |cx| {
        let store = Store::create(cx);
        let ui_state = UiState::create(cx, "http://127.0.0.1:8080".to_string(), String::new());
        let transport: Arc<dyn ConsoleTransport> = Arc::new(Mock);
        let screens = ScreensCtx::new(
            cx,
            transport.clone(),
            overlays.clone(),
            cx.signal(abstractcore_console::screens::Access::Admin),
            ScreensOptions {
                notice: Some(store.notice),
                poll_interval: Duration::from_millis(10),
                opener: Some(Rc::new(|_url: &str| Ok(()))),
                ..ScreensOptions::default()
            },
        );
        *out.borrow_mut() = Some((store, ui_state, screens.store));
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
    let (store, ui, screens) = (*slot.borrow()).expect("created");
    Harness {
        app,
        term,
        driver,
        store,
        ui,
        rx,
        screens,
    }
}

impl Harness {
    fn turn(&mut self) -> String {
        self.driver
            .turn(&mut self.app, &mut self.term)
            .expect("turn");
        self.term.screen().to_text()
    }
    fn turns(&mut self, n: usize) -> String {
        let mut last = String::new();
        for _ in 0..n {
            last = self.turn();
        }
        last
    }
    fn key(&mut self, bytes: &[u8]) {
        self.term.push_input(bytes);
    }
    fn drain_cmds(&mut self) -> Vec<Cmd> {
        let mut out = Vec::new();
        while let Ok(c) = self.rx.try_recv() {
            out.push(c);
        }
        out
    }
    fn find_cmd(&mut self, mut pred: impl FnMut(&Cmd) -> bool) -> Option<Cmd> {
        while let Ok(cmd) = self.rx.try_recv() {
            if pred(&cmd) {
                return Some(cmd);
            }
        }
        None
    }
    fn connect_as_admin(&mut self) {
        self.store.conn.set(ConnPhase::Connected(admin_identity()));
        self.turn();
    }
    fn browse_connected(&mut self) {
        self.connect_as_admin();
        self.ui.wizard.set(false);
        self.ui.screen.set(ui::SCREEN_WORKFLOWS);
        self.turns(2);
    }
    fn goto_screen(&mut self, n: usize) {
        self.ui.wizard.set(false);
        self.ui.screen.set(n);
        self.turns(2);
    }
    fn settle_until(&mut self, what: &str, pred: impl Fn(&str) -> bool) -> String {
        let mut last = String::new();
        for _ in 0..200 {
            last = self.turn();
            if pred(&last) {
                return last;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        panic!("never saw {what}:\n{last}");
    }
    fn settle_until_contains(&mut self, needle: &str) -> String {
        let n = needle.to_string();
        self.settle_until(needle, move |s| s.contains(&n))
    }
    /// Write the screen text under $R7W2_SHOTS_DIR (captures for the gate).
    fn shoot(&mut self, name: &str) {
        let s = self.turns(2);
        if let Ok(dir) = std::env::var("R7W2_SHOTS_DIR") {
            let size = self.term.screen().size();
            std::fs::create_dir_all(&dir).expect("shots dir");
            std::fs::write(format!("{dir}/{name}-{}x{}.txt", size.w, size.h), s).expect("shot");
        }
    }
}

fn identity(admin: bool) -> Identity {
    let (id, roles) = if admin {
        ("admin", json!(["admin", "user"]))
    } else {
        ("alice", json!(["user"]))
    };
    Identity::from_me(&json!({
        "ok": true,
        "principal": {"user_id": id, "tenant_id": "default", "roles": roles, "admin": admin},
        "auth": {"mode": "users"},
        "routing": {"mode": "per-principal"}
    }))
    .expect("identity parses")
}

fn admin_identity() -> Identity {
    identity(true)
}

fn user_identity() -> Identity {
    identity(false)
}

fn profiles_fixture() -> ProfilesData {
    ProfilesData::from_value(&json!({
        "ok": true,
        "can_create_gateway_scope": true,
        "profiles": [
            {"id": "acme", "virtual_provider": "endpoint:acme", "display_name": "acme",
             "description": "test endpoint", "provider_family": "openai-compatible",
             "base_url": "http://127.0.0.1:9999/v1", "base_url_configured": true,
             "api_key_set": true, "api_key_fingerprint": "deadbeef1234", "scope": "gateway",
             "allowed_models": [], "enabled": true},
            {"id": "openai", "provider_id": "openai", "display_name": "OpenAI",
             "description": "env key", "provider_family": "openai", "base_url": "",
             "base_url_configured": false, "api_key_set": true,
             "api_key_fingerprint": "cafecafe0000", "scope": "environment", "enabled": true,
             "managed": false, "synthetic": true, "source": "environment"},
            {"id": "ollama", "provider_id": "ollama", "display_name": "Ollama",
             "description": "", "provider_family": "ollama", "base_url": "http://127.0.0.1:11434",
             "base_url_configured": true, "api_key_set": false, "scope": "environment",
             "enabled": true, "managed": false, "synthetic": true, "source": "environment"}
        ]
    }))
}

// ---------------------------------------------------------------------
// Providers → Local providers (round 7: the Engines tab merged in). The
// section reads the web's routes through the JSON lane: fixtures land in
// `store.json` exactly as the worker posts them.
// ---------------------------------------------------------------------

fn engines_payload() -> Value {
    json!({"schema": "gateway_engines_v2", "install_allowed": true,
    "generated_at": "2026-10-04T02:25:51Z",
    "engines": [
        {"id": "llamacpp", "name": "llama.cpp", "supported": true, "installed": false,
         "description": "GGUF models in the gateway's own Python.",
         "install": {"method": "wheel", "notes": "Installs llama-cpp-python into the gateway's environment.",
                     "steps": ["pip install", "verify import"], "needs_admin": false,
                     "command_preview": ["pip install llama-cpp-python"]},
         "actions": [{"id": "install", "label": "Install", "enabled": true}], "active_job": null},
        {"id": "ollama", "name": "Ollama", "supported": true, "installed": false, "provider": "ollama",
         "description": "A local model server with its own model library.",
         "install": {"method": "app", "needs_admin": false},
         "actions": [{"id": "install", "label": "Install", "enabled": true},
                     {"id": "docs", "label": "Docs", "enabled": true, "url": "https://docs.ollama.com"}],
         "active_job": null},
        {"id": "mlx", "name": "MLX (mlx-lm)", "supported": true, "installed": true, "running": null,
         "version": "0.32.2", "install": {"method": "wheel"}, "actions": []},
        {"id": "vllm", "name": "vLLM", "supported": false, "installed": false,
         "support_reason": "vLLM needs an NVIDIA GPU.", "install": {"method": "unsupported"}, "actions": []}
    ]})
}

impl Harness {
    /// Providers (key 7), the engines read answered with `payload`.
    fn open_local_providers(&mut self, payload: Value) -> String {
        self.key(b"7");
        self.turns(3);
        self.store.json.set("engines", Loadable::Ready(payload));
        self.settle_until("the engine rows", |s| {
            s.contains("llama.cpp") && s.contains("Ollama")
        })
    }

    /// Select engine row `i` (the section's order: installed, absent,
    /// unsupported — the web's `rank`).
    fn select_engine_row(&mut self, i: usize) -> String {
        self.screens.engine_sel.set(i);
        self.turns(2)
    }

    fn json_send(&mut self, path: &str) -> Option<(String, Value)> {
        let p = path.to_string();
        self.find_cmd(|c| matches!(c, Cmd::Json(JsonCmd::Send { path, .. }) if *path == p))
            .map(|c| match c {
                Cmd::Json(JsonCmd::Send { method, body, .. }) => (method, body),
                _ => unreachable!(),
            })
    }
}

#[test]
fn seven_opens_providers_on_its_local_engines_and_is_refused_in_the_wizard() {
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    h.key(b"7");
    let s = h.turns(3);
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_PROVIDERS);
    assert!(s.contains("Looking at this computer's engines..."), "{s}");
    assert!(
        h.find_cmd(
            |c| matches!(c, Cmd::Json(JsonCmd::Get { path, .. }) if path == "/engines?probe=1")
        )
        .is_some(),
        "Providers reads GET /engines?probe=1 (the web's engineRefresh)"
    );
    h.store
        .json
        .set("engines", Loadable::Ready(engines_payload()));
    let s = h.settle_until_contains("llama.cpp");
    for needle in [
        "▸ Local providers",
        "Engines that run models on this computer, and their server connections.",
        "1 of 4 installed · checked 02:25:51 UTC",
        "Not installed",
        "Ready",
        "Not for this computer",
        "i Install",
        "b Browse models",
    ] {
        assert!(s.contains(needle), "missing {needle:?}:\n{s}");
    }
    // The ordering: installed first (MLX), unsupported last (vLLM).
    let mlx = s.find("MLX (mlx-lm)").unwrap();
    let llama = s.find("llama.cpp").unwrap();
    let vllm = s.find("vLLM ").unwrap();
    assert!(mlx < llama && llama < vllm, "{s}");
    // Wizard: digits are refused WITH a reason.
    h.ui.wizard.set(true);
    h.ui.screen.set(ui::SCREEN_REVIEW);
    h.turns(2);
    h.key(b"7");
    let s = h.turns(2);
    assert_eq!(
        h.ui.screen.get_untracked(),
        ui::SCREEN_REVIEW,
        "wizard does not jump"
    );
    assert!(
        s.contains("screen jumps (1-9,0,W,H,T,N,S,I) work in browse mode"),
        "{s}"
    );
}

#[test]
fn install_confirm_shows_the_plan_and_the_gateway_host() {
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    h.open_local_providers(engines_payload());
    // Row 1 = llama.cpp (a wheel install: notes, steps, the command).
    h.select_engine_row(1);
    h.drain_cmds();
    h.key(b"i");
    let s = h.turns(2);
    for needle in [
        "Install llama.cpp on gateway host studio (10.0.0.5:8080)?",
        "Installs llama-cpp-python into the gateway's environment.",
        "pip install · verify import",
        "pip install llama-cpp-python",
        "y Install now · n Not now",
    ] {
        assert!(s.contains(needle), "missing {needle:?}:\n{s}");
    }
    // Not now: nothing is sent.
    h.key(b"n");
    let s = h.turns(2);
    assert!(!s.contains("Install llama.cpp on"), "{s}");
    assert!(h.drain_cmds().is_empty(), "Not now sends nothing");
    // y: the web's install-go, location auto.
    h.key(b"i");
    h.turns(2);
    h.key(b"y");
    h.turns(2);
    let (method, body) = h
        .json_send("/engines/llamacpp/install")
        .expect("install POST");
    assert_eq!(method, "POST");
    assert_eq!(body, json!({"dry_run": false, "location": "auto"}));
    let s = h.turns(1);
    assert!(s.contains("Starting the install..."), "pending label:\n{s}");
}

#[test]
fn an_app_engine_install_offers_both_locations_from_two_dry_runs() {
    use abstractgateway_console::store::json::WriteState;
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    h.open_local_providers(engines_payload());
    h.select_engine_row(2); // Ollama (an app)
    h.drain_cmds();
    h.key(b"i");
    let s = h.turns(2);
    assert!(
        s.contains("Checking where Ollama can go on this computer..."),
        "{s}"
    );
    let cmds = h.drain_cmds();
    let dry: Vec<Value> = cmds
        .iter()
        .filter_map(|c| match c {
            Cmd::Json(JsonCmd::Send { path, body, .. }) if path == "/engines/ollama/install" => {
                Some(body.clone())
            }
            _ => None,
        })
        .collect();
    assert_eq!(
        dry,
        vec![
            json!({"dry_run": true, "location": "user"}),
            json!({"dry_run": true, "location": "system"})
        ],
        "the two plans (engineLoadPlans)"
    );
    h.store.json.set_write(
        "engine.plan.ollama.user",
        Some(WriteState::Done(
            json!({"plan": {"target": "/Users/me/Applications/Ollama.app"}}),
        )),
    );
    h.store.json.set_write(
        "engine.plan.ollama.system",
        Some(WriteState::Done(json!({"plan": {"target": "/Applications/Ollama.app", "needs_admin": true,
            "admin_reason": "Your account cannot write there, so an administrator password is asked first."}}))),
    );
    let s = h.turns(3);
    assert!(
        s.contains("Install puts it in your own Applications folder"),
        "{s}"
    );
    assert!(s.contains("/Users/me/Applications/Ollama.app"), "{s}");
    assert!(
        s.contains("y Install · u Install for all users (administrator) · n Not now"),
        "{s}"
    );
    h.key(b"u");
    h.turns(2);
    let (_, body) = h
        .json_send("/engines/ollama/install")
        .expect("install POST");
    assert_eq!(body, json!({"dry_run": false, "location": "system"}));
}

#[test]
fn a_running_install_blocks_q_and_c_cancels_it() {
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    h.open_local_providers(engines_payload());
    h.store.json.set(
        "engines.job.llamacpp",
        Loadable::Ready(
            json!({"job_id": "eng_1", "engine": "llamacpp", "state": "installing",
            "message": "Building wheels", "percent": 40.0, "can_cancel": true}),
        ),
    );
    h.select_engine_row(1);
    let s = h.settle_until_contains("Installing — 40%");
    assert!(s.contains("Building wheels"), "{s}");
    assert!(
        abstractgateway_console::ui::providers::engines::engine_job_running(&h.store),
        "an install runs"
    );
    // Browse-mode q refuses while the gateway job runs.
    h.key(b"q");
    h.settle_until_contains("models/engines job is running on the gateway");
    h.drain_cmds();
    h.key(b"c");
    h.turns(2);
    let (method, _) = h
        .json_send("/engines/jobs/eng_1/cancel")
        .expect("cancel POST");
    assert_eq!(method, "POST");
    // The job ends: q is free again.
    h.store.json.set(
        "engines.job.llamacpp",
        Loadable::Ready(json!({"job_id": "eng_1", "engine": "llamacpp", "state": "cancelled"})),
    );
    let s = h.settle_until_contains("The llama.cpp install was cancelled.");
    assert!(s.contains("cancelled"), "{s}");
    assert!(!abstractgateway_console::ui::providers::engines::engine_job_running(&h.store));
}

#[test]
fn a_reconnect_forgets_the_old_gateways_engines_and_reloads() {
    use abstractcore_console::screens::Remote;
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    h.open_local_providers(engines_payload());
    // The worker's probe path: Probing + Store::reset_domains (what the
    // worker does before it probes another gateway).
    h.store.conn.set(ConnPhase::Probing);
    h.store.reset_domains();
    h.turns(2);
    assert!(
        matches!(h.store.json.get_untracked("engines"), Loadable::NotAsked),
        "Probing forgets the old gateway's engines"
    );
    h.drain_cmds();
    h.turns(3);
    assert!(
        h.drain_cmds().iter().all(|c| !matches!(c, Cmd::Json(_))),
        "no read while not connected"
    );
    // Connected again, still on Providers: it reloads by itself.
    h.connect_as_admin();
    h.turns(3);
    assert!(
        h.find_cmd(
            |c| matches!(c, Cmd::Json(JsonCmd::Get { path, .. }) if path == "/engines?probe=1")
        )
        .is_some(),
        "the post-reconnect engines read"
    );
    // The UI-side reset (Connect button) clears the shared screens too.
    h.screens.engines.set(Remote::Loading);
    let ctx_reset = h.screens;
    abstractgateway_console::ui::reset_screens(&ctx_reset);
    assert!(h.screens.engines.with_untracked(Remote::is_not_asked));
}

#[test]
fn wizard_engines_step_is_providers_with_its_goal() {
    let mut h = harness_sized(Size::new(150, 40));
    h.connect_as_admin();
    h.ui.wizard.set(true);
    h.ui.screen.set(ui::SCREEN_PROVIDERS);
    h.turns(3);
    h.store
        .json
        .set("engines", Loadable::Ready(engines_payload()));
    let s = h.settle_until_contains("llama.cpp");
    assert!(
        s.contains("Step 3/7") && s.contains("install a local engine"),
        "{s}"
    );
}

#[test]
fn browse_models_opens_the_models_page_filtered_to_the_engine() {
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    h.open_local_providers(engines_payload());
    h.select_engine_row(0); // MLX: built in, Ready
    h.key(b"b");
    h.turns(2);
    assert_eq!(h.ui.screen.get_untracked(), ui::SCREEN_CATALOG);
    // The Models page took the hand-over: its Provider filter is the engine.
    assert_eq!(
        abstractgateway_console::ui::catalog::with_state(|p| p.filters.provider.clone()),
        "mlx"
    );
    assert_eq!(h.screens.engine_filter.get_untracked(), None, "consumed");
}

#[test]
fn a_non_admin_sees_no_install_and_is_told_why() {
    let mut h = harness_sized(Size::new(150, 40));
    h.store.conn.set(ConnPhase::Connected(user_identity()));
    h.ui.wizard.set(false);
    h.ui.screen.set(ui::SCREEN_WORKFLOWS);
    h.turns(2);
    let s = h.open_local_providers(engines_payload());
    assert!(
        !s.contains("i Install"),
        "a non-admin's rows carry no Install:\n{s}"
    );
    h.select_engine_row(1);
    h.key(b"i");
    let s = h.turns(2);
    assert!(
        s.contains("does not offer Install now (an admin's action)"),
        "{s}"
    );
}

#[test]
fn remote_presets_open_the_family_form_prefilled() {
    let mut h = harness_sized(Size::new(150, 40));
    h.browse_connected();
    h.goto_screen(ui::SCREEN_PROVIDERS);
    ui::providers::set_section(&h.store, 1);
    h.turns(2);
    h.store.profiles.set(Loadable::Ready(profiles_fixture()));
    let s = h.turns(3);
    for needle in [
        "▸ Remote providers",
        "Cloud accounts and OpenAI-compatible servers. Keys stay on the gateway; only fingerprints are shown.",
        "OpenAI API or an OpenAI-compatible OpenAI deployment.",
        "Custom OpenAI-compatible",
        "Not connected",
    ] {
        assert!(s.contains(needle), "missing {needle:?}:\n{s}");
    }
    h.key(b"a"); // the selected preset: OpenAI
    let s = h.turns(3);
    assert!(s.contains("Configure OpenAI"), "{s}");
    assert!(s.contains("openai"), "{s}");
}

#[test]
fn v_cycles_the_three_sections() {
    let mut h = harness_sized(Size::new(120, 40));
    h.browse_connected();
    h.goto_screen(ui::SCREEN_PROVIDERS);
    for want in [
        "▸ Remote providers",
        "▸ Available Providers",
        "▸ Local providers",
    ] {
        h.key(b"v");
        let s = h.turns(2);
        assert!(s.contains(want), "{want}:\n{s}");
    }
}

/// Every section at 80×24 and 120×40: the WUI sentences survive whole (rows
/// wrap, never cut). R7W2_SHOTS_DIR=<dir> writes the text captures.
#[test]
fn snapshots_per_section_at_both_sizes() {
    for size in [Size::new(80, 24), Size::new(120, 40)] {
        let mut h = harness_sized(size);
        h.browse_connected();
        let s = h.open_local_providers(engines_payload());
        h.store.profiles.set(Loadable::Ready(profiles_fixture()));
        assert!(
            s.contains("Engines that run models on this computer"),
            "{s}"
        );
        h.shoot("providers-local");
        h.select_engine_row(2);
        h.key(b"\r");
        let s = h.turns(3);
        assert!(
            s.contains("A local model server with its own model"),
            "expanded:\n{s}"
        );
        assert!(s.contains("Connection"), "the local connection:\n{s}");
        h.shoot("providers-engine-expanded");
        h.key(b"\r");
        h.select_engine_row(1);
        h.key(b"i");
        let s = h.turns(2);
        assert!(
            s.contains("Install llama.cpp on gateway host studio"),
            "{s}"
        );
        h.shoot("providers-install-confirm");
        h.key(b"n");
        h.key(b"v");
        let s = h.turns(2);
        assert!(s.contains("Anthropic"), "{s}");
        h.shoot("providers-remote");
        h.key(b"v");
        let s = h.turns(2);
        assert!(s.contains("endpoint:acme"), "{s}");
        assert!(
            s.contains("deadbeef") && !s.contains("deadbeef1234"),
            "fingerprint, 8 chars:\n{s}"
        );
        h.shoot("providers-available");
    }
}
